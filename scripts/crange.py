#!/usr/bin/env python3
"""Safe, project-scoped command line interface for the CRANGE infrastructure."""
from __future__ import annotations

import argparse
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
    return json.loads(run(["terraform", "output", "-json"], cwd=path, capture=True).stdout)


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
    return run(command, cwd=path, capture=capture, env=terraform_env(), timeout=1800)


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
        deployed = {kind: outputs.get(kind, {}).get('value', {}) or {}
                    for kind in ('jump_boxes', 'scenario_boxes', 'scenario_containers')}
    except CrangeError:
        deployed = {kind: {} for kind in ('jump_boxes', 'scenario_boxes', 'scenario_containers')}

    connections = []
    project_id = int(cfg['project_id'])
    defaults = {}
    for name, box in sorted(cfg.get('jump_boxes', {}).items()):
        team = str(box['team_id'])
        number = int(box['jump_number'])
        if team not in defaults or number < defaults[team][0]:
            defaults[team] = (number, name)
    for name, box in sorted(cfg.get('jump_boxes', {}).items()):
        live = deployed['jump_boxes'].get(name, {})
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
            connections.extend(connection_records(project_id, kind, name, box, address))

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


def cmd_project_init(args: argparse.Namespace) -> None:
    project = int(args.project)
    if project < 1 or project > 255 or project in (60, 61):
        raise CrangeError("Project ID must be 1–255, excluding reserved 60 and 61.")
    if not re.fullmatch(r"10\.60\.2\.(?:[2-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-4])/24", args.transit):
        raise CrangeError("Transit address must be a host from 10.60.2.2 through 10.60.2.254 with /24.")
    for existing in sorted(ENVS.glob("project-*")):
        if not existing.is_dir() or existing.name == "project-template":
            continue
        try:
            other = json_config(existing)
        except CrangeError:
            continue
        if other.get("transit_address") == args.transit:
            raise CrangeError(f"Transit address {args.transit} is already assigned to project {int(other['project_id']):03d}.")
    target = ENVS / f"project-{project:03d}"
    if target.exists():
        raise CrangeError(f"{target.name} already exists; refusing to overwrite it or its state.")
    template = ENVS / "project-template"
    target.mkdir()
    for filename in ("main.tf", "template-profile.json", "approved-scenario-templates.json", "approved-container-templates.json"):
        source = template / filename
        if not source.is_file():
            raise CrangeError(f"Missing project template file: {source}")
        (target / filename).write_bytes(source.read_bytes())
    (target / "terraform.tfvars.example").write_text(
        f'project_id = {project}\ntransit_address = "{args.transit}"\nteam_ids = ["1"]\n'
    )
    save_config(target, {"project_id": project, "transit_address": args.transit, "router_node":"auto", "router_placement":{"mode":"auto","preferred_node":""}, "team_ids": ["1"], "jump_boxes": {}, "custom_networks": {}, "scenario_containers": {}})
    print(f"Created {target.name} with Team 1 and Team 0 staging. Review transit address, then run: crange infra-plan {project:03d}")


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
    if args.node != net.get('node_name'):
        net['placement']=__import__('crange_scheduling').placement_intent(args.node)
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


def cmd_stage_changes(args: argparse.Namespace) -> None:
    """Fixed project workflow: audited cache preparation, then exact guest plan."""
    from crange_scheduling import read_settings, required_preparations
    path = project_dir(args.project)
    cfg = json_config(path)
    policy, _ = read_settings()
    if policy.get('enabled'):
        from crange_cache_jobs import prepare_cache
        fills = required_preparations(path, cfg)
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
    cmd_plan(argparse.Namespace(project=args.project, retain=True, replace_sdn_applier=False))


def cmd_plan(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    from crange_scheduling import prepare, tf_overrides, print_review, abandon_plan
    plan_id = os.urandom(16).hex()
    try:
        placement = prepare(path, cfg, plan_id, os.environ.get('CRANGE_ACTOR', f'cli:{os.getuid()}'))
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
            if placement:
                overrides = Path(tmp) / 'placements.tfvars.json'
                overrides.write_text(json.dumps(tf_overrides(placement)))
                plan_args += ['-var-file', str(overrides)]
            if getattr(args, 'replace_sdn_applier', False):
                plan_args.append('-replace=proxmox_sdn_applier.apply')
            tf_run(path, plan_args)
            detail = json.loads(tf_run(path, ["show", "-json", str(plan)], capture=True).stdout)
            changes = []
            for resource in detail.get("resource_changes", []):
                actions = resource.get("change", {}).get("actions", [])
                if actions != ["no-op"]:
                    changes.append((resource.get("address", "?"), actions))
            from crange_staging import staging_blockers
            blockers = staging_blockers(detail, json_config(path))
            print("\nPlan review:")
            if not changes:
                print("No resource changes.")
            for address, actions in changes:
                print(f"  {address}: {' -> '.join(actions)}")
            if blockers:
                print("Staging deployment blocked: " + ", ".join(blockers))
            if any("delete" in actions for _, actions in changes):
                print("This plan deletes or replaces resources; inspect the Terraform plan above before applying.")
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
                manifest = directory / f'{plan_id}.json'
                fd = os.open(manifest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'w') as stream:
                    json.dump(record, stream, indent=2)
                print(f'Plan ID: {plan_id}')
                print(f'Inputs SHA-256: {inputs}')

                from crange_scheduling import atomic_json
                atomic_json(path / '.crange-plans' / f'{plan_id}.json', {k:record[k] for k in ('plan_id','created','changes','placements','reservation_ids','policy_revision')},0o640)
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
    if getattr(args, 'staging_only', False):
        detail = json.loads(tf_run(path, ['show', '-json', str(plan)], capture=True).stdout)
        enforce_staging_plan(detail, json_config(path))
    if getattr(args, 'release_teams', None):
        from crange_rollouts import plan_blockers
        detail = json.loads(tf_run(path, ['show', '-json', str(plan)], capture=True).stdout)
        blockers = plan_blockers(detail, json_config(path), args.release_teams)
        if blockers:
            raise CrangeError('Release deployment cannot change staging, unselected teams or unowned resources: ' + ', '.join(blockers))
    changes = record.get('changes', [])
    destructive = [item['address'] for item in changes if 'delete' in item.get('actions', [])]
    replacements = [item['address'] for item in changes if 'delete' in item.get('actions', []) and 'create' in item.get('actions', [])]
    if destructive and not args.allow_destroy:
        raise CrangeError('Saved plan destroys resources: ' + ', '.join(destructive))
    if replacements and not args.allow_replace:
        raise CrangeError('Saved plan replaces resources: ' + ', '.join(replacements))
    from crange_scheduling import begin_apply, finish_apply, node_locks
    placement = record.get('distribution')
    nodes = {p['resolved']['node_name'] for p in (placement or {}).get('placements', [])}
    with node_locks(nodes):
        begin_apply(path, placement)
        tf_run(path, ['apply', '-parallelism=1', '-input=false', str(plan)])
        finish_apply(path, placement, succeeded=True)
    record_release_apply(path)
    plan.unlink()
    record['applied_at'] = datetime.now(timezone.utc).isoformat()
    manifest.write_text(json.dumps(record, indent=2) + '\n')
    from crange_scheduling import atomic_json
    atomic_json(path / '.crange-plans' / f'{args.plan_id}.json', {k:record[k] for k in ('plan_id','created','changes','placements','reservation_ids','policy_revision','applied_at') if k in record},0o640)


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
        tf_run(path, ["plan", "-input=false", "-out", str(plan)])
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
    cmd_route_sync(args)
    inventory_path = ROOT / "ansible" / "inventory" / "generated" / f"project-{int(args.project):03d}.json"
    run([sys.executable, str(ROOT / "scripts" / "generate-project-inventory.py"), "--terraform-dir", str(path), "--output", str(inventory_path)])
    try:
        inventory = json.loads(inventory_path.read_text())
        groups = inventory["all"]["children"]
        router_groups = groups["routers"]["children"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CrangeError(f"Cannot read generated project inventory: {exc}") from exc
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
    print("Infrastructure applied. Phase 2/2: configuring routes, routers, and jump boxes", flush=True)
    try:
        cmd_configure_team(argparse.Namespace(project=args.project, team=0, yes=True))
    except CrangeError as exc:
        raise CrangeError(f"Infrastructure deployment succeeded, but post-deploy configuration failed: {exc}") from exc
    print("Staging deployment and configuration completed.", flush=True)


def cmd_inventory(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    output = ROOT / "ansible" / "inventory" / "generated" / f"project-{int(args.project):03d}.json"
    run([sys.executable, str(ROOT / "scripts" / "generate-project-inventory.py"), "--terraform-dir", str(path), "--output", str(output)])


def ansible(args: argparse.Namespace, playbook: str, limit: list[str], *, tags: list[str] | None = None) -> None:
    path = project_dir(args.project)
    output = ROOT / "ansible" / "inventory" / "generated" / f"project-{int(args.project):03d}.json"
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
    run(command, cwd=ROOT, env={**os.environ, "ANSIBLE_TIMEOUT": "90"})


def cmd_configure(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    if not args.yes:
        raise CrangeError("Configuration applies Ansible changes; pass --yes after reviewing the playbook.")
    output = ROOT / "ansible" / "inventory" / "generated" / f"project-{int(args.project):03d}.json"
    run([sys.executable, str(ROOT / "scripts" / "generate-project-inventory.py"), "--terraform-dir", str(path), "--output", str(output)])
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
        self.token = values["PROXMOX_VE_API_TOKEN"]
        self.ssl_context = ssl._create_unverified_context() if os.environ.get("PROXMOX_VE_INSECURE", "true").lower() == "true" else ssl.create_default_context()

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
        try:
            with urllib.request.urlopen(req, context=self.ssl_context, timeout=90) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            # Never echo response bodies or headers; API failures can contain sensitive data.
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
        while time.monotonic() < deadline:
            result = self.request("GET", path)
            if result.get("status") == "stopped":
                if result.get("exitstatus") != "OK":
                    raise CrangeError(f"Proxmox task on {node} failed: {result.get('exitstatus', 'unknown')}.")
                return
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


def templates_for(path: Path) -> dict:
    source = path / "approved-scenario-templates.json"
    try:
        data = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CrangeError(f"Cannot read approved scenario templates {source}: {exc}") from exc
    if not data:
        raise CrangeError("No approved scenario VM templates are configured.")
    return data


def container_templates_for(path: Path) -> dict:
    try:
        data = json.loads((path / "approved-container-templates.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CrangeError(f"Cannot read approved container templates: {exc}") from exc
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
    node = live.get('node_name', box['node_name'])
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


def cmd_scenario_add(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    templates = templates_for(path)
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
    scenario_boxes(cfg)[key] = {
        "team_id": team, "name": args.name, "ip_last_octet": args.ip,
        "network_key": network_key, "network_id": network_id,
        "template": args.template, "node_name": args.node,
        "placement": __import__("crange_scheduling").placement_intent(args.node, getattr(args,"preferred_node","")),
        "cores": args.cores, "memory_mb": args.memory,
        "full_clone": args.full_clone if args.full_clone is not None else args.node != "auto",
        "started": True, "health_checks": checks, "guac_protocol": getattr(args, "guac_protocol", "ssh"),
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
    if args.template not in templates_for(path):
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
    if args.node != box.get('node_name'):
        box['placement']=__import__('crange_scheduling').placement_intent(args.node)
    if args.template != box.get("template"):
        reference = templates_for(path)[args.template]
        box.pop("image", None)
        if reference.get("image_id"):box["image"] = reference["image_id"]
    box.update({"template": args.template, "node_name": args.node,
                "cores": args.cores, "memory_mb": args.memory,
                "health_checks": checks, "guac_protocol": getattr(args, "guac_protocol", None) or box.get("guac_protocol", "ssh")})
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
    if args.template not in container_templates_for(path):
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
    scenario_containers(cfg)[key] = {
        "team_id": team, "name": args.name, "ip_last_octet": args.ip,
        "template": args.template, "node_name": args.node,
        "placement": __import__("crange_scheduling").placement_intent(args.node, getattr(args,"preferred_node","")),
        "cores": args.cores, "memory_mb": args.memory, "started": True,
        "network_key": network_key, "network_id": network_id,
        "health_checks": checks, "guac_protocol": getattr(args, "guac_protocol", "ssh"),
    }
    reference = container_templates_for(path)[args.template]
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
    if args.template not in container_templates_for(path):
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
    if args.node != box.get('node_name'):
        box['placement']=__import__('crange_scheduling').placement_intent(args.node)
    if args.template != box.get("template"):
        reference = container_templates_for(path)[args.template]
        box.pop("image", None)
        if reference.get("image_id"):box["image"] = reference["image_id"]
    box.update({"template": args.template, "node_name": args.node,
                "cores": args.cores, "memory_mb": args.memory,
                "ip_last_octet": args.ip, "network_key": network_key,
                "network_id": network_id, "health_checks": checks,
                "guac_protocol": getattr(args, "guac_protocol", None) or box.get("guac_protocol", "ssh")})
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


def wait_for_boot_health(project: int, team: str, address: str, check: dict,
                         startup_timeout: int = 180) -> None:
    """Allow guest startup time without changing an individual probe's timeout."""
    deadline = time.monotonic() + startup_timeout
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
    tests_dir = ROOT / 'releases' / f'project-{project:03d}' / 'tests'
    if tests_dir.exists() and any(tests_dir.glob('*.json')):
        raise CrangeError('Clean up release test clones before capturing staging.')
    networks = {key: {field: net[field] for field in ('owner_team_id', 'network_id',
                'uplink_last_octet', 'node_name', 'cores', 'memory_mb') if field in net}
                for key, net in cfg.get('custom_networks', {}).items() if str(net['owner_team_id']) == '254'}
    boxes = [{**box, "key": key, "kind": "vm"} for key, box in sorted(scenario_boxes(cfg).items()) if str(box["team_id"]) == "254"]
    boxes += [{**box, "key": key, "kind": "container"} for key, box in sorted(scenario_containers(cfg).items()) if str(box["team_id"]) == "254"]
    if not boxes:
        raise CrangeError("Team 0 has no scenario VMs or containers to capture.")
    for box in boxes:
        if box["kind"] == "vm" and not validate_health_checks(box.get("health_checks", [])):
            raise CrangeError(f"Scenario VM {box['key']} needs at least one health check before release capture.")
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
        for box in boxes:
            node = applied[box["kind"]][box["key"]].get("node_name", box["node_name"])
            vmid = int(applied[box["kind"]][box["key"]]["id"])
            actual = api.vm(node, vmid) if box["kind"] == "vm" else api.container(node, vmid)
            vnet = f"p{project:03d}c{box['network_id']:03d}" if box.get("network_key") else f"p{project:03d}t254"
            if actual.get("name", actual.get("hostname")) != f"{vnet}-{box['name']}" or int(actual.get("template", 0)):
                raise CrangeError(f"Applied VM {node}/{vmid} does not match staging box {box['key']}.")
            status = api.status(node, vmid) if box["kind"] == "vm" else api.container_status(node, vmid)
            if status != "running":
                raise CrangeError(f"Staging VM {node}/{vmid} must be running before capture.")
        revision = run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture=True).stdout.strip()
        dirty = bool(run(["git", "status", "--porcelain"], cwd=ROOT, capture=True).stdout.strip())
        try:
            for box in boxes:
                node = applied[box["kind"]][box["key"]].get("node_name", box["node_name"])
                vmid = int(applied[box["kind"]][box["key"]]["id"])
                slug = hashlib.sha256(f"{project}:{args.version}:{box['key']}".encode()).hexdigest()[:12]
                alias = f"p{project:03d}-image-{slug}"
                name = f"p{project:03d}-release-{slug}-{box['name'][:15]}"
                original_template = box["template"]
                source_profile = None
                if box["kind"] == "vm":
                    try:
                        source_profile = {'agent_enabled': False, 'upgrade_packages': True, **json.loads((path / 'template-profile.json').read_text())}
                        source_profile.update(templates_for(path)[original_template].get('profile', {}))
                    except (OSError, ValueError, TypeError) as exc:
                        raise CrangeError('Cannot read the approved VM hardware profile before capture.') from exc
                try:
                    if box["kind"] == "vm":
                        api.shutdown(node, vmid, applied["vm"][box["key"]]["address"])
                    else:
                        api.container_stop(node, vmid)
                    if box["kind"] == "vm":
                        image_id = api.capture_image(node, vmid, name,
                            f"CRANGE project {project:03d} release {args.version} box {box['key']}")
                    else:
                        image_id = api.capture_container_image(node, vmid, name,
                            f"CRANGE project {project:03d} release {args.version} CT {box['key']}")
                finally:
                    status = api.status(node, vmid) if box["kind"] == "vm" else api.container_status(node, vmid)
                    if status == "stopped":
                        (api.start if box["kind"] == "vm" else api.container_start)(node, vmid)
                reference = {"template_id": image_id, "template_node": node, "kind": box["kind"]}
                if box["kind"] == "vm":
                    reference['profile'] = {**source_profile, 'upgrade_packages': False}
                references[alias] = reference
                captured.append((node, image_id, box["kind"]))
                box["source_template"] = original_template
                box["source_vm_id"] = vmid
                box["template"] = alias
                box["full_clone"] = True
            permutations = [entry.strip().split(",") for entry in getattr(args, "boot_order", [])]
            scenario_names = {box["name"] for box in boxes}
            if not permutations:
                permutations = [[box["name"] for box in boxes]]
            for order in permutations:
                if len(order) != len(scenario_names) or set(order) != scenario_names or len(set(order)) != len(order):
                    raise CrangeError("Each --boot-order must list every staging VM name exactly once, comma-separated.")
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
            # Preserve any successful templates for diagnosis; they are never reused
            # without a completed, integrity-checked manifest.
            leftovers = []
            for node, vmid, kind in reversed(captured):
                try:
                    (api.delete_vm if kind == "vm" else api.delete_container)(node, vmid)
                except CrangeError:
                    leftovers.append(f"{node}/{vmid}")
            if leftovers:
                print("Could not remove incomplete release images: " + ", ".join(leftovers), file=sys.stderr)
            raise
    print(f"Captured immutable guest images and release {content['release_id']} at {destination.relative_to(ROOT)}")
    print(f"SHA-256 {content['integrity_sha256']}")

def cmd_release_show(args: argparse.Namespace) -> None:
    release = load_release(int(args.project), args.version)
    print(json.dumps(release, indent=2, sort_keys=True))


def cmd_release_deploy(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
    release = load_release(int(args.project), args.version)
    verify_release_images(release)
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
    print(f"Release {args.version} declared for teams {', '.join(wanted)}. Review infra-plan before applying fresh clones.")


def cmd_release_apply(args):
    path = project_dir(args.project)
    cfg = json_config(path)
    teams = [str(int(team)) for team in args.teams]
    if not teams or len(teams) != len(set(teams)) or any(team not in set(map(str, cfg.get('team_ids', []))) for team in teams):
        raise CrangeError('Choose configured ordinary teams without duplicates.')
    if not getattr(args, 'plan_id', None):
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
    args.release_teams = teams
    cmd_apply(args)
    print('Release infrastructure applied. Configuring selected teams.', flush=True)
    try:
        for team in teams:
            cmd_configure_team(argparse.Namespace(project=args.project, team=int(team), yes=True))
        cmd_release_check(argparse.Namespace(project=args.project, teams=list(map(int, teams))))
    except CrangeError as exc:
        raise CrangeError('Release infrastructure applied, but configuration or health verification failed: ' + str(exc)) from exc
    print('Selected-team release deployment and health checks completed.')


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
                    vmid, node = int(live['id']), live.get('node_name', box['node_name'])
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
    tests_dir = ROOT / "releases" / f"project-{int(args.project):03d}" / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    record_path = tests_dir / f"{args.version}.json"
    if record_path.exists():
        raise CrangeError(f"A test record already exists for {args.version}; check or clean it up first.")
    validate_custom_networks(cfg)
    used = {int(b['ip_last_octet']) for field in ('scenario_boxes', 'scenario_containers')
            for b in cfg.get(field, {}).values() if str(b['team_id']) == '254' and not b.get('network_key')}
    used.update(10 + int(b['jump_number']) for b in cfg.get('jump_boxes', {}).values()
                if str(b['team_id']) == '254' and not b.get('network_key'))
    used.update(int(n['uplink_last_octet']) for n in cfg.get('custom_networks', {}).values()
                if str(n['owner_team_id']) == '254')
    free = iter(host for host in range(90, 101) if host not in used)
    network_map, owned = release_networks(cfg, release, '254', 'test', range(90, 101))
    additions = {
        'scenario_boxes': release_scenario_boxes(path, release, '254', f'test{args.version}', network_map),
        'scenario_containers': release_scenario_containers(path, release, '254', f'test{args.version}', network_map),
    }
    prepared = {'boxes': {}, 'containers': {}}
    for field, clones in additions.items():
        for key, box in clones.items():
            if key in cfg.get(field, {}):
                raise CrangeError(f'Test clone key {key} already exists.')
            if not box.get('network_key'):
                try:
                    box['ip_last_octet'] = next(free)
                except StopIteration as exc:
                    raise CrangeError('Team 0 has too few reserved test addresses (.90-.100).') from exc
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
            node, vmid = live.get('node_name', box['node_name']), int(live['id'])
            actual = api.vm(node, vmid) if field == 'scenario_boxes' else api.container(node, vmid)
            vnet = f"p{project:03d}c{box['network_id']:03d}" if box.get('network_key') else f"p{project:03d}t254"
            if actual.get('name', actual.get('hostname')) != vnet + '-' + box['name'] or int(actual.get('template', 0)):
                raise CrangeError('Disposable test guest identity has changed; refusing power operations.')
            ids[source_name] = (node, vmid)
    if not permutations:
        permutations = [[b["name"] for b in release["scenario"]["boxes"] + release["scenario"].get("containers", [])]]
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
        for name in order:
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
                    wait_for_boot_health(project, "254", live["address"], check)
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


def cmd_distribution_inventory(args):
    from crange_scheduling import discover, read_settings
    print(json.dumps(discover(ProxmoxAPI(), read_settings()[0]), indent=2))


def cmd_distribution_prepare(args):
    from crange_cache_jobs import prepare_cache
    prepare_cache(project_id=int(args.project), image_id=args.image, node_name=args.node,
                  datastore_id=args.storage, owner=os.environ.get('CRANGE_ACTOR', f'cli:{os.getuid()}'))


def cmd_cache_gc_review(args):
    from crange_cache_jobs import review_cache_gc
    plan_id = review_cache_gc(image_id=args.image, node_name=args.node, template_id=args.template_id,
                             owner=os.environ.get('CRANGE_ACTOR', f'cli:{os.getuid()}'))
    print(f'Cache GC plan ID: {plan_id}. Review the retained dependency/ownership evidence before applying.')


def cmd_cache_gc_apply(args):
    from crange_cache_jobs import apply_cache_gc
    apply_cache_gc(args.plan_id)



def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="crange", description="Manage CRANGE teams, jump boxes, and reviewed Terraform/Ansible workflows.")
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser('cluster-refresh', help='Collect a sanitized worker-owned cluster capacity snapshot'); q.set_defaults(func=cmd_cluster_refresh)
    q = sub.add_parser('distribution-inventory', help='Read all cluster workloads and conservative placement capacity')
    q.set_defaults(func=cmd_distribution_inventory)
    q = sub.add_parser('distribution-prepare', help='Prepare and verify an approved immutable image replica')
    q.add_argument('image', type=lambda value: value if re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', value) else (_ for _ in ()).throw(argparse.ArgumentTypeError('Invalid image ID')))
    q.add_argument('--project', required=True, type=int, help='Authorized project whose approved image/cache quota is charged')
    q.add_argument('--node', required=True, choices=sorted(NODES))
    q.add_argument('--storage', required=True, choices=['local-lvm', 'ssd-lvm', 'hdd-lvm'])
    q.set_defaults(func=cmd_distribution_prepare)
    q = sub.add_parser('cache-gc-review', help='Admin: retain an exact local-cache eviction review')
    q.add_argument('image'); q.add_argument('--node', required=True, choices=sorted(NODES)); q.add_argument('--template-id',required=True,type=int); q.set_defaults(func=cmd_cache_gc_review)
    q = sub.add_parser('cache-gc-apply', help='Admin: apply an exact retained local-cache eviction review')
    q.add_argument('plan_id',type=lambda v:v if re.fullmatch(r'[0-9a-f]{32}',v) else (_ for _ in ()).throw(argparse.ArgumentTypeError('Invalid cache GC plan ID'))); q.set_defaults(func=cmd_cache_gc_apply)
    q = sub.add_parser('serve', help='Serve dashboard and API on port 8000')
    q.add_argument('--host', default='127.0.0.1')
    q.add_argument('--port', type=int, default=8000)
    q.set_defaults(func=cmd_serve)
    q = sub.add_parser('worker', help='Run the controller job worker')
    q.set_defaults(func=cmd_worker)
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
    q = sub.add_parser('template-publish', help='Publish a prepared installer as an approved immutable template')
    q.add_argument('build_id'); q.add_argument('--projects', type=int, nargs='+', required=True); q.add_argument('--prepared', action='store_true')
    q.set_defaults(func=lambda a: __import__('crange_templates').publish(a.build_id,a.projects,a.prepared))
    q = sub.add_parser('template-upload', help='Transfer a completed ISO upload to node storage')
    q.add_argument('upload_id'); q.add_argument('--node', required=True, choices=sorted(NODES))
    q.set_defaults(func=lambda a: __import__('crange_template_media').transfer(a.upload_id,a.node))
    q = sub.add_parser("projects", help="List Terraform project environments"); q.set_defaults(func=cmd_projects)
    q = sub.add_parser("project-init", help="Create a new standardized project environment from project-template")
    q.add_argument("project"); q.add_argument("--transit", required=True); q.set_defaults(func=cmd_project_init)
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
    q.add_argument("--node", default="auto", choices=sorted(NODES | {"auto"}), help="Legacy explicit node pin; defaults to Automatic"); q.add_argument("--preferred-node", default="", choices=["",*sorted(NODES)], help="Soft suggestion with Automatic"); q.add_argument("--uplink", type=int, required=True)
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
    q = sub.add_parser("stage-changes", help="Prepare approved local caches and retain an exact reviewed guest plan"); q.add_argument("project"); q.set_defaults(func=cmd_stage_changes)
    q = sub.add_parser("infra-plan", help="Initialize, plan, and summarize project changes"); q.add_argument("project"); q.add_argument("--retain", action="store_true", help="Keep plan for exact reviewed apply"); q.add_argument("--replace-sdn-applier", action="store_true", help="Repair a partially applied SDN network by reapplying cluster SDN"); q.set_defaults(func=cmd_plan)
    q = sub.add_parser("infra-apply", help="Apply the exact reviewed plan"); q.add_argument("project"); q.add_argument("--yes", action="store_true"); q.add_argument("--plan-id", help="Apply a saved, integrity-checked plan"); q.add_argument("--allow-destroy", action="store_true"); q.add_argument("--allow-replace", action="store_true"); q.set_defaults(func=cmd_apply)
    q = sub.add_parser("stage-deploy", help="Apply the reviewed staging plan and configure routes, routers, and jump boxes")
    q.add_argument("project"); q.add_argument("--yes", action="store_true"); q.add_argument("--plan-id", required=True)
    q.add_argument("--allow-destroy", action="store_true"); q.add_argument("--allow-replace", action="store_true"); q.set_defaults(func=cmd_stage_deploy)
    q = sub.add_parser("configure-team", help="Configure one team's routers and jump boxes without touching other teams")
    q.add_argument("project"); q.add_argument("team", type=int, help="Team number, or 0 for staging")
    q.add_argument("--yes", action="store_true"); q.set_defaults(func=cmd_configure_team)
    q = sub.add_parser("inventory"); q.add_argument("project"); q.set_defaults(func=cmd_inventory)
    q = sub.add_parser('route-sync', help='Ensure the applied project aggregate route without firewall changes')
    q.add_argument('project'); q.set_defaults(func=cmd_route_sync)
    q = sub.add_parser("configure", help="Apply reviewed Ansible router routes, routers, or jumpbox config")
    q.add_argument("project"); q.add_argument("target", choices=("routes", "routers", "boxes", "all")); q.add_argument("--yes", action="store_true"); q.set_defaults(func=cmd_configure)
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
    q.add_argument("--guac-protocol", choices=("ssh", "rdp"), default="ssh")
    q.set_defaults(func=cmd_scenario_add)
    q = sub.add_parser("scenario-edit", help="Edit a declared scenario VM and its health checks")
    q.add_argument("project"); q.add_argument("name")
    q.add_argument("--ip", type=int); q.add_argument("--network", default=None, help="Custom network; empty restores standard team subnet")
    q.add_argument("--template", required=True); q.add_argument("--node", default="auto", choices=sorted(NODES | {"auto"}), help="Legacy explicit node pin; defaults to Automatic"); q.add_argument("--preferred-node", default="", choices=["",*sorted(NODES)], help="Soft suggestion with Automatic")
    q.add_argument("--cores", type=int, required=True); q.add_argument("--memory", type=int, required=True)
    q.add_argument("--health", required=True, help="JSON ping/TCP/HTTP health checks; empty uses ping")
    q.add_argument("--guac-protocol", choices=("ssh", "rdp"), default=None)
    q.set_defaults(func=cmd_scenario_edit)
    q = sub.add_parser("scenario-remove"); q.add_argument("project"); q.add_argument("name"); q.set_defaults(func=cmd_scenario_remove)
    q = sub.add_parser("container-add", help="Declare a scenario CT from an approved template")
    q.add_argument("project"); q.add_argument("name"); q.add_argument("--team", type=int, required=True)
    q.add_argument("--ip", type=int, required=True); q.add_argument("--template", required=True)
    q.add_argument("--node", default="auto", choices=sorted(NODES | {"auto"}), help="Legacy explicit node pin; defaults to Automatic"); q.add_argument("--preferred-node", default="", choices=["",*sorted(NODES)], help="Soft suggestion with Automatic"); q.add_argument("--network", help="Custom network key")
    q.add_argument("--cores", type=int, default=1)
    q.add_argument("--memory", type=int, default=1024); q.add_argument("--health", default="[]", help="JSON ping/TCP/HTTP readiness checks; empty uses ping"); q.add_argument("--guac-protocol", choices=("ssh", "rdp"), default="ssh"); q.set_defaults(func=cmd_container_add)
    q = sub.add_parser("container-edit", help="Edit a declared scenario CT and its health checks")
    q.add_argument("project"); q.add_argument("name"); q.add_argument("--template", required=True)
    q.add_argument("--node", default="auto", choices=sorted(NODES | {"auto"}), help="Legacy explicit node pin; defaults to Automatic"); q.add_argument("--preferred-node", default="", choices=["",*sorted(NODES)], help="Soft suggestion with Automatic"); q.add_argument("--ip", type=int, required=True)
    q.add_argument("--network", default="", help="Custom network key, or empty for the team's standard subnet")
    q.add_argument("--cores", type=int, required=True); q.add_argument("--memory", type=int, required=True)
    q.add_argument("--health", required=True, help="JSON ping/TCP/HTTP health checks; empty uses ping"); q.add_argument("--guac-protocol", choices=("ssh", "rdp"), default=None)
    q.set_defaults(func=cmd_container_edit)
    q = sub.add_parser("container-remove"); q.add_argument("project"); q.add_argument("name"); q.set_defaults(func=cmd_container_remove)
    q = sub.add_parser("container-health"); q.add_argument("project"); q.add_argument("name", nargs="?"); q.set_defaults(func=cmd_container_health)
    q = sub.add_parser("scenario-health"); q.add_argument("project"); q.add_argument("name", nargs="?"); q.set_defaults(func=cmd_scenario_health)
    q = sub.add_parser("release-capture"); q.add_argument("project"); q.add_argument("version"); q.add_argument("--boot-order", action="append", default=[], help="Comma-separated staging VM start order; repeat to record permutations"); q.set_defaults(func=cmd_release_capture)
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
