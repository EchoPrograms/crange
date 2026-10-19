"""Fixed Project 100 MHL service configuration; no caller-supplied Ansible inputs."""
from __future__ import annotations

import importlib.util
import json
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import fcntl
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
PROJECT = 100
NETWORKS = (254, 1, 2, 3, 4, 5)
KEY = "/home/crange/.ssh/id_ed25519_range"
GUESTS = {
    "mhl-db01": ("MHL-DB01", 30, "mhl_databases"),
    "mhl-app01": ("MHL-APP01", 40, "mhl_applications"),
    "mhl-ci01": ("MHL-CI01", 50, "mhl_build"),
    "mhl-bkp01": ("MHL-BKP01", 60, "mhl_backups"),
}


PROFILE_IDS = {'business': 'project-100-mhl-business-v1',
               'windows': 'project-100-mhl-windows-ad-file-v1'}


def recipe_digest(kind, root=None):
    from crange_mhl_recipes import digest
    return digest(kind, root or ROOT)


def verify_release_profiles(release, *, require_current=False, preserve_recipes=False):
    profiles = release.get('scenario', {}).get('service_profiles', {})
    if not isinstance(profiles, dict):
        raise ServiceConfigurationError('Release service profiles are malformed.')
    if profiles and int(release.get('project_id', 0)) != PROJECT:
        raise ServiceConfigurationError('MHL service profiles belong only to Project 100.')
    for kind, profile in profiles.items():
        if (kind not in PROFILE_IDS or not isinstance(profile, dict)
                or profile.get('profile') != PROFILE_IDS[kind]):
            raise ServiceConfigurationError('Release service recipe changed or lacks verified provenance; configure staging and capture a new release.')
        from crange_mhl_recipes import verify, RecipeError
        try:
            expected = profile.get('source_sha256')
            verify(kind,expected,ROOT)
            if require_current and recipe_digest(kind) != expected:
                raise RecipeError('Staging service recipe changed; configure staging before capture.')
            if preserve_recipes:
                from crange_mhl_recipes import preserve
                preserve(kind,expected,ROOT)
        except (RecipeError,OSError,ValueError) as exc:raise ServiceConfigurationError(str(exc)) from exc
    return sorted(profiles)


class ServiceConfigurationError(ValueError):
    pass


def _adapter():
    path = ROOT / "scripts/generate-mhl-inventory.py"
    spec = importlib.util.spec_from_file_location("crange_mhl_inventory", path)
    if spec is None or spec.loader is None:
        raise ServiceConfigurationError("Project 100 MHL inventory adapter is unavailable.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_selected(project_path: Path, network_id: int, canonical: dict, test_scope=None) -> dict:
    if network_id not in NETWORKS and test_scope is None:
        raise ServiceConfigurationError("Choose Team 0 staging or a declared Project 100 team from 1 through 5.")
    try:
        cfg = json.loads((project_path / "crange.auto.tfvars.json").read_text())
        applied = json.loads((project_path / "crange-applied-placements.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ServiceConfigurationError("Project 100 guests are not deployed yet; apply the reviewed scenario plan first.") from exc
    approval_path = project_path / 'approved-container-templates.json'
    approvals = json.loads(approval_path.read_text()) if approval_path.exists() else {}
    if int(cfg.get("project_id", 0)) != PROJECT:
        raise ServiceConfigurationError("MHL business services are only available for Project 100.")
    if test_scope is None and network_id != 254 and str(network_id) not in {str(team) for team in cfg.get("team_ids", [])}:
        raise ServiceConfigurationError(f"Team {network_id} is not declared in Project 100.")
    try:
        selected = _adapter().selected_hosts(canonical, network_id, test_scope["keys"] if test_scope else None)
    except (KeyError, TypeError, ValueError) as exc:
        raise ServiceConfigurationError(f"MHL services need all four deployed guests in this scope: {exc}") from exc
    result = {"all": {"vars": {"mhl_domain": "mhl.test",
                                "mhl_db_ip": f"10.100.{network_id}.30",
                                "mhl_app_ip": f"10.100.{network_id}.40",
                                "mhl_test_scope": test_scope is not None},
                       "children": selected}}
    group_to_key = {"mhl_databases": "mhl-db01", "mhl_applications": "mhl-app01",
                    "mhl_build": "mhl-ci01", "mhl_backups": "mhl-bkp01"}
    for group, key in group_to_key.items():
        hostname, last_octet, _ = GUESTS[key]
        host = result["all"]["children"][group]["hosts"][hostname]
        placement = applied.get(f"container:{host.get('crange_box_key')}")
        address = f"10.100.{network_id}.{last_octet}"
        guest_id = host.get("crange_guest_id")
        if (host.get("crange_project_id") != PROJECT
                or isinstance(guest_id, bool) or not isinstance(guest_id, int) or guest_id < 100
                or not host.get("crange_image_id")
                or not host.get("crange_template")
                or host.get("ansible_host") != address
                or host.get("ansible_connection") != "ssh"
                or host.get("ansible_user") != ("root" if (placement or {}).get("template_file_id") else approvals.get(host.get("crange_template"), {}).get("profile", {}).get("management_user", "user"))
                or host.get("ansible_become") is not True
                or host.get("ansible_ssh_private_key_file") != KEY
                or not isinstance(placement, dict)
                or placement.get("guest_id") != host.get("crange_guest_id")
                or placement.get("image_id") != host.get("crange_image_id")
                or placement.get("node_name") != host.get("crange_node_name")
                or placement.get("declared_template") != host.get("crange_template")):
            raise ServiceConfigurationError(
                f"Applied identity, image, address, or fixed SSH path for {key} does not match this project's deployed record."
            )
        network_key = host.get("crange_box_key", "")
        declarations = cfg.get("scenario_containers", {})
        declared = declarations.get(network_key, {}) if isinstance(declarations, dict) else {}
        expected = f"t254-{key}" if network_id == 254 else f"{network_key}"
        if test_scope and (network_key != test_scope["keys"][key] or declared.get("release_scope")!="test" or declared.get("release_id")!=test_scope["release_id"] or declared.get("image")!=host.get("crange_image_id")):
            raise ServiceConfigurationError("MHL test container differs from its verified cohort.")
        if test_scope is None and network_id == 254 and network_key != expected:
            raise ServiceConfigurationError(f"Staging guest {key} is not the declared Team 0 MHL container.")
        if test_scope is None and network_id != 254 and (not network_key.endswith(f"-t{network_id:03d}")
                                  or declared.get("release_scope") != "team"
                                  or not declared.get("release_id")):
            raise ServiceConfigurationError(f"{key} is not an applied guest from an explicit team release.")
        # The fixed group supplies the worker-owned user and SSH key. Reject
        # command syntax in any optional canonical proxy value.
        proxy = host.get("ansible_ssh_common_args", "")
        if not isinstance(proxy, str) or any(token in proxy for token in (";", "&&", "|", "`", "$(", "-i ")):
            raise ServiceConfigurationError(f"The controller SSH path for {key} is not a canonical generated value.")
    return result


def _private_vars(network_id: int, secrets_root: Path) -> Path:
    directory = secrets_root / "project-100"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / f"mhl-business-network-{network_id}.yml"
    if not path.exists():
        fields = {
            "mhl_db_runtime_password": secrets.token_urlsafe(36).replace("=", ""),
            "mhl_ci_synthetic_token": secrets.token_urlsafe(36).replace("=", ""),
        }
        content = "".join(f"{name}: '{value}'\n" for name, value in fields.items())
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ServiceConfigurationError("The private MHL service credential file must be a regular mode-0600 file.")
    content = path.read_text()
    if (not re.fullmatch(
            r"mhl_db_runtime_password: '[A-Za-z0-9_-]{20,128}'\nmhl_ci_synthetic_token: '[A-Za-z0-9_-]{16,128}'\n", content)):
        raise ServiceConfigurationError("The private MHL service credential file is malformed; repair it through the platform secret process.")
    return path


def configure(project_path: Path, network_id: int, *, secrets_root: Path | None = None,
              run=subprocess.run, canonical_path: Path | None = None, test_scope=None,
              recipe_sha256=None) -> None:
    from crange_mhl_recipes import source_directory
    with source_directory('business',recipe_sha256,ROOT) as recipe_root:
        _configure_business(project_path,network_id,secrets_root=secrets_root,run=run,
                            canonical_path=canonical_path,test_scope=test_scope,recipe_root=recipe_root)


def _configure_business(project_path, network_id, *, secrets_root=None, run=subprocess.run,
                        canonical_path=None, test_scope=None, recipe_root=None):
    recipe_root = recipe_root or ROOT
    if network_id not in NETWORKS and test_scope is None:
        raise ServiceConfigurationError("Choose Team 0 staging or a declared Project 100 team from 1 through 5.")
    canonical_path = canonical_path or ROOT / "ansible/inventory/generated/project-100.json"
    try:
        canonical = json.loads(canonical_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ServiceConfigurationError("Generate the applied Project 100 inventory before configuring business services.") from exc
    inventory = _validate_selected(project_path, network_id, canonical, test_scope)
    secrets_root = secrets_root or Path.home() / ".config/crange/secrets"
    vars_file = _private_vars(network_id, secrets_root)
    with tempfile.TemporaryDirectory(prefix="crange-mhl-") as temporary:
        os.chmod(temporary, 0o700)
        inventory_path = Path(temporary) / "inventory.json"
        fd = os.open(inventory_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(inventory, sort_keys=True) + "\n")
        secret_values = [line.split(": ", 1)[-1].strip("'\" ")
                         for line in vars_file.read_text().splitlines()]
        def sanitized(value):
            for secret in secret_values:
                if secret:
                    value = value.replace(secret, "[redacted]")
            return value
        environment={**os.environ, "ANSIBLE_TIMEOUT": "90", "ANSIBLE_ROLES_PATH": str(recipe_root / "ansible/roles"),
                     "ANSIBLE_DISPLAY_ARGS_TO_STDOUT": "false", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}
        # Network reassignment precedes the immutable recipe: a captured
        # PostgreSQL cluster cannot start while listening on its old LAN IP.
        for playbook in (ROOT / "ansible/linux/project-100-service-rebind.yml",
                         recipe_root / "ansible/linux/project-100-business.yml"):
            print("Rebinding restored database services." if playbook.name.endswith('service-rebind.yml')
                  else "Applying the captured Linux business-service recipe.",flush=True)
            command = ["ansible-playbook", "-f", "1", "-i", str(inventory_path),
                       str(playbook), "--extra-vars", "@" + str(vars_file)]
            result = run(command, cwd=ROOT, text=True, capture_output=True, timeout=3600,env=environment)
            if result.returncode:
                safe = sanitized(result.stderr or result.stdout or "MHL service configuration failed.")
                raise ServiceConfigurationError(safe[-12000:])
            print(sanitized(result.stdout or "MHL service configuration completed.")[-12000:])

    _record_profile(network_id, inventory, project_path, source_sha256=recipe_digest("business", recipe_root))


def _record_profile(network_id: int, inventory: dict, project_path: Path, source_sha256=None) -> None:
    """Keep release-safe service profile evidence without credential material."""
    record_path = Path("/var/lib/crange-jobs/mhl-service-profiles.json")
    try:
        record_path.parent.mkdir(mode=0o2770, parents=True, exist_ok=True)
        current = json.loads(record_path.read_text()) if record_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        current = {}
    guest_identities = {}
    for group in inventory["all"]["children"].values():
        for hostname, host in group.get("hosts", {}).items():
            guest_identities[host["crange_box_key"]] = {
                "hostname": hostname,
                "guest_id": host["crange_guest_id"],
                "image_id": host["crange_image_id"],
            }
    current.setdefault(str(network_id), {})["business"] = {
        "profile": "project-100-mhl-business-v1",
        "source_sha256": source_sha256 or recipe_digest("business"),
        "configured_at": datetime.now(timezone.utc).isoformat(),
        "guests": guest_identities,
    }
    record_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
    os.chmod(record_path, 0o660)


def profile_for(network_id: int) -> dict:
    """Return the non-secret configured profile metadata for release capture."""
    record_path = Path("/var/lib/crange-jobs/mhl-service-profiles.json")
    try:
        data = json.loads(record_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    scope = data.get(str(network_id), {})
    result = {}
    for name in ("business", "windows"):
        record = scope.get(name, {}) if isinstance(scope, dict) else {}
        if isinstance(record, dict):
            clean = {key: record[key] for key in ("profile", "configured_at", "guests", "source_sha256") if key in record}
            if clean:
                result[name] = clean
    return result


def _safe_write(path: Path, content: str, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), mode)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _checked_private_file(path: Path, label: str) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ServiceConfigurationError(f"Private {label} must be a worker-owned regular mode-0600 file.")


def _windows_scope_hosts(project_path: Path, network_id: int, canonical: dict, test_scope=None) -> tuple[dict, dict]:
    """Select exactly the applied Project 100 DC and file server for one network."""
    if network_id not in NETWORKS and test_scope is None:
        raise ServiceConfigurationError("Choose Team 0 staging or a declared Project 100 team from 1 through 5.")
    try:
        cfg = json.loads((project_path / "crange.auto.tfvars.json").read_text())
        applied = json.loads((project_path / "crange-applied-placements.json").read_text())
        approvals = json.loads((project_path / "approved-scenario-templates.json").read_text())
        windows_group = canonical["all"]["children"]["windows_guests"]
        groups = windows_group["children"]
        hosts = windows_group["hosts"]
        inherited = {**canonical["all"].get("vars", {}), **windows_group.get("vars", {})}
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ServiceConfigurationError("Project 100 Windows service guests are not deployed or the applied inventory is incomplete; apply the reviewed Windows release first.") from exc
    if int(cfg.get("project_id", 0)) != PROJECT:
        raise ServiceConfigurationError("MHL Windows services are only available for Project 100.")
    if test_scope is None and network_id != 254 and str(network_id) not in {str(team) for team in cfg.get("team_ids", [])}:
        raise ServiceConfigurationError(f"Team {network_id} is not declared in Project 100.")
    scope_name = "staging_windows" if network_id == 254 or test_scope else f"team_{network_id:03d}_windows"
    scoped_names = set()
    windows_vms = groups.get("windows_vms", {})
    scoped_names.update((windows_vms.get("children", {}).get(scope_name, {}).get("hosts", {}) or {}).keys())
    if not scoped_names:
        raise ServiceConfigurationError(f"No applied Windows guests are present in {('Team 0 staging' if network_id == 254 else f'Team {network_id}')}; publish and deploy the Windows release first.")

    result = {}
    wanted = {"mhl-dc01": ("MHL-DC01", 10, "mhl_domain_controllers"),
              "mhl-file01": ("MHL-FS01", 20, "mhl_file_servers")}
    declared_boxes = cfg.get("scenario_boxes", {})
    for marker, (guest_hostname, octet, group) in wanted.items():
        candidates = []
        for inventory_name in scoped_names:
            values = hosts.get(inventory_name)
            if not isinstance(values, dict):
                continue
            box_key = values.get("crange_box_key", "")
            declaration = declared_boxes.get(box_key, {})
            # Release keys may be shortened/hashes. Role identity comes from
            # the recorded source name, then the full applied checks below.
            matches = (box_key == test_scope["keys"][marker] if test_scope else box_key == f"t254-{marker}" if network_id == 254
                       else isinstance(declaration, dict) and declaration.get("release_source_name") == marker)
            if matches:
                candidates.append((inventory_name, values))
        if len(candidates) != 1:
            raise ServiceConfigurationError(f"Expected exactly one applied Windows guest for {marker} in selected network; check declared release guest placement.")
        inventory_name, source_value = candidates[0]
        source = {**inherited, **source_value}
        box_key = source.get("crange_box_key")
        box = declared_boxes.get(box_key, {}) if isinstance(declared_boxes, dict) else {}
        template = box.get("template") if isinstance(box, dict) else None
        profile = approvals.get(template, {}).get("profile", {}) if isinstance(approvals, dict) else {}
        identity = applied.get(f"vm:{box_key}") if isinstance(applied, dict) else None
        guest_id, image_id = source.get("crange_guest_id"), source.get("crange_image_id")
        address = f"10.100.{network_id}.{octet}"
        if (not isinstance(box, dict)
                or str(box.get("team_id")) != str(254 if test_scope else network_id)
                or (test_scope is None and network_id == 254 and box.get("release_scope") not in (None, "staging"))
                or (test_scope is None and network_id != 254 and (box.get("release_scope") != "team" or not box.get("release_id")))
                or (test_scope and (box_key != test_scope["keys"][marker] or box.get("release_scope")!="test" or box.get("release_id")!=test_scope["release_id"] or box.get("image")!=image_id))
                or source.get("crange_project_id") != PROJECT
                or source.get("crange_guest_network_id") != str(network_id)
                or source.get("ansible_host") != address
                or source.get("ansible_connection") != "winrm"
                or source.get("ansible_port") != 5985
                or source.get("ansible_winrm_transport") != "ntlm"
                or source.get("ansible_winrm_message_encryption") != "always"
                or isinstance(guest_id, bool) or not isinstance(guest_id, int)
                or not isinstance(image_id, str) or not image_id
                or not isinstance(template, str) or not template
                or not isinstance(profile, dict) or not str(profile.get("os_type", "")).startswith("win")
                or profile.get("cloud_init") is not False
                or not isinstance(identity, dict)
                or identity.get("guest_id") != guest_id
                or identity.get("image_id") != image_id
                or identity.get("declared_template") != template
                or identity.get("template_id") != source.get("crange_template_id")
                or identity.get("template_node") != source.get("crange_template_node")
                or identity.get("node_name") != source.get("crange_node_name")):
            raise ServiceConfigurationError(f"Applied identity, approved Windows OS profile, source CIDR, or WinRM path for {marker} does not match the selected network.")
        output = dict(source)
        output.update({"mhl_project_id": PROJECT, "mhl_network_id": network_id,
                       "mhl_guest_hostname": guest_hostname, "mhl_test_scope": test_scope is not None})
        if marker == "mhl-file01":
            output["mhl_dc_dns"] = f"10.100.{network_id}.10"
        result[group] = (inventory_name, box_key, output)

    dc = result["mhl_domain_controllers"]
    fs = result["mhl_file_servers"]
    if dc[0] == fs[0] or dc[2]["ansible_host"] == fs[2]["ansible_host"]:
        raise ServiceConfigurationError("MHL domain controller and file server must be distinct applied guests.")
    selected = {
        "all": {
            "vars": {"ansible_connection": "winrm", "ansible_port": 5985,
                     "ansible_winrm_transport": "ntlm", "ansible_winrm_message_encryption": "always",
                     "ansible_winrm_server_cert_validation": "ignore"},
            "children": {
                "mhl_domain_controllers": {"hosts": {dc[0]: dc[2]}},
                "mhl_domain_members": {"children": {
                    "mhl_file_servers": {"hosts": {fs[0]: fs[2]}}
                }},
            },
        }
    }
    bindings = {"mhl_domain_controllers": (dc[0], dc[1], dc[2]),
                "mhl_file_servers": (fs[0], fs[1], fs[2])}
    return selected, {group: {"inventory_name": host, "box_key": key, "values": values}
                      for group, (host, key, values) in bindings.items()}


def _windows_forest_vault(network_id: int, dc_box_key: str, dc_guest_id: int,
                          dc_image_id: str, dc_credential: dict,
                          secrets_root: Path, run=subprocess.run) -> tuple[Path, Path, dict]:
    directory = secrets_root / "project-100"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    forest_key = hashlib.sha256(f"100:{network_id}:{dc_box_key}:{dc_guest_id}:{dc_image_id}".encode()).hexdigest()[:16]
    stem = f"mhl-windows-network-{network_id}-{forest_key}"
    vault_path = directory / f"{stem}.vault.yml"
    password_path = directory / f"{stem}.vault-password"
    if vault_path.exists() != password_path.exists():
        raise ServiceConfigurationError("The private MHL forest vault is incomplete; ask a platform administrator to recover its matching password file.")
    if vault_path.exists():
        _checked_private_file(vault_path, "MHL forest vault")
        _checked_private_file(password_path, "MHL forest vault password")
        viewed = run(["ansible-vault", "view", "--vault-password-file", str(password_path), str(vault_path)],
                     cwd=ROOT, text=True, capture_output=True, timeout=60)
        if viewed.returncode:
            raise ServiceConfigurationError("The private MHL forest vault could not be opened; no credentials were changed.")
        try:
            payload = json.loads(viewed.stdout)
        except json.JSONDecodeError as exc:
            raise ServiceConfigurationError("The private MHL forest vault has invalid content; no credentials were changed.") from exc
        binding = payload.get("mhl_forest_binding", {})
        if (binding.get("project_id") != 100 or binding.get("network_id") != network_id
                or binding.get("dc_box_key") != dc_box_key or binding.get("dc_guest_id") != dc_guest_id
                or binding.get("dc_image_id") != dc_image_id
                or payload.get("mhl_domain_admin_password") != dc_credential.get("password")):
            raise ServiceConfigurationError("The saved MHL forest vault does not match the selected controller and private WinRM binding; refusing credential rotation.")
        return vault_path, password_path, payload

    password = secrets.token_urlsafe(48).replace("=", "")
    _safe_write(password_path, password + "\n")
    stable = {
        "mhl_forest_binding": {"project_id": 100, "network_id": network_id,
                               "dc_box_key": dc_box_key, "dc_guest_id": dc_guest_id,
                               "dc_image_id": dc_image_id},
        "mhl_local_admin_user": dc_credential["username"],
        "mhl_domain_admin_user": "Administrator@mhl.test",
        "mhl_domain_admin_password": dc_credential["password"],
        "mhl_dsrm_password": secrets.token_urlsafe(36).replace("=", ""),
        "mhl_backup_service_password": secrets.token_urlsafe(36).replace("=", ""),
        "mhl_warehouse_deploy_password": secrets.token_urlsafe(36).replace("=", ""),
        "mhl_employees": [
            {"username": "jordan.lee", "display_name": "Jordan Lee", "password": secrets.token_urlsafe(36).replace("=", "")},
            {"username": "taylor.reed", "display_name": "Taylor Reed", "password": secrets.token_urlsafe(36).replace("=", "")},
        ],
    }
    with tempfile.TemporaryDirectory(prefix="crange-mhl-vault-", dir=directory) as temporary:
        os.chmod(temporary, 0o700)
        plaintext = Path(temporary) / "vault.json"
        encrypted = Path(temporary) / "vault.encrypted"
        _safe_write(plaintext, json.dumps(stable, sort_keys=True) + "\n")
        result = run(["ansible-vault", "encrypt", "--vault-password-file", str(password_path),
                      "--output", str(encrypted), str(plaintext)], cwd=ROOT, text=True,
                     capture_output=True, timeout=60)
        if result.returncode or not encrypted.is_file():
            password_path.unlink(missing_ok=True)
            raise ServiceConfigurationError("Could not create the private MHL forest vault; no guest changes were made.")
        os.chmod(encrypted, 0o600)
        os.replace(encrypted, vault_path)
    _checked_private_file(vault_path, "MHL forest vault")
    return vault_path, password_path, stable


def _record_windows_profile(network_id: int, bindings: dict, source_sha256=None) -> None:
    record_path = Path("/var/lib/crange-jobs/mhl-service-profiles.json")
    try:
        current = json.loads(record_path.read_text()) if record_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        current = {}
    identities = {}
    for row in bindings.values():
        host = row["values"]
        identities[row["box_key"]] = {"hostname": row["inventory_name"],
                                        "guest_id": host["crange_guest_id"],
                                        "image_id": host["crange_image_id"]}
    current.setdefault(str(network_id), {})["windows"] = {
        "profile": "project-100-mhl-windows-ad-file-v1",
        "source_sha256": source_sha256 or recipe_digest("windows"),
        "configured_at": datetime.now(timezone.utc).isoformat(),
        "guests": identities,
    }
    record_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
    os.chmod(record_path, 0o660)


def configure_windows(project_path: Path, network_id: int, *, secrets_root: Path | None = None,
                      run=subprocess.run, canonical_path: Path | None = None, recipe_sha256=None) -> None:
    from crange_mhl_recipes import source_directory
    with source_directory('windows',recipe_sha256,ROOT) as recipe_root:
        _configure_windows(project_path,network_id,secrets_root=secrets_root,run=run,
                           canonical_path=canonical_path,recipe_root=recipe_root)


def _configure_windows(project_path, network_id, *, secrets_root=None, run=subprocess.run,
                       canonical_path=None, recipe_root=None):
    recipe_root = recipe_root or ROOT
    secrets_root = secrets_root or Path.home() / ".config/crange/secrets"
    if network_id not in NETWORKS:
        raise ServiceConfigurationError("Choose Team 0 staging or a declared Project 100 team from 1 through 5.")
    if canonical_path is None:
        with tempfile.TemporaryDirectory(prefix="crange-mhl-canonical-") as temporary:
            canonical_path = Path(temporary) / "project-100.json"
            generated = run([sys.executable, str(ROOT / "scripts/generate-project-inventory.py"),
                             "--terraform-dir", str(project_path), "--output", str(canonical_path)],
                            cwd=ROOT, text=True, capture_output=True, timeout=300)
            if generated.returncode:
                raise ServiceConfigurationError("Generate applied Project 100 inventory after deploying the reviewed Windows release, then retry.")
            canonical = json.loads(canonical_path.read_text())
            _configure_windows_inventory(project_path, network_id, canonical,
                                         secrets_root=secrets_root, run=run, recipe_root=recipe_root)
        return
    try:
        canonical = json.loads(canonical_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ServiceConfigurationError("The canonical Project 100 Windows inventory could not be read.") from exc
    _configure_windows_inventory(project_path, network_id, canonical,
                                 secrets_root=secrets_root, run=run, recipe_root=recipe_root)


def _secret_strings(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                yield from _secret_strings(item)
            elif isinstance(item, str) and ("password" in str(key).lower() or "token" in str(key).lower()):
                yield item
    elif isinstance(value, list):
        for item in value:
            yield from _secret_strings(item)


def _local_admin_username(username):
    """SAM account names exclude the WinRM computer/domain qualification."""
    name=username.rsplit('\\',1)[-1].split('@',1)[0] if isinstance(username,str) else ''
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,20}',name) or name in ('.','..'):
        raise ServiceConfigurationError('Bound Windows local administrator account name is invalid.')
    return name


def _configure_windows_inventory(project_path: Path, network_id: int, canonical: dict, *,
                                 secrets_root: Path, run=subprocess.run, test_scope=None, recipe_root=None) -> None:
    recipe_root = recipe_root or ROOT
    inventory, bindings = _windows_scope_hosts(project_path, network_id, canonical, test_scope)
    try:
        from crange_windows_guest_admin import reference, resolve
    except ImportError as exc:
        raise ServiceConfigurationError("Worker-private Windows credential support is unavailable.") from exc
    credentials = {}
    windows_groups = inventory["all"]["children"]
    # Reuse the fixed management/health baseline with only these validated hosts.
    windows_groups['windows_guests']={'children':{'mhl_domain_controllers':{},'mhl_domain_members':{}}}
    for group, row in bindings.items():
        host = row["inventory_name"]
        values = row["values"]
        try:
            credential = resolve(reference(PROJECT, row["box_key"]), project_id=PROJECT,
                                 box_key=row["box_key"], guest_id=values["crange_guest_id"],
                                 image_id=values["crange_image_id"])
        except (OSError, ValueError) as exc:
            raise ServiceConfigurationError(
                f"Windows guest {row['box_key']} has no private credential binding for its applied image. Use Configure Windows guests after the network and image prerequisites are satisfied, then retry."
            ) from None
        values["ansible_user"] = credential["username"]
        values["ansible_password"] = credential["password"]
        credentials[host] = credential
    dc_row = bindings["mhl_domain_controllers"]
    dc_credential = credentials[dc_row["inventory_name"]]
    vault_path, password_path, stable = _windows_forest_vault(
        network_id, dc_row["box_key"], dc_row["values"]["crange_guest_id"],
        dc_row["values"]["crange_image_id"], dc_credential, secrets_root, run=run)
    runtime = {
        # Archived roles use one variable in both controller/member plays.
        # Extra vars resolve this fixed expression per host; authentication
        # keeps its qualified principal while win_user receives a bare SAM name.
        "mhl_local_admin_user": "{{ mhl_guest_admin_local_users[inventory_hostname] }}",
        "mhl_guest_admin_local_users": {host:_local_admin_username(value["username"])
                                        for host,value in credentials.items()},
        "mhl_local_admin_password": dc_credential["password"],
        "mhl_guest_admin_passwords": {host: credentials[host]["password"] for host in credentials},
    }
    values_to_redact = set(_secret_strings(stable))
    values_to_redact.update(credential["password"] for credential in credentials.values())
    environment = {**os.environ, "ANSIBLE_TIMEOUT": "90", "ANSIBLE_DISPLAY_ARGS_TO_STDOUT": "false",
                   "CRANGE_MHL_WINDOWS_VAULT_FILE": str(vault_path),
                   "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}
    with tempfile.TemporaryDirectory(prefix="crange-mhl-windows-") as temporary:
        os.chmod(temporary, 0o700)
        inventory_path = Path(temporary) / "inventory.json"
        runtime_path = Path(temporary) / "runtime-vars.json"
        _safe_write(inventory_path, json.dumps(inventory, sort_keys=True) + "\n")
        _safe_write(runtime_path, json.dumps(runtime, sort_keys=True) + "\n")
        for playbook in (ROOT / "ansible/windows/crange-guests.yml",
                         recipe_root / "ansible/windows/project-100-domain.yml",
                         recipe_root / "ansible/windows/project-100-members.yml"):
            phase = ("Windows management and router-only health access" if playbook.name=='crange-guests.yml' else
                     "Active Directory forest and company directory" if playbook.name=='project-100-domain.yml' else
                     "domain membership and file services")
            print(f"Configuring {phase} in network {network_id}; Windows role installation and reboots may take several minutes.", flush=True)
            command = ["ansible-playbook", "-f", "1", "-i", str(inventory_path),
                       str(playbook),
                       "--vault-password-file", str(password_path),
                       "--extra-vars", "@" + str(runtime_path)]
            result = run(command, cwd=ROOT, text=True, capture_output=True,
                         timeout=7200, env=environment)
            output = result.stdout or ""
            if result.returncode:
                # Ansible reports task failures on stdout; stderr may contain
                # only warnings. Preserve both, then redact private credentials.
                output = "\n".join(value for value in (output, result.stderr) if value) or "The fixed MHL Windows role failed."
                for secret in values_to_redact:
                    if secret:
                        output = output.replace(secret, "[redacted]")
                raise ServiceConfigurationError(output[-12000:])
            for secret in values_to_redact:
                if secret:
                    output = output.replace(secret, "[redacted]")
            if output.strip():
                print(output[-12000:])
    _record_windows_profile(network_id, bindings, source_sha256=recipe_digest("windows", recipe_root))
    print("MHL forest and file-server roles configured for the selected network.")


def configure_test_cohort(project_path, release, record):
    """Infer service replay from immutable profiles; target no original staging guests."""
    profiles=verify_release_profiles(release)
    if not profiles: return
    from crange_mhl_test_scope import select
    cfg=json.loads((project_path/'crange.auto.tfvars.json').read_text())
    scope=select(cfg,release,record)
    with tempfile.TemporaryDirectory(prefix='crange-mhl-test-') as temporary:
        os.chmod(temporary,0o700)
        canonical_path=Path(temporary)/'inventory.json'
        result=subprocess.run([sys.executable,str(ROOT/'scripts/generate-project-inventory.py'),
                               '--terraform-dir',str(project_path),'--output',str(canonical_path)],
                              cwd=ROOT,text=True,capture_output=True,timeout=300)
        if result.returncode:raise ServiceConfigurationError('Disposable test inventory could not be generated.')
        canonical=json.loads(canonical_path.read_text())
        network=scope['network_id']
        print(f'Replaying immutable MHL service profiles on isolated test LAN {network}.',flush=True)
        recipes=release['scenario']['service_profiles']
        if 'windows' in profiles:
            from crange_mhl_recipes import source_directory
            with source_directory('windows',recipes['windows']['source_sha256'],ROOT) as recipe_root:
                _configure_windows_inventory(project_path,network,canonical,
                    secrets_root=Path.home()/'.config/crange/secrets',test_scope=scope,recipe_root=recipe_root)
        if 'business' in profiles:
            configure(project_path,network,canonical_path=canonical_path,test_scope=scope,
                      recipe_sha256=recipes['business']['source_sha256'])
