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
NODES = {f"proxmox{i}" for i in range(1, 6)}


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
    command = ["bash", "-lc", "set -a; . /home/crange/.config/crange/proxmox.env; set +a; export PROXMOX_VE_INSECURE=true; exec terraform " + shlex.join(args)]
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
        connections.append({
            'key': f'jumpbox:{name}', 'name': f'P{project_id:03d} · {name}',
            'resource': name, 'kind': 'jumpbox', 'team_id': str(box['team_id']),
            'address': address, 'protocol': 'rdp', 'port': 3389,
            'default_for_team': defaults[str(box['team_id'])][1] == name,
        })
    for kind, field, label in (('vm', 'scenario_boxes', 'VM'), ('container', 'scenario_containers', 'CT')):
        for name, box in sorted(cfg.get(field, {}).items()):
            live = deployed[field].get(name, {})
            address = live.get('address')
            if not address:
                continue
            protocol = box.get('guac_protocol', 'ssh')
            connections.append({
                'key': f'{kind}:{name}', 'name': f'P{project_id:03d} · {label} · {name}',
                'resource': name, 'kind': kind, 'team_id': str(box['team_id']),
                'address': address, 'protocol': protocol,
                'port': 3389 if protocol == 'rdp' else 22, 'default_for_team': False,
            })

    users = []
    for user in all_users:
        if not user.get('enabled', True) or user.get('admin'):
            continue
        team_ids = {str(team) for team in user.get('teams', {}).get(str(project_id), [])}
        selected = set(user.get('connections', {}).get(str(project_id), []))
        allowed = [connection['key'] for connection in connections
                   if (connection['default_for_team'] and connection['team_id'] in team_ids)
                   or connection['key'] in selected]
        users.append({'username': user['username'], 'teams': sorted(team_ids),
                      'connection_keys': sorted(set(allowed))})

    manifest = {'schema_version': 2, 'project_id': project_id,
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
    save_config(target, {"project_id": project, "transit_address": args.transit, "team_ids": ["1"], "jump_boxes": {}, "custom_networks": {}, "scenario_containers": {}})
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
        if net.get("node_name") not in NODES:
            raise CrangeError(f"Custom network {key} uses an invalid router node.")
        if not 1 <= int(net.get("cores", 1)) <= 16 or not 512 <= int(net.get("memory_mb", 512)) <= 65536:
            raise CrangeError(f"Custom network {key} router size must be 1-16 cores and 512-65536 MB RAM.")
        ids.add(network_id)
        uplinks.add((owner, uplink))
        if any(str(box.get("team_id")) == owner and not box.get("network_key") and int(box.get("ip_last_octet", -1)) == uplink for box in cfg.get("scenario_containers", {}).values()):
            raise CrangeError(f"Custom router uplink overlaps a scenario CT address in parent network {owner}.")
    # Parent-side router addresses may not collide with static scenario/jump hosts.
    for owner, host in uplinks:
        if any(str(box.get("team_id")) == owner and int(box.get("ip_last_octet", -1)) == host for box in cfg.get("scenario_boxes", {}).values()):
            raise CrangeError(f"Custom router uplink 10.{cfg['project_id']}.{owner}.{host} overlaps a scenario VM.")
        if any(str(box.get("team_id")) == owner and int(box.get("ip_last_octet", -1)) == host and not box.get("network_key") for box in cfg.get("scenario_containers", {}).values()):
            raise CrangeError(f"Custom router uplink 10.{cfg['project_id']}.{owner}.{host} overlaps a scenario CT.")
        if any(str(box.get("team_id")) == owner and 10 + int(box.get("jump_number", 0)) == host and not box.get("network_key") for box in cfg.get("jump_boxes", {}).values()):
            raise CrangeError(f"Custom router uplink 10.{cfg['project_id']}.{owner}.{host} overlaps a jump box.")
    for box in cfg.get("scenario_containers", {}).values():
        if box.get("network_key"):
            network = networks.get(box["network_key"])
            if not network or str(network["owner_team_id"]) != str(box["team_id"]):
                raise CrangeError("Scenario CT custom network attachment is invalid.")
            box["network_id"] = int(network["network_id"])
        else:
            box.setdefault("network_key", "")
            box.setdefault("network_id", int(box["team_id"]))


def custom_uplink_available(cfg: dict, owner: str, host: int) -> bool:
    if any(str(net["owner_team_id"]) == owner and int(net["uplink_last_octet"]) == host for net in cfg.get("custom_networks", {}).values()):
        return False
    if any(str(box["team_id"]) == owner and int(box.get("ip_last_octet", -1)) == host for box in cfg.get("scenario_boxes", {}).values()):
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
    if args.node not in NODES or not 2 <= args.uplink <= 100:
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
    net.update({"network_id": args.id, "node_name": args.node,
                "uplink_last_octet": args.uplink, "cores": args.cores,
                "memory_mb": args.memory})
    for box in cfg.get("jump_boxes", {}).values():
        if box.get("network_key") == args.name:
            box["network_id"] = args.id
    for box in scenario_containers(cfg).values():
        if box.get("network_key") == args.name:
            box["network_id"] = args.id
    validate_custom_networks(cfg)
    validate_project_quota(path, cfg)
    save_config(path, cfg)
    print(f"Updated custom network {args.name}. Run infra-plan and review before deploying its router or subnet changes.")


def mutate_box(args: argparse.Namespace, add: bool) -> None:
    path = project_dir(args.project)
    cfg = json_config(path)
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
        if network_id == int(owner) and any(str(v["team_id"]) == owner and int(v.get("ip_last_octet", -1)) == 10 + number for v in cfg.get("scenario_boxes", {}).values()):
            raise CrangeError(f"Address .{10 + number} is already assigned to a scenario VM.")
        if any(int(v.get("network_id", v["team_id"])) == network_id and int(v.get("ip_last_octet", -1)) == 10 + number for v in cfg.get("scenario_containers", {}).values()):
            raise CrangeError(f"Address .{10 + number} is already assigned to a scenario container.")
        if args.node not in NODES:
            raise CrangeError("Node must be one of proxmox1 through proxmox5.")
        if not 1 <= args.cores <= 16 or not 512 <= args.memory <= 65536:
            raise CrangeError("Requested size exceeds CLI limits (1–16 cores, 512–65536 MB RAM).")
        full_clone = args.full_clone if args.full_clone is not None else args.node != "proxmox4"
        boxes[name] = {"team_id": owner, "jump_number": number, "node_name": args.node,
                       "network_key": network_key, "network_id": network_id,
                       "cores": args.cores, "memory_mb": args.memory,
                       "full_clone": full_clone, "started": True}
        validate_custom_networks(cfg)
    else:
        if name not in boxes:
            raise CrangeError(f"Box {name} is not configured.")
        del boxes[name]
    save_config(path, cfg)
    network_text = f" in network {boxes[name].get('network_key') or boxes[name]['team_id']}" if add else ""
    print(f"Jump box {name} {'added to' if add else 'removed from'} project {int(cfg['project_id']):03d}{network_text}. Run infra-plan before apply.")

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


def cmd_plan(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    json_config(path)
    tf_run(path, ["init", "-input=false", "-upgrade=false"])
    with tempfile.TemporaryDirectory(prefix="crange-plan-") as tmp:
        plan = Path(tmp) / "review.tfplan"
        plan_args = ["plan", "-input=false", "-out", str(plan)]
        if getattr(args, 'replace_sdn_applier', False):
            plan_args.append('-replace=proxmox_sdn_applier.apply')
        tf_run(path, plan_args)
        detail = json.loads(tf_run(path, ["show", "-json", str(plan)], capture=True).stdout)
        changes = []
        for resource in detail.get("resource_changes", []):
            actions = resource.get("change", {}).get("actions", [])
            if actions != ["no-op"]:
                changes.append((resource.get("address", "?"), actions))
        print("\nPlan review:")
        if not changes:
            print("No resource changes.")
        for address, actions in changes:
            print(f"  {address}: {' -> '.join(actions)}")
        if any("delete" in actions for _, actions in changes):
            print("This plan deletes or replaces resources; inspect the Terraform plan above before applying.")
        if getattr(args, 'retain', False):
            plan_id = os.urandom(16).hex()
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
                      'inputs_sha256': inputs, 'created': time.time(),
                      'changes': [{'address': address, 'actions': actions} for address, actions in changes]}
            manifest = directory / f'{plan_id}.json'
            fd = os.open(manifest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(record, stream, indent=2)
            print(f'Plan ID: {plan_id}')
            print(f'Inputs SHA-256: {inputs}')


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
    changes = record.get('changes', [])
    destructive = [item['address'] for item in changes if 'delete' in item.get('actions', [])]
    replacements = [item['address'] for item in changes if 'delete' in item.get('actions', []) and 'create' in item.get('actions', [])]
    if destructive and not args.allow_destroy:
        raise CrangeError('Saved plan destroys resources: ' + ', '.join(destructive))
    if replacements and not args.allow_replace:
        raise CrangeError('Saved plan replaces resources: ' + ', '.join(replacements))
    tf_run(path, ['apply', '-input=false', str(plan)])
    plan.unlink()
    record['applied_at'] = datetime.now(timezone.utc).isoformat()
    manifest.write_text(json.dumps(record, indent=2) + '\n')


def cmd_apply(args: argparse.Namespace) -> None:
    if not args.yes:
        raise CrangeError("Applying changes requires --yes after reviewing the plan.")
    path = project_dir(args.project)
    json_config(path)
    if getattr(args, 'plan_id', None):
        return cmd_apply_saved(args, path)
    tf_run(path, ["init", "-input=false", "-upgrade=false"])
    with tempfile.TemporaryDirectory(prefix="crange-apply-") as tmp:
        plan = Path(tmp) / "review.tfplan"
        tf_run(path, ["plan", "-input=false", "-out", str(plan)])
        detail = json.loads(tf_run(path, ["show", "-json", str(plan)], capture=True).stdout)
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


def cmd_stage_deploy(args: argparse.Namespace) -> None:
    """Apply one reviewed staging plan, then configure the complete range path."""
    print("Phase 1/2: applying the reviewed infrastructure plan", flush=True)
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


def ansible(args: argparse.Namespace, playbook: str, limit: list[str]) -> None:
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
    for group in limit:
        command += ["--limit", group]
    run(command, cwd=ROOT, env={**os.environ, "ANSIBLE_TIMEOUT": "90"})


def cmd_configure(args: argparse.Namespace) -> None:
    path = project_dir(args.project)
    if not args.yes:
        raise CrangeError("Configuration applies Ansible changes; pass --yes after reviewing the playbook.")
    output = ROOT / "ansible" / "inventory" / "generated" / f"project-{int(args.project):03d}.json"
    run([sys.executable, str(ROOT / "scripts" / "generate-project-inventory.py"), "--terraform-dir", str(path), "--output", str(output)])
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
        data = urllib.parse.urlencode(fields).encode() if fields is not None else None
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
        self.task(node, upid, timeout=600)

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

    def capture_image(self, node: str, source_id: int, name: str, description: str) -> int:
        for _attempt in range(3):
            image_id = int(self.request("GET", "/cluster/nextid"))
            try:
                upid = self.request("POST", f"/nodes/{node}/qemu/{source_id}/clone",
                    {"newid": image_id, "name": name, "description": description,
                     "pool": "crange-iac", "full": 1, "storage": "local-lvm"})
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
        for attempt in range(3):
            image_id = int(self.request("GET", "/cluster/nextid"))
            try:
                upid = self.request("POST", f"/nodes/{node}/lxc/{source_id}/clone",
                    {"newid": image_id, "hostname": name, "description": description,
                     "pool": "crange-iac", "full": 1, "storage": "local-lvm"})
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
        if box.get("node_name") not in NODES or not 1 <= int(box.get("cores", 1)) <= 16 or not 512 <= int(box.get("memory_mb", 512)) <= 65536:
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
        addr = (team, last)
        if addr in seen:
            raise CrangeError(f"Duplicate static address in team {team}: .{last}.")
        seen.add(addr)
        if box.get("template") not in templates:
            raise CrangeError(f"Scenario box {key} uses unapproved template {box.get('template')!r}.")
        if box.get("node_name") not in NODES:
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
    if release.get("schema_version") not in (2, 3) or any(
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


def release_scenario_boxes(path: Path, release: dict, team_id: str, prefix: str) -> dict:
    cfg = json_config(path)
    templates = templates_for(path)
    result = {}
    for source in release["scenario"]["boxes"]:
        template_key = source["template"]
        if template_key not in templates:
            raise CrangeError(f"Template {template_key!r} in release is no longer approved in this project.")
        pinned = release.get("scenario", {}).get("templates", {}).get(template_key)
        if not pinned or templates[template_key] != pinned:
            raise CrangeError(f"Template {template_key!r} no longer matches the pinned release reference.")
        key = f"{prefix}-{source['name']}-t{int(team_id):03d}"
        if len(key) > 63:
            key = f"{prefix[:16]}-{hashlib.sha1(key.encode()).hexdigest()[:10]}-t{int(team_id):03d}"
        item = {k: source[k] for k in ("name", "ip_last_octet", "template", "node_name", "cores", "memory_mb", "full_clone", "started", "health_checks")}
        version_identity = f"{release.get('release_id', release.get('version', 'release'))}:{source['name']}"
        version_mark = hashlib.sha1(version_identity.encode()).hexdigest()[:8]
        prefix_slug = re.sub(r"[^a-z0-9-]+", "-", prefix.lower()).strip("-")
        kind = "t" if prefix_slug.startswith("test") else "r"
        unique_name = f"{kind}{version_mark}-{source['name'][:15]}"
        item["name"] = unique_name
        item["team_id"] = team_id
        result[key] = item
    return result


def release_scenario_containers(path: Path, release: dict, team_id: str, prefix: str) -> dict:
    result = {}
    approved = container_templates_for(path)
    for source in release["scenario"].get("containers", []):
        key = f"{prefix}-{source['name']}-t{int(team_id):03d}"
        pinned = release.get("scenario", {}).get("templates", {}).get(source["template"])
        if not pinned or approved.get(source["template"]) != {"template_id": pinned["template_id"], "template_node": pinned["template_node"]}:
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
        }
    return result


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
    if any(str(b["team_id"]) == team and int(b["ip_last_octet"]) == args.ip for b in scenario_boxes(cfg).values()) or any(str(b["team_id"]) == team and 10 + int(b["jump_number"]) == args.ip for b in cfg.get("jump_boxes", {}).values()):
        raise CrangeError(f"Static address 10.{cfg['project_id']}.{team}.{args.ip} is already assigned.")
    if args.node not in NODES:
        raise CrangeError("Node must be one of proxmox1 through proxmox5.")
    key = f"t{int(team):03d}-{args.name}"
    if key in scenario_boxes(cfg):
        raise CrangeError(f"Scenario VM {key} already exists.")
    try:
        checks = validate_health_checks(json.loads(args.health))
    except json.JSONDecodeError as exc:
        raise CrangeError(f"Health checks are not valid JSON: {exc}") from exc
    scenario_boxes(cfg)[key] = {
        "team_id": team, "name": args.name, "ip_last_octet": args.ip,
        "template": args.template, "node_name": args.node,
        "cores": args.cores, "memory_mb": args.memory,
        "full_clone": args.full_clone if args.full_clone is not None else args.node != "proxmox4",
        "started": True, "health_checks": checks, "guac_protocol": getattr(args, "guac_protocol", "ssh"),
    }
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
    if args.node not in NODES:
        raise CrangeError("Node must be one of proxmox1 through proxmox5.")
    if not 1 <= args.cores <= 16 or not 512 <= args.memory <= 65536:
        raise CrangeError("Cores must be 1-16 and memory 512-65536 MB.")
    try:
        checks = validate_health_checks(json.loads(args.health))
    except json.JSONDecodeError as exc:
        raise CrangeError(f"Health checks are not valid JSON: {exc}") from exc
    box.update({"template": args.template, "node_name": args.node,
                "cores": args.cores, "memory_mb": args.memory,
                "health_checks": checks, "guac_protocol": getattr(args, "guac_protocol", None) or box.get("guac_protocol", "ssh")})
    if args.node != "proxmox4":
        box["full_clone"] = True
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
        "cores": args.cores, "memory_mb": args.memory, "started": True,
        "network_key": network_key, "network_id": network_id,
        "health_checks": checks, "guac_protocol": getattr(args, "guac_protocol", "ssh"),
    }
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
    if args.node not in NODES:
        raise CrangeError("Node must be one of proxmox1 through proxmox5.")
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
    payload = {"health_address": address, "health_check": check}
    run(["ansible-playbook", "-i", str(inventory), "--limit", host,
         "--extra-vars", json.dumps(payload), str(ROOT / "ansible" / "scenario-health.yml")],
        cwd=ROOT, capture=True, env={**os.environ, "ANSIBLE_TIMEOUT": "90"})


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
    validate_scenario_config(path, cfg)
    validate_container_config(path, cfg)
    boxes = [dict(key=key, kind="vm", **box) for key, box in sorted(scenario_boxes(cfg).items()) if str(box["team_id"]) == "254"]
    boxes += [dict(key=key, kind="container", **box) for key, box in sorted(scenario_containers(cfg).items()) if str(box["team_id"]) == "254"]
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
            expected_address = f"10.{project}.254.{box['ip_last_octet']}"
            if not live or live.get("address") != expected_address:
                raise CrangeError(f"Staging VM {box['key']} is not applied at {expected_address}.")
            for check in box.get("health_checks", []):
                check_endpoint(project, "254", expected_address, check)
        api = ProxmoxAPI()
        approvals = templates_for(path)
        ct_approvals = container_templates_for(path)
        references = {}
        captured = []
        for box in boxes:
            node = box["node_name"]
            vmid = int(applied[box["kind"]][box["key"]]["id"])
            actual = api.vm(node, vmid) if box["kind"] == "vm" else api.container(node, vmid)
            if actual.get("name", actual.get("hostname")) != f"p{project:03d}t254-{box['name']}" or int(actual.get("template", 0)):
                raise CrangeError(f"Applied VM {node}/{vmid} does not match staging box {box['key']}.")
            status = api.status(node, vmid) if box["kind"] == "vm" else api.container_status(node, vmid)
            if status != "running":
                raise CrangeError(f"Staging VM {node}/{vmid} must be running before capture.")
        revision = run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture=True).stdout.strip()
        dirty = bool(run(["git", "status", "--porcelain"], cwd=ROOT, capture=True).stdout.strip())
        try:
            for box in boxes:
                node = box["node_name"]
                vmid = int(applied[box["kind"]][box["key"]]["id"])
                slug = hashlib.sha256(f"{project}:{args.version}:{box['key']}".encode()).hexdigest()[:12]
                alias = f"p{project:03d}-image-{slug}"
                name = f"p{project:03d}-release-{slug}-{box['name'][:15]}"
                original_template = box["template"]
                try:
                    if box["kind"] == "vm":
                        api.shutdown(node, vmid, f"10.{project}.254.{box['ip_last_octet']}")
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
                references[alias] = reference
                captured.append((node, image_id, box["kind"]))
                box["source_template"] = original_template
                box["source_vm_id"] = vmid
                box["template"] = alias
                box["full_clone"] = False
            permutations = [entry.strip().split(",") for entry in getattr(args, "boot_order", [])]
            scenario_names = {box["name"] for box in boxes}
            if not permutations:
                permutations = [[box["name"] for box in boxes]]
            for order in permutations:
                if len(order) != len(scenario_names) or set(order) != scenario_names or len(set(order)) != len(order):
                    raise CrangeError("Each --boot-order must list every staging VM name exactly once, comma-separated.")
            content = {
                "schema_version": 3,
                "release_id": f"p{project:03d}-{args.version}",
                "version": args.version,
                "project_id": project,
                "source_revision": revision,
                "source_tree_dirty": dirty,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "scenario": {"boxes": [{k: v for k, v in b.items() if k != "kind"} for b in boxes if b["kind"] == "vm"],
                             "containers": [{k: v for k, v in b.items() if k != "kind"} for b in boxes if b["kind"] == "container"],
                             "templates": references,
                             "boot_order_permutations": permutations,
                             "team_overrides": {}},
            }
            content["integrity_sha256"] = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            # Only expose image aliases after every capture has succeeded.
            approvals.update({k: {"template_id": v["template_id"], "template_node": v["template_node"]} for k, v in references.items() if v["kind"] == "vm"})
            ct_approvals.update({k: {"template_id": v["template_id"], "template_node": v["template_node"]} for k, v in references.items() if v["kind"] == "container"})
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
    if not wanted or any(t not in set(map(str, cfg.get("team_ids", []))) for t in wanted):
        raise CrangeError("Choose one or more teams already declared in this project.")
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
            raise CrangeError("Override node must be proxmox1 through proxmox5.")
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
        additions = release_scenario_boxes(path, release, team, f"r{args.version}")
        for key, box in additions.items():
            source_name = next(x["name"] for x in release["scenario"]["boxes"] if key.endswith(f"-{x['name']}-t{int(team):03d}") or key == f"r{args.version}-{x['name']}-t{int(team):03d}")
            for field, value in overrides.get(team, {}).get(source_name, {}).items():
                box[field] = value
                if field == "node_name" and value != release["scenario"]["templates"][box["template"]]["template_node"]:
                    box["full_clone"] = True
            box["release_source_name"] = source_name
            current = scenario_boxes(cfg).get(key)
            if current and current != box:
                raise CrangeError(f"Existing scenario VM {key} differs from this release; refusing to overwrite it.")
            scenario_boxes(cfg)[key] = box
        ct_additions = release_scenario_containers(path, release, team, f"r{args.version}")
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
    validate_scenario_config(path, cfg)
    save_config(path, cfg)
    print(f"Release {args.version} declared for teams {', '.join(wanted)}. Review infra-plan before applying fresh clones.")


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
    used = {int(b["ip_last_octet"]) for b in scenario_boxes(cfg).values() if str(b["team_id"]) == "254"}
    used.update(int(b["ip_last_octet"]) for b in scenario_containers(cfg).values() if str(b["team_id"]) == "254")
    free = [host for host in range(90, 101) if host not in used]
    boxes = release["scenario"]["boxes"]
    # Verify all pinned image references before mutating desired state.
    release_scenario_boxes(path, release, "254", f"test{args.version}")
    if len(boxes) > len(free):
        raise CrangeError("Team 0 has only 11 reserved test-clone addresses (.90-.100); test fewer boxes per release.")
    prepared = {}
    prepared_cts = {}
    for source, host in zip(boxes, free):
        addition = release_scenario_boxes(path, {"version": args.version, "scenario": {"boxes": [source], "templates": release["scenario"]["templates"]}}, "254", f"test{args.version}")
        key, box = next(iter(addition.items()))
        box["ip_last_octet"] = host
        box["started"] = True
        if key in scenario_boxes(cfg):
            raise CrangeError(f"Test clone key {key} already exists.")
        scenario_boxes(cfg)[key] = box
        prepared[key] = {"address": f"10.{int(args.project)}.254.{host}", "health_checks": box["health_checks"]}
    ct_sources = release["scenario"].get("containers", [])
    remaining = [h for h in range(90, 101) if h not in used and h not in {v["ip_last_octet"] for v in scenario_boxes(cfg).values() if str(v["team_id"]) == "254"}]
    if len(ct_sources) > len(remaining):
        raise CrangeError("Team 0 has too few reserved test addresses (.90-.100) for release containers.")
    for source, host in zip(ct_sources, remaining):
        additions = release_scenario_containers(path, {"release_id": release["release_id"], "scenario": {"containers": [source], "templates": release["scenario"]["templates"]}}, "254", f"test{args.version}")
        key, box = next(iter(additions.items()))
        box["ip_last_octet"] = host
        box["started"] = True
        scenario_containers(cfg)[key] = box
        prepared_cts[key] = {"address": f"10.{int(args.project)}.254.{host}", "health_checks": source.get("health_checks", [])}
    validate_scenario_config(path, cfg)
    save_config(path, cfg)
    record_path.write_text(json.dumps({"version": args.version, "boxes": prepared, "containers": prepared_cts}, indent=2) + "\n")
    record_path.chmod(0o640)
    print(f"Declared {len(prepared)} fresh Team 0 test clones for release {args.version}.")
    print("Run infra-plan and infra-apply, then release-test-check. After checking, run release-test-cleanup and apply its reviewed destroy plan.")


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
    vmids = {}
    for key in record["boxes"]:
        if key not in applied_boxes:
            continue
        source = next((b for b in release["scenario"]["boxes"] if key.endswith(f"-{b['name']}-t254")), None)
        if source:
            vmids[source["name"]] = (source["node_name"], int(applied_boxes[key]["id"]))
    ctids = {}
    for key in record.get("containers", {}):
        live = applied_cts.get(key)
        source = next((b for b in release["scenario"].get("containers", []) if key.endswith(f"-{b['name']}-t254")), None)
        if live and source:
            ctids[source["name"]] = (source["node_name"], int(live["id"]))
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
                    check_endpoint(project, "254", live["address"], check)
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
    for key in record["boxes"]:
        scenario_boxes(cfg).pop(key, None)
    for key in record.get("containers", {}):
        scenario_containers(cfg).pop(key, None)
    validate_scenario_config(path, cfg)
    save_config(path, cfg)
    result_dir = ROOT / "releases" / f"project-{project:03d}" / "test-results"
    result_dir.mkdir(parents=True, exist_ok=True)
    archived = result_dir / f"{args.version}-test-clones.json"
    archived.write_text(json.dumps({"version": args.version, "test_clones": sorted(record["boxes"]), "cleaned_up_at": datetime.now(timezone.utc).isoformat()}, indent=2) + "\n")
    record_path.unlink()
    print("Removed test clones from desired state. Review infra-plan and use infra-apply --yes --allow-destroy to destroy them.")


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
        AuthStore(Path.home() / '.local/share/crange/auth.sqlite3').set_password(
            args.username, password, admin=args.admin, projects=args.projects)
    except ValueError as exc:
        raise CrangeError(str(exc)) from exc
    print(f'Password set for {args.username}. Existing sessions revoked.')


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="crange", description="Manage CRANGE teams, jump boxes, and reviewed Terraform/Ansible workflows.")
    sub = p.add_subparsers(dest="command", required=True)
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
    q.set_defaults(func=cmd_password)
    q = sub.add_parser("projects", help="List Terraform project environments"); q.set_defaults(func=cmd_projects)
    q = sub.add_parser("project-init", help="Create a new standardized project environment from project-template")
    q.add_argument("project"); q.add_argument("--transit", required=True); q.set_defaults(func=cmd_project_init)
    q = sub.add_parser("project-show", help="Show declared and applied project resources"); q.add_argument("project"); q.set_defaults(func=cmd_project_show)
    q = sub.add_parser("guac-sync", help="Render reviewed Guacamole access desired state"); q.add_argument("project"); q.set_defaults(func=cmd_guac_sync)
    q = sub.add_parser("guac-apply", help="Apply a Guacamole access manifest through the configured connector"); q.add_argument("project"); q.set_defaults(func=cmd_guac_apply)
    q = sub.add_parser("guac-config", help="Configure the protected Guacamole connector credentials"); q.set_defaults(func=cmd_guac_config)
    for verb, add in (("team-add", True), ("team-remove", False)):
        q = sub.add_parser(verb); q.add_argument("project"); q.add_argument("team", type=int); q.set_defaults(func=lambda a, add=add: mutate_team(a, add))
    for verb, add in (("network-add", True), ("network-remove", False)):
        q = sub.add_parser(verb, help="Create or remove a routed custom /24 network")
        q.add_argument("project"); q.add_argument("name")
        if add:
            q.add_argument("--team", type=int, required=True, help="Parent team, or 0 for staging")
            q.add_argument("--id", type=int, required=True, help="Unused project subnet number 1-253")
            q.add_argument("--node", required=True, choices=sorted(NODES))
        q.set_defaults(func=lambda a, add=add: mutate_custom_network(a, add))
    q = sub.add_parser("network-edit", help="Edit a custom routed network's subnet, router node, uplink, and size")
    q.add_argument("project"); q.add_argument("name"); q.add_argument("--id", type=int, required=True)
    q.add_argument("--node", required=True, choices=sorted(NODES)); q.add_argument("--uplink", type=int, required=True)
    q.add_argument("--cores", type=int, required=True); q.add_argument("--memory", type=int, required=True)
    q.set_defaults(func=cmd_network_edit)
    q = sub.add_parser("box-list"); q.add_argument("project"); q.set_defaults(func=cmd_boxes)
    q = sub.add_parser("box-state", help="Set desired jump box power state"); q.add_argument("project"); q.add_argument("name"); q.add_argument("state", choices=("start", "stop")); q.set_defaults(func=cmd_box_state)
    for verb, add in (("box-add", True), ("box-remove", False)):
        q = sub.add_parser(verb); q.add_argument("project"); q.add_argument("name")
        if add:
            q.add_argument("--team", type=int, required=True); q.add_argument("--network", help="Custom network key; defaults to the team's standard LAN"); q.add_argument("--node", required=True, choices=sorted(NODES)); q.add_argument("--cores", type=int, default=2); q.add_argument("--memory", type=int, default=4096, help="RAM in MB"); q.add_argument("--full-clone", action="store_true", default=None, help="Use a full clone (required when deploying away from the template node)")
        q.set_defaults(func=lambda a, add=add: mutate_box(a, add))
    q = sub.add_parser("infra-plan", help="Initialize, plan, and summarize project changes"); q.add_argument("project"); q.add_argument("--retain", action="store_true", help="Keep plan for exact reviewed apply"); q.add_argument("--replace-sdn-applier", action="store_true", help="Repair a partially applied SDN network by reapplying cluster SDN"); q.set_defaults(func=cmd_plan)
    q = sub.add_parser("infra-apply", help="Apply the exact reviewed plan"); q.add_argument("project"); q.add_argument("--yes", action="store_true"); q.add_argument("--plan-id", help="Apply a saved, integrity-checked plan"); q.add_argument("--allow-destroy", action="store_true"); q.add_argument("--allow-replace", action="store_true"); q.set_defaults(func=cmd_apply)
    q = sub.add_parser("stage-deploy", help="Apply the reviewed staging plan and configure routes, routers, and jump boxes")
    q.add_argument("project"); q.add_argument("--yes", action="store_true"); q.add_argument("--plan-id", required=True)
    q.add_argument("--allow-destroy", action="store_true"); q.add_argument("--allow-replace", action="store_true"); q.set_defaults(func=cmd_stage_deploy)
    q = sub.add_parser("configure-team", help="Configure one team's routers and jump boxes without touching other teams")
    q.add_argument("project"); q.add_argument("team", type=int, help="Team number, or 0 for staging")
    q.add_argument("--yes", action="store_true"); q.set_defaults(func=cmd_configure_team)
    q = sub.add_parser("inventory"); q.add_argument("project"); q.set_defaults(func=cmd_inventory)
    q = sub.add_parser("configure", help="Apply reviewed Ansible router routes, routers, or jumpbox config")
    q.add_argument("project"); q.add_argument("target", choices=("routes", "routers", "boxes", "all")); q.add_argument("--yes", action="store_true"); q.set_defaults(func=cmd_configure)
    q = sub.add_parser("scenario-list"); q.add_argument("project"); q.set_defaults(func=cmd_scenario_list)
    q = sub.add_parser("scenario-add", help="Declare a scenario VM in staging or a project team")
    q.add_argument("project"); q.add_argument("name"); q.add_argument("--team", type=int, required=True, help="Team number, or 0 for staging")
    q.add_argument("--ip", type=int, required=True, help="Static host octet .2-.100")
    q.add_argument("--template", required=True); q.add_argument("--node", required=True, choices=sorted(NODES))
    q.add_argument("--cores", type=int, default=2); q.add_argument("--memory", type=int, default=4096)
    q.add_argument("--full-clone", action="store_true", default=None)
    q.add_argument("--health", default="[]", help="JSON ping/TCP/HTTP health checks; empty uses ping")
    q.add_argument("--guac-protocol", choices=("ssh", "rdp"), default="ssh")
    q.set_defaults(func=cmd_scenario_add)
    q = sub.add_parser("scenario-edit", help="Edit a declared scenario VM and its health checks")
    q.add_argument("project"); q.add_argument("name")
    q.add_argument("--template", required=True); q.add_argument("--node", required=True, choices=sorted(NODES))
    q.add_argument("--cores", type=int, required=True); q.add_argument("--memory", type=int, required=True)
    q.add_argument("--health", required=True, help="JSON ping/TCP/HTTP health checks; empty uses ping")
    q.add_argument("--guac-protocol", choices=("ssh", "rdp"), default=None)
    q.set_defaults(func=cmd_scenario_edit)
    q = sub.add_parser("scenario-remove"); q.add_argument("project"); q.add_argument("name"); q.set_defaults(func=cmd_scenario_remove)
    q = sub.add_parser("container-add", help="Declare a scenario CT from an approved template")
    q.add_argument("project"); q.add_argument("name"); q.add_argument("--team", type=int, required=True)
    q.add_argument("--ip", type=int, required=True); q.add_argument("--template", required=True)
    q.add_argument("--node", required=True, choices=sorted(NODES)); q.add_argument("--network", help="Custom network key")
    q.add_argument("--cores", type=int, default=1)
    q.add_argument("--memory", type=int, default=1024); q.add_argument("--health", default="[]", help="JSON ping/TCP/HTTP readiness checks; empty uses ping"); q.add_argument("--guac-protocol", choices=("ssh", "rdp"), default="ssh"); q.set_defaults(func=cmd_container_add)
    q = sub.add_parser("container-edit", help="Edit a declared scenario CT and its health checks")
    q.add_argument("project"); q.add_argument("name"); q.add_argument("--template", required=True)
    q.add_argument("--node", required=True, choices=sorted(NODES)); q.add_argument("--ip", type=int, required=True)
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
    q = sub.add_parser("release-test-prepare", help="Declare fresh Team 0 release test clones")
    q.add_argument("project"); q.add_argument("version"); q.set_defaults(func=cmd_release_test_prepare)
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
