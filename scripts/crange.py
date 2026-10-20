#!/usr/bin/env python3
"""Safe, project-scoped command line interface for the CRANGE infrastructure."""
from __future__ import annotations

import argparse
import copy
import errno
import fcntl
import ssl
import urllib.error
import urllib.parse
import urllib.request
import json
import os
import hashlib
import hmac
import ipaddress
import time
from datetime import datetime, timezone
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ENVS = ROOT / "terraform" / "environments"
NODES = {f"proxmox{i}" for i in range(1, 7)}


class CrangeError(RuntimeError):
    pass


def run(command: list[str], *, cwd: Path = ROOT, capture: bool = False, env: dict | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, cwd=cwd, text=True, check=False,
                                stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.PIPE if capture else None, env=env, timeout=timeout)
    except FileNotFoundError as exc:
        raise CrangeError(f"Required command not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CrangeError(f"Command timed out after {timeout}s: {shlex.join(command)}") from exc
    if result.returncode:
        details = (result.stderr or result.stdout or "").strip() if capture else ""
        raise CrangeError(f"Command failed ({result.returncode}): {shlex.join(command)}" + (f"\n{details}" if details else ""))
    return result


def project_dir(project: str) -> Path:
    if not re.fullmatch(r"[0-9]{1,3}", project):
        raise CrangeError("Project must be a numeric ID.")
    path = ENVS / f"project-{int(project):03d}"
    if not path.is_dir():
        raise CrangeError(f"No Terraform environment at {path.relative_to(ROOT)}")
    from crange_project_lifecycle import require_active
    require_active(path)
    return path


def json_config(path: Path) -> dict:
    cfg = path / "crange.auto.tfvars.json"
    if cfg.exists():
        try:
            return json.loads(cfg.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise CrangeError(f"Cannot read {cfg}: {exc}") from exc
    # Import the standard project inputs once from the existing HCL tfvars file.
    tfvars = path / "terraform.tfvars"
    if tfvars.exists():
        content = tfvars.read_text()
        project_match = re.search(r"(?m)^\s*project_id\s*=\s*(\d+)\s*$", content)
        transit_match = re.search(r'(?m)^\s*transit_address\s*=\s*"([^"\n]+)"\s*$', content)
        teams_match = re.search(r"(?ms)^\s*team_ids\s*=\s*\[([^]]*)\]", content)
        if project_match and transit_match and teams_match:
            teams = re.findall(r'"(\d+)"', teams_match.group(1))
            boxes = {}
            box_map = re.search(r"(?m)^\s*jump_boxes\s*=\s*\{", content)
            if box_map:
                start = box_map.end() - 1
                depth = 0
                end = start
                for end in range(start, len(content)):
                    if content[end] == "{":
                        depth += 1
                    elif content[end] == "}":
                        depth -= 1
                        if depth == 0:
                            break
                box_body = content[start + 1:end]
                for item in re.finditer(r'(?ms)^\s*"([^"\n]+)"\s*=\s*\{(.*?)^\s*\}', box_body):
                    name, body = item.group(1), item.group(2)
                    fields = {}
                    for field in ("team_id", "jump_number", "node_name", "cores", "memory_mb", "full_clone", "started"):
                        found = re.search(rf'(?m)^\s*{field}\s*=\s*("[^"]*"|[0-9]+|true|false)\s*$', body)
                        if found:
                            raw = found.group(1)
                            fields[field] = raw.strip('"') if raw.startswith('"') else (raw == "true" if raw in ("true", "false") else int(raw))
                    if all(k in fields for k in ("team_id", "jump_number", "node_name")):
                        boxes[name] = fields
            return {"project_id": int(project_match.group(1)), "transit_address": transit_match.group(1), "team_ids": teams, "jump_boxes": boxes, "custom_networks": {}, "scenario_boxes": {}, "scenario_containers": {}}
    raise CrangeError(f"{path.name} has no standardized CRANGE project inputs.")


def save_config(path: Path, data: dict) -> None:
    validate_project_quota(path, data)
    cfg = path / "crange.auto.tfvars.json"
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".crange.", dir=path)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.chmod(tmp_name, 0o640)
        os.replace(tmp_name, cfg)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


DEFAULT_QUOTA = {
    'teams': 32, 'custom_networks': 64, 'jump_boxes': 128,
    'scenario_boxes': 256, 'scenario_containers': 256,
    'cores': 512, 'memory_mb': 1048576,
}


def project_quota(path: Path) -> dict:
    source = path / 'quota-policy.json'
    if not source.exists():
        return DEFAULT_QUOTA.copy()
    try:
        policy = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CrangeError(f'Invalid quota policy: {exc}') from exc
    if not isinstance(policy, dict) or set(policy) != set(DEFAULT_QUOTA):
        raise CrangeError('Quota policy must define every supported limit.')
    for key, value in policy.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > DEFAULT_QUOTA[key]:
            raise CrangeError(f'Quota {key} must be between 1 and {DEFAULT_QUOTA[key]}.')
    return policy


def validate_project_quota(path: Path, data: dict) -> None:
    quota = project_quota(path)
    resources = {key: data.get(key, {}) for key in ('custom_networks', 'jump_boxes', 'scenario_boxes', 'scenario_containers')}
    counts = {'teams': len(data.get('team_ids', [])), **{key: len(items) for key, items in resources.items()}}
    machines = [box for key in ('jump_boxes', 'scenario_boxes', 'scenario_containers') for box in resources[key].values()]
    custom_routers = list(resources['custom_networks'].values())
    router_cores = 2 + counts['teams'] + sum(int(net.get('cores', 1)) for net in custom_routers)
    router_memory = (2 + counts['teams']) * 512 + sum(int(net.get('memory_mb', 512)) for net in custom_routers)
    counts['cores'] = router_cores + sum(int(box.get('cores', 1)) for box in machines)
    counts['memory_mb'] = router_memory + sum(int(box.get('memory_mb', 512)) for box in machines)
    for key, value in counts.items():
        if value > quota[key]:
            raise CrangeError(f'Project {key} quota exceeded: {value} requested, {quota[key]} allowed.')


def output_json(path: Path) -> dict:
    from crange_guest_outputs import complete_metadata
    raw = json.loads(run(["terraform", "output", "-json"], cwd=path, capture=True).stdout)
    try:
        def read(name):
            file = path / name
            return json.loads(file.read_text()) if file.exists() else {}
        return complete_metadata(raw, read('crange.auto.tfvars.json'), read('crange-applied-placements.json'))
    except (OSError, ValueError) as exc:
        raise CrangeError(f'Cannot read applied guest identity metadata: {exc}') from exc


def terraform_env() -> dict:
    env = os.environ.copy()
    env["PROXMOX_VE_INSECURE"] = "true"
    return env


def tf_run(path: Path, args: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    # Source the root-only credential file without displaying it. Insecure TLS is
    # scoped to this Terraform process because the cluster uses a private CA.
    if args and args[0] in ('init', 'plan', 'apply'):
        args = [args[0], '-no-color', *args[1:]]
    import crange_proxmox_compat
    identity = crange_proxmox_compat.reviewed_identity()
    endpoint_override = ("export PROXMOX_VE_ENDPOINT=" + shlex.quote(identity['endpoint']) + "; ") if identity['enabled'] else ""
    command = ["bash", "-lc", "set -a; . /home/crange/.config/crange/proxmox.env; set +a; export PROXMOX_VE_INSECURE=true; " + endpoint_override + "exec terraform " + shlex.join(args)]
    # A reviewed batch can spend several minutes starting each Windows clone.
    # Keep read/review commands short, but align apply with the bounded worker
    # workflow budget instead of killing healthy serial provisioning at 30m.
    timeout = 12 * 3600 if args and args[0] == 'apply' else 1800
    return run(command, cwd=path, capture=capture, env=terraform_env(), timeout=timeout)


def cmd_projects(_args: argparse.Namespace) -> None:
    found = sorted(ENVS.glob("project-[0-9][0-9][0-9]"))
    for path in found:
        try:
            cfg = json_config(path)
            print(f"{int(cfg['project_id']):03d}\t{path.name}\tmanaged")
        except CrangeError:
            continue


def cmd_project_show(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    print(f"Project {int(cfg['project_id']):03d} ({path.name})")
    print(f"Transit: {cfg['transit_address']}")
    print("Teams: " + ", ".join(sorted(cfg.get("team_ids", []), key=int)))
    for key, box in sorted(cfg.get("jump_boxes", {}).items()):
        print(f"Jump box {key}: team {box['team_id']} network {box.get('network_key') or box['team_id']} number {box['jump_number']} on {box['node_name']}")
    for key, network in sorted(cfg.get("custom_networks", {}).items()):
        print(f"Custom network {key}: 10.{cfg['project_id']}.{network['network_id']}.0/24 behind team/network {network['owner_team_id']}")
    if (path / "terraform.tfstate").exists():
        try:
            outputs = output_json(path)
            print("Terraform outputs:")
            print(json.dumps({name: value["value"] for name, value in outputs.items()}, indent=2, sort_keys=True))
        except CrangeError as exc:
            print(f"Terraform outputs unavailable: {exc}", file=sys.stderr)


def cmd_guac_sync(args: argparse.Namespace) -> None:
    """Render approved per-user Guacamole access from project assignments."""
    path = project_dir(args.project)
    cfg = json_config(path)

    def connector_status(configured, url, state, error=None):
        status_path = Path('/var/lib/crange-jobs/guacamole-status.json')
        try:
            status_path.parent.mkdir(mode=0o2770, parents=True, exist_ok=True)
            status_path.write_text(json.dumps({'configured': configured, 'url': url, 'state': state, 'error': error}) + '\n')
            status_path.chmod(0o660)
        except OSError:
            pass

    from crange_auth import AuthStore
    auth_path = Path(os.environ.get('CRANGE_AUTH_DB', Path.home() / '.local/share/crange/auth.sqlite3'))
    all_users = AuthStore(auth_path).list_users(project=str(cfg['project_id']))
    deployed = {}
    try:
        outputs = output_json(path)
        from crange_guest_outputs import has_guest_id
        deployed = {kind: {key: guest for key, guest in (outputs.get(kind, {}).get('value', {}) or {}).items()
                           if has_guest_id(guest)}
                    for kind in ('jump_boxes', 'scenario_boxes', 'scenario_containers')}
    except CrangeError:
        deployed = {kind: {} for kind in ('jump_boxes', 'scenario_boxes', 'scenario_containers')}

    connections = []
    project_id = int(cfg['project_id'])
    windows_rdp_bindings = {}
    try:
        approvals = templates_for(path)
        applied_placements = json.loads((path / 'crange-applied-placements.json').read_text())
    except (CrangeError, OSError, json.JSONDecodeError):
        approvals, applied_placements = {}, {}
    from crange_windows_guest_rdp import reference as windows_rdp_reference, resolve as resolve_windows_rdp
    for box_key, box in sorted(cfg.get('scenario_boxes', {}).items()):
        template = box.get('template')
        profile = approvals.get(template, {}).get('profile', {}) if isinstance(approvals, dict) else {}
        if not isinstance(profile, dict) or not str(profile.get('os_type', '')).startswith('win'):
            continue
        live = deployed.get('scenario_boxes', {}).get(box_key, {})
        placement = live.get('placement', {}) if isinstance(live, dict) else {}
        sidecar = applied_placements.get(f'vm:{box_key}') if isinstance(applied_placements, dict) else None
        if (not isinstance(live, dict) or type(live.get('id')) is not int
                or not isinstance(placement, dict) or not isinstance(sidecar, dict)
                or not isinstance(placement.get('image_id'), str)
                or sidecar.get('guest_id') != live.get('id')
                or sidecar.get('image_id') != placement.get('image_id')
                or sidecar.get('declared_template') != template
                or sidecar.get('template_id') != placement.get('template_id')
                or sidecar.get('template_node') != placement.get('template_node')):
            continue
        ref = windows_rdp_reference(project_id, box_key, live['id'], placement.get('image_id'))
        identity = {'project_id': project_id, 'crange_box_key': box_key,
                    'crange_guest_id': live['id'], 'crange_image_id': placement.get('image_id'),
                    'credential_ref': ref}
        try:
            resolve_windows_rdp(ref, project_id=project_id, box_key=box_key,
                                guest_id=live['id'], image_id=placement.get('image_id'))
        except (OSError, ValueError):
            continue
        windows_rdp_bindings[box_key] = identity
    defaults = {}
    for name, box in sorted(cfg.get('jump_boxes', {}).items()):
        team = str(box['team_id'])
        number = int(box['jump_number'])
        if team not in defaults or number < defaults[team][0]:
            defaults[team] = (number, name)
    for name, box in sorted(cfg.get('jump_boxes', {}).items()):
        live = deployed['jump_boxes'].get(name, {})
        if not live.get('id'):
            continue
        address = box.get('address') or live.get('address')
        # Staged resources do not have a usable Guacamole endpoint yet. They
        # enter the manifest automatically on the next sync after deployment.
        if not address:
            continue
        from crange_jumpboxes import connection_records
        connections.extend(connection_records(project_id, 'jumpbox', name, box, address,
                                               defaults[str(box['team_id'])][1] == name))
    for kind, field in (('vm', 'scenario_boxes'), ('container', 'scenario_containers')):
        for name, box in sorted(cfg.get(field, {}).items()):
            live = deployed[field].get(name, {})
            address = live.get('address')
            if not address:
                continue
            from crange_jumpboxes import connection_records
            rows = connection_records(project_id, kind, name, box, address)
            if kind == 'vm':
                profile = approvals.get(box.get('template'), {}).get('profile', {}) if isinstance(approvals, dict) else {}
                if isinstance(profile, dict) and str(profile.get('os_type', '')).startswith('win'):
                    for row in rows:
                        row['guest_os'] = 'windows'
                        if row.get('protocol') == 'rdp':
                            row.update({key: value for key, value in windows_rdp_bindings.get(name, {}).items()
                                        if key != 'credential_ref'})
                            if name in windows_rdp_bindings and not row.get('credential_ref'):
                                row['credential_ref'] = windows_rdp_bindings[name]['credential_ref']
                            elif row.get('credential_ref') is None:
                                row.pop('credential_ref', None)
            connections.extend(rows)

    users = []
    for user in all_users:
        if not user.get('enabled', True):
            continue
        team_ids = {str(team) for team in user.get('teams', {}).get(str(project_id), [])}
        selected = set(user.get('connections', {}).get(str(project_id), []))
        allowed = ([connection['key'] for connection in connections] if user.get('admin') else
                   [connection['key'] for connection in connections
                    if (connection['default_for_team'] and connection['team_id'] in team_ids)
                    or connection['key'] in selected])
        users.append({'username': user['username'], 'teams': sorted(team_ids),
                      'connection_keys': sorted(set(allowed))})

    manifest = {'schema_version': 3, 'auth_mode': 'crange-json', 'project_id': project_id,
                'generated_at': datetime.now(timezone.utc).isoformat(),
                'users': users, 'connections': connections,
                'status': 'pending_connector_apply'}
    target = ROOT / 'guacamole' / 'access' / f'project-{project_id:03d}.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    print(f'Wrote Guacamole access for {len(users)} users and {len(connections)} declared connections to {target}.')
    from crange_guac import GuacamoleClient
    connector = GuacamoleClient()
    configured = bool(connector.username and connector.password)
    connector_status(configured, connector.base_url, 'ready_to_apply' if configured else 'pending_configuration')
    print('Apply access explicitly after reviewing the manifest.' if configured else
          'Connector credentials are missing. Configure them on the controller before applying access.')


def cmd_guac_apply(args: argparse.Namespace) -> None:
    manifest_path = ROOT / 'guacamole' / 'access' / f'project-{int(args.project):03d}.json'
    if not manifest_path.is_file():
        raise CrangeError(f'No Guacamole access manifest exists for project {int(args.project):03d}; run guac-sync first.')
    from crange_guac import apply_manifest, GuacamoleError
    try:
        applied = apply_manifest(manifest_path)
    except GuacamoleError as exc:
        try:
            manifest = json.loads(manifest_path.read_text())
            manifest.update(status='apply_failed', last_error=str(exc), last_apply_at=datetime.now(timezone.utc).isoformat())
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
        except (OSError, json.JSONDecodeError):
            pass
        raise CrangeError(str(exc)) from exc
    manifest = json.loads(manifest_path.read_text())
    manifest.update(status='applied', applied_at=datetime.now(timezone.utc).isoformat(),
                    last_error=None, applied_connections=applied)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    print(f'Applied Guacamole access for {len(applied)} managed connection(s) from {manifest_path}.')
    from crange_auth import AuthStore
    from crange_guac_users import ProjectUsers
    auth = AuthStore(os.environ.get('CRANGE_AUTH_DB', Path.home() / '.local/share/crange/auth.sqlite3'))
    if ProjectUsers(auth).list(int(args.project)) or manifest_path.with_name(manifest_path.stem+'.users-applied.json').exists():
        cmd_guac_users(args)

def cmd_guac_refresh(args: argparse.Namespace) -> None:
    cmd_guac_sync(args)
    cmd_guac_apply(args)


def cmd_guac_users(args: argparse.Namespace) -> None:
    from crange_auth import AuthStore
    from crange_guac import GuacamoleClient, GuacamoleError
    from crange_guac_users import apply_users
    project_dir(args.project)
    auth = AuthStore(os.environ.get('CRANGE_AUTH_DB', Path.home() / '.local/share/crange/auth.sqlite3'))
    key_file = Path(os.environ.get('CRANGE_GUAC_JSON_KEY', Path.home() / '.config/crange/guacamole-json.secret'))
    try:
        apply_users(int(args.project), auth, GuacamoleClient(), ROOT, key_file.read_text().strip())
    except (ValueError, OSError, GuacamoleError) as exc:
        raise CrangeError(str(exc)) from exc


def cmd_guac_config(_args: argparse.Namespace) -> None:
    import getpass
    url = input('Guacamole URL [http://10.60.0.10:8080]: ').strip() or 'http://10.60.0.10:8080'
    username = input('Guacamole administrator username: ').strip()
    password = getpass.getpass('Guacamole administrator password: ')
    if not username or not password:
        raise CrangeError('Guacamole username and password are required.')
    target = Path.home() / '.config/crange/guacamole.env'
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_text(f'GUACAMOLE_URL={url}\nGUACAMOLE_USERNAME={username}\nGUACAMOLE_PASSWORD={password}\n')
    target.chmod(0o600)
    status_path = Path('/var/lib/crange-jobs/guacamole-status.json')
    try:
        status_path.parent.mkdir(mode=0o2770, parents=True, exist_ok=True)
        status_path.write_text(json.dumps({'configured': True, 'url': url, 'state': 'configured'}) + '\n')
        status_path.chmod(0o660)
    except OSError:
        pass
    print(f'Wrote protected Guacamole connector configuration to {target}.')


def cmd_guac_ticket_broker(_args: argparse.Namespace) -> None:
    from crange_guac_broker import serve
    serve()


def cmd_guac_json_key(_args: argparse.Namespace) -> None:
    import secrets
    target = Path.home() / '.config/crange/guacamole-json.secret'
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists():
        raise CrangeError(f'Guacamole JSON-auth key already exists at {target}; refusing to rotate it.')
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(secrets.token_hex(16) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    print(f'Generated the protected Guacamole JSON-auth key at {target}.')


def _occupied_project_transit_addresses() -> set[str]:
    """Collect transit addresses from project configs, OPNsense, and live guests."""
    occupied: set[str] = set()
    for existing in sorted(ENVS.glob("project-*")):
        if not existing.is_dir() or existing.name == "project-template":
            continue
        try:
            other = json_config(existing)
        except CrangeError as exc:
            raise CrangeError(f"Cannot safely allocate transit address: {existing.name} has unreadable project config.") from exc
        raw = other.get("transit_address")
        if raw:
            try:
                address = ipaddress.ip_interface(raw).ip
            except (ValueError, TypeError) as exc:
                raise CrangeError(f"Cannot safely allocate transit address: {existing.name} has an invalid transit address.") from exc
            if address in ipaddress.ip_network("10.60.2.0/24"):
                occupied.add(str(address))

    # Both external inventories are authoritative inputs. Fail closed if either
    # cannot be read so concurrent project creation cannot claim a live address.
    api = ProxmoxAPI()
    resources = api.request("GET", "/cluster/resources", {"type": "vm"})
    for resource in resources:
        node, vmid, kind = resource.get("node"), resource.get("vmid"), resource.get("type")
        if not node or not vmid or kind not in ("lxc", "qemu"):
            continue
        config = api.request("GET", f"/nodes/{node}/{kind}/{vmid}/config")
        for value in config.values():
            if not isinstance(value, str):
                continue
            for candidate in re.findall(r"(?<![0-9])10\.60\.2\.(?:[0-9]{1,3})(?![0-9])", value):
                try:
                    address = ipaddress.ip_address(candidate)
                except ValueError:
                    continue
                if address in ipaddress.ip_network("10.60.2.0/24"):
                    occupied.add(str(address))

    credential_file = Path.home() / ".config/crange/opnsense-api.txt"
    helper = ROOT / "scripts" / "opnsense-project-routes.py"
    shell = f"set -a; . {shlex.quote(str(credential_file))}; set +a; exec python3 {shlex.quote(str(helper))} occupied-transit"
    result = run(["bash", "-c", shell], capture=True, timeout=60)
    try:
        opnsense_addresses = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CrangeError("OPNsense returned an invalid transit-address inventory.") from exc
    if not isinstance(opnsense_addresses, list):
        raise CrangeError("OPNsense returned an invalid transit-address inventory.")
    for raw in opnsense_addresses:
        try:
            address = ipaddress.ip_address(raw)
        except (ValueError, TypeError) as exc:
            raise CrangeError("OPNsense returned an invalid transit address.") from exc
        if address in ipaddress.ip_network("10.60.2.0/24"):
            occupied.add(str(address))
    return occupied


def _allocate_project_transit_address(occupied: set[str]) -> str:
    transit = ipaddress.ip_network("10.60.2.0/24")
    for address in transit.hosts():
        if address in (transit.network_address + 1, transit.broadcast_address):
            continue
        if str(address) not in occupied:
            return f"{address}/24"
    raise CrangeError("No unused project transit address remains in 10.60.2.0/24.")


def cmd_project_init(args: argparse.Namespace) -> None:
    project = int(args.project)
    if project < 1 or project > 255 or project in (60, 61):
        raise CrangeError("Project ID must be 1–255, excluding reserved 60 and 61.")
    target = ENVS / f"project-{project:03d}"
    if target.exists():
        raise CrangeError(f"{target.name} already exists; refusing to overwrite it or its state.")
    template = ENVS / "project-template"
    lock_path = ENVS / ".project-init.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if target.exists():
            raise CrangeError(f"{target.name} already exists; refusing to overwrite it or its state.")
        transit = _allocate_project_transit_address(_occupied_project_transit_addresses())
        staging = Path(tempfile.mkdtemp(prefix=f".project-{project:03d}-", dir=ENVS))
        try:
            for filename in ("main.tf", "variables.tf", "template-profile.json", "approved-scenario-templates.json", "approved-container-templates.json"):
                source = template / filename
                if not source.is_file():
                    raise CrangeError(f"Missing project template file: {source}")
                (staging / filename).write_bytes(source.read_bytes())
            (staging / "terraform.tfvars.example").write_text(
                f'project_id = {project}\ntransit_address = "{transit}"\nteam_ids = ["1"]\n'
            )
            save_config(staging, {"project_id": project, "transit_address": transit, "router_node":"auto", "router_placement":{"mode":"auto","preferred_node":""}, "team_ids": ["1"], "jump_boxes": {}, "custom_networks": {}, "scenario_containers": {}})
            # The dashboard runs in the crange group and reads the known config
            # file (0640); execute-only group access allows traversal without
            # exposing a directory listing or Terraform state to the web user.
            os.chmod(staging, 0o710)
            os.replace(staging, target)
        finally:
            if staging.exists():
                import shutil
                shutil.rmtree(staging)
        print(f"Created {target.name} with transit {transit}, Team 1, and Team 0 staging. Run: crange infra-plan {project:03d}")


def mutate_team(args: argparse.Namespace, add: bool) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    team = int(args.team)
    if not 1 <= team <= 253:
        raise CrangeError("Ordinary team IDs must be between 1 and 253; staging is managed as Team 0 / network 254.")
    teams = set(str(x) for x in cfg.get("team_ids", []))
    if add:
        teams.add(str(team))
    else:
        if str(team) not in teams:
            raise CrangeError(f"Team {team} is not configured.")
        linked = [name for name, box in cfg.get("jump_boxes", {}).items() if str(box["team_id"]) == str(team)]
        if linked:
            raise CrangeError(f"Team {team} still has jump boxes: {', '.join(linked)}. Remove them first.")
        linked_networks = [name for name, network in cfg.get("custom_networks", {}).items() if str(network["owner_team_id"]) == str(team)]
        if linked_networks:
            raise CrangeError(f"Team {team} still owns custom networks: {', '.join(linked_networks)}. Remove them first.")
        linked_cts = [name for name, box in cfg.get("scenario_containers", {}).items() if str(box["team_id"]) == str(team)]
        if linked_cts:
            raise CrangeError(f"Team {team} still has scenario CTs: {', '.join(linked_cts)}. Remove them first.")
        scenario_linked = [name for name, box in scenario_boxes(cfg).items() if str(box["team_id"]) == str(team)]
        if scenario_linked:
            raise CrangeError(f"Team {team} still has scenario VMs: {', '.join(scenario_linked)}. Remove them first.")
        teams.remove(str(team))
        if not teams:
            raise CrangeError("A project must retain at least one ordinary team.")
    cfg["team_ids"] = sorted(teams, key=int)
    save_config(path, cfg)
    print(f"Team {team} {'added to' if add else 'removed from'} project {int(cfg['project_id']):03d}. Run infra plan before apply.")


def validate_custom_networks(cfg: dict) -> None:
    networks = cfg.get("custom_networks", {})
    ids = set()
    uplinks = set()
    declared_teams = set(map(str, cfg.get("team_ids", [])))
    for key, net in networks.items():
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", key) or not isinstance(net, dict):
            raise CrangeError(f"Custom network key {key!r} is invalid.")
        owner = str(net.get("owner_team_id", ""))
        if owner != "254" and owner not in declared_teams:
            raise CrangeError(f"Custom network {key} has undeclared owner team {owner}.")
        try:
            network_id = int(net["network_id"])
            uplink = int(net["uplink_last_octet"])
        except (KeyError, ValueError, TypeError) as exc:
            raise CrangeError(f"Custom network {key} has invalid network or uplink IDs.") from exc
        if not 1 <= network_id <= 253 or network_id in {int(t) for t in declared_teams} or network_id in ids:
            raise CrangeError(f"Custom network {key} needs a unique unused network ID from 1 to 253.")
        if not 2 <= uplink <= 100 or (owner, uplink) in uplinks:
            raise CrangeError(f"Custom network {key} needs a unique parent-router address in .2-.100.")
        if net.get("node_name") not in NODES | {"auto"}:
            raise CrangeError(f"Custom network {key} uses an invalid router node.")
        if not 1 <= int(net.get("cores", 1)) <= 16 or not 512 <= int(net.get("memory_mb", 512)) <= 65536:
            raise CrangeError(f"Custom network {key} router size must be 1-16 cores and 512-65536 MB RAM.")
        ids.add(network_id)
        uplinks.add((owner, uplink))
        if any(str(box.get("team_id")) == owner and not box.get("network_key") and int(box.get("ip_last_octet", -1)) == uplink for box in cfg.get("scenario_containers", {}).values()):
            raise CrangeError(f"Custom router uplink overlaps a scenario CT address in parent network {owner}.")
    # Parent-side router addresses may not collide with static scenario/jump hosts.
    for owner, host in uplinks:
        if any(str(box.get("team_id")) == owner and not box.get("network_key") and int(box.get("ip_last_octet", -1)) == host for box in cfg.get("scenario_boxes", {}).values()):
            raise CrangeError(f"Custom router uplink 10.{cfg['project_id']}.{owner}.{host} overlaps a scenario VM.")
        if any(str(box.get("team_id")) == owner and int(box.get("ip_last_octet", -1)) == host and not box.get("network_key") for box in cfg.get("scenario_containers", {}).values()):
            raise CrangeError(f"Custom router uplink 10.{cfg['project_id']}.{owner}.{host} overlaps a scenario CT.")
        if any(str(box.get("team_id")) == owner and 10 + int(box.get("jump_number", 0)) == host and not box.get("network_key") for box in cfg.get("jump_boxes", {}).values()):
            raise CrangeError(f"Custom router uplink 10.{cfg['project_id']}.{owner}.{host} overlaps a jump box.")
    for box in list(cfg.get("scenario_containers", {}).values()) + list(cfg.get("scenario_boxes", {}).values()):
        if box.get("network_key"):
            network = networks.get(box["network_key"])
            if not network or str(network["owner_team_id"]) != str(box["team_id"]):
                raise CrangeError("Scenario guest custom network attachment is invalid.")
            box["network_id"] = int(network["network_id"])
        else:
            box.setdefault("network_key", "")
            box["network_id"] = int(box["team_id"])


def custom_uplink_available(cfg: dict, owner: str, host: int) -> bool:
    if any(str(net["owner_team_id"]) == owner and int(net["uplink_last_octet"]) == host for net in cfg.get("custom_networks", {}).values()):
        return False
    if any(str(box["team_id"]) == owner and not box.get("network_key") and int(box.get("ip_last_octet", -1)) == host for box in cfg.get("scenario_boxes", {}).values()):
        return False
    if any(str(box["team_id"]) == owner and int(box.get("ip_last_octet", -1)) == host and not box.get("network_key") for box in cfg.get("scenario_containers", {}).values()):
        return False
    return not any(str(box["team_id"]) == owner and not box.get("network_key") and 10 + int(box["jump_number"]) == host for box in cfg.get("jump_boxes", {}).values())


def mutate_custom_network(args: argparse.Namespace, add: bool) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    networks = cfg.setdefault("custom_networks", {})
    key = args.name.lower()
    if add:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", key):
            raise CrangeError("Network name must use lowercase letters, digits, and hyphens (max 24 chars).")
        if key in networks:
            raise CrangeError(f"Custom network {key} already exists.")
        if args.team != 0 and not 1 <= args.team <= 253:
            raise CrangeError("Choose an ordinary team 1-253 or 0 for staging.")
        owner = "254" if args.team == 0 else str(args.team)
        if owner != "254" and owner not in set(map(str, cfg.get("team_ids", []))):
            raise CrangeError(f"Owner team {owner} is not configured.")
        network_id = int(args.id if hasattr(args, "id") else args.network_id)
        if not 1 <= network_id <= 253 or network_id in set(map(int, cfg.get("team_ids", []))):
            raise CrangeError("Custom network ID must be an unused project subnet number from 1 to 253.")
        if any(int(n["network_id"]) == network_id for n in networks.values()):
            raise CrangeError(f"Custom network ID {network_id} is already in use.")
        uplink = next((host for host in range(100, 1, -1) if custom_uplink_available(cfg, owner, host)), None)
        if uplink is None:
            raise CrangeError(f"No static parent-side router address remains in team/network {owner}.")
        networks[key] = {"owner_team_id": owner, "network_id": network_id,
                         "uplink_last_octet": uplink, "node_name": args.node,
                         "placement": __import__("crange_scheduling").placement_intent(args.node, getattr(args,"preferred_node","")),
                         "cores": 1, "memory_mb": 512}
        validate_custom_networks(cfg)
        save_config(path, cfg)
        print(f"Custom network {key} declared: 10.{cfg['project_id']}.{network_id}.0/24 behind a router in network {owner} at 10.{cfg['project_id']}.{owner}.{uplink}. Run infra-plan.")
    else:
        if key not in networks:
            raise CrangeError(f"Custom network {key} is not configured.")
        linked = [name for name, box in cfg.get("jump_boxes", {}).items() if box.get("network_key") == key]
        if linked:
            raise CrangeError(f"Custom network {key} still has jump boxes: {', '.join(linked)}. Remove them first.")
        linked_vms = [name for name, box in cfg.get("scenario_boxes", {}).items() if box.get("network_key") == key]
        if linked_vms:
            raise CrangeError(f"Custom network {key} still has scenario VMs: {', '.join(linked_vms)}. Remove them first.")
        linked_cts = [name for name, box in cfg.get("scenario_containers", {}).items() if box.get("network_key") == key]
        if linked_cts:
            raise CrangeError(f"Custom network {key} still has scenario CTs: {', '.join(linked_cts)}. Remove them first.")
        del networks[key]
        save_config(path, cfg)
        print(f"Custom network {key} removed from desired state. Review infra-plan before apply.")


def cmd_network_edit(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    networks = cfg.setdefault("custom_networks", {})
    net = networks.get(args.name)
    if net is None:
        raise CrangeError(f"Custom network {args.name} is not configured.")
    owner = str(net["owner_team_id"])
    if not 1 <= args.id <= 253 or (args.id != int(net["network_id"]) and (args.id in set(map(int, cfg.get("team_ids", []))) or any(int(other["network_id"]) == args.id for key, other in networks.items() if key != args.name))):
        raise CrangeError("Network ID must be an unused project subnet number from 1 to 253.")
    if args.node not in NODES | {"auto"} or not 2 <= args.uplink <= 100:
        raise CrangeError("Choose an approved Proxmox node and an uplink address from .2 to .100.")
    if not 1 <= args.cores <= 16 or not 512 <= args.memory <= 65536:
        raise CrangeError("Router size must be 1-16 cores and 512-65536 MB RAM.")
    for key, other in networks.items():
        if key != args.name and str(other["owner_team_id"]) == owner and int(other["uplink_last_octet"]) == args.uplink:
            raise CrangeError(f"Parent-side router address .{args.uplink} is already reserved.")
    if any(str(box.get("team_id")) == owner and not box.get("network_key") and int(box.get("ip_last_octet", -1)) == args.uplink for box in scenario_boxes(cfg).values()):
        raise CrangeError(f"Parent-side router address .{args.uplink} is already assigned to a VM.")
    if any(str(box.get("team_id")) == owner and not box.get("network_key") and int(box.get("ip_last_octet", -1)) == args.uplink for box in cfg.get("scenario_containers", {}).values()):
        raise CrangeError(f"Parent-side router address .{args.uplink} is already assigned to a container.")
    if any(str(box.get("team_id")) == owner and not box.get("network_key") and 10 + int(box.get("jump_number", 0)) == args.uplink for box in cfg.get("jump_boxes", {}).values()):
        raise CrangeError(f"Parent-side router address .{args.uplink} is already assigned to a jump box.")
    if args.node != net.get('node_name') or getattr(args, 'preferred_node', None) is not None:
        net['placement']=__import__('crange_scheduling').placement_intent(args.node, getattr(args, 'preferred_node', None) or '')
    net.update({"network_id": args.id, "node_name": args.node,
                "uplink_last_octet": args.uplink, "cores": args.cores,
                "memory_mb": args.memory})
    for box in cfg.get("jump_boxes", {}).values():
        if box.get("network_key") == args.name:
            box["network_id"] = args.id
    for box in list(scenario_containers(cfg).values()) + list(scenario_boxes(cfg).values()):
        if box.get("network_key") == args.name:
            box["network_id"] = args.id
    validate_custom_networks(cfg)
    validate_project_quota(path, cfg)
    save_config(path, cfg)
    print(f"Updated custom network {args.name}. Run infra-plan and review before deploying its router or subnet changes.")


def mutate_box(args: argparse.Namespace, add: bool, *, cfg=None, save=True) -> None:
    path = project_dir(args.project)
    cfg = json_config(path) if cfg is None else cfg
    boxes = cfg.setdefault("jump_boxes", {})
    name = args.name.lower()
    if add:
        match = re.fullmatch(r"([a-z][a-z0-9-]{0,30})-j([0-9]{2})", name)
        if not match:
            raise CrangeError("Jump box name must look like t001-j01 or corp-j01.")
        if name in boxes:
            raise CrangeError(f"Box {name} already exists.")
        team = int(args.team)
        owner = "254" if team == 0 else str(team)
        number = int(match.group(2))
        network_key = getattr(args, "network", None) or ""
        if network_key:
            net = cfg.get("custom_networks", {}).get(network_key)
            if not net:
                raise CrangeError(f"Custom network {network_key} is not configured.")
            if str(net["owner_team_id"]) != owner:
                raise CrangeError(f"Custom network {network_key} belongs to team/network {net['owner_team_id']}, not team {team}.")
            network_id = int(net["network_id"])
        else:
            if team != 0 and (not 1 <= team <= 253 or owner not in set(map(str, cfg.get("team_ids", [])))):
                raise CrangeError(f"Team {team} is not configured.")
            network_id = int(owner)
        if not 1 <= number <= 90:
            raise CrangeError("Jump box number must be 01–90.")
        if any(int(v.get("network_id", v["team_id"])) == network_id and int(v["jump_number"]) == number for v in boxes.values()):
            raise CrangeError(f"Network {network_id} already has jump box address .{10 + number}.")
        if network_id == int(owner) and any(str(n["owner_team_id"]) == owner and int(n["uplink_last_octet"]) == 10 + number for n in cfg.get("custom_networks", {}).values()):
            raise CrangeError(f"Address .{10 + number} is reserved for a custom router uplink.")
        if any(int(v.get("network_id", v["team_id"])) == network_id and int(v.get("ip_last_octet", -1)) == 10 + number for v in cfg.get("scenario_boxes", {}).values()):
            raise CrangeError(f"Address .{10 + number} is already assigned to a scenario VM.")
        if any(int(v.get("network_id", v["team_id"])) == network_id and int(v.get("ip_last_octet", -1)) == 10 + number for v in cfg.get("scenario_containers", {}).values()):
            raise CrangeError(f"Address .{10 + number} is already assigned to a scenario container.")
        if args.node not in NODES | {"auto"}:
            raise CrangeError("Node must be one of proxmox1 through proxmox6.")
        if not 1 <= args.cores <= 16 or not 512 <= args.memory <= 65536:
            raise CrangeError("Requested size exceeds CLI limits (1–16 cores, 512–65536 MB RAM).")
        full_clone = args.full_clone if args.full_clone is not None else args.node != "auto"
        boxes[name] = {"team_id": owner, "jump_number": number, "node_name": args.node,
                       "network_key": network_key, "network_id": network_id,
                       "placement": __import__("crange_scheduling").placement_intent(args.node, (getattr(args,"preferred_node",None) if getattr(args,"preferred_node",None) is not None else "proxmox4" if args.node=="auto" else "")),
                       "cores": args.cores, "memory_mb": args.memory,
                       "full_clone": full_clone, "started": True}
        reference = templates_for(path).get("approved-base", {}) if (path / "approved-scenario-templates.json").is_file() else {}
        if reference.get("image_id"):
            boxes[name]["image"] = reference["image_id"]
            if args.full_clone is None:boxes[name]["full_clone"] = False
        validate_custom_networks(cfg)
    else:
        if name not in boxes:
            raise CrangeError(f"Box {name} is not configured.")
        del boxes[name]
    if save:
        save_config(path, cfg)
    network_text = f" in network {boxes[name].get('network_key') or boxes[name]['team_id']}" if add else ""
    if save: print(f"Jump box {name} {'added to' if add else 'removed from'} project {int(cfg['project_id']):03d}{network_text}. Run infra-plan before apply.")

def cmd_box_state(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    boxes = cfg.get("jump_boxes", {})
    if args.name not in boxes:
        raise CrangeError(f"Box {args.name} is not configured.")
    boxes[args.name]["started"] = args.state == "start"
    save_config(path, cfg)
    print(f"Box {args.name} desired state set to {args.state}. Run infra plan, review it, then infra apply.")


def cmd_boxes(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    boxes = cfg.get("jump_boxes", {})
    if not boxes:
        print("No jump boxes declared.")
        return
    live = {}
    if (path / "terraform.tfstate").exists():
        try:
            live = output_json(path).get("jump_boxes", {}).get("value", {})
        except CrangeError:
            pass
    for name, item in sorted(boxes.items()):
        current = live.get(name, {})
        addr = current.get("address", "not applied")
        power = "running" if item.get("started", True) else "stopped"
        print(f"{name}\tteam={item['team_id']}\tnode={item['node_name']}\t{power}\t{addr}")


def automation_inputs_digest(path: Path) -> str:
    """Bind reviewed plans to template approvals and Terraform source inputs."""
    files = {f'project/{name}': path / name for name in (
        'approved-scenario-templates.json', 'approved-container-templates.json', 'template-profile.json')}
    files.update({f'project/{p.name}': p for p in path.glob('*.tf')})
    modules = ROOT / 'terraform' / 'modules'
    files.update({f'modules/{p.relative_to(modules)}': p for p in modules.rglob('*.tf')})
    values = {name: hashlib.sha256(source.read_bytes()).hexdigest() if source.exists() else None
              for name, source in files.items()}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _bootstrap_project(path: Path, cfg: dict, api=None):
    """Select a provider/zone-verified node for an empty routers-only project."""
    from crange import ProxmoxAPI
    from crange_scheduling import (bootstrap_node_candidates, placement_intent,
                                   provider_container_placement_verified,
                                   network_attestation_valid, read_settings)
    if any((path / name).exists() for name in ('terraform.tfstate', 'terraform.tfstate.backup', '.terraform.tfstate.lock.info')):
        raise CrangeError('Project bootstrap is only available before Terraform state exists.')
    from crange_project_lifecycle import state as lifecycle_state
    restored = lifecycle_state(path).get('needs_redeploy', False)
    if restored:
        cfg = copy.deepcopy(cfg)
        for field in ('jump_boxes', 'custom_networks', 'scenario_boxes', 'scenario_containers'):
            cfg[field] = {}
    if any(cfg.get(key) for key in ('jump_boxes', 'custom_networks', 'scenario_boxes', 'scenario_containers')):
        raise CrangeError('Project bootstrap only creates base routers and networks; remove scenario guests before bootstrapping.')
    policy, catalog = read_settings()
    if not policy.get('enabled'):
        raise CrangeError('Automatic placement policy is disabled; project bootstrap requires an enrolled node.')
    candidates = bootstrap_node_candidates(policy)
    if not candidates:
        raise CrangeError('No node has both provider-container acceptance and a current CRANGE zone network attestation.')
    node = candidates[0]
    image_id = cfg.get('router_image', 'debian-12-router-v1')
    image = catalog.get('images', {}).get(image_id)
    if not image or image.get('kind') != 'container' or image.get('source_type') != 'stock_archive' or image.get('verified') is not True:
        raise CrangeError('Router bootstrap requires the verified Debian stock archive image.')
    archive_nodes = {replica.get('node') for replica in image.get('replicas', [])
                     if replica.get('state') == 'ready' and replica.get('template_file_id')}
    eligible = [candidate for candidate in candidates if candidate in archive_nodes]
    if not eligible:
        raise CrangeError('The verified router archive is unavailable on every enrolled bootstrap node.')
    pid = int(cfg['project_id'])
    prefix = f'p{pid:03d}'
    expected_vnets = {prefix + 'tr', prefix + 't254'} | {f'{prefix}t{int(team):03d}' for team in cfg['team_ids']}
    if cfg.get('custom_networks'):
        raise CrangeError('Custom networks must be added after the base project router bootstrap.')
    api = api or ProxmoxAPI()
    nodes = api.request('GET', '/nodes')
    if not isinstance(nodes, list) or not nodes:
        raise CrangeError('Proxmox node inventory is unavailable for bootstrap.')
    live = api.request('GET', '/cluster/resources', {'type': 'vm'})
    for resource in live:
        tags = set(str(resource.get('tags', '')).split(';'))
        if restored and ('crange-image' in tags or resource.get('template')):
            continue
        if prefix in tags or str(resource.get('name', '')).startswith(prefix + '-'):
            raise CrangeError('Project-scoped guests already exist; use the normal reviewed plan instead of bootstrap.')
    vnets = api.request('GET', '/cluster/sdn/vnets')
    names = {row.get('vnet') for row in vnets if isinstance(row, dict)}
    if names & expected_vnets:
        raise CrangeError('Project VNets already exist outside Terraform state; reconcile them before bootstrap.')
    cfg = copy.deepcopy(cfg)
    cfg['router_node'] = node
    cfg['router_placement'] = placement_intent(node)
    cfg['bootstrap_candidate_nodes'] = eligible
    return cfg, node, eligible, expected_vnets


def _validate_bootstrap_plan(detail: dict, project_id: int, node: str, networks: set[str], cfg: dict) -> list[dict]:
    prefix = f'p{project_id:03d}'
    expected = {
        'proxmox_sdn_applier.finalizer', 'proxmox_sdn_vnet.transit',
        'proxmox_sdn_vnet.staging', 'terraform_data.sdn_revision',
        'proxmox_sdn_applier.apply',
        'module.project_router.proxmox_virtual_environment_container.router',
        'module.staging_router.proxmox_virtual_environment_container.router',
    }
    expected.update(f'proxmox_sdn_vnet.team[{json.dumps(str(team))}]' for team in cfg['team_ids'])
    expected.update(f'module.team_router[{json.dumps(str(team))}].proxmox_virtual_environment_container.router' for team in cfg['team_ids'])
    changes = []
    seen = set()
    for item in detail.get('resource_changes', []):
        address = item.get('address', '')
        actions = item.get('change', {}).get('actions', [])
        if actions == ['no-op']:
            continue
        if address not in expected or actions != ['create']:
            raise CrangeError('Bootstrap plan may create only the project SDN foundation and router containers; inspect/reconcile the project before retrying.')
        seen.add(address)
        after = item.get('change', {}).get('after') or {}
        if address.endswith('.router'):
            module_address = address.removesuffix('.proxmox_virtual_environment_container.router')
            resource_key = ('router:project' if module_address == 'module.project_router' else
                            'router:staging' if module_address == 'module.staging_router' else
                            'router:team:' + json.loads(module_address[len('module.team_router['):-1])
                            if module_address.startswith('module.team_router[') else None)
            expected_node = cfg.get('bootstrap_router_nodes', {}).get(resource_key, node)
            if after.get('node_name') != expected_node or after.get('pool_id') != 'crange-iac':
                raise CrangeError('Bootstrap router escaped its reviewed node or pool.')
            tags = set(after.get('tags') or [])
            if prefix not in tags:
                raise CrangeError('Bootstrap router ownership tag is missing.')
            os_block = after.get('operating_system') or []
            if len(os_block) != 1 or os_block[0].get('template_file_id') != 'crange-library:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst':
                raise CrangeError('Bootstrap router must use the verified Debian 12.12 archive.')
        elif address.startswith('proxmox_sdn_vnet.'):
            if after.get('id') not in networks:
                raise CrangeError('Bootstrap plan contains an unexpected VNet.')
        changes.append({'address': address, 'actions': actions})
    missing = expected - seen
    if missing:
        raise CrangeError('Bootstrap plan does not contain the complete fresh project foundation: ' + ', '.join(sorted(missing)))
    return changes


def cmd_stage_changes(args: argparse.Namespace) -> None:
    """Fixed project workflow: audited cache preparation, then exact guest plan."""
    from crange_scheduling import read_settings, required_preparations
    path = project_dir(args.project)
    resume_network_foundations(args, path)
    cfg = json_config(path)
    policy, _ = read_settings()
    from crange_staging import staging_projection
    stage_cfg = staging_projection(cfg)
    from crange_network_foundation import pending
    from crange_scheduling import applied_resources
    networks = pending(stage_cfg, applied_resources(path))
    if networks:
        print('Network foundation phase: reviewing new staging subnets before guest placement.', flush=True)
        return cmd_plan(argparse.Namespace(project=args.project, retain=True,
            replace_sdn_applier=False, staging=True, network_foundation=networks))
    if policy.get('enabled'):
        from crange_scheduling import purge_expired_guest_reviews, DATA
        from crange_placement import ReservationLedger
        ledger=ReservationLedger(DATA/'reservations.sqlite3')
        try:
            retired=purge_expired_guest_reviews(path,cfg,os.environ.get('CRANGE_ACTOR',f'cli:{os.getuid()}'),ProxmoxAPI(),ledger)
            for plan_id in retired:
                print(f'Released expired unused guest review {plan_id}; no guests or provider tasks found.',flush=True)
        finally:ledger.close()
        from crange_cache_jobs import prepare_cache, reconcile_unstarted_caches, resume_project_snapshot_caches
        for cache_job in reconcile_unstarted_caches(int(args.project)):
            print(f"Released pre-allocation cache reservation {cache_job}; no provider allocation was submitted.",flush=True)
        from crange_scheduling import declarations
        resume_project_snapshot_caches(int(args.project),{row['image'] for row in declarations(path,stage_cfg)})
        fills = required_preparations(path, stage_cfg)
        print(f'Cache preparation phase: {len(fills)} required local cache fill(s).', flush=True)
        revision = json.dumps(cfg, sort_keys=True)
        for fill in fills:
            if json.dumps(json_config(path), sort_keys=True) != revision:
                raise CrangeError('Declarations changed during cache preparation; stage again.')
            print(f'Preparing {fill["image"]} on {fill["node"]}/{fill["storage"]} for {", ".join(fill["resource_keys"])}', flush=True)
            result = prepare_cache(project_id=int(cfg['project_id']), image_id=fill['image'], node_name=fill['node'],
                          datastore_id=fill['storage'], additional_capacity=fill['additional_capacity'],
                          owner=os.environ.get('CRANGE_ACTOR', f'cli:{os.getuid()}'))
            print(f'Local cache verified: {fill["image"]} on {fill["node"]}/{fill["storage"]}, template {result.get("template_id", "verified")}.', flush=True)
        if json.dumps(json_config(path), sort_keys=True) != revision:
            raise CrangeError('Declarations changed during cache preparation; stage again.')
        print('Cache preparation verified. Generating the reviewed guest plan.', flush=True)
    else:
        print('Automatic distribution disabled; retaining a plan for legacy pinned resources.', flush=True)
    cmd_plan(argparse.Namespace(project=args.project, retain=True, replace_sdn_applier=False, staging=True))


def cmd_release_stage(args):
    """Prepare caches and review only the selected declared team releases."""
    from crange_rollouts import review_projection
    from crange_scheduling import read_settings, required_preparations
    path = project_dir(args.project)
    cfg = json_config(path)
    try:
        scoped = review_projection(cfg, args.teams)
    except ValueError as exc:
        raise CrangeError(str(exc)) from exc
    policy, _ = read_settings()
    if policy.get('enabled'):
        from crange_cache_jobs import prepare_cache, resume_project_snapshot_caches
        from crange_scheduling import declarations
        revision = json.dumps(cfg, sort_keys=True)
        resume_project_snapshot_caches(int(args.project), {row['image'] for row in declarations(path, scoped)})
        fills = required_preparations(path, scoped)
        print(f'Release cache preparation: {len(fills)} required local cache fill(s).', flush=True)
        for fill in fills:
            if json.dumps(json_config(path), sort_keys=True) != revision:
                raise CrangeError('Declarations changed during release preparation; review again.')
            print(f'Preparing {fill["image"]} on {fill["node"]}/{fill["storage"]}', flush=True)
            prepare_cache(project_id=int(args.project), image_id=fill['image'], node_name=fill['node'],
                          datastore_id=fill['storage'], additional_capacity=fill['additional_capacity'],
                          owner=os.environ.get('CRANGE_ACTOR', f'cli:{os.getuid()}'))
        if json.dumps(json_config(path), sort_keys=True) != revision:
            raise CrangeError('Declarations changed during release preparation; review again.')
    cmd_plan(argparse.Namespace(project=args.project, retain=True, replace_sdn_applier=False,
                                release_teams=list(map(str, args.teams))))


def cmd_plan(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    full_cfg = cfg
    release_teams = getattr(args, 'release_teams', None)
    if release_teams:
        from crange_rollouts import review_projection
        try:
            cfg = review_projection(cfg, release_teams)
        except ValueError as exc:
            raise CrangeError(str(exc)) from exc
    if getattr(args, 'staging', False):
        from crange_staging import staging_projection
        cfg = staging_projection(cfg)
    network_foundation = getattr(args, 'network_foundation', None)
    if network_foundation:
        cfg = {**cfg, 'jump_boxes': {}, 'scenario_boxes': {}, 'scenario_containers': {}}
    from crange_scheduling import prepare, tf_overrides, print_review, abandon_plan
    bootstrap_node = None
    bootstrap_nodes = None
    bootstrap_router_nodes = None
    bootstrap_networks = set()
    if getattr(args, 'bootstrap', False):
        cfg, bootstrap_node, bootstrap_nodes, bootstrap_networks = _bootstrap_project(path, cfg)
    plan_id = os.urandom(16).hex()
    owner = os.environ.get('CRANGE_ACTOR', f'cli:{os.getuid()}')
    from crange_scheduling import purge_expired_guest_reviews, DATA, read_settings
    if read_settings()[0].get('enabled'):
        from crange_placement import ReservationLedger
        ledger = ReservationLedger(DATA / 'reservations.sqlite3')
        try:
            for retired in purge_expired_guest_reviews(path, full_cfg, owner, ProxmoxAPI(), ledger):
                print(f'Released expired unused guest review {retired}; no guests or provider tasks found.', flush=True)
        finally:
            ledger.close()
    try:
        placement = prepare(path, cfg, plan_id, owner,
                            bootstrap_node=bootstrap_node, bootstrap_nodes=bootstrap_nodes,
                            **({'network_foundation': network_foundation} if network_foundation else {}))
        if bootstrap_node:
            bootstrap_router_nodes = (placement or {}).get('bootstrap_router_nodes', {})
            expected_router_keys = {'router:project','router:staging'} | {f'router:team:{team}' for team in cfg['team_ids']}
            if set(bootstrap_router_nodes) != expected_router_keys or not set(bootstrap_router_nodes.values()) <= set(bootstrap_nodes):
                raise CrangeError('Bootstrap reservations do not map each router to an accepted node.')
            cfg['bootstrap_router_nodes'] = bootstrap_router_nodes
    except __import__('crange_placement').PlacementError as exc:
        if 'local linked cache' in str(exc) or 'verified local image replica' in str(exc):
            from crange_scheduling import required_preparations
            try:
                requirements = required_preparations(path, cfg)
            except ValueError as capacity_error:
                raise CrangeError(f'{exc}; cache preparation cannot fit safely: {capacity_error}') from exc
            print('Required cache preparations: '+json.dumps(requirements,sort_keys=True))
            raise CrangeError(f'{exc}; run stage-changes {args.project} to prepare approved caches before review.') from exc
        raise

    print_review(placement)
    try:
        tf_run(path, ["init", "-input=false", "-upgrade=false"])
        with tempfile.TemporaryDirectory(prefix="crange-plan-") as tmp:
            plan = Path(tmp) / "review.tfplan"
            plan_args = ["plan", "-input=false", "-out", str(plan)]
            from crange_templates import media_tf_overrides
            overrides = Path(tmp) / 'placements.tfvars.json'
            overrides.write_text(json.dumps(media_tf_overrides(path, full_cfg if (getattr(args, 'staging', False) or release_teams) else cfg, placement, scoped=bool(getattr(args, 'staging', False) or release_teams))))
            plan_args += ['-var-file', str(overrides)]
            if network_foundation:
                from crange_network_foundation import targets
                plan_args += ['-target=' + target for target in targets(network_foundation)]
            elif release_teams:
                from crange_rollouts import review_targets
                plan_args += ['-target=' + target for target in review_targets(full_cfg, release_teams)]
            elif getattr(args, 'staging', False):
                from crange_staging import staging_targets, changed_staging_targets
                state = json.loads(tf_run(path, ['show', '-json'], capture=True).stdout)
                comparison = Path(tmp) / 'scope-comparison.tfplan'
                print('Selecting changed staging resources from local state; live provider review follows.', flush=True)
                tf_run(path, ['plan', '-input=false', '-refresh=false', '-out', str(comparison),
                              '-var-file', str(overrides),
                              *['-target=' + target for target in staging_targets(full_cfg, state)]])
                comparison_detail = json.loads(tf_run(path, ['show', '-json', str(comparison)], capture=True).stdout)
                try:
                    targets = changed_staging_targets(comparison_detail, full_cfg)
                except ValueError as exc:
                    raise CrangeError(str(exc)) from exc
                plan_args += ['-target=' + target for target in targets]
            if getattr(args, 'replace_sdn_applier', False):
                plan_args.append('-replace=proxmox_sdn_applier.apply')
            tf_run(path, plan_args)
            detail = json.loads(tf_run(path, ["show", "-json", str(plan)], capture=True).stdout)
            if network_foundation:
                from crange_network_foundation import validate_plan
                validate_plan(detail, full_cfg, network_foundation, placement)
            if release_teams:
                from crange_rollouts import plan_blockers
                release_blockers = plan_blockers(detail, full_cfg, release_teams)
                if release_blockers:
                    raise CrangeError('Release plan affects resources outside its selected releases: ' + ', '.join(release_blockers))
            changes = []
            for resource in detail.get("resource_changes", []):
                actions = resource.get("change", {}).get("actions", [])
                if actions != ["no-op"]:
                    changes.append((resource.get("address", "?"), actions))
            if bootstrap_node:
                _validate_bootstrap_plan(detail, int(args.project), bootstrap_node, bootstrap_networks, cfg)
            from crange_staging import staging_blockers
            blockers = staging_blockers(detail, json_config(path))
            if getattr(args, 'staging', False) and blockers:
                raise CrangeError('Staging plan would affect resources outside staging: ' + ', '.join(blockers))
            from crange_apply_scope import reviewed_distribution
            from crange_scheduling import applied_resources
            placement = reviewed_distribution(placement, detail, applied_resources(path))
            print("\nPlan review:")
            if not changes:
                print("No resource changes.")
            for address, actions in changes:
                print(f"  {address}: {' -> '.join(actions)}")
            if blockers and not release_teams:
                print("Staging deployment blocked: " + ", ".join(blockers))
            if any("delete" in actions and not (network_foundation and address == 'proxmox_sdn_applier.apply')
                   for address, actions in changes):
                print("This plan deletes or replaces resources; inspect the Terraform plan above before applying.")
            elif network_foundation:
                print('This foundation plan creates new routers and subnets; existing guests are unchanged.')
            if getattr(args, 'retain', False):
                directory = Path.home() / '.local/share/crange/plans' / f'project-{int(args.project):03d}'
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                destination = directory / f'{plan_id}.tfplan'
                with open(plan, 'rb') as source, open(destination, 'xb') as target:
                    while chunk := source.read(1024 * 1024):
                        target.write(chunk)
                destination.chmod(0o600)
                digest = hashlib.sha256(destination.read_bytes()).hexdigest()
                config = path / 'crange.auto.tfvars.json'
                inputs = hashlib.sha256(config.read_bytes() if config.exists() else (path / 'terraform.tfvars').read_bytes()).hexdigest()
                record = {'project': int(args.project), 'plan_id': plan_id, 'sha256': digest,
                          'inputs_sha256': inputs, 'automation_inputs_sha256': automation_inputs_digest(path), 'created': time.time(),
                          'changes': [{'address': address, 'actions': actions} for address, actions in changes],
                          'staging_blockers': blockers, 'distribution': placement,
                          'placements': placement['placements'] if placement else [],
                          'reservation_ids': placement['reservation_ids'] if placement else [],
                          'policy_revision': placement['policy_revision'] if placement else None}
                if placement:
                    record['apply_scope'] = placement['apply_scope']
                if release_teams:
                    record['release_teams'] = sorted(map(str, release_teams))
                if network_foundation:
                    record['network_foundation'] = list(network_foundation)
                if bootstrap_node:
                    record.update(bootstrap=True, bootstrap_node=bootstrap_node,
                                  bootstrap_nodes=bootstrap_nodes, bootstrap_router_nodes=bootstrap_router_nodes,
                                  bootstrap_networks=sorted(bootstrap_networks))
                manifest = directory / f'{plan_id}.json'
                fd = os.open(manifest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'w') as stream:
                    json.dump(record, stream, indent=2)
                from crange_scheduling import atomic_json
                project_plan_manifest = path / '.crange-plans' / f'{plan_id}.json'
                atomic_json(project_plan_manifest, {k:record[k] for k in ('plan_id','created','changes','placements','reservation_ids','policy_revision','apply_scope','bootstrap','bootstrap_node','bootstrap_nodes','bootstrap_router_nodes','bootstrap_networks','network_foundation','release_teams') if k in record},0o640)
                replaced = (placement or {}).get('replaced_reviewed_plan_ids', [])
                if replaced:
                    from crange_scheduling import DATA
                    from crange_placement import ReservationLedger
                    ledger = ReservationLedger(DATA / 'reservations.sqlite3')
                    try:
                        ledger.finalize_review_replacement(plan_id, replaced, int(args.project), owner)
                    except Exception:
                        destination.unlink(missing_ok=True)
                        manifest.unlink(missing_ok=True)
                        project_plan_manifest.unlink(missing_ok=True)
                        raise
                    finally:
                        ledger.close()
                print(f'Plan ID: {plan_id}')
                print(f'Inputs SHA-256: {inputs}')
                return plan_id
            abandon_plan(placement)
    except Exception:
        abandon_plan(placement)
        raise


def cmd_apply_saved(args: argparse.Namespace, path: Path) -> None:
    if not re.fullmatch(r'[0-9a-f]{32}', args.plan_id):
        raise CrangeError('Invalid saved plan ID.')
    directory = Path.home() / '.local/share/crange/plans' / f'project-{int(args.project):03d}'
    manifest = directory / f'{args.plan_id}.json'
    plan = directory / f'{args.plan_id}.tfplan'
    try:
        record = json.loads(manifest.read_text())
        plan_bytes = plan.read_bytes()
    except (OSError, json.JSONDecodeError) as exc:
        raise CrangeError(f'Saved plan is unavailable: {exc}') from exc
    if record.get('project') != int(args.project) or record.get('plan_id') != args.plan_id:
        raise CrangeError('Saved plan belongs to another project.')
    if time.time() - record.get('created', 0) > 3600:
        raise CrangeError('Saved plan expired; review a fresh plan.')
    if not hmac.compare_digest(hashlib.sha256(plan_bytes).hexdigest(), record.get('sha256', '')):
        raise CrangeError('Saved plan failed integrity verification.')
    config = path / 'crange.auto.tfvars.json'
    current = hashlib.sha256(config.read_bytes() if config.exists() else (path / 'terraform.tfvars').read_bytes()).hexdigest()
    if not hmac.compare_digest(current, record.get('inputs_sha256', '')):
        raise CrangeError('Project inputs changed since review; create a new plan.')
    automation_digest = record.get('automation_inputs_sha256')
    if not automation_digest:
        raise CrangeError('Saved plan predates template/source input verification; review a fresh plan.')
    if not hmac.compare_digest(automation_inputs_digest(path), automation_digest):
        raise CrangeError('Template approvals or Terraform source changed since review; create a new plan.')
    bootstrap_cfg = None
    if record.get('bootstrap'):
        bootstrap_cfg, bootstrap_node, bootstrap_nodes, bootstrap_networks = _bootstrap_project(path, json_config(path))
        if (bootstrap_node != record.get('bootstrap_node') or
                bootstrap_nodes != record.get('bootstrap_nodes') or
                sorted(bootstrap_networks) != record.get('bootstrap_networks')):
            raise CrangeError('Bootstrap node or project networks changed since review; create a new bootstrap plan.')
        bootstrap_router_nodes = record.get('bootstrap_router_nodes', {})
        expected_router_keys = {'router:project','router:staging'} | {f'router:team:{team}' for team in bootstrap_cfg['team_ids']}
        if set(bootstrap_router_nodes) != expected_router_keys or not set(bootstrap_router_nodes.values()) <= set(bootstrap_nodes):
            raise CrangeError('Saved bootstrap placements no longer match accepted nodes.')
        bootstrap_cfg['bootstrap_router_nodes'] = bootstrap_router_nodes
        detail = json.loads(tf_run(path, ['show', '-json', str(plan)], capture=True).stdout)
        _validate_bootstrap_plan(detail, int(args.project), bootstrap_node, bootstrap_networks, bootstrap_cfg)
    if record.get('network_foundation'):
        from crange_network_foundation import pending, validate_plan
        cfg = json_config(path)
        keys = record['network_foundation']
        from crange_scheduling import applied_resources
        if set(keys) != set(pending(cfg, applied_resources(path))):
            raise CrangeError('Declared custom-network foundation changed since review; stage again.')
        detail = json.loads(tf_run(path, ['show', '-json', str(plan)], capture=True).stdout)
        validate_plan(detail, cfg, keys, record['distribution'])
    if getattr(args, 'staging_only', False):
        detail = json.loads(tf_run(path, ['show', '-json', str(plan)], capture=True).stdout)
        enforce_staging_plan(detail, json_config(path))
    if getattr(args, 'release_teams', None):
        if record.get('release_teams') and set(record['release_teams']) != set(map(str, args.release_teams)):
            raise CrangeError('Reviewed release teams differ from the requested deployment teams.')
        from crange_rollouts import plan_blockers
        detail = json.loads(tf_run(path, ['show', '-json', str(plan)], capture=True).stdout)
        blockers = plan_blockers(detail, json_config(path), args.release_teams)
        if blockers:
            raise CrangeError('Release deployment cannot change staging, unselected teams or unowned resources: ' + ', '.join(blockers))
    changes = record.get('changes', [])
    guest_changes = [item for item in changes if not (record.get('network_foundation')
                                                    and item['address'] == 'proxmox_sdn_applier.apply')]
    destructive = [item['address'] for item in guest_changes if 'delete' in item.get('actions', [])]
    replacements = [item['address'] for item in guest_changes if 'delete' in item.get('actions', []) and 'create' in item.get('actions', [])]
    if destructive and not args.allow_destroy:
        raise CrangeError('Saved plan destroys resources: ' + ', '.join(destructive))
    if replacements and not args.allow_replace:
        raise CrangeError('Saved plan replaces resources: ' + ', '.join(replacements))
    from crange_scheduling import begin_apply, finish_apply, node_locks
    placement = record.get('distribution')
    if placement:
        if not placement.get('apply_scope'):
            raise CrangeError('Saved plan predates scoped apply verification; review a fresh plan.')
        from crange_apply_scope import reviewed_distribution
        from crange_scheduling import applied_resources
        detail = json.loads(tf_run(path, ['show', '-json', str(plan)], capture=True).stdout)
        placement = reviewed_distribution(placement, detail, applied_resources(path))
    nodes = {p['resolved']['node_name'] for p in (placement or {}).get('placements', [])}
    nodes.update(identity['node_name'] for identity in (placement or {}).get('apply_scope', {}).get('removed_guests', {}).values())
    retiring = []
    if getattr(args, 'release_teams', None):
        from crange_release_retire import retirements
        retiring = retirements(json_config(path), args.release_teams, detail)
        nodes.update(guest['node'] for guest in retiring)
    with node_locks(nodes):
        begin_apply(path, placement)
        if retiring:
            from crange_release_retire import quiesce
            quiesce(ProxmoxAPI(), retiring,
                    path / '.crange-rollout-quiesce' / f'{args.plan_id}.json', args.plan_id)
        tf_run(path, ['apply', '-parallelism=1', '-input=false', str(plan)])
        finish_apply(path, placement, succeeded=True)
    record_release_apply(path)
    plan.unlink()
    record['applied_at'] = datetime.now(timezone.utc).isoformat()
    from crange_apply_scope import guest_key
    args.applied_guest_keys = sorted({key for item in record.get('changes', [])
                                     if (key := guest_key(item['address'])) is not None})
    manifest.write_text(json.dumps(record, indent=2) + '\n')
    from crange_scheduling import atomic_json
    atomic_json(path / '.crange-plans' / f'{args.plan_id}.json', {k:record[k] for k in ('plan_id','created','changes','placements','reservation_ids','policy_revision','apply_scope','applied_at','bootstrap','bootstrap_node','bootstrap_nodes','bootstrap_router_nodes','bootstrap_networks','network_foundation') if k in record},0o640)
    if record.get('network_foundation'):
        args.network_foundation_applied = True
        cmd_configure_network_foundation(args, record['network_foundation'])
        record['foundation_configured_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(manifest, record, 0o600)
        summary_path = path / '.crange-plans' / f'{args.plan_id}.json'
        summary = json.loads(summary_path.read_text())
        summary['foundation_configured_at'] = record['foundation_configured_at']
        atomic_json(summary_path, summary, 0o640)
    if record.get('bootstrap'):
        status = {'schema_version': 1, 'project_id': int(args.project), 'state': 'awaiting_network_acceptance',
                  'bootstrap_plan_id': args.plan_id, 'node': record['bootstrap_node'],
                  'nodes': record.get('bootstrap_nodes', []),
                  'router_nodes': record.get('bootstrap_router_nodes', {}),
                  'networks': record['bootstrap_networks'], 'applied_at': record['applied_at'],
                  'acceptance_recorded': False}
        status['state'] = 'configuration_pending'
        atomic_json(path / '.crange-bootstrap.json', status, 0o640)
        try:
            cmd_configure(argparse.Namespace(project=args.project, target='all', yes=True))
        except Exception:
            status['state'] = 'configuration_failed'
            atomic_json(path / '.crange-bootstrap.json', status, 0o640)
            raise
        status['state'] = 'awaiting_network_acceptance'
        status['configured_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(path / '.crange-bootstrap.json', status, 0o640)
        print('Router/network foundation deployed and configured. Exact per-VNet network acceptance is required before scenario placement or release.')


def cmd_bootstrap_plan(args: argparse.Namespace) -> None:
    args.bootstrap = True
    args.retain = True
    args.replace_sdn_applier = False
    cmd_plan(args)


def cmd_bootstrap_configure(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    status_path = path / '.crange-bootstrap.json'
    try:
        status = json.loads(status_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CrangeError('Bootstrap configuration record is unavailable.') from exc
    if status.get('project_id') != int(args.project) or status.get('state') not in ('configuration_failed', 'configuration_pending', 'awaiting_network_acceptance'):
        raise CrangeError('Project is not at the router bootstrap configuration step.')
    if not (path / 'terraform.tfstate').is_file():
        raise CrangeError('Router bootstrap state is missing; do not configure unreviewed resources.')
    status['state'] = 'configuration_pending'
    from crange_scheduling import atomic_json
    atomic_json(status_path, status, 0o640)
    try:
        cmd_configure(argparse.Namespace(project=args.project, target='all', yes=True))
    except Exception:
        status['state'] = 'configuration_failed'
        atomic_json(status_path, status, 0o640)
        raise
    status['state'] = 'awaiting_network_acceptance'
    status['configured_at'] = datetime.now(timezone.utc).isoformat()
    atomic_json(status_path, status, 0o640)
    print('Routers configured. Exact per-VNet network acceptance is required before scenario placement or release.')


def cmd_bootstrap_apply(args: argparse.Namespace) -> None:
    args.yes = True
    args.allow_destroy = False
    args.allow_replace = False
    cmd_apply_saved(args, project_dir(args.project))


def cmd_apply(args: argparse.Namespace) -> None:
    if not args.yes:
        raise CrangeError("Applying changes requires --yes after reviewing the plan.")
    path = project_dir(args.project)
    json_config(path)
    if getattr(args, 'plan_id', None):
        return cmd_apply_saved(args, path)
    from crange_scheduling import read_settings
    if read_settings()[0].get('enabled'):
        args.retain = True
        args.plan_id = cmd_plan(args)
        return cmd_apply_saved(args, path)
    from crange_scheduling import prepare
    prepare(path, json_config(path), os.urandom(16).hex(), os.environ.get('CRANGE_ACTOR', f'cli:{os.getuid()}'))
    tf_run(path, ["init", "-input=false", "-upgrade=false"])
    with tempfile.TemporaryDirectory(prefix="crange-apply-") as tmp:
        plan = Path(tmp) / "review.tfplan"
        from crange_templates import media_tf_overrides
        overrides = Path(tmp) / 'media.tfvars.json'
        overrides.write_text(json.dumps(media_tf_overrides(path, json_config(path))))
        tf_run(path, ["plan", "-input=false", "-out", str(plan), "-var-file", str(overrides)])
        detail = json.loads(tf_run(path, ["show", "-json", str(plan)], capture=True).stdout)
        if getattr(args, 'staging_only', False):
            enforce_staging_plan(detail, json_config(path))
        destructive = []
        replacements = []
        for resource in detail.get("resource_changes", []):
            actions = resource.get("change", {}).get("actions", [])
            if "delete" in actions:
                destructive.append(resource.get("address", "?"))
                if len(actions) > 1:
                    replacements.append(resource.get("address", "?"))
        if destructive and not args.allow_destroy:
            raise CrangeError("Plan removes/replaces " + ", ".join(destructive) + ". Re-run only after review with --allow-destroy.")
        if replacements and not args.allow_replace:
            raise CrangeError("Plan replaces " + ", ".join(replacements) + ". Re-run only after review with --allow-replace.")
        tf_run(path, ["apply", "-input=false", str(plan)])
        record_release_apply(path)


def cmd_route_sync(args):
    """Ensure only the applied project's OPNsense aggregate route and gateway."""
    path = project_dir(args.project)
    cfg = json_config(path)
    project = int(cfg['project_id'])
    spec = output_json(path).get('opnsense_route', {}).get('value', {})
    expected = {'project_id': project, 'network': f'10.{project}.0.0/16',
                'gateway_name': f'P{project:03d}', 'gateway_address': cfg['transit_address'].split('/')[0],
                'interface': cfg.get('opnsense_transit_interface', 'opt2')}
    if spec != expected or project != int(args.project):
        raise CrangeError('Applied project route does not match the approved project topology.')
    helper = ROOT / 'scripts/opnsense-project-routes.py'
    command = ['bash', '-lc', 'set -a; . /home/crange/.config/crange/opnsense-api.txt; set +a; ' +
               'export OPNSENSE_INSECURE=1; exec ' + shlex.join([sys.executable, str(helper), 'ensure'])]
    try:
        result = subprocess.run(command, input=json.dumps(spec), cwd=ROOT, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CrangeError(f'Project route synchronization failed: {exc}') from exc
    if result.returncode:
        lines = result.stderr.strip().splitlines()
        reason = lines[-1] if lines else 'route helper failed'
        raise CrangeError('Project route synchronization failed: ' + reason)
    print(result.stdout.strip())


def cmd_mhl_business_configure(args: argparse.Namespace) -> None:
    if not args.yes:
        raise CrangeError("MHL business configuration applies the fixed Project 100 service roles; pass --yes after reviewing their definitions.")
    if int(args.project) != 100:
        raise CrangeError("MHL business services are only available for Project 100.")
    from crange_mhl_services import configure, ServiceConfigurationError
    try:
        cmd_inventory(args)
        configure(project_dir(args.project), args.network_id, recipe_sha256=getattr(args, "recipe_sha256", None))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise CrangeError(str(exc)) from exc


def cmd_mhl_windows_configure(args: argparse.Namespace) -> None:
    if not args.yes:
        raise CrangeError("MHL Windows service configuration applies the fixed Project 100 domain and file-server roles; pass --yes after reviewing the playbooks.")
    if int(args.project) != 100:
        raise CrangeError("MHL Windows services are only available for Project 100.")
    from crange_mhl_services import configure_windows, ServiceConfigurationError
    try:
        cmd_inventory(args)
        configure_windows(project_dir(args.project), args.network_id, recipe_sha256=getattr(args, "recipe_sha256", None))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise CrangeError(str(exc)) from exc


def _windows_base_action(args: argparse.Namespace, action: str) -> None:
    import crange_windows_base
    try:
        result = (crange_windows_base.prepare(args.build_id) if action == "prepare"
                  else crange_windows_base.seal(args.build_id))
    except crange_windows_base.WindowsBaseError as exc:
        raise CrangeError(str(exc)) from exc
    print(result.get("result", "Windows base action completed; clone identity proof remains separate."))


def cmd_template_windows_base_prepare(args: argparse.Namespace) -> None:
    _windows_base_action(args, "prepare")


def cmd_template_windows_base_seal(args: argparse.Namespace) -> None:
    _windows_base_action(args, "seal")


def cmd_configure_team(args: argparse.Namespace) -> None:
    """Configure only one team's routers and jump boxes, plus shared return routes."""
    if not args.yes:
        raise CrangeError("Team configuration applies Ansible changes; pass --yes after reviewing the playbooks.")
    path = project_dir(args.project)
    cfg = json_config(path)
    if isinstance(args.team, bool) or not 0 <= int(args.team) <= 253:
        raise CrangeError("Choose staging (0) or an ordinary team from 1 to 253.")
    team = 254 if int(args.team) == 0 else int(args.team)
    if team != 254 and str(team) not in {str(item) for item in cfg.get("team_ids", [])}:
        raise CrangeError(f"Team {team} is not configured.")
    from crange_ssh_identity import refresh_created_guests
    resource_keys = getattr(args, 'guest_keys', None)
    refreshed = refresh_created_guests(path, team, **({'resource_keys': resource_keys} if resource_keys is not None else {}))
    if refreshed:
        print("Refreshed SSH identities for newly applied guests: " + ", ".join(refreshed), flush=True)
    inventory_path = ROOT / "ansible" / "inventory" / "generated" / f"project-{int(args.project):03d}.json"
    run([sys.executable, str(ROOT / "scripts" / "generate-project-inventory.py"), "--terraform-dir", str(path), "--output", str(inventory_path)])
    try:
        inventory = json.loads(inventory_path.read_text())
        groups = inventory["all"]["children"]
        router_groups = groups["routers"]["children"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CrangeError(f"Cannot read generated project inventory: {exc}") from exc
    if resource_keys is not None:
        from crange_configure_scope import configuration_inventory
        try:
            inventory, needs_path = configuration_inventory(inventory, cfg, team, resource_keys)
        except ValueError as exc:
            raise CrangeError(str(exc)) from exc
        if not needs_path:
            print('No surviving guest configuration changes in the reviewed plan.', flush=True)
            return
        with tempfile.TemporaryDirectory(prefix='crange-configure-scope-') as directory:
            private = Path(directory) / 'inventory.json'
            private.write_text(json.dumps(inventory))
            private.chmod(0o600)
            scoped_args = argparse.Namespace(**{**vars(args), 'inventory_path': private})
            cmd_route_sync(scoped_args)
            _configure_team_inventory(scoped_args, cfg, team, inventory['all']['children'])
        return
    cmd_route_sync(args)
    _configure_team_inventory(args, cfg, team, groups)


def _configure_team_inventory(args, cfg, team, groups):
    router_groups = groups['routers']['children']
    # The project router has the shared return routes; avoid touching other team routers.
    for host in router_groups.get("project_routers", {}).get("hosts", {}):
        ansible(args, "router-routes.yml", [host])
    selected_group = "staging_routers" if team == 254 else "team_routers"
    selected_hosts = router_groups.get(selected_group, {}).get("hosts", {})
    router_host = f"p{int(args.project):03d}t{team:03d}_router"
    if router_host in selected_hosts:
        ansible(args, "router-routes.yml", [router_host])
        ansible(args, "routers.yml", [router_host])
    # Custom routers and jump boxes are filtered by their owning team/network.
    for host, values in router_groups.get("custom_routers", {}).get("hosts", {}).items():
        if int(values.get("router_owner_team_id", -1)) == team:
            ansible(args, "routers.yml", [host])
    jump_group = "staging_jumpboxes" if team == 254 else f"team_{team:03d}_jumpboxes"
    ansible(args, "jumpboxes.yml", [jump_group])
    container_group = "staging_containers" if team == 254 else f"team_{team:03d}_containers"
    ansible(args, "containers.yml", [container_group])
    vm_group = "staging_vms" if team == 254 else f"team_{team:03d}_vms"
    for group in (jump_group, container_group, vm_group):
        ansible(args, "root-access.yml", [group])
    ansible(args, "vm-swap.yml", [vm_group])
    if any(str(cfg.get("scenario_boxes", {}).get(values.get("crange_box_key"), {}).get("team_id")) == str(team)
           for values in groups.get("windows_guests", {}).get("hosts", {}).values()):
        cmd_configure(argparse.Namespace(project=args.project, target="windows", yes=True, team_scope=team,
                                        **({'inventory_path': args.inventory_path} if getattr(args, 'inventory_path', None) else {})))


def enforce_staging_plan(detail: dict, cfg: dict) -> None:
    from crange_staging import staging_blockers
    blockers = staging_blockers(detail, cfg)
    if blockers:
        raise CrangeError('Staging deployment cannot change team or shared project resources: ' +
                          ', '.join(blockers) + '. Review these changes in Project Overview.')


def cmd_stage_deploy(args: argparse.Namespace) -> None:
    """Apply one reviewed staging plan, then configure the complete range path."""
    print("Phase 1/2: applying the reviewed infrastructure plan", flush=True)
    args.staging_only = True
    cmd_apply(args)
    if getattr(args, 'network_foundation_applied', False):
        print('Custom network foundation deployed and configured. Verify the new LAN before staging scenario guests.', flush=True)
        return
    print("Infrastructure applied. Phase 2/2: configuring routes, routers, and jump boxes", flush=True)
    try:
        cmd_configure_team(argparse.Namespace(project=args.project, team=0, yes=True,
                                             **({'guest_keys': args.applied_guest_keys} if hasattr(args, 'applied_guest_keys') else {})))
    except CrangeError as exc:
        raise CrangeError(f"Infrastructure deployment succeeded, but post-deploy configuration failed: {exc}") from exc
    print("Staging deployment and configuration completed.", flush=True)


def cmd_configure_network_foundation(args, keys):
    """Only parent routes/firewall and exact new custom routers; no guest roles."""
    path = project_dir(args.project)
    cfg = json_config(path)
    if not keys or any(str(cfg['custom_networks'][key]['owner_team_id']) != '254' for key in keys):
        raise CrangeError('Only declared staging custom networks can use this foundation workflow.')
    from crange_scheduling import atomic_json
    status_path = path / '.crange-network-foundation.json'
    status = {'project_id': int(args.project), 'networks': keys, 'state': 'configuration_pending',
              'plan_id': getattr(args, 'plan_id', None)}
    atomic_json(status_path, status, 0o640)
    try:
        from crange_ssh_identity import refresh_created_guests
        refresh_created_guests(path, 254)
        cmd_inventory(args)
        inventory_path = ROOT / 'ansible/inventory/generated' / f'project-{int(args.project):03d}.json'
        inventory = json.loads(inventory_path.read_text())
        routers = inventory['all']['children']['routers']['children']
        for host in routers.get('project_routers', {}).get('hosts', {}):
            ansible(args, 'router-routes.yml', [host])
        parent = f'p{int(args.project):03d}t254_router'
        if parent not in routers.get('staging_routers', {}).get('hosts', {}):
            raise CrangeError('Applied staging parent router is missing from inventory.')
        ansible(args, 'routers.yml', [parent])
        custom = routers.get('custom_routers', {}).get('hosts', {})
        for key in keys:
            number = cfg['custom_networks'][key]['network_id']
            host = f'p{int(args.project):03d}c{number:03d}_router'
            if host not in custom or int(custom[host].get('router_owner_team_id', -1)) != 254:
                raise CrangeError('Applied custom router is missing or has the wrong owner.')
            ansible(args, 'routers.yml', [host])
        status['state'] = 'awaiting_network_acceptance'
        status['configured_at'] = datetime.now(timezone.utc).isoformat()
    except Exception:
        status['state'] = 'configuration_failed'
        atomic_json(status_path, status, 0o640)
        raise
    atomic_json(status_path, status, 0o640)


def cmd_inventory(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    output = ROOT / "ansible" / "inventory" / "generated" / f"project-{int(args.project):03d}.json"
    run([sys.executable, str(ROOT / "scripts" / "generate-project-inventory.py"), "--terraform-dir", str(path), "--output", str(output)])


def ansible(args: argparse.Namespace, playbook: str, limit: list[str], *, tags: list[str] | None = None) -> None:
    path = project_dir(args.project)
    output = getattr(args, 'inventory_path', None) or ROOT / "ansible" / "inventory" / "generated" / f"project-{int(args.project):03d}.json"
    if not output.exists():
        raise CrangeError(f"Inventory is missing. Run: crange inventory {args.project}")
    try:
        inventory = json.loads(output.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CrangeError(f"Cannot read generated inventory: {exc}") from exc
    for group in limit:
        def find_group(node):
            if isinstance(node, dict):
                if group in node:
                    return node[group]
                for value in node.values():
                    found = find_group(value)
                    if found is not None:
                        return found
            return None
        entry = find_group(inventory) or {}
        members = entry.get("hosts", {}) if isinstance(entry, dict) else {}
        if not members:
            def contains_host(node):
                if isinstance(node, dict):
                    return group in node.get("hosts", {}) or any(contains_host(value) for value in node.values())
                if isinstance(node, list):
                    return any(contains_host(value) for value in node)
                return False
            if not contains_host(inventory):
                print(f"Skipping {playbook} for empty inventory group {group}.")
                return
    command = ["ansible-playbook", "-f", "1", "-i", str(output), str(ROOT / "ansible" / playbook)]
    if tags:
        command += ['--tags', ','.join(tags)]
    for group in limit:
        command += ["--limit", group]
    # The controller only has the C locale generated; inheriting a host locale
    # such as en_US.UTF-8 makes Ansible abort before it can reach the routers.
    run(command, cwd=ROOT, env={**os.environ, "ANSIBLE_TIMEOUT": "90",
                               "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"})


def _windows_configure_credentials(project_id: int, inventory: dict, path: Path) -> dict:
    """Resolve worker-private credentials only for exact applied guest/image identities."""
    try:
        all_group = inventory["all"]
        children = all_group["children"]
        windows = children.get("windows_guests", {})
        all_vars = all_group.get("vars", {})
        if not isinstance(windows, dict):
            raise TypeError("windows_guests must be an object")
        hosts = windows.get("hosts", {})
    except (KeyError, TypeError) as exc:
        raise CrangeError("Generated inventory has an invalid windows_guests group.") from exc
    windows_vars = windows.get("vars", {})
    if not isinstance(hosts, dict) or not isinstance(all_vars, dict) or not isinstance(windows_vars, dict):
        raise CrangeError("Generated Windows inventory has invalid host or group variables.")

    def contains_password(value):
        if isinstance(value, dict):
            return "ansible_password" in value or any(contains_password(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_password(item) for item in value)
        return False

    if "ansible_password" in all_vars or contains_password(windows):
        raise CrangeError("Windows passwords must come only from the worker-private guest credential store.")
    if not hosts:
        return {}

    applied_path = path / "crange-applied-placements.json"
    try:
        applied = json.loads(applied_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CrangeError("Windows configuration requires crange-applied-placements.json with actual guest/image identities.") from exc
    if not isinstance(applied, dict):
        raise CrangeError("Windows applied-placement record is malformed.")

    try:
        from crange_windows_guest_admin import reference, resolve
    except ImportError as exc:
        raise CrangeError("Worker-private Windows credential support is unavailable.") from exc
    credentials = {}
    for host, values in sorted(hosts.items()):
        key_prefix = f"p{project_id:03d}_vm_"
        box_key = values.get("crange_box_key")
        if (not isinstance(box_key, str) or host != key_prefix + box_key
                or values.get("crange_project_id") != project_id):
            raise CrangeError(f"Windows inventory host {host} lacks a matching project/box identity.")
        guest_id = values.get("crange_guest_id")
        image_id = values.get("crange_image_id")
        template = values.get("crange_template")
        template_id = values.get("crange_template_id")
        template_node = values.get("crange_template_node")
        network_id = values.get("crange_guest_network_id")
        try:
            network_id_int = int(network_id)
            guest_address = ipaddress.ip_address(values.get("ansible_host", ""))
            guest_network = ipaddress.ip_network(f"10.{project_id}.{network_id_int}.0/24")
            address_in_scope = guest_address in guest_network
        except (TypeError, ValueError):
            network_id_int = 0
            address_in_scope = False
        if (isinstance(guest_id, bool) or not isinstance(guest_id, int)
                or not isinstance(image_id, str) or not image_id
                or not isinstance(template, str) or not template
                or isinstance(template_id, bool) or not isinstance(template_id, int)
                or not isinstance(template_node, str) or not template_node
                or isinstance(network_id, bool) or not 1 <= network_id_int <= 254
                or not address_in_scope):
            raise CrangeError(f"Windows inventory host {host} lacks approved image or deployed guest metadata.")
        placement = applied.get(f"vm:{box_key}")
        if (not isinstance(placement, dict) or placement.get("guest_id") != guest_id
                or placement.get("image_id") != image_id
                or placement.get("declared_template") != template
                or placement.get("template_id") != template_id
                or placement.get("template_node") != template_node):
            raise CrangeError(f"Applied placement for Windows guest {box_key} does not match generated guest/image identity.")
        try:
            credentials[host] = resolve(
                reference(project_id, box_key), project_id=project_id,
                box_key=box_key, guest_id=guest_id, image_id=image_id,
            )
        except (OSError, ValueError) as exc:
            raise CrangeError(
                f"No worker-private Windows credential is bound to project {project_id}, guest {box_key}, "
                f"guest ID {guest_id}, and approved image {image_id}."
            ) from None
    return credentials


def _run_windows_management_routes(args, inventory, scope=None):
    groups = inventory['all']['children'].get('routers', {}).get('children', {})
    for group_name in ('project_routers', 'team_routers', 'staging_routers', 'custom_routers'):
        for host, values in groups.get(group_name, {}).get('hosts', {}).items():
            if scope is not None and group_name != 'project_routers':
                if group_name == 'custom_routers':
                    if str(values.get('router_owner_team_id')) != str(scope):
                        continue
                elif host != f'p{int(args.project):03d}t{int(scope):03d}_router':
                    continue
            ansible(args, 'windows-routes.yml', [host])


def _run_windows_guest_baseline(inventory: dict, credentials: dict) -> None:
    """Run only the fixed Windows access baseline with a temporary private inventory."""
    if not credentials:
        return
    windows_hosts = inventory["all"]["children"]["windows_guests"]["hosts"]
    for host, credential in credentials.items():
        windows_hosts[host]["ansible_user"] = credential["username"]
        windows_hosts[host]["ansible_password"] = credential["password"]
    with tempfile.TemporaryDirectory(prefix="crange-windows-") as temporary:
        os.chmod(temporary, 0o700)
        private_inventory = Path(temporary) / "windows-inventory.json"
        descriptor = os.open(private_inventory, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(inventory, indent=2) + "\n")
        command = [
            "ansible-playbook", "-f", "1", "-i", str(private_inventory),
            str(ROOT / "ansible/windows/crange-guests.yml"),
            "--limit", "windows_guests",
        ]
        run(command, cwd=ROOT, env={**os.environ, "ANSIBLE_TIMEOUT": "90",
                                    "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"})


def cmd_configure(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    if not args.yes:
        raise CrangeError("Configuration applies Ansible changes; pass --yes after reviewing the playbook.")
    output = getattr(args, 'inventory_path', None) or ROOT / "ansible" / "inventory" / "generated" / f"project-{int(args.project):03d}.json"
    if not getattr(args, 'inventory_path', None):
        run([sys.executable, str(ROOT / "scripts" / "generate-project-inventory.py"), "--terraform-dir", str(path), "--output", str(output)])
    windows_inventory = None
    windows_credentials = {}
    windows_context = {}
    if args.target in ("boxes", "all", "windows"):
        try:
            windows_inventory = json.loads(output.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CrangeError(f"Cannot inspect generated Windows inventory: {exc}") from exc
        scope = getattr(args, 'team_scope', None)
        if scope is not None:
            import copy
            windows_inventory = copy.deepcopy(windows_inventory)
            cfg = json_config(path)
            group = windows_inventory['all']['children'].get('windows_guests', {})
            group['hosts'] = {host: values for host, values in group.get('hosts', {}).items()
                              if str(cfg.get('scenario_boxes', {}).get(values.get('crange_box_key'), {}).get('team_id')) == str(scope)}
            group['children'] = {}
            if not group['hosts']:
                print('Skipping Windows baseline: no applied Windows guests in this team.', flush=True)
                if args.target == 'windows':
                    return
        # First contact for a published Windows clone is a fixed, ownership-
        # checked QGA initializer. It creates the worker-private binding before
        # the WinRM credentials preflight; existing bindings are never rotated.
        windows_hosts = windows_inventory.get("all", {}).get("children", {}).get("windows_guests", {}).get("hosts", {})
        windows_keys = []
        for guest_values in windows_hosts.values():
            guest_key = guest_values.get("crange_box_key") if isinstance(guest_values, dict) else None
            if isinstance(guest_key, str):
                windows_keys.append(guest_key)
        if args.target == "windows" and not windows_keys:
            raise CrangeError("No declared Windows guests are available in this project; add Windows guests and deploy the reviewed scenario release first.")
        if windows_keys:
            applied_path = path / "crange-applied-placements.json"
            try:
                applied = json.loads(applied_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise CrangeError("Windows configuration requires crange-applied-placements.json with actual guest/image identities.") from exc
            if not isinstance(applied, dict):
                raise CrangeError("Windows applied-placement record is malformed.")
            for guest_key in set(windows_keys):
                placement = applied.get(f"vm:{guest_key}")
                guest_values = next((values for values in windows_hosts.values()
                                     if isinstance(values, dict) and values.get("crange_box_key") == guest_key), {})
                if (not isinstance(placement, dict)
                        or placement.get("guest_id") != guest_values.get("crange_guest_id")
                        or placement.get("image_id") != guest_values.get("crange_image_id")
                        or placement.get("declared_template") != guest_values.get("crange_template")
                        or placement.get("template_id") != guest_values.get("crange_template_id")
                        or placement.get("template_node") != guest_values.get("crange_template_node")):
                    raise CrangeError(f"Applied placement for Windows guest {guest_key} does not match generated guest/image identity.")
            from crange_windows_guest_initialize import initialize
            windows_api = ProxmoxAPI()
            for guest_key in sorted(set(windows_keys)):
                try:
                    windows_context[guest_key] = initialize(windows_api, int(args.project), guest_key)
                except Exception as exc:
                    # Provider errors may include user-controlled response data;
                    # expose only the fixed exception type/message here.
                    detail = ("The Proxmox worker token lacks the required QEMU Guest Agent execution capability; "
                              "grant only the reviewed guest-agent permissions for this VM scope. No ACL was changed."
                              if "HTTP 403" in str(exc) else str(exc))
                    raise CrangeError(f"Windows initialization failed for {guest_key}: {detail}") from None
        windows_credentials = _windows_configure_credentials(int(args.project), windows_inventory, path)
        for host,credential in windows_credentials.items():
            key=windows_hosts[host]['crange_box_key']
            if windows_context.get(key,{}).get('admin_login'):
                credential['username']=windows_context[key]['admin_login']
    if args.target == "windows":
        _run_windows_management_routes(args, windows_inventory, getattr(args, "team_scope", None))
        _run_windows_guest_baseline(windows_inventory, windows_credentials)
        return
    cmd_route_sync(args)
    if args.target in ("routes", "all"):
        ansible(args, "router-routes.yml", ["project_routers"])
        ansible(args, "router-routes.yml", ["team_routers"])
        ansible(args, "router-routes.yml", ["staging_routers"])
    if args.target in ("routers", "all"):
        # Project first, then team/staging routers: team SSH is proxied via project router.
        ansible(args, "routers.yml", ["project_routers"])
        ansible(args, "routers.yml", ["team_routers"])
        ansible(args, "routers.yml", ["staging_routers"])
        ansible(args, "routers.yml", ["custom_routers"])
    if args.target in ("boxes", "all"):
        ansible(args, "jumpboxes.yml", ["jumpboxes"])
        ansible(args, "containers.yml", ["scenario_containers"])
        for group in ("jumpboxes", "scenario_containers", "scenario_vms"):
            ansible(args, "root-access.yml", [group])
        ansible(args, "vm-swap.yml", ["scenario_vms"])
        _run_windows_management_routes(args, windows_inventory)
        _run_windows_guest_baseline(windows_inventory, windows_credentials)



class ProxmoxAPI:
    """Fixed Proxmox release-image operations using the controller's scoped API token."""

    def __init__(self) -> None:
        values = {}
        source = Path("/home/crange/.config/crange/proxmox.env")
        try:
            for line in source.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:]
                key, sep, raw = line.partition("=")
                if sep and key in ("PROXMOX_VE_ENDPOINT", "PROXMOX_VE_API_TOKEN"):
                    parsed = shlex.split(raw, comments=False)
                    if len(parsed) != 1:
                        raise CrangeError(f"Invalid {key} in Proxmox credential file.")
                    values[key] = parsed[0]
        except OSError as exc:
            raise CrangeError(f"Cannot read Proxmox credential file: {exc}") from exc
        if not all(values.get(key) for key in ("PROXMOX_VE_ENDPOINT", "PROXMOX_VE_API_TOKEN")):
            raise CrangeError("Proxmox endpoint and token are required in the controller credential file.")
        self.endpoint = values["PROXMOX_VE_ENDPOINT"].rstrip("/")
        if not self.endpoint.endswith("/api2/json"):
            self.endpoint += "/api2/json"
        self.compat_enabled = False
        self.compat_upstream = self.endpoint
        compat_path = Path('/home/crange/.config/crange/proxmox-compat.json')
        if compat_path.exists():
            import pwd
            from crange_proxmox_compat import load_config
            settings = load_config(compat_path, owner_uid=pwd.getpwnam('crange').pw_uid)
            self.endpoint = f'https://127.0.0.1:{settings["port"]}/api2/json'
            self.compat_enabled = True
            self.compat_upstream = settings.get('upstream', self.compat_upstream)
        self.token = values["PROXMOX_VE_API_TOKEN"]
        self.ssl_context = ssl._create_unverified_context() if os.environ.get("PROXMOX_VE_INSECURE", "true").lower() == "true" else ssl.create_default_context()

    def websocket_endpoint(self):
        """Use the verified real gateway for WebSockets, outside the HTTP adapter."""
        if not self.compat_enabled:
            return self.endpoint
        self.request('GET', '/cluster/status')
        url = self.endpoint.removesuffix('/api2/json') + '/__crange_compat/status'
        with urllib.request.urlopen(url, context=self.ssl_context, timeout=5) as response:
            state = json.loads(response.read(8192))
        active = urllib.parse.urlsplit(state.get('active_upstream', ''))
        configured = urllib.parse.urlsplit(self.compat_upstream)
        allowed = {(configured.scheme, configured.netloc)}
        if configured.hostname in ('10.0.0.101', '10.0.0.104', '10.0.0.106') and configured.port == 8006:
            allowed.update(('https', f'10.0.0.{number}:8006') for number in (101, 104, 106))
        if (not state.get('recently_verified') or (active.scheme, active.netloc) not in allowed
                or active.username or active.password or active.query or active.fragment
                or active.path not in ('', '/', '/api2/json', '/api2/json/')):
            raise CrangeError('Verified cluster console gateway is unavailable.')
        return urllib.parse.urlunsplit((active.scheme, active.netloc, '/api2/json', '', ''))

    def request(self, method: str, path: str, fields: dict | None = None) -> object:
        url = self.endpoint + path
        encoded = urllib.parse.urlencode(fields, doseq=True) if fields is not None else None
        if method in ('GET', 'DELETE'):
            if encoded:
                url += ('&' if '?' in url else '?') + encoded
            data = None
        else:
            data = encoded.encode() if encoded is not None else None
        req = urllib.request.Request(url, data=data, method=method,
            headers={"Authorization": "PVEAPIToken=" + self.token,
                     "Content-Type": "application/x-www-form-urlencoded"})
        attempts = getattr(self, 'read_attempts', 3) if method == 'GET' else 1
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(req, context=self.ssl_context, timeout=getattr(self, 'request_timeout', 90)) as response:
                    body = json.load(response)
                break
            except urllib.error.HTTPError as exc:
                # PVE may briefly return gateway 595/596 while a node API
                # request is still healthy. Retry only idempotent reads; never
                # replay a create, restore, task start, or other mutation.
                if method == 'GET' and exc.code in (595, 596) and attempt + 1 < attempts:
                    time.sleep(1 << attempt)
                    continue
                raise CrangeError(f"Proxmox API {method} {path} failed with HTTP {exc.code}.") from exc
            except (OSError, ValueError) as exc:
                raise CrangeError(f"Proxmox API {method} {path} failed: {type(exc).__name__}.") from exc
        if not isinstance(body, dict) or "data" not in body:
            raise CrangeError(f"Proxmox API {method} {path} returned no data.")
        return body["data"]

    def task(self, node: str, upid: str, timeout: int = 900) -> None:
        if not isinstance(upid, str) or not upid.startswith('UPID:'):
            raise CrangeError(f"Proxmox on {node} returned an invalid task ID.")
        deadline = time.monotonic() + timeout
        path = f"/nodes/{node}/tasks/{urllib.parse.quote(upid, safe='')}/status"
        next_progress, last_progress = 0, None
        while time.monotonic() < deadline:
            try:
                result = self.request("GET", path)
            except CrangeError as error:
                if getattr(error.__cause__, 'code', None) not in (595, 596):
                    raise
                # The provider task remains authoritative. Resume observing the
                # same handle within its deadline; never replay its mutation.
                time.sleep(2)
                continue
            if result.get("status") == "stopped":
                if result.get("exitstatus") != "OK":
                    # PVE starts e1000 guests successfully but warns that its
                    # host-side MTU option only applies to VirtIO. Accept only
                    # this observed warning, on a start task, after checking
                    # the complete bounded log. Other warnings remain errors.
                    if (re.fullmatch(r"WARNINGS: [1-9][0-9]*", str(result.get('exitstatus', ''))) and
                            (':qmstart:' in upid or ':vzdump:' in upid)):
                        lines = self.request('GET', path.rsplit('/', 1)[0] + '/log', {'limit': 1000})
                        messages = [row.get('t', '') for row in lines]
                        warnings = [message for message in messages if message.startswith('WARN:')]
                        if (len(warnings) == int(result['exitstatus'].split(':')[1]) and
                                len(lines) < 1000 and
                                all(re.fullmatch(r"WARN: netdev net[0-9]+: ignoring MTU '[0-9]+', not using VirtIO", message)
                                    for message in warnings) and
                                messages[-1:] == ['TASK ' + result['exitstatus']] and
                                ((':qmstart:' in upid and all(message in warnings or message == 'TASK ' + result['exitstatus'] for message in messages))
                                 or (':vzdump:' in upid and 'INFO: Backup job finished successfully' in messages
                                     and all(message.startswith('INFO:') or message in warnings
                                             or message == 'TASK ' + result['exitstatus'] for message in messages)))):
                            print(f"crange: warning: {node} task completed successfully; non-VirtIO NIC ignores host MTU.", flush=True)
                            return
                    raise CrangeError(f"Proxmox task on {node} failed: {result.get('exitstatus', 'unknown')}.")
                return
            if any(kind in upid for kind in (':vzdump:', ':qmrestore:', ':qmclone:')) and time.monotonic() >= next_progress:
                next_progress = time.monotonic() + 30
                try:
                    lines = self.request('GET', path.rsplit('/', 1)[0] + '/log', {'limit': 1000})
                    # Print only numeric transfer evidence, never raw provider
                    # logs which might contain configuration or credentials.
                    if ':qmclone:' in upid:
                        pattern = r'^transferred ([0-9.]+ (?:MiB|GiB|TiB)) of ([0-9.]+ (?:MiB|GiB|TiB)) \(([0-9]+(?:\.[0-9]+)?)%\)$'
                        samples = [re.fullmatch(pattern, row.get('t', '')) for row in lines]
                        samples = [sample for sample in samples if sample and float(sample[3]) <= 100]
                        if samples and samples[-1][3] != last_progress:
                            sample = samples[-1]; last_progress = sample[3]
                            print(f'Clone progress: {sample[3]}% ({sample[1]} of {sample[2]})', flush=True)
                        time.sleep(2)
                        continue
                    restore = ':qmrestore:' in upid
                    pattern = (r"^progress ([0-9]{1,3})% \(read ([0-9]+) bytes, duration ([0-9]+) sec\)$" if restore else
                               r"^INFO:\s+([0-9]{1,3})% \(([0-9.]+ (?:MiB|GiB|TiB)) of ([0-9.]+ (?:MiB|GiB|TiB))\)")
                    samples = [re.search(pattern, row.get('t', ''))
                               for row in lines]
                    samples = [sample for sample in samples if sample and int(sample[1]) <= 100]
                    if samples and samples[-1][1] != last_progress:
                        sample = samples[-1]; last_progress = sample[1]
                        message = (f'Restore progress: {sample[1]}% ({int(sample[2])/1024**3:.2f} GiB read)' if restore else
                                   f'Backup progress: {sample[1]}% ({sample[2]} of {sample[3]})')
                        print(message, flush=True)
                        if restore and sample[1] == '100':
                            print('Archive reading complete; waiting for durable disk writes and restore finalization. The task is still running.', flush=True)
                except CrangeError:
                    # Status remains authoritative if optional log access is
                    # unavailable. Never replay the operation to obtain logs.
                    pass
            time.sleep(2)
        raise CrangeError(f"Proxmox task on {node} did not finish within {timeout} seconds.")

    def vm(self, node: str, vmid: int) -> dict:
        return self.request("GET", f"/nodes/{node}/qemu/{vmid}/config")

    def container(self, node: str, vmid: int) -> dict:
        return self.request("GET", f"/nodes/{node}/lxc/{vmid}/config")

    def status(self, node: str, vmid: int) -> str:
        return self.request("GET", f"/nodes/{node}/qemu/{vmid}/status/current")["status"]

    def container_status(self, node: str, vmid: int) -> str:
        return self.request("GET", f"/nodes/{node}/lxc/{vmid}/status/current")["status"]

    def container_stop(self, node: str, vmid: int) -> None:
        upid = self.request("POST", f"/nodes/{node}/lxc/{vmid}/status/shutdown", {"timeout": 90})
        self.task(node, upid, timeout=180)
        if self.container_status(node, vmid) != "stopped":
            raise CrangeError(f"Staging CT {vmid} did not stop cleanly; image capture aborted.")

    def container_start(self, node: str, vmid: int) -> None:
        upid = self.request("POST", f"/nodes/{node}/lxc/{vmid}/status/start", {})
        self.task(node, upid, timeout=180)

    def container_template(self, node: str, vmid: int) -> None:
        upid = self.request("POST", f"/nodes/{node}/lxc/{vmid}/template", {})
        # LXC template conversion forks a worker but returns null in the API.
        # Support a task ID if a future endpoint supplies one, then verify the
        # actual template flag before considering the capture complete.
        if upid is not None:
            self.task(node, upid, timeout=600)
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if int(self.container(node, vmid).get('template', 0)) == 1:
                return
            time.sleep(2)
        raise CrangeError(f"Captured CT {node}/{vmid} did not become a template within 600 seconds.")

    def delete_container(self, node: str, vmid: int) -> None:
        upid = self.request("DELETE", f"/nodes/{node}/lxc/{vmid}", {"purge": 1})
        self.task(node, upid, timeout=600)

    def shutdown(self, node: str, vmid: int, address: str) -> None:
        # A fixed guest command gives Linux images a clean filesystem shutdown
        # when their firmware/ACPI path is unreliable. No user-supplied command
        # is ever executed by the controller.
        windows = str(self.vm(node, vmid).get('ostype', '')).startswith('win')
        if not windows:
            key = Path("/home/crange/.ssh/id_ed25519_range")
            try:
                subprocess.run(["ssh", "-i", str(key), "-o", "IdentitiesOnly=yes",
                    "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                    "-o", "ConnectTimeout=5", f"user@{address}",
                    "sudo -n systemctl poweroff"], check=False, capture_output=True,
                    text=True, timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                pass
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if self.status(node, vmid) == "stopped":
                    return
                time.sleep(2)
        upid = self.request("POST", f"/nodes/{node}/qemu/{vmid}/status/shutdown", {"timeout": 90, "forceStop": 0})
        self.task(node, upid, timeout=120)
        if self.status(node, vmid) != "stopped":
            raise CrangeError(f"Staging VM {vmid} did not stop cleanly; image capture aborted.")

    def start(self, node: str, vmid: int) -> None:
        upid = self.request("POST", f"/nodes/{node}/qemu/{vmid}/status/start", {})
        self.task(node, upid, timeout=180)

    def delete_vm(self, node: str, vmid: int) -> None:
        upid = self.request("DELETE", f"/nodes/{node}/qemu/{vmid}", {"purge": 1})
        self.task(node, upid, timeout=600)

    def image_datastore(self, node: str, source_id: int, kind: str) -> str:
        """Administrator-owned image destination; absent settings preserve legacy capture."""
        setting = Path.home() / '.config/crange/image-storage.json'
        if not setting.exists():
            return 'local-lvm'
        try:
            policy = json.loads(setting.read_text())
            datastore = policy['image_datastore']
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CrangeError('Invalid administrator image-storage setting.') from exc
        if datastore not in ('local-lvm', 'crange-library'):
            raise CrangeError('Image datastore must be local-lvm or crange-library.')
        status = self.request('GET', f'/nodes/{node}/storage/{datastore}/status')
        required = 'images' if kind == 'vm' else 'rootdir'
        if not isinstance(status, dict) or int(status.get('active', 0)) != 1 or required not in str(status.get('content', '')).split(','):
            raise CrangeError(f'Image datastore {datastore} is inactive or does not support {required}.')
        from crange_scheduling import disk_allocations
        config = self.vm(node, source_id) if kind == 'vm' else self.container(node, source_id)
        try:
            allocation = sum(disk_allocations(config).values())
        except ValueError as exc:
            raise CrangeError('Image capture has no defensible configured disk size.') from exc
        if allocation <= 0:
            raise CrangeError('Image capture has no defensible configured disk size.')
        total, available = float(status.get('total', 0)), float(status.get('avail', 0))
        reserve = max(total * .20, 20 * 1024 ** 3)
        if total <= 0 or available - allocation * 1.1 * 1024 ** 3 < reserve:
            raise CrangeError(f'Image datastore {datastore} lacks capture space and reserved headroom.')
        return datastore

    def capture_image(self, node: str, source_id: int, name: str, description: str) -> int:
        datastore = self.image_datastore(node, source_id, "vm")
        for _attempt in range(3):
            image_id = int(self.request("GET", "/cluster/nextid"))
            try:
                upid = self.request("POST", f"/nodes/{node}/qemu/{source_id}/clone",
                    {"newid": image_id, "name": name, "description": description,
                     "pool": "crange-iac", "full": 1, "storage": datastore,
                     **({"format": "qcow2"} if datastore == "crange-library" else {})})
            except CrangeError:
                # A parallel allocator may have used this ID; a final failure is surfaced.
                if _attempt == 2:
                    raise
                continue
            try:
                self.task(node, upid, timeout=1800)
                upid = self.request("POST", f"/nodes/{node}/qemu/{image_id}/template", {})
                self.task(node, upid, timeout=600)
                if int(self.vm(node, image_id).get("template", 0)) != 1:
                    raise CrangeError(f"Captured image {image_id} is not a Proxmox template.")
                return image_id
            except Exception:
                try:
                    self.delete_vm(node, image_id)
                except CrangeError:
                    print(f"Could not remove incomplete release image {node}/{image_id}.", file=sys.stderr)
                raise
        raise CrangeError("Could not allocate a release image VM ID.")

    def verify_image(self, node: str, vmid: int) -> None:
        if int(self.vm(node, vmid).get("template", 0)) != 1:
            raise CrangeError(f"Release image {node}/{vmid} is missing or is not a template.")

    def verify_container_image(self, node: str, vmid: int) -> None:
        if int(self.container(node, vmid).get("template", 0)) != 1:
            raise CrangeError(f"Release CT image {node}/{vmid} is missing or is not a template.")

    def capture_container_image(self, node: str, source_id: int, name: str, description: str) -> int:
        datastore = self.image_datastore(node, source_id, "container")
        for attempt in range(3):
            image_id = int(self.request("GET", "/cluster/nextid"))
            try:
                upid = self.request("POST", f"/nodes/{node}/lxc/{source_id}/clone",
                    {"newid": image_id, "hostname": name, "description": description,
                     "pool": "crange-iac", "full": 1, "storage": datastore})
            except CrangeError:
                if attempt == 2:
                    raise
                continue
            try:
                self.task(node, upid, timeout=1800)
                self.container_template(node, image_id)
                self.verify_container_image(node, image_id)
                return image_id
            except Exception:
                try:
                    upid = self.request("DELETE", f"/nodes/{node}/lxc/{image_id}", {"purge": 1})
                    self.task(node, upid, timeout=600)
                except CrangeError:
                    print(f"Could not remove incomplete release CT image {node}/{image_id}.", file=sys.stderr)
                raise
        raise CrangeError("Could not allocate release CT image ID.")


def templates_for(path: Path, *, creation=False) -> dict:
    source = path / "approved-scenario-templates.json"
    try:
        data = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CrangeError(f"Cannot read approved scenario templates {source}: {exc}") from exc
    if not data:
        raise CrangeError("No approved scenario VM templates are configured.")
    if creation:
        from crange_template_selection import creation_templates
        return creation_templates(data)
    return data


def container_templates_for(path: Path, *, creation=False) -> dict:
    try:
        data = json.loads((path / "approved-container-templates.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CrangeError(f"Cannot read approved container templates: {exc}") from exc
    if creation:
        from crange_template_selection import creation_templates
        return creation_templates(data)
    return data



def scenario_boxes(cfg: dict) -> dict:
    return cfg.setdefault("scenario_boxes", {})


def scenario_containers(cfg: dict) -> dict:
    return cfg.setdefault("scenario_containers", {})


def validate_container_config(path: Path, cfg: dict) -> None:
    allowlist_path = path / "approved-container-templates.json"
    try:
        allowlist = json.loads(allowlist_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CrangeError(f"Cannot read approved container templates: {exc}") from exc
    used = {(str(b.get("network_id", b["team_id"])), int(b["ip_last_octet"])) for b in scenario_boxes(cfg).values()}
    used.update((str(b.get("network_id", b["team_id"])), 10 + int(b["jump_number"])) for b in cfg.get("jump_boxes", {}).values())
    for key, box in scenario_containers(cfg).items():
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", str(box.get("name", ""))):
            raise CrangeError(f"Container {key} has an invalid name.")
        team = str(box.get("team_id", ""))
        if team != "254" and team not in set(map(str, cfg.get("team_ids", []))):
            raise CrangeError(f"Container {key} targets undeclared team {team}.")
        if box.get("template") not in allowlist:
            raise CrangeError(f"Container {key} uses an unapproved CT template {box.get('template')!r}.")
        if box.get("network_key"):
            network = cfg.get("custom_networks", {}).get(box["network_key"])
            if not network or str(network["owner_team_id"]) != team:
                raise CrangeError(f"Container {key} has an invalid custom network attachment.")
            network_id = int(network["network_id"])
        else:
            network_id = int(box.get("network_id", team))
        if network_id == 254 and team != "254":
            raise CrangeError(f"Container {key} uses reserved staging network ID with another team.")
        if not 2 <= int(box.get("ip_last_octet", 0)) <= 100:
            raise CrangeError(f"Container {key} address must use .2-.100.")
        addr = (str(network_id), int(box["ip_last_octet"]))
        if addr in used:
            raise CrangeError(f"Duplicate static address for team/network {team}: .{addr[1]}.")
        used.add(addr)
        if box.get("node_name") not in NODES | {"auto"} or not 1 <= int(box.get("cores", 1)) <= 16 or not 512 <= int(box.get("memory_mb", 512)) <= 65536:
            raise CrangeError(f"Container {key} has invalid node or resources.")
        validate_health_checks(box.get("health_checks", []))


def validate_health_checks(checks: object) -> list[dict]:
    if not isinstance(checks, list):
        raise CrangeError("Health checks must be a JSON list.")
    if not checks:
        checks = [{"type": "ping"}]
    normalized = []
    for check in checks:
        if not isinstance(check, dict) or check.get("type") not in ("ping", "tcp", "http"):
            raise CrangeError("Health checks support only objects with type 'ping', 'tcp', or 'http'.")
        if check["type"] == "ping":
            try:
                timeout = int(check.get("timeout", 5))
            except (TypeError, ValueError) as exc:
                raise CrangeError("Ping timeout must be numeric.") from exc
            if not 1 <= timeout <= 60:
                raise CrangeError("Ping timeout must be 1-60 seconds.")
            normalized.append({"type": "ping", "timeout": timeout})
            continue
        try:
            port = int(check["port"])
            timeout = int(check.get("timeout", 10))
            status = int(check.get("status", 200))
        except (KeyError, TypeError, ValueError) as exc:
            raise CrangeError("Each health check needs a numeric port; timeout/status must be numeric.") from exc
        if not 1 <= port <= 65535 or not 1 <= timeout <= 60:
            raise CrangeError("Health check port must be 1-65535 and timeout 1-60 seconds.")
        result = {"type": check["type"], "port": port, "timeout": timeout}
        if check["type"] == "http":
            result.update(path=str(check.get("path", "/")), status=status)
            if not result["path"].startswith("/") or not 100 <= status <= 599:
                raise CrangeError("HTTP checks need an absolute path and expected status 100-599.")
        normalized.append(result)
    return normalized


def health_check_label(check: dict) -> str:
    if check["type"] == "ping":
        return "ping"
    return f"{check['type']}:{check['port']}"


def guest_network_id(cfg: dict, team: str, key: str) -> int:
    if not key:
        return int(team)
    network = cfg.get('custom_networks', {}).get(key)
    if not network or str(network['owner_team_id']) != str(team):
        raise CrangeError('Custom network must belong to the guest owner team or staging environment.')
    return int(network['network_id'])


def validate_scenario_config(path: Path, cfg: dict) -> None:
    templates = templates_for(path)
    seen = {(str(box.get("network_id", box["team_id"])), 10 + int(box["jump_number"])) for box in cfg.get("jump_boxes", {}).values()}
    seen.update((str(net["owner_team_id"]), int(net["uplink_last_octet"])) for net in cfg.get("custom_networks", {}).values())
    for key, box in scenario_boxes(cfg).items():
        if not isinstance(box, dict):
            raise CrangeError(f"Scenario box {key} must be a JSON object.")
        name = str(box.get("name", ""))
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", name):
            raise CrangeError(f"Scenario box {key} has an invalid name; use a-z, 0-9, and hyphens (max 24 chars).")
        team = str(box.get("team_id", ""))
        if team != "254" and team not in set(map(str, cfg.get("team_ids", []))):
            raise CrangeError(f"Scenario box {key} targets undeclared team {team}.")
        try:
            last = int(box["ip_last_octet"])
            cores = int(box.get("cores", 2))
            memory = int(box.get("memory_mb", 4096))
        except (KeyError, ValueError, TypeError) as exc:
            raise CrangeError(f"Scenario box {key} has invalid resource or IP values.") from exc
        if not 2 <= last <= 100 or not 1 <= cores <= 16 or not 512 <= memory <= 65536:
            raise CrangeError(f"Scenario box {key} exceeds static IP or resource limits.")
        network_id = guest_network_id(cfg, team, box.get("network_key", ""))
        box["network_id"] = network_id
        addr = (str(network_id), last)
        if addr in seen:
            raise CrangeError(f"Duplicate static address in team {team}: .{last}.")
        seen.add(addr)
        if box.get("template") not in templates:
            raise CrangeError(f"Scenario box {key} uses unapproved template {box.get('template')!r}.")
        if box.get("node_name") not in NODES | {"auto"}:
            raise CrangeError(f"Scenario box {key} uses an invalid Proxmox node.")
        validate_health_checks(box.get("health_checks", []))
    validate_container_config(path, cfg)


def manifest_path(project: int, version: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", version) or version in (".", ".."):
        raise CrangeError("Release version must contain 1-64 letters, digits, dots, underscores, or hyphens.")
    return ROOT / "releases" / f"project-{project:03d}" / f"{version}.json"


def load_release(project: int, version: str) -> dict:
    path = manifest_path(project, version)
    try:
        release = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CrangeError(f"Cannot read release {version}: {exc}") from exc
    digest = release.pop("integrity_sha256", None)
    actual = hashlib.sha256(json.dumps(release, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if digest != actual:
        raise CrangeError(f"Release {version} failed its integrity check.")
    release["integrity_sha256"] = digest
    return release


def verify_release_images(release: dict) -> None:
    if release.get("schema_version") not in (2, 3, 4) or any(
        "source_vm_id" not in box or "source_template" not in box
        for box in release["scenario"]["boxes"] + release["scenario"].get("containers", [])
    ):
        raise CrangeError("This release predates guest-image capture and cannot be tested or deployed.")
    api = ProxmoxAPI()
    for reference in release["scenario"]["templates"].values():
        if reference.get("kind") == "container":
            api.verify_container_image(reference["template_node"], int(reference["template_id"]))
        else:
            api.verify_image(reference["template_node"], int(reference["template_id"]))


def release_attachment(source, team, mapping):
    from crange_release_networks import attachment
    try:
        return attachment(source, team, mapping)
    except ValueError as exc:
        raise CrangeError(str(exc)) from exc


def release_networks(cfg, release, team, scope, reserved_parent=()):
    from crange_release_networks import allocate_networks
    try:
        return allocate_networks(cfg, release, team, scope, reserved_parent)
    except ValueError as exc:
        raise CrangeError(str(exc)) from exc


def template_identity(reference: dict) -> dict:
    """Pin image and hardware settings while allowing harmless catalog labels."""
    result = {field: reference.get(field) for field in ('template_id', 'template_node')}
    if 'profile' in reference:
        result['profile'] = reference['profile']
    if 'image_id' in reference:
        result['image_id'] = reference['image_id']
    return result


def release_scenario_boxes(path: Path, release: dict, team_id: str, prefix: str, network_map: dict | None = None) -> dict:
    cfg = json_config(path)
    templates = templates_for(path)
    result = {}
    for source in release["scenario"]["boxes"]:
        template_key = source["template"]
        if template_key not in templates:
            raise CrangeError(f"Template {template_key!r} in release is no longer approved in this project.")
        pinned = release.get("scenario", {}).get("templates", {}).get(template_key)
        if (not pinned or pinned.get('kind', 'vm') != 'vm'
                or template_identity(templates[template_key]) != template_identity(pinned)):
            raise CrangeError(f"Template {template_key!r} no longer matches the pinned release reference.")
        key = f"{prefix}-{source['name']}-t{int(team_id):03d}"
        if len(key) > 63:
            key = f"{prefix[:16]}-{hashlib.sha1(key.encode()).hexdigest()[:10]}-t{int(team_id):03d}"
        item = {k: source[k] for k in ("name", "ip_last_octet", "template", "node_name", "cores", "memory_mb", "full_clone", "started", "health_checks")}
        # A staging guest's base image is not the captured release image.
        # Legacy captures also have a stable alias suitable for publication.
        item['image'] = pinned.get('image_id', template_key)
        version_identity = f"{release.get('release_id', release.get('version', 'release'))}:{source['name']}"
        version_mark = hashlib.sha1(version_identity.encode()).hexdigest()[:8]
        prefix_slug = re.sub(r"[^a-z0-9-]+", "-", prefix.lower()).strip("-")
        kind = "t" if prefix_slug.startswith("test") else "r"
        unique_name = f"{kind}{version_mark}-{source['name'][:14]}"
        item["name"] = unique_name
        item["team_id"] = team_id
        item["guac_protocol"] = source.get("guac_protocol", "ssh")
        item.update({k: source[k] for k in ("display_name", "is_jumpbox", "guac_services", "guac_credentials") if k in source})
        item.update(release_attachment(source, team_id, network_map or {}))
        item['release_id'] = release.get('release_id', release.get('version'))
        item['release_scope'] = 'test' if prefix.startswith('test') else 'team'
        item['release_source_name'] = source['name']
        result[key] = item
    return result


def release_scenario_containers(path: Path, release: dict, team_id: str, prefix: str, network_map: dict | None = None) -> dict:
    result = {}
    approved = container_templates_for(path)
    for source in release["scenario"].get("containers", []):
        key = f"{prefix}-{source['name']}-t{int(team_id):03d}"
        pinned = release.get("scenario", {}).get("templates", {}).get(source["template"])
        if (not pinned or pinned.get('kind', 'container') != 'container'
                or template_identity(approved.get(source["template"], {})) != template_identity(pinned)):
            raise CrangeError(f"CT template {source['template']!r} is not pinned by the release allowlist.")
        version_mark = hashlib.sha1(f"{release['release_id']}:{source['name']}".encode()).hexdigest()[:8]
        result[key] = {
            "team_id": team_id, "name": f"r{version_mark}-{source['name'][:13]}",
            "ip_last_octet": source["ip_last_octet"], "template": source["template"],
            "node_name": source["node_name"], "cores": source["cores"],
            "memory_mb": source["memory_mb"], "started": source.get("started", True),
            "health_checks": source.get("health_checks", []),
            "network_key": "", "network_id": int(team_id),
            "release_source_name": source["name"],
            "guac_protocol": source.get("guac_protocol", "ssh"),
            'release_id': release.get('release_id', release.get('version')),
            'release_scope': 'test' if prefix.startswith('test') else 'team',
            'image': pinned.get('image_id', source['template']),
        }
        result[key].update({k: source[k] for k in ("display_name", "is_jumpbox", "guac_services", "guac_credentials") if k in source})
        result[key].update(release_attachment(source, team_id, network_map or {}))
    return result


def cmd_jumpbox_settings(args: argparse.Namespace) -> None:
    from crange_jumpboxes import FIELDS, settings
    path = project_dir(args.project)
    cfg = json_config(path)
    box = cfg.get(FIELDS[args.kind], {}).get(args.name)
    if not box:
        raise CrangeError('Choose a box declared in this project.')
    try:
        value = settings(args.kind, args.display_name, args.is_jumpbox == 'true', json.loads(args.services))
    except (ValueError, TypeError) as exc:
        raise CrangeError(str(exc)) from exc
    box.update(value)
    from crange_jumpboxes import connection_records
    titles = [record['name'] for kind, field in FIELDS.items()
              for key, other in cfg.get(field, {}).items()
              for record in connection_records(int(args.project), kind, key, other, 'unapplied')]
    if len(titles) != len(set(titles)):
        raise CrangeError('Another box or service already uses this display name.')
    mode = getattr(args, 'credentials_mode', 'keep')
    if mode == 'custom':
        from crange_guest_credentials import install
        try:
            install(int(args.project), args.credential_ref, args.sealed_credentials)
        except ValueError as exc:
            raise CrangeError(str(exc)) from exc
        box['guac_credentials'] = args.credential_ref
    elif mode == 'default':
        box.pop('guac_credentials', None)
    save_config(path, cfg)
    print(f'Updated remote access for {args.name}: ' + ', '.join(value['guac_services']) + '.')
    live = output_json(path).get(FIELDS[args.kind], {}).get('value', {}).get(args.name)
    if live:
        from crange_jumpboxes import router_path
        inventory_path = ROOT / 'ansible/inventory/generated' / f'project-{int(args.project):03d}.json'
        run([sys.executable, str(ROOT / 'scripts/generate-project-inventory.py'),
             '--terraform-dir', str(path), '--output', str(inventory_path)])
        try:
            groups = json.loads(inventory_path.read_text())['all']['children']['routers']['children']
            hosts = router_path(int(args.project), live, groups)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CrangeError(f'Remote settings saved, but access configuration failed: {exc}') from exc
        for host in hosts:
            ansible(args, 'routers.yml', [host], tags=['guac_access'])
    cmd_guac_refresh(args)
    print('Remote access published. Guest identity, network and running services are unchanged.')


def cmd_machine_state(args: argparse.Namespace) -> None:
    """Power one applied scenario guest after verifying its project identity."""
    path = project_dir(args.project)
    cfg = json_config(path)
    field = 'scenario_boxes' if args.kind == 'vm' else 'scenario_containers'
    box = cfg.get(field, {}).get(args.name)
    if not box:
        raise CrangeError('Scenario guest is not declared in this project.')
    live = output_json(path).get(field, {}).get('value', {}).get(args.name)
    if not live:
        raise CrangeError('Deploy this guest before changing its power state.')
    node = live.get('node_name') or live.get('placement', {}).get('node_name') or box['node_name']
    if node == 'auto':
        raise CrangeError('Applied guest placement is missing; refresh project inventory before a power action.')
    vmid = int(live['id'])
    api = ProxmoxAPI()
    actual = api.vm(node, vmid) if args.kind == 'vm' else api.container(node, vmid)
    network = cfg.get('custom_networks', {}).get(box.get('network_key', ''))
    vnet = f"p{int(args.project):03d}c{int(network['network_id']):03d}" if network else f"p{int(args.project):03d}t{int(box['team_id']):03d}"
    expected = vnet + '-' + box['name']
    if actual.get('name', actual.get('hostname')) != expected or int(actual.get('template', 0)):
        raise CrangeError('Applied guest identity does not match this project declaration; refusing power action.')
    validate_project_quota(path, cfg)
    status = api.status(node, vmid) if args.kind == 'vm' else api.container_status(node, vmid)
    wanted = 'running' if args.state == 'start' else 'stopped'
    if status != wanted:
        if args.state == 'start':
            (api.start if args.kind == 'vm' else api.container_start)(node, vmid)
        elif args.kind == 'vm':
            api.shutdown(node, vmid, live['address'])
        else:
            api.container_stop(node, vmid)
        status = api.status(node, vmid) if args.kind == 'vm' else api.container_status(node, vmid)
        if status != wanted:
            raise CrangeError(f'Guest did not reach {wanted}; desired state was not changed.')
    box['started'] = args.state == 'start'
    save_config(path, cfg)
    print(f'{args.name}: {wanted}. Only this guest was operated on; desired power state updated.')


def cmd_scenario_list(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    validate_custom_networks(cfg)
    validate_scenario_config(path, cfg)
    boxes = scenario_boxes(cfg)
    if not boxes:
        print("No scenario VMs declared.")
        return
    for key, box in sorted(boxes.items()):
        print(f"{key}\tteam={box['team_id']}\t10.{cfg['project_id']}.{box['team_id']}.{box['ip_last_octet']}\t{box['template']}\t{box['node_name']}")


def scenario_guac_services(args: argparse.Namespace, existing: dict | None = None, default: str = "ssh") -> list[str]:
    from crange_jumpboxes import services
    raw = getattr(args, "guac_protocols", None)
    if raw is not None:
        try:
            raw = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError as exc:
            raise CrangeError("Guacamole services must be a JSON list.") from exc
    elif getattr(args, "guac_protocol", None):
        raw = [args.guac_protocol]
    elif existing:
        raw = existing.get("guac_services", [existing.get("guac_protocol", default)])
    else:
        raw = [default]
    try:
        return services(raw)
    except ValueError as exc:
        raise CrangeError(str(exc)) from exc


def cmd_scenario_add(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    templates = templates_for(path, creation=True)
    if args.template not in templates:
        raise CrangeError(f"Template {args.template!r} is not approved. Approved: {', '.join(sorted(templates))}")
    team = "254" if args.team == 0 else str(args.team)
    if team != "254" and team not in set(map(str, cfg.get("team_ids", []))):
        raise CrangeError(f"Team {team} is not configured.")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", args.name):
        raise CrangeError("Name must use lowercase letters, digits, and hyphens (max 24 chars).")
    if not 2 <= args.ip <= 100 or not 1 <= args.cores <= 16 or not 512 <= args.memory <= 65536:
        raise CrangeError("IP must be .2-.100, cores 1-16, and memory 512-65536 MB.")
    network_key = getattr(args, 'network', None) or ''
    network_id = guest_network_id(cfg, team, network_key)
    if any(int(b.get('network_id', b['team_id'])) == network_id and int(b['ip_last_octet']) == args.ip for b in scenario_boxes(cfg).values()) or any(int(b.get('network_id', b['team_id'])) == network_id and 10 + int(b['jump_number']) == args.ip for b in cfg.get('jump_boxes', {}).values()):
        raise CrangeError('Static address is already assigned in this network.')
    if args.node not in NODES | {"auto"}:
        raise CrangeError("Node must be one of proxmox1 through proxmox6.")
    key = f"t{int(team):03d}-{args.name}"
    if key in scenario_boxes(cfg):
        raise CrangeError(f"Scenario VM {key} already exists.")
    try:
        checks = validate_health_checks(json.loads(args.health))
    except json.JSONDecodeError as exc:
        raise CrangeError(f"Health checks are not valid JSON: {exc}") from exc
    remote_services = scenario_guac_services(args)
    scenario_boxes(cfg)[key] = {
        "team_id": team, "name": args.name, "ip_last_octet": args.ip,
        "network_key": network_key, "network_id": network_id,
        "template": args.template, "node_name": args.node,
        "placement": __import__("crange_scheduling").placement_intent(args.node, getattr(args,"preferred_node","")),
        "cores": args.cores, "memory_mb": args.memory,
        "full_clone": args.full_clone if args.full_clone is not None else args.node != "auto",
        "started": True, "health_checks": checks, "guac_protocol": remote_services[0], "guac_services": remote_services,
    }
    if templates[args.template].get("image_id"):
        scenario_boxes(cfg)[key]["image"] = templates[args.template]["image_id"]
        if args.full_clone is None:scenario_boxes(cfg)[key]["full_clone"] = False
    validate_custom_networks(cfg)
    validate_scenario_config(path, cfg)
    save_config(path, cfg)
    print(f"Declared {key} in team/network {team}. Run infra-plan and review before applying.")


def cmd_scenario_edit(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    box = scenario_boxes(cfg).get(args.name)
    if box is None:
        raise CrangeError(f"Scenario VM {args.name} is not declared.")
    if args.template not in templates_for(path, creation=args.template != box.get("template")):
        raise CrangeError(f"Template {args.template!r} is not approved.")
    if args.node not in NODES | {"auto"}:
        raise CrangeError("Node must be one of proxmox1 through proxmox6.")
    if not 1 <= args.cores <= 16 or not 512 <= args.memory <= 65536:
        raise CrangeError("Cores must be 1-16 and memory 512-65536 MB.")
    try:
        checks = validate_health_checks(json.loads(args.health))
    except json.JSONDecodeError as exc:
        raise CrangeError(f"Health checks are not valid JSON: {exc}") from exc
    ip = getattr(args, 'ip', None)
    if ip is not None:
        box['ip_last_octet'] = ip
    network_key = getattr(args, 'network', None)
    if network_key is not None:
        box['network_key'] = network_key
    box['network_id'] = guest_network_id(cfg, str(box['team_id']), box.get('network_key', ''))
    if args.node != box.get('node_name') or getattr(args, 'preferred_node', None) is not None:
        box['placement']=__import__('crange_scheduling').placement_intent(args.node, getattr(args, 'preferred_node', None) or '')
    if args.template != box.get("template"):
        reference = templates_for(path)[args.template]
        box.pop("image", None)
        if reference.get("image_id"):box["image"] = reference["image_id"]
    remote_services = scenario_guac_services(args, box)
    box.update({"template": args.template, "node_name": args.node,
                "cores": args.cores, "memory_mb": args.memory,
                "health_checks": checks, "guac_protocol": remote_services[0], "guac_services": remote_services})
    validate_custom_networks(cfg)
    validate_scenario_config(path, cfg)
    save_config(path, cfg)
    print(f"Updated {args.name}. Run infra-plan and review to stage the VM changes.")


def cmd_container_add(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    team = "254" if args.team == 0 else str(args.team)
    if team != "254" and team not in set(map(str, cfg.get("team_ids", []))):
        raise CrangeError(f"Team {team} is not configured.")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", args.name):
        raise CrangeError("Container name must use lowercase letters, digits, and hyphens (max 24 chars).")
    if args.template not in container_templates_for(path, creation=True):
        raise CrangeError("Container template is not in the platform-approved CT allowlist.")
    if not 2 <= args.ip <= 100 or not 1 <= args.cores <= 16 or not 512 <= args.memory <= 65536:
        raise CrangeError("CT static IP must be .2-.100, cores 1-16, and memory 512-65536 MB.")
    network_key = getattr(args, "network", None) or ""
    network_id = int(team)
    if network_key:
        net = cfg.get("custom_networks", {}).get(network_key)
        if not net or str(net["owner_team_id"]) != team:
            raise CrangeError("CT custom network must exist and be owned by the selected team or staging.")
        network_id = int(net["network_id"])
    if any(int(box.get("network_id", box["team_id"])) == network_id and int(box["ip_last_octet"]) == args.ip for box in scenario_boxes(cfg).values()):
        raise CrangeError("CT address collides with a scenario VM in this network.")
    if any(int(box.get("network_id", box["team_id"])) == network_id and 10 + int(box["jump_number"]) == args.ip for box in cfg.get("jump_boxes", {}).values()):
        raise CrangeError("CT address collides with a jump box in this network.")
    key = f"t{int(team):03d}-{args.name}"
    if key in scenario_containers(cfg):
        raise CrangeError(f"Container {key} already exists.")
    try:
        checks = validate_health_checks(json.loads(args.health))
    except json.JSONDecodeError as exc:
        raise CrangeError(f"Health checks are not valid JSON: {exc}") from exc
    remote_services = scenario_guac_services(args)
    scenario_containers(cfg)[key] = {
        "team_id": team, "name": args.name, "ip_last_octet": args.ip,
        "template": args.template, "node_name": args.node,
        "placement": __import__("crange_scheduling").placement_intent(args.node, getattr(args,"preferred_node","")),
        "cores": args.cores, "memory_mb": args.memory, "started": True,
        "network_key": network_key, "network_id": network_id,
        "health_checks": checks, "guac_protocol": remote_services[0], "guac_services": remote_services,
    }
    reference = container_templates_for(path, creation=True)[args.template]
    if reference.get("image_id"):scenario_containers(cfg)[key]["image"] = reference["image_id"]
    validate_scenario_config(path, cfg)
    validate_container_config(path, cfg)
    save_config(path, cfg)
    print(f"Declared CT {key} from approved template {args.template}. Review infra-plan before applying.")


def cmd_container_edit(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    box = scenario_containers(cfg).get(args.name)
    if box is None:
        raise CrangeError(f"Scenario container {args.name} is not declared.")
    if args.template not in container_templates_for(path, creation=args.template != box.get("template")):
        raise CrangeError("Container template is not in the platform-approved CT allowlist.")
    if args.node not in NODES | {"auto"}:
        raise CrangeError("Node must be one of proxmox1 through proxmox6.")
    if not 2 <= args.ip <= 100 or not 1 <= args.cores <= 16 or not 512 <= args.memory <= 65536:
        raise CrangeError("CT IP must be .2-.100, cores 1-16, and memory 512-65536 MB.")
    team = str(box["team_id"])
    network_key = args.network or ""
    network_id = int(team)
    if network_key:
        net = cfg.get("custom_networks", {}).get(network_key)
        if not net or str(net["owner_team_id"]) != team:
            raise CrangeError("CT custom network must belong to its existing team or staging environment.")
        network_id = int(net["network_id"])
    for key, other in scenario_containers(cfg).items():
        if key != args.name and int(other.get("network_id", other["team_id"])) == network_id and int(other["ip_last_octet"]) == args.ip:
            raise CrangeError("CT address collides with another scenario container in this network.")
    if any(int(other.get("network_id", other["team_id"])) == network_id and int(other["ip_last_octet"]) == args.ip for other in scenario_boxes(cfg).values()):
        raise CrangeError("CT address collides with a scenario VM in this network.")
    if any(int(other.get("network_id", other["team_id"])) == network_id and 10 + int(other["jump_number"]) == args.ip for other in cfg.get("jump_boxes", {}).values()):
        raise CrangeError("CT address collides with a jump box in this network.")
    try:
        checks = validate_health_checks(json.loads(args.health))
    except json.JSONDecodeError as exc:
        raise CrangeError(f"Health checks are not valid JSON: {exc}") from exc
    if args.node != box.get('node_name') or getattr(args, 'preferred_node', None) is not None:
        box['placement']=__import__('crange_scheduling').placement_intent(args.node, getattr(args, 'preferred_node', None) or '')
    if args.template != box.get("template"):
        reference = container_templates_for(path)[args.template]
        box.pop("image", None)
        if reference.get("image_id"):box["image"] = reference["image_id"]
    remote_services = scenario_guac_services(args, box)
    box.update({"template": args.template, "node_name": args.node,
                "cores": args.cores, "memory_mb": args.memory,
                "ip_last_octet": args.ip, "network_key": network_key,
                "network_id": network_id, "health_checks": checks,
                "guac_protocol": remote_services[0], "guac_services": remote_services})
    validate_custom_networks(cfg)
    validate_scenario_config(path, cfg)
    validate_container_config(path, cfg)
    validate_project_quota(path, cfg)
    save_config(path, cfg)
    print(f"Updated CT {args.name}. Run infra-plan and review to stage the changes.")


def cmd_container_health(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    live = output_json(path).get("scenario_containers", {}).get("value", {})
    names = [args.name] if args.name else sorted(scenario_containers(cfg))
    failed = False
    for key in names:
        item = scenario_containers(cfg).get(key)
        output = live.get(key)
        if not item or not output:
            print(f"{key}: not declared/applied")
            failed = True
            continue
        checks = validate_health_checks(item.get("health_checks", []))
        if not checks:
            print(f"{key} {output['address']}: no health checks declared")
            failed = True
        for check in checks:
            try:
                check_endpoint(int(args.project), str(item["team_id"]), output["address"], check)
                print(f"PASS {key} {health_check_label(check)}")
            except (OSError, CrangeError) as exc:
                failed = True
                print(f"FAIL {key} {health_check_label(check)}: {exc}")
    if failed:
        raise CrangeError("One or more scenario container health checks failed.")


def cmd_container_remove(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    key = args.name if args.name in scenario_containers(cfg) else next((k for k, b in scenario_containers(cfg).items() if b["name"] == args.name), None)
    if not key:
        raise CrangeError(f"Scenario CT {args.name} is not configured.")
    del scenario_containers(cfg)[key]
    save_config(path, cfg)
    print(f"Removed CT {key} from desired state. Review infra-plan before apply.")


def cmd_scenario_remove(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    if args.name not in scenario_boxes(cfg):
        raise CrangeError(f"Scenario VM {args.name} is not declared.")
    del scenario_boxes(cfg)[args.name]
    save_config(path, cfg)
    print(f"Removed {args.name} from desired state. Run infra-plan; deleting a live VM requires --allow-destroy and --allow-replace as applicable.")


def check_endpoint(project: int, team: str, address: str, check: dict) -> None:
    inventory = ROOT / "ansible" / "inventory" / "generated" / f"project-{project:03d}.json"
    if not inventory.exists():
        raise CrangeError(f"Inventory is missing. Run: crange inventory {project}")
    host = f"p{project:03d}t{int(team):03d}_router"
    # Check readiness from the router on the guest's own subnet. A custom
    # subnet's isolation must not be weakened just to probe its services.
    try:
        endpoint = ipaddress.IPv4Address(address)
        standard = ipaddress.IPv4Network(f"10.{project}.{int(team)}.0/24")
        if endpoint not in standard:
            data = json.loads(inventory.read_text())
            custom = data['all']['children']['routers']['children']['custom_routers']['hosts']
            matches = [name for name, values in custom.items()
                       if str(values.get('router_owner_team_id')) == str(int(team))
                       and endpoint in ipaddress.IPv4Network(values['router_team_network'])]
            if len(matches) != 1:
                raise CrangeError('Health endpoint has no unique applied network router owned by this team.')
            host = matches[0]
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        raise CrangeError('Cannot identify the applied network router for this health endpoint.') from exc
    payload = {"health_address": address, "health_check": check}
    run(["ansible-playbook", "-i", str(inventory), "--limit", host,
         "--extra-vars", json.dumps(payload), str(ROOT / "ansible" / "scenario-health.yml")],
        cwd=ROOT, capture=True, env={**os.environ, "ANSIBLE_TIMEOUT": "90"})


def guest_startup_timeout(profile):
    """Keep Windows policy/service startup bounded separately from probe timeouts."""
    return 600 if str(profile.get('os_type','')).startswith('win') else 180


def wait_for_boot_health(project: int, team: str, address: str, check: dict,
                         startup_timeout: int = 180, *, startup_deadline: float | None = None) -> None:
    """Allow startup time without extending one guest's budget for each probe."""
    deadline = time.monotonic() + startup_timeout
    if startup_deadline is not None:
        deadline = min(deadline, startup_deadline)
    while True:
        try:
            check_endpoint(project, team, address, check)
            return
        except (OSError, CrangeError) as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CrangeError(f"Service was not ready within {startup_timeout}s of startup: {exc}") from exc
            print(f"Waiting for {address} {health_check_label(check)} after startup", flush=True)
            time.sleep(min(3, remaining))


def cmd_scenario_health(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    validate_scenario_config(path, cfg)
    applied = output_json(path).get("scenario_boxes", {}).get("value", {})
    names = [args.name] if args.name else sorted(scenario_boxes(cfg))
    failed = False
    for name in names:
        declared = scenario_boxes(cfg).get(name)
        live = applied.get(name)
        if not declared or not live:
            print(f"{name}: not declared/applied")
            failed = True
            continue
        address = live["address"]
        checks = validate_health_checks(declared.get("health_checks", []))
        if not checks:
            print(f"{name} {address}: no health checks declared")
            failed = True
        for check in checks:
            try:
                check_endpoint(int(args.project), str(declared["team_id"]), address, check)
                print(f"PASS {name} {health_check_label(check)}")
            except (OSError, CrangeError) as exc:
                failed = True
                print(f"FAIL {name} {health_check_label(check)}: {exc}")
    if failed:
        raise CrangeError("One or more scenario health checks failed.")


def cmd_release_capture(args: argparse.Namespace) -> None:
    project = int(args.project)
    path = project_dir(args.project)
    cfg = json_config(path)
    validate_custom_networks(cfg)
    validate_scenario_config(path, cfg)
    validate_container_config(path, cfg)
    if project == 100:
        from crange_mhl_services import profile_for, verify_release_profiles
        try:
            verify_release_profiles({'project_id': project, 'scenario': {'service_profiles': profile_for(254)}}, require_current=True, preserve_recipes=True)
        except ValueError as exc:
            raise CrangeError(str(exc)) from exc
    tests_dir = ROOT / 'releases' / f'project-{project:03d}' / 'tests'
    if tests_dir.exists() and any(tests_dir.glob('*.json')):
        raise CrangeError('Clean up release test clones before capturing staging.')
    networks = {key: {field: net[field] for field in ('owner_team_id', 'network_id',
                'uplink_last_octet', 'node_name', 'cores', 'memory_mb') if field in net}
                for key, net in cfg.get('custom_networks', {}).items() if str(net['owner_team_id']) == '254'}
    boxes = [{**box, "key": key, "kind": "vm"} for key, box in sorted(scenario_boxes(cfg).items()) if str(box["team_id"]) == "254"]
    boxes += [{**box, "key": key, "kind": "container"} for key, box in sorted(scenario_containers(cfg).items()) if str(box["team_id"]) == "254"]
    selected = getattr(args, 'machine', [])
    if selected:
        if len(selected) != len(set(selected)) or not set(selected) <= {box['key'] for box in boxes}:
            raise CrangeError('Each --machine must select one distinct applied Team 0 scenario key.')
        boxes = [box for box in boxes if box['key'] in selected]
        used_networks = {box.get('network_key') for box in boxes if box.get('network_key')}
        networks = {key: value for key, value in networks.items() if key in used_networks}
    if not boxes:
        raise CrangeError("Team 0 has no scenario VMs or containers to capture.")
    for box in boxes:
        if box["kind"] == "vm" and not validate_health_checks(box.get("health_checks", [])):
            raise CrangeError(f"Scenario VM {box['key']} needs at least one health check before release capture.")
    permutations = [entry.strip().split(",") for entry in getattr(args, "boot_order", [])]
    scenario_names = {box["name"] for box in boxes}
    if not permutations:
        permutations = [[box["name"] for box in boxes]]
    for order in permutations:
        if len(order) != len(scenario_names) or set(order) != scenario_names or len(set(order)) != len(order):
            raise CrangeError("Each --boot-order must list every staging VM name exactly once, comma-separated.")
    destination = manifest_path(project, args.version)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (destination.parent / ".capture.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.exists():
            raise CrangeError(f"Release {args.version} already exists and is immutable.")
        outputs = output_json(path)
        applied = {
            "vm": outputs.get("scenario_boxes", {}).get("value", {}),
            "container": outputs.get("scenario_containers", {}).get("value", {}),
        }
        for box in boxes:
            live = applied[box["kind"]].get(box["key"])
            expected_address = f"10.{project}.{box.get('network_id', 254)}.{box['ip_last_octet']}"
            if not live or live.get("address") != expected_address:
                raise CrangeError(f"Staging VM {box['key']} is not applied at {expected_address}.")
            print(f"Checking staging health: {box['key']}", flush=True)
            for check in box.get("health_checks", []):
                check_endpoint(project, "254", expected_address, check)
        applied_networks = outputs.get('routers', {}).get('value', {}).get('custom_networks', {})
        for key, net in networks.items():
            live = applied_networks.get(key, {})
            if live.get('lan') != f"10.{project}.{net['network_id']}.0/24" or str(live.get('owner_team_id')) != '254' or live.get('upstream_address') != f"10.{project}.254.{net['uplink_last_octet']}":
                raise CrangeError(f'Staging custom network {key} is not applied at its declared topology.')
        api = ProxmoxAPI()
        approvals = templates_for(path)
        ct_approvals = container_templates_for(path)
        references = {}
        captured = []
        from crange_capture import capture_policy, capture_vm, capture_container
        # Gate every image before stopping any staging guest.
        capture_profiles = {}
        from crange_scheduling import read_settings
        _, source_catalog = read_settings()
        for box in boxes:
            try:
                reference = (approvals if box['kind']=='vm' else ct_approvals)[box['template']]
                profile = {'agent_enabled': False, 'upgrade_packages': True,
                           **(json.loads((path / 'template-profile.json').read_text()) if box['kind']=='vm' else {})}
                profile.update(reference.get('profile', {}))
                image_id = box.get('image') or reference.get('image_id')
                catalog_image = source_catalog.get('images', {}).get(image_id, {})
                profile['init_system'] = 'windows' if str(profile.get('os_type','')).startswith('win') else catalog_image.get('init_system', 'systemd')
                source = applied[box['kind']][box['key']]
                source_node = source.get('node_name') or source.get('placement',{}).get('node_name') or box['node_name']
                capture_policy(source_node, profile, kind=box['kind'])
                if box['kind']=='container':
                    from crange_distribution import _source_capacity
                    _source_capacity(ProxmoxAPI().container(source_node,int(source['id'])), 'lxc')
                if profile['init_system']=='windows': profile['scenario_snapshot']=True
                capture_profiles[box['key']] = profile
            except (OSError, ValueError, TypeError) as exc:
                raise CrangeError(str(exc)) from exc
        for box in boxes:
            node = applied[box["kind"]][box["key"]].get("node_name") or applied[box["kind"]][box["key"]].get("placement", {}).get("node_name") or box["node_name"]
            vmid = int(applied[box["kind"]][box["key"]]["id"])
            actual = api.vm(node, vmid) if box["kind"] == "vm" else api.container(node, vmid)
            vnet = f"p{project:03d}c{box['network_id']:03d}" if box.get("network_key") else f"p{project:03d}t254"
            if actual.get("name", actual.get("hostname")) != f"{vnet}-{box['name']}" or int(actual.get("template", 0)):
                raise CrangeError(f"Applied VM {node}/{vmid} does not match staging box {box['key']}.")
            if box['kind'] == 'vm' and str(actual.get('agent', '')).split(',')[0] not in ('1', 'enabled=1'):
                raise CrangeError('Staging VM QGA is disabled; rebuild with a tested approved agent profile before capture.')
            status = api.status(node, vmid) if box["kind"] == "vm" else api.container_status(node, vmid)
            if status != "running":
                raise CrangeError(f"Staging VM {node}/{vmid} must be running before capture.")
        revision = run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture=True).stdout.strip()
        dirty = bool(run(["git", "status", "--porcelain"], cwd=ROOT, capture=True).stdout.strip())
        try:
            for box in boxes:
                node = applied[box["kind"]][box["key"]].get("node_name") or applied[box["kind"]][box["key"]].get("placement", {}).get("node_name") or box["node_name"]
                vmid = int(applied[box["kind"]][box["key"]]["id"])
                slug = hashlib.sha256(f"{project}:{args.version}:{box['key']}".encode()).hexdigest()[:12]
                alias = f"p{project:03d}-image-{slug}"
                name = f"p{project:03d}-release-{slug}-{box['name'][:15]}"
                original_template = box["template"]
                original_image = applied[box['kind']][box['key']].get('placement',{}).get('image_id')
                source_profile = None
                if box["kind"] == "vm":
                    try:
                        source_profile = {'agent_enabled': False, 'upgrade_packages': True, **json.loads((path / 'template-profile.json').read_text())}
                        source_profile.update(templates_for(path)[original_template].get('profile', {}))
                    except (OSError, ValueError, TypeError) as exc:
                        raise CrangeError('Cannot read the approved VM hardware profile before capture.') from exc
                try:
                    if box['kind']=='vm' and capture_profiles[box['key']].get('init_system')=='windows':
                        from crange_windows_snapshot_guest import capture_credentials
                        if not original_image:
                            raise CrangeError('Windows capture requires its applied source image credential identity.')
                        capture_credentials(project,box['key'],vmid,original_image,alias)
                    checkpoint = None
                    if box['kind'] in ('vm','container'):
                        from crange_scheduling import DATA
                        checkpoint_path = DATA / 'capture-journal' / f'capture-p{project:03d}-{slug}.json'
                        if checkpoint_path.exists():
                            checkpoint = json.loads(checkpoint_path.read_text()).get('state')
                    if checkpoint in ('captured','verified'):
                        print(f"Reusing immutable capture checkpoint; staging source stays running: {box['key']}",flush=True)
                    else:
                        print(f"Stopping staging source for capture: {box['key']} ({node}/{vmid})", flush=True)
                        if box["kind"] == "vm":
                            api.shutdown(node, vmid, applied["vm"][box["key"]]["address"])
                        else:
                            api.container_stop(node, vmid)
                    print(f"Capturing isolated image and verifying a fresh clone: {box['key']}", flush=True)
                    if box["kind"] == "vm":
                        image_id, evidence = capture_vm(api, project=project, node=node,
                            source_id=vmid, name=name,
                            description=f"CRANGE project {project:03d} release {args.version} box {box['key']}",
                            slug=slug, profile=capture_profiles[box['key']])
                    else:
                        image_id, evidence = capture_container(api, project=project, node=node,
                            source_id=vmid, name=name,
                            description=f"CRANGE project {project:03d} release {args.version} CT {box['key']}",
                            slug=slug, profile=capture_profiles[box['key']])
                finally:
                    capture_failure = sys.exc_info()[1]
                    status = api.status(node, vmid) if box["kind"] == "vm" else api.container_status(node, vmid)
                    if status == "stopped":
                        print(f"Restarting staging source: {box['key']}", flush=True)
                        try:
                            (api.start if box["kind"] == "vm" else api.container_start)(node, vmid)
                        except CrangeError as restart_failure:
                            if capture_failure is None:
                                raise
                            print(f'crange: warning: source restart also failed: {restart_failure}', flush=True)
                image_node = evidence.get('node', node)
                reference = {"template_id": image_id, "template_node": image_node, "kind": box["kind"], "image_id": alias}
                if box['kind'] == 'container':
                    original_reference = container_templates_for(path).get(original_template, {})
                    placement = applied['container'][box['key']].get('placement', {})
                    management_user = 'root' if placement.get('template_file_id') else original_reference.get('profile', {}).get('management_user', 'user')
                    if management_user not in ('root', 'user'):
                        raise CrangeError('Captured container has an unsupported approved management account.')
                    reference['profile'] = {'management_user': management_user}
                if box["kind"] == "vm":
                    reference['profile'] = {**source_profile, 'upgrade_packages': False}
                    captured_machine = api.vm(image_node, image_id).get('machine')
                    if captured_machine:
                        reference['profile']['machine'] = captured_machine
                    reference['distribution_evidence'] = evidence
                reference['distribution_evidence'] = evidence
                references[alias] = reference
                captured.append((image_node, image_id, box["kind"]))
                box["source_template"] = original_template
                if original_image:
                    box['source_image_id'] = original_image
                box["source_vm_id"] = vmid
                box["template"] = alias
                box["image"] = alias
                box["full_clone"] = True
            service_profile = {}
            if project == 100:
                from crange_mhl_services import profile_for
                service_profile = profile_for(254)
                for profile_name, configured_profile in service_profile.items():
                    configured = configured_profile.get('guests', {}) if isinstance(configured_profile, dict) else {}
                    if selected and not set(configured) <= {box['key'] for box in boxes}:
                        continue
                    for box in boxes:
                        identity = configured.get(box['key']) if isinstance(configured, dict) else None
                        live = applied[box['kind']].get(box['key'], {})
                        placement = live.get('placement', {}) if isinstance(live, dict) else {}
                        if identity and (identity.get('guest_id') != live.get('id')
                                         or identity.get('image_id') != placement.get('image_id')):
                            raise CrangeError(f'MHL {profile_name} services were configured against an older staging guest identity; rerun the fixed service action before release capture.')
            if selected:
                captured_keys = {box['key'] for box in boxes}
                service_profile = {key:value for key,value in service_profile.items()
                                   if set(value.get('guests', {})) <= captured_keys}
            content = {
                "schema_version": 4,
                "release_id": f"p{project:03d}-{args.version}",
                "version": args.version,
                "project_id": project,
                "source_revision": revision,
                "source_tree_dirty": dirty,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "scenario": {"boxes": [{k: v for k, v in b.items() if k != "kind"} for b in boxes if b["kind"] == "vm"],
                             "containers": [{k: v for k, v in b.items() if k != "kind"} for b in boxes if b["kind"] == "container"],
                             "networks": networks,
                             "templates": references,
                             "service_profiles": service_profile,
                             "boot_order_permutations": permutations,
                             "team_overrides": {}},
            }
            content["integrity_sha256"] = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            # Only expose image aliases after every capture has succeeded.
            approvals.update({k: template_identity(v) for k, v in references.items() if v["kind"] == "vm"})
            ct_approvals.update({k: template_identity(v) for k, v in references.items() if v["kind"] == "container"})
            allowlist = path / "approved-scenario-templates.json"
            fd, tmp_name = tempfile.mkstemp(prefix=".approved-templates.", dir=path)
            try:
                with os.fdopen(fd, "w") as stream:
                    stream.write(json.dumps(approvals, indent=2, sort_keys=True) + "\n")
                os.chmod(tmp_name, 0o640)
                os.replace(tmp_name, allowlist)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            fd, tmp_name = tempfile.mkstemp(prefix=".approved-ct-templates.", dir=path)
            try:
                with os.fdopen(fd, "w") as stream:
                    stream.write(json.dumps(ct_approvals, indent=2, sort_keys=True) + "\n")
                os.chmod(tmp_name, 0o640)
                os.replace(tmp_name, path / "approved-container-templates.json")
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o440)
            with os.fdopen(fd, "w") as stream:
                stream.write(json.dumps(content, indent=2, sort_keys=True) + "\n")
        except Exception:
            # Protected worker journals and reservations own these artifacts.
            # Automatic deletion would invalidate provenance or race uncertain tasks.
            print('Capture interrupted; inspect protected capture/source journals and reconcile before retry.', file=sys.stderr)
            raise
    for alias, reference in references.items():
        evidence = reference['distribution_evidence']
        cmd_release_publish(argparse.Namespace(project=args.project, version=args.version,
            template=alias, export_job=evidence['export_job'], probe_job=evidence['probe_job']))
    print(f"Captured immutable guest images and release {content['release_id']} at {destination.relative_to(ROOT)}")
    print(f"SHA-256 {content['integrity_sha256']}")

def cmd_release_recipe(args: argparse.Namespace) -> None:
    """Publish configured service changes with explicitly reused parent images."""
    from crange_mhl_services import profile_for, verify_release_profiles
    from crange_release_recipe import build, publish
    from crange_distribution import catalog_transaction
    from crange_scheduling import read_settings, CONFIG
    if int(args.project) != 100:
        raise CrangeError('Fixed service-recipe updates are currently available for Project 100.')
    path = project_dir(args.project)
    cfg = json_config(path)
    parent = load_release(100, args.parent_version)
    destination = manifest_path(100, args.version)
    try:
        verify_release_profiles(parent)
        profiles = profile_for(254)
        verify_release_profiles({'project_id': 100, 'scenario': {'service_profiles': profiles}},
                                require_current=True, preserve_recipes=True)
        verify_release_images(parent)
        _, catalog = read_settings()
        release = build(parent, cfg, output_json(path), profiles, catalog, version=args.version,
            revision=run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture=True).stdout.strip(),
            dirty=bool(run(['git', 'status', '--porcelain'], cwd=ROOT, capture=True).stdout.strip()),
            created_at=datetime.now(timezone.utc).isoformat(),
            actor=os.environ.get('CRANGE_ACTOR', f'cli:{os.getuid()}'))
        def update(current):
            if json_config(path) != cfg:
                raise ValueError('Staging definitions changed during publication; review again.')
            return publish(destination, release, current)
        catalog_transaction(CONFIG / 'image-catalog.json', update)
    except (ValueError, OSError) as exc:
        raise CrangeError(str(exc)) from exc
    print(f'Published service update {release["release_id"]} using verified images from {parent["release_id"]}.')
    print('No disks captured and no deployed teams changed. Prepare and check a fresh release test before rollout.')
    print(f'SHA-256 {load_release(100, args.version)["integrity_sha256"]}')


def cmd_release_show(args: argparse.Namespace) -> None:
    release = load_release(int(args.project), args.version)
    print(json.dumps(release, indent=2, sort_keys=True))


def cmd_release_publish(args: argparse.Namespace) -> None:
    """Administrator publication from protected, completed worker journals."""
    from crange_catalog import publish_release_image
    from crange_distribution import catalog_transaction, SSHVolumeHasher, _source_config_identity
    from crange_scheduling import CONFIG, DATA, node_locks, disk_allocations
    from crange_placement import ReservationLedger
    release = load_release(int(args.project), args.version)
    if int(release['project_id']) != int(args.project):
        raise CrangeError('Release publication project scope does not match.')
    reference = release['scenario']['templates'].get(args.template)
    if not reference:
        raise CrangeError('Choose a captured template alias from release-show.')
    def journal_record(job):
        if not re.fullmatch(r'[A-Za-z0-9-]{1,64}', job):
            raise CrangeError('Invalid distribution job ID.')
        file = DATA / 'source-journal' / (job + '.json')
        info = file.lstat()
        if file.is_symlink() or info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
            raise CrangeError('Publication requires protected controller-owned worker journals.')
        record = json.loads(file.read_text())
        if record.get('job_id') != job:
            raise CrangeError('Worker journal job identity does not match.')
        return record
    export = journal_record(args.export_job)
    probe = journal_record(args.probe_job)
    ledger = ReservationLedger(DATA / 'reservations.sqlite3')
    try:
        job = ledger.get_job(args.probe_job)
        if (job['state'] != 'released' or job['project_id'] != int(args.project)
                or job['allocations'] != [probe.get('reservation')]):
            raise CrangeError('Source probe capacity reservation has not been reconciled exactly.')
    finally:
        ledger.close()
    node, vmid = reference['template_node'], int(reference['template_id'])
    kind = reference.get('kind', 'vm')
    with node_locks([node], library=True):
        api = ProxmoxAPI()
        config = api.vm(node, vmid) if kind == 'vm' else api.container(node, vmid)
        status = api.status(node, vmid) if kind == 'vm' else api.container_status(node, vmid)
        if (int(config.get('template', 0)) != 1 or status != 'stopped'
                or _source_config_identity(config, vmid) != export.get('identity_capture', {}).get('source_config_sha256')):
            raise CrangeError('Captured release template changed since worker verification.')
        # Validate all evidence before asking a node to read an archive path.
        datastores = disk_allocations(config)
        if len(datastores) != 1:
            raise CrangeError('Release publication requires one defensible source datastore.')
        source_datastore = next(iter(datastores))
        publish_release_image({'schema_version': 1, 'images': {}}, release, args.template, export, probe,
                              source_datastore=source_datastore)
        if SSHVolumeHasher()(node, export['volume_id']) != export['artifact_sha256']:
            raise CrangeError('Release artifact checksum changed since export.')
        catalog_transaction(CONFIG / 'image-catalog.json', lambda catalog:
            publish_release_image(catalog, release, args.template, export, probe,
                                  source_datastore=source_datastore))
    print(f"Published project-scoped immutable release image {reference.get('image_id', args.template)}.")


def cmd_release_deploy(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    release = load_release(int(args.project), args.version)
    verify_release_images(release)
    from crange_mhl_services import verify_release_profiles
    try:
        verify_release_profiles(release, preserve_recipes=True)
    except ValueError as exc:
        raise CrangeError(str(exc)) from exc
    tests_dir = ROOT / "releases" / f"project-{int(args.project):03d}" / "tests"
    if (tests_dir / f"{args.version}.json").exists():
        raise CrangeError("Clean up the release test clones before rollout.")
    results_dir = ROOT / "releases" / f"project-{int(args.project):03d}" / "test-results"
    passed = False
    if results_dir.exists():
        for result_file in results_dir.glob(f"{args.version}-*.json"):
            try:
                result = json.loads(result_file.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if result.get("release_id") == release["release_id"] and result.get("result") == "passed":
                passed = True
                break
    if not passed:
        raise CrangeError("This release has no recorded passing fresh-clone test. Run release-test-prepare, apply, release-test-check, and cleanup first.")
    release.setdefault("scenario", {}).setdefault("team_overrides", {})
    wanted = [str(int(team)) for team in args.teams]
    if not wanted or len(wanted) != len(set(wanted)) or any(t not in set(map(str, cfg.get("team_ids", []))) for t in wanted):
        raise CrangeError("Choose one or more teams already declared in this project.")
    action = getattr(args, 'release_action', 'deploy')
    from crange_rollouts import begin
    try:
        for team in wanted:
            begin(cfg, team, action, args.version, getattr(args, 'from_version', None))
    except ValueError as exc:
        raise CrangeError(str(exc)) from exc
    overrides = {}
    target_resources = {(team, source["name"], "vm") for team in wanted for source in release["scenario"]["boxes"]}
    target_resources.update((team, source["name"], "container") for team in wanted for source in release["scenario"].get("containers", []))
    for raw in getattr(args, "team_override", []):
        try:
            team_text, box_name, assignment = raw.split(":", 2)
            field, value = assignment.split("=", 1)
            team = str(int(team_text))
        except (ValueError, TypeError) as exc:
            raise CrangeError("Overrides use TEAM:BOX:FIELD=VALUE.") from exc
        valid_box_names = {b["name"] for b in release["scenario"]["boxes"] + release["scenario"].get("containers", [])}
        if team not in wanted or box_name not in valid_box_names:
            raise CrangeError(f"Override {raw!r} must target a selected team and a release box.")
        if field not in ("ip_last_octet", "cores", "memory_mb", "node_name", "started"):
            raise CrangeError(f"Override field {field!r} is not allowed.")
        try:
            if field in ("ip_last_octet", "cores", "memory_mb"):
                value = int(value)
            elif field == "started":
                if value.lower() not in ("true", "false"):
                    raise ValueError()
                value = value.lower() == "true"
        except ValueError as exc:
            raise CrangeError(f"Override value for {field} is invalid.") from exc
        if field == "ip_last_octet" and not 2 <= value <= 100:
            raise CrangeError("Override IP must be .2-.100.")
        if field == "cores" and not 1 <= value <= 16:
            raise CrangeError("Override cores must be 1-16.")
        if field == "memory_mb" and not 512 <= value <= 65536:
            raise CrangeError("Override memory must be 512-65536 MB.")
        if field == "node_name" and value not in NODES:
            raise CrangeError("Override node must be proxmox1 through proxmox6.")
        if field == "ip_last_octet":
            occupied = {int(b["ip_last_octet"]) for b in scenario_boxes(cfg).values() if str(b["team_id"]) == team}
            occupied.update(10 + int(b["jump_number"]) for b in cfg.get("jump_boxes", {}).values() if str(b["team_id"]) == team)
            if value in occupied:
                raise CrangeError(f"Override .{value} collides with an existing static address in team {team}.")
        overrides.setdefault(team, {}).setdefault(box_name, {})[field] = value
    for team, per_box in overrides.items():
        for box_name, values in per_box.items():
            kinds = {kind for target_team, target_name, kind in target_resources if target_team == team and target_name == box_name}
            if not kinds:
                raise CrangeError(f"Override refers to unknown release object {team}:{box_name}.")
            if "node_name" in values and "container" in kinds:
                source = next(b for b in release["scenario"]["containers"] if b["name"] == box_name)
                ref = release["scenario"]["templates"][source["template"]]
                if values["node_name"] != ref["template_node"]:
                    raise CrangeError("CT release images use local-lvm; deploy on their template node.")
    for team in wanted:
        reserved_parent = [int(box['ip_last_octet']) for field in ('boxes', 'containers')
                           for box in release['scenario'].get(field, []) if not box.get('network_key')]
        reserved_parent += [int(values['ip_last_octet']) for values in overrides.get(team, {}).values()
                            if 'ip_last_octet' in values]
        network_map, _ = release_networks(cfg, release, team, 'team', reserved_parent)
        additions = release_scenario_boxes(path, release, team, f"r{args.version}", network_map)
        for key, box in additions.items():
            source_name = box["release_source_name"]
            for field, value in overrides.get(team, {}).get(source_name, {}).items():
                box[field] = value
                if field == "node_name" and value != release["scenario"]["templates"][box["template"]]["template_node"]:
                    box["full_clone"] = True
            box["release_source_name"] = source_name
            current = scenario_boxes(cfg).get(key)
            if current and current != box:
                raise CrangeError(f"Existing scenario VM {key} differs from this release; refusing to overwrite it.")
            scenario_boxes(cfg)[key] = box
        ct_additions = release_scenario_containers(path, release, team, f"r{args.version}", network_map)
        for key, box in ct_additions.items():
            source_name = box["release_source_name"]
            for field, value in overrides.get(team, {}).get(source_name, {}).items():
                if field not in ("ip_last_octet", "cores", "memory_mb", "node_name", "started"):
                    raise CrangeError(f"Override field {field!r} is not allowed.")
                if field == "node_name" and value != release["scenario"]["templates"][box["template"]]["template_node"]:
                    raise CrangeError("CT release images use local-lvm; deploy on the template node.")
                box[field] = value
            current = scenario_containers(cfg).get(key)
            if current and current != box:
                raise CrangeError(f"Existing scenario CT {key} differs from this release; refusing to overwrite it.")
            scenario_containers(cfg)[key] = box
        release["scenario"]["team_overrides"][team] = overrides.get(team, {})
        from crange_rollouts import declare
        declare(cfg, team, release, action)
    validate_custom_networks(cfg)
    validate_scenario_config(path, cfg)
    validate_container_config(path, cfg)
    save_config(path, cfg)
    print(f"Release {args.version} declared for teams {', '.join(wanted)}. Use Review rollout to prepare images and review these teams before applying fresh clones.")


def cmd_release_configure(args):
    if not args.yes:
        raise CrangeError('Release configuration changes selected guests; pass --yes.')
    args.configure_only = True
    cmd_release_apply(args)


def cmd_release_apply(args):
    path = project_dir(args.project)
    cfg = json_config(path)
    teams = [str(int(team)) for team in args.teams]
    if not teams or len(teams) != len(set(teams)) or any(team not in set(map(str, cfg.get('team_ids', []))) for team in teams):
        raise CrangeError('Choose configured ordinary teams without duplicates.')
    if not getattr(args, 'configure_only', False) and not getattr(args, 'plan_id', None):
        raise CrangeError('Release deployment requires a retained, reviewed plan.')
    from crange_rollouts import verify_owned
    for team in teams:
        record = cfg.get('release_rollouts', {}).get(team)
        if not record:
            raise CrangeError(f'Team {team} has no declared release.')
        try:
            verify_owned(cfg, team, record['desired'])
        except ValueError as exc:
            raise CrangeError(str(exc)) from exc
    from crange_mhl_services import verify_release_profiles
    profiles_by_team = {}
    recipes_by_team = {}
    for team in teams:
        release = load_release(int(args.project), cfg['release_rollouts'][team]['desired']['version'])
        if int(release.get('project_id', -1)) != int(args.project):
            raise CrangeError('Release service profile project scope differs from the requested project.')
        try:
            profiles_by_team[team] = verify_release_profiles(release)
            recipes_by_team[team] = release.get("scenario", {}).get("service_profiles", {})
        except ValueError as exc:
            raise CrangeError(str(exc)) from exc
    if getattr(args, 'configure_only', False):
        from crange_release_resume import validate_applied
        validate_applied(path, cfg, teams)
        print('Verified existing release infrastructure. Resuming selected-team configuration; no Terraform apply.', flush=True)
    else:
        args.release_teams = teams
        cmd_apply(args)
        print('Release infrastructure applied. Configuring selected teams.', flush=True)
    try:
        for team in teams:
            cmd_configure_team(argparse.Namespace(project=args.project, team=int(team), yes=True))
            for profile in profiles_by_team[team]:
                operation = cmd_mhl_business_configure if profile == 'business' else cmd_mhl_windows_configure
                operation(argparse.Namespace(project=args.project, network_id=int(team), yes=True,
                                             recipe_sha256=recipes_by_team[team][profile]["source_sha256"]))
            cmd_release_check(argparse.Namespace(project=args.project, teams=[int(team)]))
    except CrangeError as exc:
        raise CrangeError('Release infrastructure applied, but configuration or health verification failed: ' + str(exc)) from exc
    print('Selected-team release configuration and health checks completed.' if getattr(args, 'configure_only', False)
          else 'Selected-team release deployment and health checks completed.')


def record_release_apply(path):
    cfg = json_config(path)
    if not cfg.get('release_rollouts'):
        return
    from crange_rollouts import applied
    try:
        changed = applied(cfg, output_json(path))
    except ValueError as exc:
        raise CrangeError('Infrastructure applied, but release tracking failed: ' + str(exc)) from exc
    if changed:
        save_config(path, cfg)
        print('Recorded applied team releases. Run release-check to verify their health.')


def applied_guest_node(api, field, live):
    """Resolve the physical node from current cluster identity, never a preference."""
    vmid = int(live['id'])
    kind = 'qemu' if field == 'scenario_boxes' else 'lxc'
    rows = api.request('GET', '/cluster/resources', {'type': 'vm'})
    if not isinstance(rows, list):
        raise CrangeError('Live cluster guest inventory is unavailable.')
    matches = [row for row in rows if str(row.get('vmid')) == str(vmid) and row.get('type') == kind]
    if len(matches) != 1 or matches[0].get('node') not in NODES:
        raise CrangeError('Applied release guest has no unambiguous live cluster node.')
    return matches[0]['node']


def cmd_release_check(args):
    path = project_dir(args.project)
    cfg = json_config(path)
    teams = [str(int(team)) for team in args.teams]
    if not teams or len(set(teams)) != len(teams) or any(team not in set(map(str, cfg.get('team_ids', []))) for team in teams):
        raise CrangeError('Choose configured ordinary teams without duplicates.')
    from crange_rollouts import verify_owned, health
    api = ProxmoxAPI()
    outputs = output_json(path)
    any_failed = False
    for team in teams:
        record = cfg.get('release_rollouts', {}).get(team)
        if not record or record.get('applied') != record.get('desired'):
            raise CrangeError(f'Team {team} has no matching applied release to check.')
        try:
            verify_owned(cfg, team, record['desired'])
        except ValueError as exc:
            raise CrangeError(str(exc)) from exc
        failures = []
        for field in ('scenario_boxes', 'scenario_containers'):
            for key, box in record['desired']['resources'][field].items():
                try:
                    live = outputs.get(field, {}).get('value', {}).get(key, {})
                    address = f"10.{args.project}.{box['network_id']}.{box['ip_last_octet']}"
                    if not live.get('id') or live.get('address') != address:
                        raise CrangeError('Applied guest address differs from the release.')
                    vmid, node = int(live['id']), applied_guest_node(api, field, live)
                    actual = api.vm(node, vmid) if field == 'scenario_boxes' else api.container(node, vmid)
                    vnet = f"p{int(args.project):03d}c{box['network_id']:03d}" if box.get('network_key') else f"p{int(args.project):03d}t{int(team):03d}"
                    if actual.get('name', actual.get('hostname')) != vnet + '-' + box['name'] or int(actual.get('template', 0)):
                        raise CrangeError('Applied guest identity differs from the release.')
                    state = api.status(node, vmid) if field == 'scenario_boxes' else api.container_status(node, vmid)
                    if state != 'running':
                        raise CrangeError('Guest is not running.')
                    for check in validate_health_checks(box.get('health_checks', [])):
                        check_endpoint(int(args.project), team, address, check)
                    print(f'PASS team {team} {key}')
                except (CrangeError, OSError) as exc:
                    failures.append({'resource': key, 'error': str(exc)})
                    print(f'FAIL team {team} {key}: {exc}')
        health(record, not failures, failures)
        any_failed |= bool(failures)
    save_config(path, cfg)
    if any_failed:
        raise CrangeError('One or more deployed release health checks failed; results recorded.')


def cmd_release_test_prepare(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    release = load_release(int(args.project), args.version)
    verify_release_images(release)
    from crange_mhl_services import verify_release_profiles
    try:
        verify_release_profiles(release, preserve_recipes=True)
    except ValueError as exc:
        raise CrangeError(str(exc)) from exc
    tests_dir = ROOT / "releases" / f"project-{int(args.project):03d}" / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    record_path = tests_dir / f"{args.version}.json"
    if record_path.exists():
        raise CrangeError(f"A test record already exists for {args.version}; check or clean it up first.")
    validate_custom_networks(cfg)
    network_map, owned = release_networks(cfg, release, '254', 'test')
    additions = {
        'scenario_boxes': release_scenario_boxes(path, release, '254', f'test{args.version}', network_map),
        'scenario_containers': release_scenario_containers(path, release, '254', f'test{args.version}', network_map),
    }
    prepared = {'boxes': {}, 'containers': {}}
    for field, clones in additions.items():
        for key, box in clones.items():
            if key in cfg.get(field, {}):
                raise CrangeError(f'Test clone key {key} already exists.')
            box['started'] = True
            cfg.setdefault(field, {})[key] = box
            prepared['boxes' if field == 'scenario_boxes' else 'containers'][key] = {
                'address': f"10.{int(args.project)}.{box['network_id']}.{box['ip_last_octet']}",
                'health_checks': box['health_checks'],
            }
    validate_custom_networks(cfg)
    validate_scenario_config(path, cfg)
    validate_container_config(path, cfg)
    record = {'version': args.version, 'release_id': release['release_id'],
              'networks': owned, **prepared}
    # Persist ownership before desired state so a failed write cannot orphan clones.
    record_path.write_text(json.dumps(record, indent=2) + "\n")
    record_path.chmod(0o640)
    try:
        save_config(path, cfg)
    except Exception:
        record_path.unlink()
        raise
    print(f"Declared {len(prepared['boxes'])} VMs, {len(prepared['containers'])} containers and {len(owned)} isolated test networks for release {args.version}.")
    print('Run infra-plan and infra-apply, then release-test-check. After checking, run release-test-cleanup and apply its reviewed destroy plan.')


def cmd_release_test_check(args: argparse.Namespace) -> None:
    project = int(args.project)
    release = load_release(project, args.version)
    verify_release_images(release)
    record_path = ROOT / "releases" / f"project-{project:03d}" / "tests" / f"{args.version}.json"
    try:
        record = json.loads(record_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CrangeError(f"No test-clone record found: {exc}") from exc
    path = project_dir(args.project)
    applied = output_json(path).get("scenario_boxes", {}).get("value", {})
    applied_cts = output_json(path).get("scenario_containers", {}).get("value", {})
    failed = False
    # Test every captured order on disposable clones; all permutations are fixed in the immutable manifest.
    api = ProxmoxAPI()
    permutations = release["scenario"].get("boot_order_permutations", [])
    all_outputs = output_json(path)
    applied_boxes = all_outputs.get("scenario_boxes", {}).get("value", {})
    applied = applied_boxes
    applied_cts = all_outputs.get("scenario_containers", {}).get("value", {})
    cfg = json_config(path)
    validate_custom_networks(cfg)
    vmids, ctids = {}, {}
    for field, group, applied_group, ids in (
        ('scenario_boxes', 'boxes', applied_boxes, vmids),
        ('scenario_containers', 'containers', applied_cts, ctids),
    ):
        for key, info in record.get(group, {}).items():
            box = cfg.get(field, {}).get(key)
            live = applied_group.get(key)
            if not box or not live or live.get('address') != info['address'] or str(box['team_id']) != '254':
                raise CrangeError('Disposable test guest is missing or its applied address has changed.')
            if record.get('release_id') and (box.get('release_id'), box.get('release_scope')) != (record['release_id'], 'test'):
                raise CrangeError('Disposable test guest ownership has changed; refusing power operations.')
            source_name = box.get('release_source_name')
            if not source_name:
                source_name = next((b['name'] for b in release['scenario'].get(group, [])
                                    if key.endswith(f"-{b['name']}-t254")), None)
            if not source_name or source_name in ids:
                raise CrangeError('Disposable test guest has invalid release source metadata.')
            node, vmid = applied_guest_node(api, field, live), int(live['id'])
            actual = api.vm(node, vmid) if field == 'scenario_boxes' else api.container(node, vmid)
            vnet = f"p{project:03d}c{box['network_id']:03d}" if box.get('network_key') else f"p{project:03d}t254"
            if actual.get('name', actual.get('hostname')) != vnet + '-' + box['name'] or int(actual.get('template', 0)):
                raise CrangeError('Disposable test guest identity has changed; refusing power operations.')
            ids[source_name] = (node, vmid)
    if release.get('scenario', {}).get('service_profiles'):
        from crange_mhl_services import configure_test_cohort
        configure_test_cohort(path, release, record)
    if not permutations:
        permutations = [[b["name"] for b in release["scenario"]["boxes"] + release["scenario"].get("containers", [])]]
    vm_templates=templates_for(path)
    for order in permutations:
        if set(order) != (set(vmids) | set(ctids)) or len(order) != len(vmids) + len(ctids):
            raise CrangeError("Release boot-order metadata does not match its VM/CT set.")
        for name, (node, vmid) in vmids.items():
            if api.status(node, vmid) != "stopped":
                upid = api.request("POST", f"/nodes/{node}/qemu/{vmid}/status/stop", {})
                api.task(node, upid, timeout=180)
            if api.status(node, vmid) != "stopped":
                raise CrangeError(f"Disposable test clone {vmid} did not stop between boot permutations.")
        for name, (node, vmid) in ctids.items():
            if api.container_status(node, vmid) != "stopped":
                api.container_stop(node, vmid)
        boot_started_at={}
        for name in order:
            boot_started_at[name]=time.monotonic()
            if name in vmids:
                api.start(*vmids[name])
            elif name in ctids:
                api.container_start(*ctids[name])
        for name, (node, vmid) in vmids.items():
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline and api.status(node, vmid) != "running":
                time.sleep(2)
            if api.status(node, vmid) != "running":
                failed = True
                print(f"FAIL boot order {','.join(order)} {name}: VM did not reach running state")
        for name, (node, vmid) in ctids.items():
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline and api.container_status(node, vmid) != "running":
                time.sleep(2)
            if api.container_status(node, vmid) != "running":
                failed = True
                print(f"FAIL boot order {','.join(order)} {name}: CT did not reach running state")
        # Require health checks after each startup sequence, then reset clones for the next sequence.
        for key, info in {**record["boxes"], **record.get("containers", {})}.items():
            live = applied_boxes.get(key)
            if key in record.get("containers", {}):
                live = applied_cts.get(key)
            if not live:
                continue
            for check in validate_health_checks(info.get("health_checks", [])):
                try:
                    field='scenario_boxes' if key in record['boxes'] else 'scenario_containers'
                    box=cfg.get(field,{}).get(key,{})
                    profile=vm_templates.get(box.get('template'),{}).get('profile',{})
                    startup=guest_startup_timeout(profile) if key in record['boxes'] else 180
                    source_name=box.get('release_source_name')
                    if source_name not in boot_started_at:
                        raise CrangeError('Boot health lacks its validated release source identity.')
                    wait_for_boot_health(project, "254", live["address"], check,startup_timeout=startup,
                        startup_deadline=boot_started_at[source_name]+startup)
                    print(f"PASS boot order {','.join(order)} {key} {health_check_label(check)}", flush=True)
                except (OSError, CrangeError) as exc:
                    failed = True
                    print(f"FAIL boot order {','.join(order)} {key} {health_check_label(check)}: {exc}")
    for key, info in record["boxes"].items():
        live = applied.get(key)
        if not live:
            failed = True
            print(f"FAIL {key}: Terraform has not applied this test clone")
            continue
        checks = validate_health_checks(info["health_checks"])
        if not checks:
            failed = True
            print(f"FAIL {key}: release has no health checks")
        address = live["address"]
        for check in checks:
            try:
                check_endpoint(project, "254", address, check)
                print(f"PASS {key} {health_check_label(check)}")
            except (OSError, CrangeError) as exc:
                failed = True
                print(f"FAIL {key} {health_check_label(check)}: {exc}")
    for key, info in record.get("containers", {}).items():
        live = applied_cts.get(key)
        if not live:
            failed = True
            print(f"FAIL {key}: Terraform has not applied this CT test clone")
            continue
        for check in validate_health_checks(info.get("health_checks", [])):
            try:
                check_endpoint(project, "254", live["address"], check)
                print(f"PASS {key} {health_check_label(check)}")
            except (OSError, CrangeError) as exc:
                failed = True
                print(f"FAIL {key} {health_check_label(check)}: {exc}")
    if failed:
        raise CrangeError("Release test did not pass.")
    result_dir = ROOT / "releases" / f"project-{project:03d}" / "test-results"
    result_dir.mkdir(parents=True, exist_ok=True)
    result_path = result_dir / f"{args.version}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    result_path.write_text(json.dumps({"release_id": release["release_id"], "checked_at": datetime.now(timezone.utc).isoformat(), "result": "passed", "boxes": sorted(record["boxes"]), "containers": sorted(record.get("containers", {})), "boot_orders": release["scenario"].get("boot_order_permutations", [])}, indent=2) + "\n")
    print(f"All health checks passed; immutable release {args.version} test result recorded at {result_path.relative_to(ROOT)}.")


def cmd_release_test_cleanup(args: argparse.Namespace) -> None:
    project = int(args.project)
    path = project_dir(args.project)
    cfg = json_config(path)
    record_path = ROOT / "releases" / f"project-{project:03d}" / "tests" / f"{args.version}.json"
    try:
        record = json.loads(record_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CrangeError(f"No test-clone record found: {exc}") from exc
    for field, group in (('scenario_boxes', 'boxes'), ('scenario_containers', 'containers')):
        for key in record.get(group, {}):
            box = cfg.get(field, {}).get(key)
            if box and record.get('release_id') and (box.get('release_id'), box.get('release_scope')) != (record['release_id'], 'test'):
                raise CrangeError('Test guest ownership has changed; refusing cleanup.')
            cfg.setdefault(field, {}).pop(key, None)
    for key, expected in record.get('networks', {}).items():
        current = cfg.get('custom_networks', {}).get(key)
        if current is not None and current != expected:
            raise CrangeError('Test network ownership or declaration has changed; refusing cleanup.')
        if any(box.get('network_key') == key for field in ('scenario_boxes', 'scenario_containers', 'jump_boxes')
               for box in cfg.get(field, {}).values()):
            raise CrangeError('Test network still has other guests attached; refusing cleanup.')
        cfg.get('custom_networks', {}).pop(key, None)
    validate_custom_networks(cfg)
    validate_scenario_config(path, cfg)
    validate_container_config(path, cfg)
    save_config(path, cfg)
    result_dir = ROOT / "releases" / f"project-{project:03d}" / "test-results"
    result_dir.mkdir(parents=True, exist_ok=True)
    archived = result_dir / f"{args.version}-test-clones.json"
    archived.write_text(json.dumps({"version": args.version, "test_clones": sorted(record["boxes"]), "test_containers": sorted(record.get("containers", {})), "test_networks": sorted(record.get("networks", {})), "cleaned_up_at": datetime.now(timezone.utc).isoformat()}, indent=2) + "\n")
    record_path.unlink()
    print("Removed test clones from desired state. Review infra-plan and use infra-apply --yes --allow-destroy to destroy them.")


def cmd_guac_enroll(args):
    """Declare a reviewed, typed jump-box setup and reconcile its native users."""
    from crange_auth import AuthStore
    from crange_jobs import command
    auth=AuthStore(os.environ.get('CRANGE_AUTH_DB',Path.home()/'.local/share/crange/auth.sqlite3'))
    with auth.connect() as db:
        request=db.execute('SELECT boxes FROM guacamole_enrollments WHERE id=? AND project=?',
                           (args.request_id,int(args.project))).fetchone()
    if not request:raise CrangeError('Enrollment request does not belong to this project.')
    cfg=json_config(project_dir(args.project))
    boxes=json.loads(request['boxes'])
    for box in boxes:
        command('box-add',int(args.project),box)
        existing=cfg.get('jump_boxes',{}).get(box['name'])
        if existing:
            expected={'team_id':str(254 if box['team']==0 else box['team']),'node_name':box['node'],
                      'network_key':box['network'],'cores':box['cores'],'memory_mb':box['memory'],
                      'jump_number':int(box['name'][-2:])}
            if any(existing.get(k)!=v for k,v in expected.items()):
                raise CrangeError('A jump-box declaration changed since enrollment; refusing to overwrite it.')
        else:
            mutate_box(argparse.Namespace(project=args.project,**box,full_clone=None),True,cfg=cfg,save=False)
    validate_project_quota(project_dir(args.project),cfg)
    if boxes:save_config(project_dir(args.project),cfg)
    print(f'Declared {len(boxes)} jump boxes. Review and apply an infrastructure plan to deploy new boxes.')
    cmd_guac_sync(args)
    cmd_guac_users(args)


def cmd_serve(args):
    from crange_server import serve
    try:
        serve(args.host, args.port)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise CrangeError(f"address already in use: {args.host}:{args.port}; choose another port with --port") from exc
        raise CrangeError(f"cannot start dashboard on {args.host}:{args.port}: {exc}") from exc


def cmd_worker(_args):
    from crange_jobs import JobStore
    import threading
    store = JobStore(os.environ.get('CRANGE_JOB_DB', str(Path.home() / '.local/share/crange/jobs.sqlite3')))
    store.worker(threading.Event())


def cmd_network_worker(_args):
    from crange_network_worker import serve
    serve(os.environ.get('CRANGE_JOB_DB', '/var/lib/crange-jobs/jobs.sqlite3'),
          os.environ.get('CRANGE_AUTH_DB', '/var/lib/crange-web/.local/share/crange/auth.sqlite3'))


def cmd_served(args):
    from crange_service import install
    try:
        install(args.user, ROOT)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        raise CrangeError(str(exc)) from exc


def cmd_password(args):
    import getpass
    from crange_auth import AuthStore
    password = getpass.getpass('New password: ')
    if password != getpass.getpass('Confirm password: '):
        raise CrangeError('Passwords do not match.')
    try:
        AuthStore(os.environ.get('CRANGE_AUTH_DB', Path.home() / '.local/share/crange/auth.sqlite3')).set_password(
            args.username, password, admin=args.admin, projects=args.projects, role=args.role)
    except ValueError as exc:
        raise CrangeError(str(exc)) from exc
    print(f'Password set for {args.username}. Existing sessions revoked.')


def cmd_cluster_refresh(args):
    from crange_cluster import refresh_inventory_snapshot
    snapshot = refresh_inventory_snapshot()
    print(f'Cluster inventory refreshed: {len(snapshot.get("nodes", []))} nodes; policy {snapshot.get("policy_revision", "unknown")}.')


def cmd_capture_settings(args):
    """Administrator-only reviewed capture defaults, against live existing stores."""
    from crange_scheduling import CONFIG, read_settings, atomic_json
    with (CONFIG/'capture-settings.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        policy,_=read_settings()
        if args.bridge not in policy.get('isolated_bridges',[]):
            raise CrangeError('Choose an already approved isolated capture bridge.')
        if args.storage not in policy.get('allowed_storages',[]) or args.storage not in ('local-lvm','ssd-lvm','hdd-lvm','crange-thin','local-hdd'):
            raise CrangeError('Choose an administrator-allowlisted thin capture storage ID.')
        if args.backup not in {row[1] for row in policy.get('approved_backup_storage',[])}:
            raise CrangeError('Choose an existing approved shared backup storage ID.')
        nodes=policy.get('allowed_nodes',[])
        if not nodes or not policy.get('enabled'):raise CrangeError('Enable distribution before capture settings.')
        api=ProxmoxAPI();pairs=[];backups=[]
        destinations={row.get('node') for row in api.request('GET','/cluster/resources',{'type':'storage'})
                      if row.get('storage')==args.storage}
        for node in nodes:
            if node not in NODES:raise CrangeError('Capture settings require an enrolled node.')
            archive=api.request('GET',f'/nodes/{node}/storage/{args.backup}/status')
            if (int(archive.get('active',0))!=1 or int(archive.get('shared',0))!=1
                    or 'backup' not in str(archive.get('content','')).split(',')):
                raise CrangeError(f'{node}: capture archive must be existing active shared backup storage.')
            backups.append([node,args.backup])
            if node not in destinations:
                continue
            local=api.request('GET',f'/nodes/{node}/storage/{args.storage}/status')
            if (int(local.get('active',0))!=1 or local.get('type') not in ('lvmthin','lvm')
                    or not {'images','rootdir'}<=set(str(local.get('content','')).split(','))):
                raise CrangeError(f'{node}: capture storage must be active local LVM for managed VM/CT disks.')
            pairs.append([node,args.storage])
        if not pairs:raise CrangeError('No enrolled node has this approved capture storage.')
        policy['approved_storage']=[list(row) for row in sorted(set(map(tuple,policy.get('approved_storage',[])+pairs)))]
        policy['approved_backup_storage']=[list(row) for row in sorted(set(map(tuple,policy.get('approved_backup_storage',[])+backups)))]
        policy.update(capture_isolated_bridge=args.bridge,capture_datastore=args.storage,capture_backup_storage=args.backup)
        policy['revision']='capture-settings-'+hashlib.sha256(json.dumps({k:v for k,v in policy.items() if k!='revision'},sort_keys=True).encode()).hexdigest()[:16]
        atomic_json(CONFIG/'distribution-policy.json',policy)
        print(f'Capture settings saved for {len(pairs)} approved destination(s): isolated {args.bridge}, preferred local {args.storage}, archive {args.backup}. Other nodes retain their approved local fallback; resource reserves and clone verification remain required.')


def cmd_node_backup_prepare(args):
    import socket
    from crange_scheduling import DATA, atomic_json
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
        client.settimeout(240);client.connect('/run/crange-ct-read/node-backup.sock')
        client.sendall(json.dumps({'action':'prepare','node':args.node}).encode()+b'\n');raw=b''
        while not raw.endswith(b'\n') and len(raw)<8192:
            part=client.recv(8192-len(raw))
            if not part:break
            raw+=part
    result=json.loads(raw)
    if result.get('error'):raise CrangeError(str(result['error']).removeprefix('crange: '))
    if result.get('node')!=args.node or result.get('state')!='ready':raise CrangeError('Node backup preparation was not confirmed.')
    atomic_json(DATA/'node-backup-capabilities'/(args.node+'.json'),result,0o600)
    print(json.dumps(result,sort_keys=True))


def cmd_library_grow(args):
    import socket
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
        client.settimeout(240);client.connect('/run/crange-ct-read/library-grow.sock')
        client.sendall(b'{"action":"grow"}\n');raw=b''
        while not raw.endswith(b'\n') and len(raw)<8192:
            chunk=client.recv(8192-len(raw))
            if not chunk:break
            raw+=chunk
    reply=json.loads(raw)
    if reply.get('error'):raise CrangeError(reply['error'])
    print(json.dumps(reply,sort_keys=True))


def cmd_library_resources(args):
    import socket
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
        client.settimeout(300);client.connect('/run/crange-ct-read/socket')
        client.sendall(json.dumps({'command':'range-library-recover' if getattr(args,'recover',False) else 'range-library-resources','node':'proxmox5'}).encode()+b'\n')
        raw=b''
        while not raw.endswith(b'\n') and len(raw)<8192:
            chunk=client.recv(8192-len(raw))
            if not chunk:break
            raw+=chunk
    reply=json.loads(raw)
    if set(reply)!={'value'}:raise CrangeError('Library resource update was not confirmed; inspect host state before retry.')
    print(json.dumps(json.loads(reply['value']),sort_keys=True))


def cmd_capacity_policy_set(args):
    from crange_cluster import expand_capacity_policy
    print(json.dumps(expand_capacity_policy(args.cpu_ratio, args.memory_ratio, args.disk_ratio), sort_keys=True))


def cmd_memory_reserve_set(args):
    from crange_cluster import set_memory_reserve
    print(json.dumps(set_memory_reserve(args.node, args.minimum_mb, args.fraction,
                                       args.expected_revision), sort_keys=True))


def cmd_distribution_inventory(args):
    from crange_scheduling import discover, read_settings
    print(json.dumps(discover(ProxmoxAPI(), read_settings()[0]), indent=2))


def cmd_reconcile_project_reservations(args):
    from crange_scheduling import reconcile_applied_reservations
    path = project_dir(args.project)
    try:
        released = reconcile_applied_reservations(path)
    except ValueError as exc:
        raise CrangeError(str(exc)) from exc
    print(f'Reconciled {len(released)} applied project reservation(s).')
    resume_network_foundations(args, path)


def resume_network_foundations(args, path):
    """Finish an exact proven apply; never resubmit its saved Terraform plan."""
    summaries = path / '.crange-plans'
    directory = Path.home() / '.local/share/crange/plans' / f'project-{int(args.project):03d}'
    from crange_network_foundation import validate_plan
    from crange_scheduling import atomic_json, applied_resources
    for summary_path in sorted(summaries.glob('*.json')):
        try:
            summary = json.loads(summary_path.read_text())
        except PermissionError:
            # Older administrator-owned reviews are outside this worker's
            # authority. Do not broaden permissions or block owned recovery.
            continue
        keys = summary.get('network_foundation')
        if not keys or summary.get('foundation_configured_at'):
            continue
        if summary.get('reservation_state') != 'released' and not summary.get('applied_at'):
            continue
        manifest = directory / (summary_path.stem + '.json')
        record = json.loads(manifest.read_text())
        cfg = json_config(path)
        inputs = path / 'crange.auto.tfvars.json'
        if (record.get('project') != int(args.project) or record.get('plan_id') != summary_path.stem
                or record.get('network_foundation') != keys
                or hashlib.sha256(inputs.read_bytes()).hexdigest() != record.get('inputs_sha256')
                or automation_inputs_digest(path) != record.get('automation_inputs_sha256')):
            raise CrangeError('Interrupted network foundation inputs changed; review its applied state before recovery.')
        plan = directory / (summary_path.stem + '.tfplan')
        if not record.get('applied_at'):
            if not plan.exists() or hashlib.sha256(plan.read_bytes()).hexdigest() != record.get('sha256'):
                raise CrangeError('Interrupted network foundation no longer has its exact reviewed plan.')
            detail = json.loads(tf_run(path, ['show', '-json', str(plan)], capture=True).stdout)
            validate_plan(detail, cfg, keys, record['distribution'])
        actual = applied_resources(path)
        placements = {item['resource_key']: item for item in record['distribution']['placements']}
        api = ProxmoxAPI()
        pool = api.request('GET', '/pools/crange-iac').get('members', [])
        for key in keys:
            net = cfg['custom_networks'][key]
            resource = 'router:custom:' + key
            row = actual.get(resource, {})
            expected = placements[resource]['resolved']
            if row.get('node_name') != expected['node_name'] or not row.get('vm_id'):
                raise CrangeError('Interrupted network foundation router is not at its reviewed node.')
            node, guest = row['node_name'], int(row['vm_id'])
            config = api.request('GET', f'/nodes/{node}/lxc/{guest}/config')
            tasks = api.request('GET', f'/nodes/{node}/tasks', {'source':'active', 'limit':1000})
            vnet = f'p{int(args.project):03d}c{int(net["network_id"]):03d}'
            nic = lambda name: dict(part.split('=',1) for part in str(config.get(name,'')).split(',') if '=' in part)
            disk = (row.get('disk') or [{}])[0]
            request = next(item for item in record['distribution']['requests'] if item['resource_key'] == resource)
            if (config.get('template') or config.get('lock') or not config.get('unprivileged')
                    or not any(member.get('type') == 'lxc' and int(member.get('vmid',0)) == guest for member in pool)
                    or disk.get('datastore_id') != expected['datastore_id'] or disk.get('size') != request['disk_gb']
                    or not {f'p{int(args.project):03d}', vnet} <= set(str(config.get('tags','')).split(';'))
                    or api.request('GET', f'/nodes/{node}/lxc/{guest}/status/current').get('status') != 'running'
                    or not isinstance(tasks,list)
                    or any(str(task.get('id')) == str(guest) for task in tasks)
                    or nic('net0').get('bridge') != f'p{int(args.project):03d}t254'
                    or nic('net0').get('ip') != f'10.{int(args.project)}.254.{net["uplink_last_octet"]}/24'
                    or nic('net0').get('gw') != f'10.{int(args.project)}.254.1'
                    or nic('net1').get('bridge') != vnet
                    or nic('net1').get('ip') != f'10.{int(args.project)}.{net["network_id"]}.1/24'):
                raise CrangeError('Interrupted network foundation live ownership, interfaces or terminal tasks differ.')
        record['applied_at'] = record.get('applied_at') or datetime.now(timezone.utc).isoformat()
        atomic_json(manifest, record, 0o600)
        summary['applied_at'] = record['applied_at']
        atomic_json(summary_path, summary, 0o640)
        plan.unlink(missing_ok=True)
        print('Resuming only the applied custom-router configuration; Terraform is not reapplied.', flush=True)
        recovery_args = argparse.Namespace(project=args.project, plan_id=summary_path.stem)
        cmd_configure_network_foundation(recovery_args, keys)
        stamp = datetime.now(timezone.utc).isoformat()
        record['foundation_configured_at'] = summary['foundation_configured_at'] = stamp
        atomic_json(manifest, record, 0o600)
        atomic_json(summary_path, summary, 0o640)


def cmd_container_cache_acceptance(args):
    from crange_container_cache_acceptance import run
    run(args.project_id,args.image,args.node,args.storage)


def cmd_range_storage(args):
    from crange_range_storage import request
    options = ({'size_gb': args.size_gb} if args.storage_action == 'review' else
               {'plan_id': args.plan_id, 'review_sha256': args.review_sha256})
    print(json.dumps(request(args.storage_action, options), indent=2))


def cmd_capture_inspect(args):
    from crange_capture import inspect_capture
    print(json.dumps(inspect_capture(ProxmoxAPI(), project=int(args.project), version=args.version,
                                    machine=args.name, path=project_dir(args.project)), indent=2))


def cmd_capture_network_prepare(args):
    from crange_capture import private_bridge_request
    from crange_scheduling import read_settings
    policy=read_settings()[0]
    if args.node not in policy.get('allowed_nodes',[]) or policy.get('capture_isolated_bridge')!='crprobe1450':
        raise CrangeError('Choose an enrolled node with the approved fixed capture bridge.')
    print(json.dumps(private_bridge_request(args.node),indent=2))


def cmd_windows_p100_sealed_publish(args):
    from crange_windows_p100_publication import publish
    publish()


def cmd_distribution_prepare(args):
    if bool(getattr(args, 'acceptance_policy', None)) != bool(getattr(args, 'acceptance_job_id', None)):
        raise CrangeError('Isolated acceptance requires both protected policy and explicit job ID.')
    from crange_cache_jobs import prepare_cache
    print(f'Preparing local cache for {args.image} on {args.node}/{args.storage}.', flush=True)
    result = prepare_cache(project_id=int(args.project), image_id=args.image, node_name=args.node,
                  datastore_id=args.storage, owner=os.environ.get('CRANGE_ACTOR', f'cli:{os.getuid()}'),
                  acceptance_policy_path=getattr(args, 'acceptance_policy', None),
                  job_id=getattr(args, 'acceptance_job_id', None))
    print(f'Local cache verified: template {result["template_id"]} on {args.node}/{args.storage}.', flush=True)


def cmd_cache_gc_review(args):
    from crange_cache_jobs import review_cache_gc
    plan_id = review_cache_gc(image_id=args.image, node_name=args.node, template_id=args.template_id,
                             owner=os.environ.get('CRANGE_ACTOR', f'cli:{os.getuid()}'))
    print(f'Cache GC plan ID: {plan_id}. Review the retained dependency/ownership evidence before applying.')


def cmd_cache_gc_apply(args):
    from crange_cache_jobs import apply_cache_gc
    apply_cache_gc(args.plan_id)


def cmd_cache_reconcile(args):
    from crange_cache_jobs import reconcile_cache_job
    result = reconcile_cache_job(args.job_id)
    print('Cache operation reconciled: ' + json.dumps(result, sort_keys=True))


def cmd_cache_resume(args):
    from crange_cache_jobs import resume_cache_template
    result = resume_cache_template(args.job_id)
    print('Cache operation resumed: ' + json.dumps(result, sort_keys=True))


def cmd_cache_snapshot_probe_retry(args):
    from crange_cache_jobs import resume_snapshot_cache
    print(json.dumps(resume_snapshot_cache(args.job_id, replace_probe=True), sort_keys=True))


def cmd_cache_restore_resume(args):
    from crange_cache_restore import resume
    print(json.dumps(resume(args.job_id), sort_keys=True))


def cmd_windows_cache_recovery(args):
    from crange_windows_cache_recovery import reconcile, resume
    if args.action == 'resume':
        result = resume(args.image, args.source_job_id)
    else:
        result = reconcile(args.image, args.source_job_id, action=args.action)
    print('Windows cache recovery: ' + json.dumps(result, sort_keys=True))



def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="crange", description="Manage CRANGE teams, jump boxes, and reviewed Terraform/Ansible workflows.")
    sub = p.add_subparsers(dest="command", required=True)
    from crange_project_lifecycle import cli as lifecycle_cli, OPERATIONS
    for operation in sorted(OPERATIONS):
        q = sub.add_parser(operation, help='Queued project lifecycle management')
        q.add_argument('project'); q.add_argument('--confirm', required=True)
        q.set_defaults(func=lifecycle_cli, lifecycle_operation=operation)
    q = sub.add_parser('cluster-refresh', help='Collect a sanitized worker-owned cluster capacity snapshot'); q.set_defaults(func=cmd_cluster_refresh)
    q = sub.add_parser('capture-settings',help='Admin: configure audited capture storage and isolation defaults')
    q.add_argument('--bridge',required=True);q.add_argument('--storage',required=True);q.add_argument('--backup',required=True);q.set_defaults(func=cmd_capture_settings)
    q = sub.add_parser('cluster-capacity-policy-set', help='Admin: set bounded CPU/RAM/disk commitment ceilings')
    q.add_argument('--cpu-ratio', type=float, required=True); q.add_argument('--memory-ratio', type=float, required=True)
    q.add_argument('--disk-ratio', type=float, required=True); q.set_defaults(func=cmd_capacity_policy_set)
    q = sub.add_parser('container-cache-acceptance', help='Admin: verify isolated container restore and linked cache support')
    q.add_argument('--project-id',type=int,required=True);q.add_argument('--image',required=True)
    q.add_argument('--node',choices=sorted(NODES),required=True);q.add_argument('--storage',required=True)
    q.set_defaults(func=cmd_container_cache_acceptance)
    q = sub.add_parser('node-backup-prepare',help='Admin: prepare native template backups on an enrolled node');q.add_argument('--node',required=True,choices=sorted(NODES));q.set_defaults(func=cmd_node_backup_prepare)
    q = sub.add_parser('library-grow',help='Admin: grow the fixed NFS data volume by 64 GiB online');q.set_defaults(func=cmd_library_grow)
    q = sub.add_parser('library-resources',help='Admin: raise the fixed NFS library resource floor online')
    q.add_argument('--recover',action='store_true');q.set_defaults(func=cmd_library_resources)
    q = sub.add_parser('range-storage-review', help='Admin: review unused P5 HDD thin storage creation')
    q.add_argument('--size-gb', type=int, choices=(256,512), required=True)
    q.set_defaults(func=cmd_range_storage, storage_action='review')
    q = sub.add_parser('range-storage-apply', help='Admin: apply the exact reviewed storage plan')
    q.add_argument('--plan-id', required=True); q.add_argument('--review-sha256', required=True)
    q.set_defaults(func=cmd_range_storage, storage_action='apply')
    q = sub.add_parser('cluster-memory-reserve-set', help='Admin: edit one node’s host RAM admission reserve')
    q.add_argument('--node', required=True); q.add_argument('--minimum-mb', type=int, required=True)
    q.add_argument('--fraction', type=float, required=True); q.add_argument('--expected-revision', required=True)
    q.set_defaults(func=cmd_memory_reserve_set)
    q = sub.add_parser('distribution-inventory', help='Read all cluster workloads and conservative placement capacity')
    q.set_defaults(func=cmd_distribution_inventory)
    q = sub.add_parser('reconcile-project-reservations', help='Verify applied guests and release retained apply reservations')
    q.add_argument('project'); q.set_defaults(func=cmd_reconcile_project_reservations)
    q = sub.add_parser('capture-inspect', help='Inspect an owned retained Windows capture with fixed guest diagnostics')
    q.add_argument('project'); q.add_argument('version'); q.add_argument('name'); q.set_defaults(func=cmd_capture_inspect)
    q = sub.add_parser('capture-network-prepare', help='Admin: prepare fixed host isolation on a capture bridge')
    q.add_argument('--node',required=True);q.set_defaults(func=cmd_capture_network_prepare)
    q = sub.add_parser('windows-p100-sealed-publish', help='Admin: resume and publish the fixed sealed P100 Windows image')
    q.set_defaults(func=cmd_windows_p100_sealed_publish)
    q = sub.add_parser('distribution-prepare', help='Prepare and verify an approved immutable image replica')
    q.add_argument('image', type=lambda value: value if re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', value) else (_ for _ in ()).throw(argparse.ArgumentTypeError('Invalid image ID')))
    q.add_argument('--project', required=True, type=int, help='Authorized project whose approved image/cache quota is charged')
    q.add_argument('--node', required=True, choices=sorted(NODES))
    q.add_argument('--storage', required=True, choices=['local-lvm', 'ssd-lvm', 'hdd-lvm', 'crange-thin', 'local-hdd'])
    q.add_argument('--acceptance-policy', type=Path, help='Admin-only root-owned one-node isolated CT cache policy')
    q.add_argument('--acceptance-job-id', type=lambda v:v if re.fullmatch(r'[a-zA-Z0-9-]{1,64}',v) else (_ for _ in ()).throw(argparse.ArgumentTypeError('Invalid acceptance job ID')))
    q.set_defaults(func=cmd_distribution_prepare)
    q = sub.add_parser('cache-gc-review', help='Admin: retain an exact local-cache eviction review')
    q.add_argument('image'); q.add_argument('--node', required=True, choices=sorted(NODES)); q.add_argument('--template-id',required=True,type=int); q.set_defaults(func=cmd_cache_gc_review)
    q = sub.add_parser('cache-gc-apply', help='Admin: apply an exact retained local-cache eviction review')
    q.add_argument('plan_id',type=lambda v:v if re.fullmatch(r'[0-9a-f]{32}',v) else (_ for _ in ()).throw(argparse.ArgumentTypeError('Invalid cache GC plan ID'))); q.set_defaults(func=cmd_cache_gc_apply)
    q = sub.add_parser('cache-reconcile', help='Admin: reconcile one exact cache preparation journal and reservation')
    q.add_argument('job_id',type=lambda v:v if re.fullmatch(r'[a-zA-Z0-9-]{1,64}',v) else (_ for _ in ()).throw(argparse.ArgumentTypeError('Invalid cache job ID'))); q.set_defaults(func=cmd_cache_reconcile)
    q = sub.add_parser('cache-resume', help='Admin: resume one exact restored cache template through probe verification')
    q.add_argument('job_id',type=lambda v:v if re.fullmatch(r'cache-[a-zA-Z0-9-]{1,64}',v) else (_ for _ in ()).throw(argparse.ArgumentTypeError('Invalid cache job ID'))); q.set_defaults(func=cmd_cache_resume)
    q = sub.add_parser('cache-snapshot-probe-retry', help='Admin: replace only an exact failed snapshot probe, preserving its restored cache')
    q.add_argument('job_id'); q.set_defaults(func=cmd_cache_snapshot_probe_retry)
    q = sub.add_parser('cache-restore-resume', help='Admin: verify an exact completed archive restore and publish its cache')
    q.add_argument('job_id'); q.set_defaults(func=cmd_cache_restore_resume)
    q = sub.add_parser('cache-windows-p100-recovery', help='Admin: resume or reconcile the exact sealed P100 Windows cache job')
    q.add_argument('image', choices=['mhl-windows-server-2025-base-p100-v1'])
    q.add_argument('--source-job-id', required=True, type=lambda v:v if re.fullmatch(r'cache-[a-zA-Z0-9-]{1,64}',v) else (_ for _ in ()).throw(argparse.ArgumentTypeError('Invalid protected cache job ID')))
    q.add_argument('--action', required=True, choices=['resume', 'cleanup'])
    q.set_defaults(func=cmd_windows_cache_recovery)
    q = sub.add_parser('serve', help='Serve dashboard and API on port 8000')
    q.add_argument('--host', default='127.0.0.1')
    q.add_argument('--port', type=int, default=8000)
    q.set_defaults(func=cmd_serve)
    q = sub.add_parser('worker', help='Run the controller job worker')
    q.set_defaults(func=cmd_worker)
    q = sub.add_parser('network-worker', help='Run the privileged fixed network verification worker')
    q.set_defaults(func=cmd_network_worker)
    q = sub.add_parser('served', help='Install and enable the dashboard system service')
    q.add_argument('--user', default='crange', help='Unprivileged service account')
    q.set_defaults(func=cmd_served)
    q = sub.add_parser('password', help='Set a dashboard account password interactively')
    q.add_argument('username')
    q.add_argument('--admin', action='store_true')
    q.add_argument('--projects', nargs='*', default=[])
    q.add_argument('--role', choices=['participant', 'manager'], default=None,
                   help='Project access role; new accounts default to participant, existing roles are preserved')
    q.set_defaults(func=cmd_password)
    q = sub.add_parser('jumpbox-template-save',help='Save a project-scoped reusable jump-box template')
    q.add_argument('project');q.add_argument('--definition',required=True)
    q.set_defaults(func=lambda a: __import__('crange_jumpboxes').save_preset(a.project,json.loads(a.definition)))
    q = sub.add_parser('jumpbox-template-remove',help='Remove a preset without changing existing boxes or images')
    q.add_argument('project');q.add_argument('key')
    q.set_defaults(func=lambda a: __import__('crange_jumpboxes').remove_preset(a.project,a.key))
    q = sub.add_parser('custom-jumpbox-create', help='Declare a VM/container jump box from an approved template')
    q.add_argument('project');q.add_argument('--definition',required=True)
    q.set_defaults(func=lambda a: __import__('crange_jumpboxes').create(a.project,json.loads(a.definition)))
    q = sub.add_parser('template-create', help='Admin ISO installer with reviewed hardware settings')
    q.add_argument('--definition', required=True)
    q.set_defaults(func=lambda a: __import__('crange_templates').create(json.loads(a.definition)))
    q = sub.add_parser('template-cancel', help='Cancel one unfinished administrator installer build')
    q.add_argument('build_id')
    q.set_defaults(func=lambda a: __import__('crange_templates').cancel(a.build_id))
    q = sub.add_parser('template-start-installer', help='Answer the DVD boot prompt for a fresh owned Windows installer')
    q.add_argument('build_id')
    q.set_defaults(func=lambda a: __import__('crange_windows_installer').start(a.build_id))
    q = sub.add_parser('template-windows-base-prepare', help='Run the fixed Windows workgroup base preparation for an eligible owned installer')
    q.add_argument('build_id'); q.set_defaults(func=cmd_template_windows_base_prepare)
    q = sub.add_parser('template-windows-base-seal', help='Run fixed Sysprep on a prepared owned Windows installer build')
    q.add_argument('build_id'); q.set_defaults(func=cmd_template_windows_base_seal)
    q = sub.add_parser('template-publish', help='Publish a prepared installer as an approved immutable template')
    q.add_argument('build_id'); q.add_argument('--projects', type=int, nargs='+', required=True); q.add_argument('--prepared', action='store_true')
    q.set_defaults(func=lambda a: __import__('crange_templates').publish(a.build_id,a.projects,a.prepared))
    q = sub.add_parser('template-base-prepare', help='Admin: publish a maintained Linux base through isolated clone acceptance')
    q.add_argument('base', choices=sorted(__import__('crange_platform_bases').BASES))
    q.set_defaults(func=lambda a: __import__('crange_platform_bases').prepare(a.base))
    q = sub.add_parser('template-approve-image', help='Link an already verified, platform-approved VM image into selected projects')
    q.add_argument('image_id'); q.add_argument('--projects', type=int, nargs='+', required=True)
    q.set_defaults(func=lambda a: __import__('crange_templates').approve_catalog_image(a.image_id,a.projects))
    q = sub.add_parser('template-upload', help='Transfer a completed ISO upload to node storage')
    q.add_argument('upload_id'); q.add_argument('--node', required=True, choices=sorted(NODES))
    q.set_defaults(func=lambda a: __import__('crange_template_media').transfer(a.upload_id,a.node))
    q = sub.add_parser("projects", help="List Terraform project environments"); q.set_defaults(func=cmd_projects)
    q = sub.add_parser("project-init", help="Create a new standardized project environment from project-template")
    q.add_argument("project"); q.set_defaults(func=cmd_project_init)
    q = sub.add_parser("bootstrap-plan", help="Create a reviewed router-only foundation plan on an enrolled node")
    q.add_argument("project"); q.set_defaults(func=cmd_bootstrap_plan)
    q = sub.add_parser("infra-bootstrap-apply", help="Apply one exact reviewed router-only bootstrap plan")
    q.add_argument("project"); q.add_argument("--yes", action="store_true", required=True); q.add_argument("--plan-id", required=True)
    q.set_defaults(func=cmd_bootstrap_apply)
    q = sub.add_parser("bootstrap-configure", help="Configure routers after an applied bootstrap plan")
    q.add_argument("project"); q.set_defaults(func=cmd_bootstrap_configure)
    q = sub.add_parser("project-show", help="Show declared and applied project resources"); q.add_argument("project"); q.set_defaults(func=cmd_project_show)
    q = sub.add_parser("guac-sync", help="Render reviewed Guacamole access desired state"); q.add_argument("project"); q.set_defaults(func=cmd_guac_sync)
    q = sub.add_parser("guac-apply", help="Apply a Guacamole access manifest through the configured connector"); q.add_argument("project"); q.set_defaults(func=cmd_guac_apply)
    q = sub.add_parser("guac-refresh", help="Refresh deployed connections and project access"); q.add_argument("project"); q.set_defaults(func=cmd_guac_refresh)
    q = sub.add_parser('guac-box-control', help='Control one applied, project-owned Guacamole box')
    q.add_argument('project'); q.add_argument('resource_key'); q.add_argument('action', choices=('start', 'shutdown', 'stop', 'restart'))
    q.set_defaults(func=lambda a: __import__('crange_guac_boxes').power(a.project, a.resource_key, a.action))
    q = sub.add_parser('guac-snapshot', help='Manage snapshots of one applied, project-owned Guacamole box')
    q.add_argument('project'); q.add_argument('resource_key')
    q.add_argument('action', choices=('list', 'create', 'restore', 'delete'))
    q.add_argument('--snapshot', default='')
    q.set_defaults(func=lambda a: __import__('crange_guac_boxes').snapshots(a.project, a.resource_key, a.action, a.snapshot))
    q = sub.add_parser("guac-users", help="Apply project-owned Guacamole accounts and connection permissions"); q.add_argument("project"); q.set_defaults(func=cmd_guac_users)
    q = sub.add_parser("guac-config", help="Configure the protected Guacamole connector credentials"); q.set_defaults(func=cmd_guac_config)
    q = sub.add_parser("guac-json-key", help="Generate the shared Guacamole JSON-auth key without displaying it"); q.set_defaults(func=cmd_guac_json_key)
    q = sub.add_parser("guac-ticket-broker", help=argparse.SUPPRESS); q.set_defaults(func=cmd_guac_ticket_broker)
    for verb, add in (("team-add", True), ("team-remove", False)):
        q = sub.add_parser(verb); q.add_argument("project"); q.add_argument("team", type=int); q.set_defaults(func=lambda a, add=add: mutate_team(a, add))
    for verb, add in (("network-add", True), ("network-remove", False)):
        q = sub.add_parser(verb, help="Create or remove a routed custom /24 network")
        q.add_argument("project"); q.add_argument("name")
        if add:
            q.add_argument("--team", type=int, required=True, help="Parent team, or 0 for staging")
            q.add_argument("--id", type=int, required=True, help="Unused project subnet number 1-253")
            q.add_argument("--node", default="auto", choices=sorted(NODES | {"auto"}), help="Legacy explicit node pin; defaults to Automatic"); q.add_argument("--preferred-node", default="", choices=["",*sorted(NODES)], help="Soft suggestion with Automatic")
        q.set_defaults(func=lambda a, add=add: mutate_custom_network(a, add))
    q = sub.add_parser("network-edit", help="Edit a custom routed network's subnet, router node, uplink, and size")
    q.add_argument("project"); q.add_argument("name"); q.add_argument("--id", type=int, required=True)
    q.add_argument("--node", default="auto", choices=sorted(NODES | {"auto"}), help="Legacy explicit node pin; defaults to Automatic"); q.add_argument("--preferred-node", default=None, choices=["",*sorted(NODES)], help="Soft suggestion with Automatic"); q.add_argument("--uplink", type=int, required=True)
    q.add_argument("--cores", type=int, required=True); q.add_argument("--memory", type=int, required=True)
    q.set_defaults(func=cmd_network_edit)
    q = sub.add_parser('box-users', help='Manage Linux accounts on one deployed project box')
    q.add_argument('project'); q.add_argument('name'); q.add_argument('--kind', choices=('jumpbox','vm','container'), required=True)
    q.add_argument('--action', choices=('list','create','delete','grant-sudo','revoke-sudo','password','access'), required=True)
    q.add_argument('--username', default=''); q.add_argument('--sealed-password', default='')
    q.set_defaults(func=lambda a: __import__('crange_box_users').execute(a))
    q = sub.add_parser("jumpbox-settings", help="Name a box, set its jump-box role and select Guacamole services")
    q.add_argument('project'); q.add_argument('name'); q.add_argument('--kind', choices=('jumpbox','vm','container'), required=True)
    q.add_argument('--display-name', required=True); q.add_argument('--is-jumpbox', choices=('true','false'), required=True)
    q.add_argument('--services', required=True, help='JSON array of ssh, rdp and vnc services')
    q.add_argument('--credentials-mode', choices=('keep','default','custom'), default='keep')
    q.add_argument('--credential-ref', default=''); q.add_argument('--sealed-credentials', default='')
    q.set_defaults(func=cmd_jumpbox_settings)
    q = sub.add_parser("box-list"); q.add_argument("project"); q.set_defaults(func=cmd_boxes)
    q = sub.add_parser("box-state", help="Set desired jump box power state"); q.add_argument("project"); q.add_argument("name"); q.add_argument("state", choices=("start", "stop")); q.set_defaults(func=cmd_box_state)
    for verb, add in (("box-add", True), ("box-remove", False)):
        q = sub.add_parser(verb); q.add_argument("project"); q.add_argument("name")
        if add:
            q.add_argument("--team", type=int, required=True); q.add_argument("--network", help="Custom network key; defaults to the team's standard LAN"); q.add_argument("--node", default="auto", choices=sorted(NODES | {"auto"}), help="Legacy explicit node pin; defaults to Automatic"); q.add_argument("--preferred-node", default=None, choices=["",*sorted(NODES)], help="Soft suggestion with Automatic"); q.add_argument("--cores", type=int, default=2); q.add_argument("--memory", type=int, default=4096, help="RAM in MB"); q.add_argument("--full-clone", action="store_true", default=None, help="Use a full clone (required when deploying away from the template node)")
        q.set_defaults(func=lambda a, add=add: mutate_box(a, add))
    q = sub.add_parser("release-configure", help="Resume configuration and health checks for an already applied release")
    q.add_argument("project"); q.add_argument("--teams", nargs="+", required=True); q.add_argument("--yes", action="store_true"); q.set_defaults(func=cmd_release_configure)
    q = sub.add_parser("release-stage", help="Prepare images and review only selected declared releases")
    q.add_argument("project"); q.add_argument("--teams", nargs="+", required=True); q.set_defaults(func=cmd_release_stage)
    q = sub.add_parser("stage-changes", help="Prepare approved local caches and retain an exact reviewed guest plan"); q.add_argument("project"); q.set_defaults(func=cmd_stage_changes)
    q = sub.add_parser("infra-plan", help="Initialize, plan, and summarize project changes"); q.add_argument("project"); q.add_argument("--retain", action="store_true", help="Keep plan for exact reviewed apply"); q.add_argument("--replace-sdn-applier", action="store_true", help="Repair a partially applied SDN network by reapplying cluster SDN"); q.set_defaults(func=cmd_plan)
    q = sub.add_parser("infra-apply", help="Apply the exact reviewed plan"); q.add_argument("project"); q.add_argument("--yes", action="store_true"); q.add_argument("--plan-id", help="Apply a saved, integrity-checked plan"); q.add_argument("--allow-destroy", action="store_true"); q.add_argument("--allow-replace", action="store_true"); q.set_defaults(func=cmd_apply)
    q = sub.add_parser("stage-deploy", help="Apply the reviewed staging plan and configure routes, routers, and jump boxes")
    q.add_argument("project"); q.add_argument("--yes", action="store_true"); q.add_argument("--plan-id", required=True)
    q.add_argument("--allow-destroy", action="store_true"); q.add_argument("--allow-replace", action="store_true"); q.set_defaults(func=cmd_stage_deploy)
    q = sub.add_parser("configure-team", help="Configure one team's routers and jump boxes without touching other teams")
    q.add_argument("project"); q.add_argument("team", type=int, help="Team number, or 0 for staging")
    q.add_argument("--yes", action="store_true"); q.set_defaults(func=cmd_configure_team)
    q = sub.add_parser("mhl-business-configure", help="Configure fixed Project 100 MHL business services in staging or one selected team")
    q.add_argument("project"); q.add_argument("--network-id", type=int, choices=(254, 1, 2, 3, 4, 5), required=True)
    q.add_argument("--yes", action="store_true"); q.set_defaults(func=cmd_mhl_business_configure)
    q = sub.add_parser("mhl-windows-configure", help="Configure fixed Project 100 MHL domain and file services in staging or one selected team")
    q.add_argument("project"); q.add_argument("--network-id", type=int, choices=(254, 1, 2, 3, 4, 5), required=True)
    q.add_argument("--yes", action="store_true"); q.set_defaults(func=cmd_mhl_windows_configure)
    q = sub.add_parser("inventory"); q.add_argument("project"); q.set_defaults(func=cmd_inventory)
    q = sub.add_parser('route-sync', help='Ensure the applied project aggregate route without firewall changes')
    q.add_argument('project'); q.set_defaults(func=cmd_route_sync)
    q = sub.add_parser("configure", help="Apply reviewed Ansible routes, routers, boxes, or Windows access baseline")
    q.add_argument("project"); q.add_argument("target", choices=("routes", "routers", "boxes", "windows", "all")); q.add_argument("--yes", action="store_true"); q.set_defaults(func=cmd_configure)
    q = sub.add_parser("machine-state", help="Start or gracefully stop one deployed scenario VM or container")
    q.add_argument("project"); q.add_argument("name"); q.add_argument("--kind", choices=("vm", "container"), required=True)
    q.add_argument("--state", choices=("start", "stop"), required=True); q.set_defaults(func=cmd_machine_state)
    q = sub.add_parser("scenario-list"); q.add_argument("project"); q.set_defaults(func=cmd_scenario_list)
    q = sub.add_parser("scenario-add", help="Declare a scenario VM in staging or a project team")
    q.add_argument("project"); q.add_argument("name"); q.add_argument("--team", type=int, required=True, help="Team number, or 0 for staging")
    q.add_argument("--ip", type=int, required=True, help="Static host octet .2-.100")
    q.add_argument("--template", required=True); q.add_argument("--node", default="auto", choices=sorted(NODES | {"auto"}), help="Legacy explicit node pin; defaults to Automatic"); q.add_argument("--preferred-node", default="", choices=["",*sorted(NODES)], help="Soft suggestion with Automatic")
    q.add_argument("--cores", type=int, default=2); q.add_argument("--memory", type=int, default=4096)
    q.add_argument("--full-clone", action="store_true", default=None)
    q.add_argument("--network", default="", help="Custom network owned by this team or staging")
    q.add_argument("--health", default="[]", help="JSON ping/TCP/HTTP health checks; empty uses ping")
    q.add_argument("--guac-protocol", choices=("ssh", "rdp", "vnc"), default="ssh")
    q.add_argument("--guac-protocols", default=None, help="JSON list of selected SSH/RDP/VNC services")
    q.set_defaults(func=cmd_scenario_add)
    q = sub.add_parser("scenario-edit", help="Edit a declared scenario VM and its health checks")
    q.add_argument("project"); q.add_argument("name")
    q.add_argument("--ip", type=int); q.add_argument("--network", default=None, help="Custom network; empty restores standard team subnet")
    q.add_argument("--template", required=True); q.add_argument("--node", default="auto", choices=sorted(NODES | {"auto"}), help="Legacy explicit node pin; defaults to Automatic"); q.add_argument("--preferred-node", default=None, choices=["",*sorted(NODES)], help="Soft suggestion with Automatic")
    q.add_argument("--cores", type=int, required=True); q.add_argument("--memory", type=int, required=True)
    q.add_argument("--health", required=True, help="JSON ping/TCP/HTTP health checks; empty uses ping")
    q.add_argument("--guac-protocol", choices=("ssh", "rdp", "vnc"), default=None)
    q.add_argument("--guac-protocols", default=None, help="JSON list of selected SSH/RDP/VNC services")
    q.set_defaults(func=cmd_scenario_edit)
    q = sub.add_parser("scenario-remove"); q.add_argument("project"); q.add_argument("name"); q.set_defaults(func=cmd_scenario_remove)
    q = sub.add_parser("container-add", help="Declare a scenario CT from an approved template")
    q.add_argument("project"); q.add_argument("name"); q.add_argument("--team", type=int, required=True)
    q.add_argument("--ip", type=int, required=True); q.add_argument("--template", required=True)
    q.add_argument("--node", default="auto", choices=sorted(NODES | {"auto"}), help="Legacy explicit node pin; defaults to Automatic"); q.add_argument("--preferred-node", default="", choices=["",*sorted(NODES)], help="Soft suggestion with Automatic"); q.add_argument("--network", help="Custom network key")
    q.add_argument("--cores", type=int, default=1)
    q.add_argument("--memory", type=int, default=1024); q.add_argument("--health", default="[]", help="JSON ping/TCP/HTTP readiness checks; empty uses ping"); q.add_argument("--guac-protocol", choices=("ssh", "rdp", "vnc"), default="ssh"); q.add_argument("--guac-protocols", default=None, help="JSON list of selected SSH/RDP/VNC services"); q.set_defaults(func=cmd_container_add)
    q = sub.add_parser("container-edit", help="Edit a declared scenario CT and its health checks")
    q.add_argument("project"); q.add_argument("name"); q.add_argument("--template", required=True)
    q.add_argument("--node", default="auto", choices=sorted(NODES | {"auto"}), help="Legacy explicit node pin; defaults to Automatic"); q.add_argument("--preferred-node", default=None, choices=["",*sorted(NODES)], help="Soft suggestion with Automatic"); q.add_argument("--ip", type=int, required=True)
    q.add_argument("--network", default="", help="Custom network key, or empty for the team's standard subnet")
    q.add_argument("--cores", type=int, required=True); q.add_argument("--memory", type=int, required=True)
    q.add_argument("--health", required=True, help="JSON ping/TCP/HTTP health checks; empty uses ping"); q.add_argument("--guac-protocol", choices=("ssh", "rdp", "vnc"), default=None); q.add_argument("--guac-protocols", default=None, help="JSON list of selected SSH/RDP/VNC services")
    q.set_defaults(func=cmd_container_edit)
    q = sub.add_parser("container-remove"); q.add_argument("project"); q.add_argument("name"); q.set_defaults(func=cmd_container_remove)
    q = sub.add_parser("container-health"); q.add_argument("project"); q.add_argument("name", nargs="?"); q.set_defaults(func=cmd_container_health)
    q = sub.add_parser("scenario-health"); q.add_argument("project"); q.add_argument("name", nargs="?"); q.set_defaults(func=cmd_scenario_health)
    q = sub.add_parser("release-capture"); q.add_argument("project"); q.add_argument("version"); q.add_argument("--machine", action="append", default=[], help="Capture only these explicit staging scenario keys; repeat for multiple machines"); q.add_argument("--boot-order", action="append", default=[], help="Comma-separated staging VM start order; repeat to record permutations"); q.set_defaults(func=cmd_release_capture)
    q = sub.add_parser('release-recipe', help='Publish configured service changes with parent disk images'); q.add_argument('project'); q.add_argument('version'); q.add_argument('--parent-version', required=True); q.set_defaults(func=cmd_release_recipe)
    q = sub.add_parser('release-publish', help='Publish a captured release image from completed protected distribution journals')
    q.add_argument('project'); q.add_argument('version'); q.add_argument('template')
    q.add_argument('--export-job', required=True); q.add_argument('--probe-job', required=True)
    q.set_defaults(func=cmd_release_publish)
    q = sub.add_parser("release-show"); q.add_argument("project"); q.add_argument("version"); q.set_defaults(func=cmd_release_show)
    q = sub.add_parser("release-deploy", help="Declare fresh release clones for selected teams")
    q.add_argument("project"); q.add_argument("version"); q.add_argument("--teams", type=int, nargs="+", required=True); q.add_argument("--team-override", action="append", default=[], help="TEAM:BOX:FIELD=VALUE; fields: ip_last_octet, cores, memory_mb, node_name, started"); q.set_defaults(func=cmd_release_deploy)
    for action in ('upgrade', 'rollback'):
        q = sub.add_parser('release-' + action, help='Declare an explicit selected-team release ' + action)
        q.add_argument('project'); q.add_argument('version'); q.add_argument('--teams', type=int, nargs='+', required=True)
        q.add_argument('--from-version', required=True, help='Expected current release version')
        q.add_argument('--team-override', action='append', default=[])
        q.set_defaults(func=cmd_release_deploy, release_action=action)
    q = sub.add_parser('release-apply', help='Apply a reviewed release plan and configure/check selected teams')
    q.add_argument('project'); q.add_argument('--teams', type=int, nargs='+', required=True)
    q.add_argument('--plan-id', required=True); q.add_argument('--yes', action='store_true')
    q.add_argument('--allow-destroy', action='store_true'); q.add_argument('--allow-replace', action='store_true')
    q.set_defaults(func=cmd_release_apply)
    q = sub.add_parser('release-check', help='Verify the applied release on selected teams')
    q.add_argument('project'); q.add_argument('--teams', type=int, nargs='+', required=True); q.set_defaults(func=cmd_release_check)
    q = sub.add_parser("release-test-prepare", help="Declare fresh Team 0 release test clones")
    q.add_argument("project"); q.add_argument("version"); q.set_defaults(func=cmd_release_test_prepare)
    q = sub.add_parser("guac-enroll"); q.add_argument("project"); q.add_argument("request_id"); q.set_defaults(func=cmd_guac_enroll)
    q = sub.add_parser("release-test-check"); q.add_argument("project"); q.add_argument("version"); q.set_defaults(func=cmd_release_test_check)
    q = sub.add_parser("release-test-cleanup"); q.add_argument("project"); q.add_argument("version"); q.set_defaults(func=cmd_release_test_cleanup)
    return p


def main() -> int:
    args = make_parser().parse_args()
    try:
        args.func(args)
    except CrangeError as exc:
        print(f"crange: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Keep the CLI usable in terminals and service logs: report the
        # actionable exception without dumping an implementation traceback.
        message = str(exc).strip() or exc.__class__.__name__
        print(f"crange: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
