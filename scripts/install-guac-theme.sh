#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "crange: install-guac-theme requires root on the Guacamole host" >&2
    exit 2
fi

source_jar=${1:-/opt/crange/guacamole/crange-theme.jar}
if [ -n "${GUACAMOLE_HOME:-}" ]; then
    guacamole_home=$GUACAMOLE_HOME
elif [ -f /opt/guacamole/compose.yaml ] && [ -d /opt/guacamole/extensions ]; then
    # The range's Docker deployment bind-mounts this host directory into the
    # container's GUACAMOLE_HOME/extensions path.
    guacamole_home=/opt/guacamole
else
    guacamole_home=/etc/guacamole
fi
extension_dir="$guacamole_home/extensions"
target="$extension_dir/crange-theme.jar"

if [ ! -f "$source_jar" ]; then
    echo "crange: theme JAR not found: $source_jar" >&2
    exit 2
fi

install -d -m 0755 "$extension_dir"
if [ -f "$target" ]; then
    backup="$target.$(date -u +%Y%m%dT%H%M%SZ).bak"
    cp -p "$target" "$backup"
    echo "Backed up existing CRANGE theme to $backup"
fi
install -o root -g root -m 0644 "$source_jar" "$target"

if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files guacamole.service 2>/dev/null | grep -q '^guacamole.service'; then
    systemctl restart guacamole.service
elif [ -f /opt/guacamole/compose.yaml ] && command -v docker >/dev/null 2>&1; then
    docker compose -f /opt/guacamole/compose.yaml restart guacamole
else
    echo "crange: installed theme, but no supported Guacamole service was found" >&2
    exit 1
fi

url=${GUACAMOLE_URL:-http://127.0.0.1:8080/}
if command -v curl >/dev/null 2>&1; then
    attempts=0
    until curl --fail --silent --show-error --max-time 5 "$url" >/dev/null; do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 12 ]; then
            echo "crange: Guacamole did not become healthy at $url" >&2
            exit 1
        fi
        sleep 2
    done
fi
echo "Installed and restarted the CRANGE Guacamole theme."
