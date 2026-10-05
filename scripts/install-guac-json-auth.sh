#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo 'crange: install-guac-json-auth must run as root on the Guacamole host' >&2
  exit 1
fi

mode=${1:---stage}
if [[ $mode != --stage && $mode != --apply ]]; then
  echo 'Usage: install-guac-json-auth.sh [--stage|--apply] < shared-key' >&2
  exit 2
fi

base=${GUACAMOLE_COMPOSE_DIR:-/opt/guacamole}
compose_file=$base/compose.yaml
if [[ ! -f $compose_file ]]; then
  echo "crange: expected $compose_file" >&2
  exit 1
fi

IFS= read -r json_key || true
if [[ ! $json_key =~ ^[[:xdigit:]]{32}$ ]]; then
  echo 'crange: expected one 128-bit hexadecimal JSON-auth key on stdin' >&2
  exit 1
fi

version=1.6.0
archive_name="guacamole-auth-json-$version.tar.gz"
base_url="https://archive.apache.org/dist/guacamole/$version/binary"
temporary=$(mktemp -d)
trap 'python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$temporary"' EXIT
curl --fail --location --silent --show-error "$base_url/$archive_name" -o "$temporary/$archive_name"
curl --fail --location --silent --show-error "$base_url/$archive_name.sha256" -o "$temporary/$archive_name.sha256"
expected_hash=$(awk '{print $1}' "$temporary/$archive_name.sha256")
printf '%s  %s\n' "$expected_hash" "$temporary/$archive_name" | sha256sum --check --status

mkdir -p "$temporary/extracted" "$base/extensions"
tar -xzf "$temporary/$archive_name" -C "$temporary/extracted"
jar=$(find "$temporary/extracted" -type f -name "guacamole-auth-json-$version.jar" -print -quit)
if [[ -z $jar ]]; then
  echo 'crange: Apache archive did not contain the expected JSON-auth extension JAR' >&2
  exit 1
fi
install -m 0644 "$jar" "$base/extensions/guacamole-auth-json-$version.jar"

umask 077
printf 'JSON_SECRET_KEY=%s\n' "$json_key" > "$base/crange-json-auth.env"
cat > "$base/compose.crange-json.yaml" <<'YAML'
services:
  guacamole:
    environment:
      # The verified JAR is mounted from extensions; avoid also loading the
      # Docker image's bundled copy. Properties still read from environment.
      JSON_ENABLED: "false"
    env_file:
      - ./crange-json-auth.env
YAML
chmod 0600 "$base/crange-json-auth.env"
chmod 0644 "$base/compose.crange-json.yaml"

cd "$base"
docker compose -f compose.yaml -f compose.crange-json.yaml config --quiet
if [[ $mode == --apply ]]; then
  docker compose -f compose.yaml -f compose.crange-json.yaml up -d --no-deps guacamole
  echo 'Guacamole JSON authentication installed; only the Guacamole web application was recreated.'
else
  echo 'Guacamole JSON authentication staged and Compose configuration validated; Guacamole was not restarted.'
fi
