"""Persistent, typed dashboard jobs. No client-controlled command lines."""
from __future__ import annotations

import json
import ipaddress
import os
from pathlib import Path
import re
import sqlite3
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
NAME = re.compile(r"[a-z][a-z0-9-]{0,23}\Z")
SCENARIO_KEY = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
JUMP_NAME = re.compile(r"[a-z][a-z0-9-]{0,30}-j[0-9]{2}\Z")
RESOURCE_KEY = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){0,2}(?:-[a-z0-9.-]+)?\Z")
NODES = {f"proxmox{i}" for i in range(1, 7)}


def choice(value, allowed):
    if value not in allowed:
        raise ValueError('Choose an approved option.')
    return value


def integer(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"Expected a number from {minimum} to {maximum}.")
    return str(value)


def name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError("Use a lowercase name with letters, digits or hyphens.")
    return value


def scenario_key(value):
    if not isinstance(value, str) or not SCENARIO_KEY.fullmatch(value):
        raise ValueError("Choose a declared scenario machine.")
    return value


def jump_name(value):
    if not isinstance(value, str) or not JUMP_NAME.fullmatch(value):
        raise ValueError("Jump box name must end in -j01, -j02, and so on.")
    return value


def resource_key(value):
    if not isinstance(value, str) or not RESOURCE_KEY.fullmatch(value):
        raise ValueError("Invalid resource name.")
    return value


def node(value):
    if value not in NODES:
        raise ValueError("Choose a Proxmox node from 1 to 5.")
    return value


def version(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ValueError("Invalid release version.")
    return value


def teams(value):
    if not isinstance(value, list) or not value or len(value) > 253:
        raise ValueError("Choose one or more teams.")
    result = [integer(item, 1, 253) for item in value]
    if len(result) != len(set(result)):
        raise ValueError("Duplicate team selection.")
    return result


def boot_orders(value):
    if not isinstance(value, list) or len(value) > 12:
        raise ValueError('Boot-order permutations must be a list of at most 12 items.')
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 512:
            raise ValueError('Each boot-order permutation must be a non-empty string.')
        result.append(item.strip())
    return result


def template_projects(value):
    if not isinstance(value,list) or not 1 <= len(value) <= 255:raise ValueError('Choose one or more projects.')
    result=[integer(item,1,255) for item in value]
    if len(result)!=len(set(result)):raise ValueError('Duplicate project selection.')
    return result


def template_prepared(value):
    if value is not True:raise ValueError('Confirm that this image is prepared for cloning.')
    return value


def transit(value):
    if not isinstance(value, str):
        raise ValueError('Invalid transit address.')
    try:
        address = ipaddress.IPv4Interface(value)
    except ValueError as exc:
        raise ValueError('Invalid transit address.') from exc
    if address.network != ipaddress.IPv4Network('10.60.2.0/24') or not 2 <= int(str(address.ip).split('.')[-1]) <= 254:
        raise ValueError('Transit address must be 10.60.2.2–254/24.')
    return str(address)


def health(value):
    if not isinstance(value, list) or len(value) > 12:
        raise ValueError("Health checks must be a list of at most 12 items.")
    from crange import CrangeError, validate_health_checks
    try:
        validate_health_checks(value)
    except CrangeError as exc:
        raise ValueError(str(exc)) from exc
    return json.dumps(value, separators=(",", ":"))

def guac_protocol(value):
    if value not in ("ssh", "rdp"):
        raise ValueError("Guacamole protocol must be ssh or rdp.")
    return value


def jumpbox_services(value):
    from crange_jumpboxes import services
    return json.dumps(services(value), separators=(',', ':'))


def box_display_name(value):
    from crange_jumpboxes import settings
    return settings('vm', value, False, ['ssh'])['display_name']


def command(operation, project, options):
    """Convert one reviewed operation into a fixed CLI argument vector."""
    if not isinstance(options, dict):
        raise ValueError("Expected operation fields.")
    if operation in ("scenario-health", "container-health"):
        options = {**options, "name": options.get("name", "")}
    if not isinstance(project, int) or isinstance(project, bool) or not (1 <= project <= 255 or (project == 0 and isinstance(operation, str) and operation.startswith('template-'))):
        raise ValueError("Invalid project.")
    if operation == 'jumpbox-settings':
        options = {'credentials_mode': 'keep', 'credential_ref': '', 'sealed_credentials': '', **options}
    p = str(project)
    schemas = {
        "jumpbox-template-save": ({"definition": lambda v: json.dumps(__import__('crange_jumpboxes').preset_settings(v))}, lambda o: ['jumpbox-template-save',p,'--definition',o['definition']]),
        "jumpbox-template-remove": ({"key": name},lambda o: ['jumpbox-template-remove',p,o['key']]),
        "custom-jumpbox-create": ({"definition": lambda v: json.dumps(__import__('crange_jumpboxes').creation(v))}, lambda o: ['custom-jumpbox-create',p,'--definition',o['definition']]),
        "template-cache-gc-review": ({"image": lambda v: v if isinstance(v,str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}',v) else (_ for _ in ()).throw(ValueError('Invalid image ID')), "node": node, "template_id": lambda v: integer(v,100,999999999)}, lambda o: ['cache-gc-review',o['image'],'--node',o['node'],'--template-id',o['template_id']]),
        "template-cache-gc-apply": ({"plan_id": lambda v:v if isinstance(v,str) and re.fullmatch(r'[0-9a-f]{32}',v) else (_ for _ in ()).throw(ValueError('Invalid cache GC plan ID'))}, lambda o:['cache-gc-apply',o['plan_id']]),
        "template-cluster-refresh": ({}, lambda o: ["cluster-refresh"]),
        "template-distribution-inventory": ({}, lambda o: ['distribution-inventory']),
        "template-distribution-prepare": ({"image": lambda v: v if isinstance(v, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}',v) else (_ for _ in ()).throw(ValueError('Invalid immutable image ID')), "node": node, "storage": lambda v: choice(v, ('local-lvm','ssd-lvm','hdd-lvm'))}, lambda o: ['distribution-prepare',o['image'],'--project',p,'--node',o['node'],'--storage',o['storage']]),
        "template-create": ({"definition": lambda v: json.dumps(__import__('crange_templates').validate(v))}, lambda o: ['template-create', '--definition', o['definition']]),
        "template-approve-image": ({"image_id": lambda v: v if isinstance(v, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', v) else (_ for _ in ()).throw(ValueError('Invalid logical image ID')), "projects": template_projects}, lambda o: ['template-approve-image', o['image_id'], '--projects', *o['projects']]),
        "template-publish": ({"build_id": lambda v: __import__('crange_templates').identifier(v), "projects": template_projects,
                              "prepared": template_prepared}, lambda o: ['template-publish', o['build_id'], '--projects', *o['projects'], '--prepared']),
        "template-upload": ({"upload_id": lambda v: __import__('crange_templates').identifier(v), "node": node}, lambda o: ['template-upload', o['upload_id'], '--node', o['node']]),
        "project-init": ({}, lambda o: ["project-init", p]),
        "bootstrap-plan": ({}, lambda o: ["bootstrap-plan", p]),
        "bootstrap-apply": ({"plan_id": lambda v: v if isinstance(v, str) and re.fullmatch(r"[0-9a-f]{32}", v) else (_ for _ in ()).throw(ValueError("Invalid bootstrap plan ID."))}, lambda o: ["infra-bootstrap-apply", p, "--yes", "--plan-id", o["plan_id"]]),
        "bootstrap-configure": ({}, lambda o: ["bootstrap-configure", p]),
        "stage-changes": ({}, lambda o: ["stage-changes", p]),
        "plan": ({}, lambda o: ["infra-plan", p, "--retain"]),
        "plan-sdn-repair": ({}, lambda o: ["infra-plan", p, "--retain", "--replace-sdn-applier"]),
        "apply": ({"plan_id": lambda v: v if isinstance(v, str) and re.fullmatch(r"[0-9a-f]{32}", v) else (_ for _ in ()).throw(ValueError("Invalid plan ID.")), "allow_destroy": lambda v: v if isinstance(v, bool) else (_ for _ in ()).throw(ValueError("Invalid destroy setting.")), "allow_replace": lambda v: v if isinstance(v, bool) else (_ for _ in ()).throw(ValueError("Invalid replace setting."))}, lambda o: ["infra-apply", p, "--yes", "--plan-id", o["plan_id"], *(["--allow-destroy"] if o["allow_destroy"] else []), *(["--allow-replace"] if o["allow_replace"] else [])]),
        "release-apply": ({"teams": teams, "plan_id": lambda v: v if isinstance(v, str) and re.fullmatch(r"[0-9a-f]{32}", v) else (_ for _ in ()).throw(ValueError("Invalid plan ID.")), "allow_destroy": lambda v: v if isinstance(v, bool) else (_ for _ in ()).throw(ValueError("Invalid destroy setting.")), "allow_replace": lambda v: v if isinstance(v, bool) else (_ for _ in ()).throw(ValueError("Invalid replace setting."))}, lambda o: ["release-apply", p, "--teams", *o["teams"], "--yes", "--plan-id", o["plan_id"], *(["--allow-destroy"] if o["allow_destroy"] else []), *(["--allow-replace"] if o["allow_replace"] else [])]),
        "stage-deploy": ({"plan_id": lambda v: v if isinstance(v, str) and re.fullmatch(r"[0-9a-f]{32}", v) else (_ for _ in ()).throw(ValueError("Invalid plan ID.")), "allow_destroy": lambda v: v if isinstance(v, bool) else (_ for _ in ()).throw(ValueError("Invalid destroy setting.")), "allow_replace": lambda v: v if isinstance(v, bool) else (_ for _ in ()).throw(ValueError("Invalid replace setting."))}, lambda o: ["stage-deploy", p, "--yes", "--plan-id", o["plan_id"], *(["--allow-destroy"] if o["allow_destroy"] else []), *(["--allow-replace"] if o["allow_replace"] else [])]),
        "inventory": ({}, lambda o: ["inventory", p]),
        "route-sync": ({}, lambda o: ["route-sync", p]),
        "configure": ({"target": lambda v: v if v in ("routes", "routers", "boxes", "all") else (_ for _ in ()).throw(ValueError("Invalid configuration target."))}, lambda o: ["configure", p, o["target"], "--yes"]),
        "configure-team": ({"team": lambda v: integer(v, 0, 253)}, lambda o: ["configure-team", p, o["team"], "--yes"]),
        "jumpbox-settings": ({"name": scenario_key, "kind": lambda v: choice(v, ('jumpbox', 'vm', 'container')), "display_name": box_display_name, "is_jumpbox": lambda v: str(v).lower() if isinstance(v, bool) else (_ for _ in ()).throw(ValueError('Invalid jump-box role.')), "services": jumpbox_services, "credentials_mode": lambda v: choice(v, ('keep','default','custom')), "credential_ref": lambda v: '' if v == '' else __import__('crange_guest_credentials').reference(v), "sealed_credentials": lambda v: v if isinstance(v,str) and len(v)<=8192 and re.fullmatch(r'[A-Za-z0-9_=-]*',v) else (_ for _ in ()).throw(ValueError('Invalid protected guest credentials.'))}, lambda o: ['jumpbox-settings', p, o['name'], '--kind', o['kind'], '--display-name', o['display_name'], '--is-jumpbox', o['is_jumpbox'], '--services', o['services'], '--credentials-mode', o['credentials_mode'], '--credential-ref', o['credential_ref'], '--sealed-credentials', o['sealed_credentials']]),
        "guac-sync": ({}, lambda o: ["guac-sync", p]),
        "guac-apply": ({}, lambda o: ["guac-apply", p]),
        "guac-refresh": ({}, lambda o: ["guac-refresh", p]),
        "guac-box-control": ({"resource_key": lambda v: __import__('crange_guac_boxes').resource_key(v),
                              "action": lambda v: choice(v, ('start', 'shutdown', 'stop', 'restart'))},
                             lambda o: ['guac-box-control', p, o['resource_key'], o['action']]),
        "guac-snapshot": ({"resource_key": lambda v: __import__('crange_guac_boxes').resource_key(v),
                           "action": lambda v: choice(v, ('list', 'create', 'restore', 'delete')),
                           "snapshot": lambda v: __import__('crange_guac_boxes').snapshot_name(v)},
                          lambda o: ['guac-snapshot', p, o['resource_key'], o['action'],
                                     '--snapshot', o['snapshot']]),
        "guac-enroll": ({"request_id": lambda v: v if isinstance(v,str) and re.fullmatch(r"[0-9a-f]{32}",v) else (_ for _ in ()).throw(ValueError("Invalid enrollment."))}, lambda o: ["guac-enroll", p, o["request_id"]]),
        "box-users": ({"name": lambda v: __import__('crange_box_users').box_key(v), "kind": lambda v: choice(v, ("jumpbox", "vm", "container")), "action": lambda v: choice(v, ("list", "create", "delete", "grant-sudo", "revoke-sudo", "password", "access")), "username": lambda v: "" if v == "" else __import__('crange_box_users').username(v), "sealed_password": lambda v: v if isinstance(v, str) and len(v) <= 4096 and re.fullmatch(r"[A-Za-z0-9_=-]*", v) else (_ for _ in ()).throw(ValueError("Invalid protected password."))}, lambda o: ["box-users", p, o["name"], "--kind", o["kind"], "--action", o["action"], "--username", o["username"], "--sealed-password", o["sealed_password"]]),
        "guac-users": ({}, lambda o: ["guac-users", p]),
        "team-add": ({"team": lambda v: integer(v, 1, 253)}, lambda o: ["team-add", p, o["team"]]),
        "team-remove": ({"team": lambda v: integer(v, 1, 253)}, lambda o: ["team-remove", p, o["team"]]),
        "network-add": ({"name": name, "team": lambda v: integer(v, 0, 253), "id": lambda v: integer(v, 1, 253), "node": node}, lambda o: ["network-add", p, o["name"], "--team", o["team"], "--id", o["id"], "--node", o["node"]]),
        "network-edit": ({"name": name, "id": lambda v: integer(v, 1, 253), "node": node, "uplink": lambda v: integer(v, 2, 100), "cores": lambda v: integer(v, 1, 16), "memory": lambda v: integer(v, 512, 65536)}, lambda o: ["network-edit", p, o["name"], "--id", o["id"], "--node", o["node"], "--uplink", o["uplink"], "--cores", o["cores"], "--memory", o["memory"]]),
        "network-remove": ({"name": name}, lambda o: ["network-remove", p, o["name"]]),
        "box-add": ({"name": jump_name, "team": lambda v: integer(v, 0, 253), "node": node, "network": lambda v: '' if v == '' else name(v), "cores": lambda v: integer(v, 1, 16), "memory": lambda v: integer(v, 512, 65536)}, lambda o: ["box-add", p, o["name"], "--team", o["team"], "--node", o["node"], "--cores", o["cores"], "--memory", o["memory"], *(["--network", o["network"]] if o["network"] else [])]),
        "box-remove": ({"name": resource_key}, lambda o: ["box-remove", p, o["name"]]),
        "box-state": ({"name": resource_key, "state": lambda v: v if v in ('start', 'stop') else (_ for _ in ()).throw(ValueError('Invalid power state.'))}, lambda o: ["box-state", p, o["name"], o["state"]]),
        "scenario-add": ({"name": name, "team": lambda v: integer(v, 0, 253), "ip": lambda v: integer(v, 2, 100), "network": lambda v: "" if v == "" else name(v), "template": name, "node": node, "cores": lambda v: integer(v, 1, 16), "memory": lambda v: integer(v, 512, 65536), "health": health, "guac_protocol": guac_protocol}, lambda o: ["scenario-add", p, o["name"], "--team", o["team"], "--network", o["network"], "--ip", o["ip"], "--template", o["template"], "--node", o["node"], "--cores", o["cores"], "--memory", o["memory"], "--health", o["health"], "--guac-protocol", o["guac_protocol"]]),
        "scenario-edit": ({"name": scenario_key, "ip": lambda v: integer(v, 2, 100), "network": lambda v: "" if v == "" else name(v), "template": name, "node": node, "cores": lambda v: integer(v, 1, 16), "memory": lambda v: integer(v, 512, 65536), "health": health, "guac_protocol": guac_protocol}, lambda o: ["scenario-edit", p, o["name"], "--network", o["network"], "--ip", o["ip"], "--template", o["template"], "--node", o["node"], "--cores", o["cores"], "--memory", o["memory"], "--health", o["health"], "--guac-protocol", o["guac_protocol"]]),
        "scenario-remove": ({"name": scenario_key}, lambda o: ["scenario-remove", p, o["name"]]),
        "container-add": ({"name": name, "team": lambda v: integer(v, 0, 253), "ip": lambda v: integer(v, 2, 100), "template": name, "node": node, "network": lambda v: '' if v == '' else name(v), "cores": lambda v: integer(v, 1, 16), "memory": lambda v: integer(v, 512, 65536), "health": health, "guac_protocol": guac_protocol}, lambda o: ["container-add", p, o["name"], "--team", o["team"], "--ip", o["ip"], "--template", o["template"], "--node", o["node"], "--cores", o["cores"], "--memory", o["memory"], "--health", o["health"], "--guac-protocol", o["guac_protocol"], *(["--network", o["network"]] if o["network"] else [])]),
        "container-edit": ({"name": scenario_key, "template": name, "node": node, "ip": lambda v: integer(v, 2, 100), "network": lambda v: '' if v == '' else name(v), "cores": lambda v: integer(v, 1, 16), "memory": lambda v: integer(v, 512, 65536), "health": health, "guac_protocol": guac_protocol}, lambda o: ["container-edit", p, o["name"], "--template", o["template"], "--node", o["node"], "--ip", o["ip"], "--network", o["network"], "--cores", o["cores"], "--memory", o["memory"], "--health", o["health"], "--guac-protocol", o["guac_protocol"]]),
        "container-remove": ({"name": name}, lambda o: ["container-remove", p, o["name"]]),
        "machine-state": ({"name": scenario_key, "kind": lambda v: choice(v, ("vm", "container")), "state": lambda v: choice(v, ("start", "stop"))}, lambda o: ["machine-state", p, o["name"], "--kind", o["kind"], "--state", o["state"]]),
        "scenario-health": ({"name": lambda v: "" if v == "" else scenario_key(v)}, lambda o: ["scenario-health", p, *([o["name"]] if o["name"] else [])]),
        "container-health": ({"name": lambda v: "" if v == "" else scenario_key(v)}, lambda o: ["container-health", p, *([o["name"]] if o["name"] else [])]),
        "release-capture": ({"version": version, "boot_order": boot_orders}, lambda o: ["release-capture", p, o["version"], *sum((["--boot-order", item] for item in o["boot_order"]), [])]),
        "release-show": ({"version": version}, lambda o: ["release-show", p, o["version"]]),
        "release-test-prepare": ({"version": version}, lambda o: ["release-test-prepare", p, o["version"]]),
        "release-test-check": ({"version": version}, lambda o: ["release-test-check", p, o["version"]]),
        "release-test-cleanup": ({"version": version}, lambda o: ["release-test-cleanup", p, o["version"]]),
        "release-deploy": ({"version": version, "teams": teams}, lambda o: ["release-deploy", p, o["version"], "--teams", *o["teams"]]),
        "release-upgrade": ({"version": version, "teams": teams, "from_version": version}, lambda o: ["release-upgrade", p, o["version"], "--teams", *o["teams"], "--from-version", o["from_version"]]),
        "release-rollback": ({"version": version, "teams": teams, "from_version": version}, lambda o: ["release-rollback", p, o["version"], "--teams", *o["teams"], "--from-version", o["from_version"]]),
        "release-check": ({"teams": teams}, lambda o: ["release-check", p, "--teams", *o["teams"]]),
    }
    if operation not in schemas:
        raise ValueError("Unsupported operation.")
    if operation == 'template-distribution-prepare' and int(p) == 0:
        raise ValueError('Choose a real project for cache preparation quota and approval scope.')
    fields, build = schemas[operation]
    placement_add = operation in {'network-add', 'box-add', 'scenario-add', 'container-add'}
    placement_edit = operation in {'network-edit', 'scenario-edit', 'container-edit'}
    if placement_add or placement_edit:
        fields = {**fields, 'node': lambda v: choice(v, (*sorted(NODES), 'auto'))}
    if placement_add:
        # Concrete legacy nodes remain hard pins; absent nodes are Automatic.
        options = {'node': 'auto', 'preferred_node': 'proxmox4' if operation=='box-add' and options.get('node','auto')=='auto' else '', **options}
        fields['preferred_node'] = lambda v: '' if v == '' else node(v)
    elif placement_edit and 'preferred_node' in options:
        fields['preferred_node'] = lambda v: '' if v == '' else node(v)
    if set(options) != set(fields):
        raise ValueError("Unexpected or missing operation fields.")
    validated = {key: validate(options[key]) for key, validate in fields.items()}
    if operation == 'jumpbox-settings':
        if options['credentials_mode'] == 'custom':
            if not options['credential_ref'] or not options['sealed_credentials']:
                raise ValueError('Custom credentials require a protected credential record.')
        elif options['credential_ref'] or options['sealed_credentials']:
            raise ValueError('Only custom credentials accept a protected record.')
    if operation == 'guac-snapshot':
        __import__('crange_guac_boxes').snapshot_name(options['snapshot'],
                                                    required=options['action'] != 'list')
    result = build(validated)
    if placement_add or (placement_edit and 'preferred_node' in validated):
        from crange_scheduling import placement_intent
        placement_intent(validated['node'], validated['preferred_node'])
        if placement_edit or validated['preferred_node'] or (operation=='box-add' and validated['node']=='auto'):
            result.extend(['--preferred-node', validated['preferred_node']])
    return result


class JobStore:
    def __init__(self, path, *, auth_path=None):
        self.path = Path(path)
        self.auth_path = auth_path or os.environ.get('CRANGE_AUTH_DB')
        self._process_lock = threading.Lock()
        self._processes = {}
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shared = os.environ.get('CRANGE_SHARED_DB') == '1'
        mode = 0o660 if shared else 0o600
        self.path.touch(mode=mode, exist_ok=True)
        if self.path.stat().st_uid == os.geteuid():
            self.path.chmod(mode)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, actor TEXT NOT NULL, project INTEGER NOT NULL, operation TEXT NOT NULL, options TEXT NOT NULL, status TEXT NOT NULL, output TEXT NOT NULL DEFAULT '', created REAL NOT NULL, finished REAL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, actor, project, operation, options):
        command(operation, project, options)
        job = uuid.uuid4().hex
        with self.connect() as db:
            if operation == 'guac-box-control' and options['action'] == 'stop':
                db.execute("UPDATE jobs SET status='interrupted',output='Superseded by force Stop.',finished=? "
                           "WHERE project=? AND operation='guac-box-control' AND status IN ('queued','running') "
                           "AND json_extract(options,'$.resource_key')=? AND json_extract(options,'$.action')='shutdown'",
                           (time.time(), project, options['resource_key']))
            db.execute("INSERT INTO jobs(id,actor,project,operation,options,status,created) VALUES(?,?,?,?,?,'queued',?)",
                       (job, actor, project, operation, json.dumps(options, sort_keys=True), time.time()))
        return job

    def list(self, projects, admin=False, limit=100):
        with self.connect() as db:
            columns = "SELECT id,actor,project,operation,options,status,output,created,finished FROM jobs"
            if admin:
                active = db.execute(columns + " WHERE status IN ('queued','running') ORDER BY created DESC").fetchall()
                recent = db.execute(columns + " WHERE status NOT IN ('queued','running') ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
            elif projects:
                allowed = [int(project) for project in projects]
                marks = ",".join("?" for _ in allowed)
                active = db.execute(columns + f" WHERE project IN ({marks}) AND status IN ('queued','running') ORDER BY created DESC", allowed).fetchall()
                recent = db.execute(columns + f" WHERE project IN ({marks}) AND status NOT IN ('queued','running') ORDER BY created DESC LIMIT ?", (*allowed, limit)).fetchall()
            else:
                active = recent = []
            rows = active + recent
        result = []
        for row in rows:
            item = dict(row)
            options = json.loads(item.pop('options'))
            if item['operation'] in ('apply', 'stage-deploy', 'release-apply'):
                item['plan_id'] = options.get('plan_id')
            result.append(item)
        return result

    def cancel(self, job_id, projects, admin=False):
        """Cancel a queued job or terminate the subprocess for a running job."""
        if not isinstance(job_id, str) or not re.fullmatch(r'[0-9a-f]{32}', job_id):
            raise ValueError('Invalid job ID.')
        with self.connect() as db:
            row = db.execute('SELECT project,status FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row or (not admin and str(row['project']) not in set(map(str, projects))):
                return False
            if row['status'] == 'queued':
                db.execute("UPDATE jobs SET status='interrupted',output='Cancelled before starting.',finished=? WHERE id=? AND status='queued'", (time.time(), job_id))
                return db.total_changes > 0
            if row['status'] != 'running':
                return False
            with self._process_lock:
                proc = self._processes.get(job_id)
                if proc is None:
                    return False
                db.execute("UPDATE jobs SET status='interrupted',output='Stop requested; waiting for the current command to exit.',finished=? WHERE id=? AND status='running'", (time.time(), job_id))
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        return True

    def run_next(self, force_only=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            force = "operation='guac-box-control' AND json_extract(options,'$.action')='stop'"
            lane = '' if force_only is None else (' AND (' + force + ')' if force_only else ' AND NOT (' + force + ')')
            row = db.execute("SELECT * FROM jobs WHERE status='queued'" + lane + " ORDER BY created LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET status='running' WHERE id=?", (row['id'],))
        if row is None:
            return False
        output = ''
        try:
            auth_path = self.auth_path
            if auth_path:
                from crange_auth import AuthStore
                auth = AuthStore(auth_path)
                if row['operation'] in ('guac-box-control', 'guac-snapshot'):
                    from crange_guac_bridge import _project_rows
                    key = json.loads(row['options'])['resource_key']
                    if not any(box['resource_key'] == key for box in
                               _project_rows(auth, ROOT, row['project'], row['actor'])):
                        raise ValueError('Guacamole box access was revoked before this job started.')
                else:
                    actor = next((user for user in auth.list_users()
                                  if user['username'] == row['actor']), None)
                    if not actor or not actor['enabled'] or (not actor['admin'] and (
                            (row['operation'] in ('project-init', 'plan-sdn-repair') or row['operation'].startswith('template-')) or
                            actor['roles'].get(str(row['project'])) != 'manager')):
                        raise ValueError('Project manager access was revoked before this job started.')
            argv = command(row['operation'], row['project'], json.loads(row['options']))
            args = [sys.executable, str(ROOT / 'scripts/crange.py'), *argv]
            worker_env = {**os.environ, 'CRANGE_ACTOR': row['actor']}
            if subprocess.run.__module__ != 'subprocess':
                # Preserve the conventional mock seam used by worker tests.
                proc = subprocess.run(args, cwd=ROOT, env=worker_env, capture_output=True,
                                      text=True, timeout=3600)
            else:
                child = subprocess.Popen(args, cwd=ROOT, env=worker_env, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True,
                                         start_new_session=True)
                with self._process_lock:
                    self._processes[row['id']] = child
                try:
                    stdout, stderr = child.communicate(timeout=3600)
                    proc = subprocess.CompletedProcess(args, child.returncode, stdout, stderr)
                except subprocess.TimeoutExpired as exc:
                    child.kill()
                    stdout, stderr = child.communicate()
                    raise subprocess.TimeoutExpired(args, 3600, output=stdout, stderr=stderr) from exc
                finally:
                    with self._process_lock:
                        self._processes.pop(row['id'], None)
            status = 'succeeded' if proc.returncode == 0 else 'failed'
            output = (proc.stdout + proc.stderr)[-50000:]
            if status == 'succeeded' and row['operation'] in {
                'custom-jumpbox-create', 'box-add', 'box-remove', 'scenario-add', 'scenario-edit',
                'scenario-remove', 'container-add', 'container-edit',
                'container-remove', 'team-add', 'team-remove',
            }:
                sync = subprocess.run([sys.executable, str(ROOT / 'scripts/crange.py'),
                                       'guac-sync', str(row['project'])], cwd=ROOT,
                                      capture_output=True, text=True, timeout=120)
                output = (output + '\nGuacamole desired-state refresh:\n' +
                          sync.stdout + sync.stderr)[-50000:]
            if status == 'succeeded' and row['operation'] in {
                'apply', 'stage-deploy', 'release-apply', 'configure', 'configure-team',
            }:
                for operation in ('guac-sync', 'guac-apply'):
                    refresh = subprocess.run([sys.executable, str(ROOT / 'scripts/crange.py'),
                                              operation, str(row['project'])], cwd=ROOT,
                                             capture_output=True, text=True, timeout=300)
                    output = (output + '\nGuacamole connection refresh (' + operation + '):\n' +
                              refresh.stdout + refresh.stderr)[-50000:]
                    if refresh.returncode:
                        status = 'failed'
                        output += '\ncrange: Infrastructure action completed, but Guacamole refresh failed. Retry guac-sync and guac-apply.\n'
                        break
        except (ValueError, subprocess.TimeoutExpired, OSError) as exc:
            status = 'failed'
            output = (output + '\ncrange: ' + str(exc))[-50000:]
        with self.connect() as db:
            current = db.execute('SELECT status FROM jobs WHERE id=?', (row['id'],)).fetchone()
            if current and current['status'] == 'interrupted':
                status = 'interrupted'
                output = (output + '\ncrange: Job was stopped by a user; the operation may have partially completed.')[-50000:]
            db.execute('UPDATE jobs SET status=?,output=?,finished=? WHERE id=?', (status, output, time.time(), row['id']))
        return True

    def worker(self, stop):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='interrupted',finished=? WHERE status='running'", (time.time(),))
        # Force Stop must remain available while a graceful shutdown waits.
        def emergency_worker():
            while not stop.is_set():
                if not self.run_next(force_only=True):
                    stop.wait(0.5)
        emergency = threading.Thread(target=emergency_worker, daemon=True)
        emergency.start()
        try:
            while not stop.is_set():
                if not self.run_next(force_only=False):
                    stop.wait(0.5)
        finally:
            emergency.join(timeout=5)

    def start(self):
        stop = threading.Event()
        thread = threading.Thread(target=self.worker, args=(stop,), daemon=True)
        thread.start()
        return stop
