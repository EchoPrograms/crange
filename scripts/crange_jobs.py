"""Persistent, typed dashboard jobs. No client-controlled command lines."""
from __future__ import annotations

import json
import ipaddress
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
NAME = re.compile(r"[a-z][a-z0-9-]{0,23}\Z")
SCENARIO_KEY = re.compile(r"t(?:[0-9]{3})-[a-z][a-z0-9-]{0,23}\Z")
JUMP_NAME = re.compile(r"[a-z][a-z0-9-]{0,30}-j[0-9]{2}\Z")
RESOURCE_KEY = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){0,2}(?:-[a-z0-9.-]+)?\Z")
NODES = {f"proxmox{i}" for i in range(1, 6)}


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


def command(operation, project, options):
    """Convert one reviewed operation into a fixed CLI argument vector."""
    if not isinstance(options, dict):
        raise ValueError("Expected operation fields.")
    if operation in ("scenario-health", "container-health"):
        options = {**options, "name": options.get("name", "")}
    if not isinstance(project, int) or isinstance(project, bool) or not 1 <= project <= 255:
        raise ValueError("Invalid project.")
    p = str(project)
    schemas = {
        "project-init": ({"transit": transit}, lambda o: ["project-init", p, "--transit", o["transit"]]),
        "plan": ({}, lambda o: ["infra-plan", p, "--retain"]),
        "plan-sdn-repair": ({}, lambda o: ["infra-plan", p, "--retain", "--replace-sdn-applier"]),
        "apply": ({"plan_id": lambda v: v if isinstance(v, str) and re.fullmatch(r"[0-9a-f]{32}", v) else (_ for _ in ()).throw(ValueError("Invalid plan ID.")), "allow_destroy": lambda v: v if isinstance(v, bool) else (_ for _ in ()).throw(ValueError("Invalid destroy setting.")), "allow_replace": lambda v: v if isinstance(v, bool) else (_ for _ in ()).throw(ValueError("Invalid replace setting."))}, lambda o: ["infra-apply", p, "--yes", "--plan-id", o["plan_id"], *(["--allow-destroy"] if o["allow_destroy"] else []), *(["--allow-replace"] if o["allow_replace"] else [])]),
        "stage-deploy": ({"plan_id": lambda v: v if isinstance(v, str) and re.fullmatch(r"[0-9a-f]{32}", v) else (_ for _ in ()).throw(ValueError("Invalid plan ID.")), "allow_destroy": lambda v: v if isinstance(v, bool) else (_ for _ in ()).throw(ValueError("Invalid destroy setting.")), "allow_replace": lambda v: v if isinstance(v, bool) else (_ for _ in ()).throw(ValueError("Invalid replace setting."))}, lambda o: ["stage-deploy", p, "--yes", "--plan-id", o["plan_id"], *(["--allow-destroy"] if o["allow_destroy"] else []), *(["--allow-replace"] if o["allow_replace"] else [])]),
        "inventory": ({}, lambda o: ["inventory", p]),
        "configure": ({"target": lambda v: v if v in ("routes", "routers", "boxes", "all") else (_ for _ in ()).throw(ValueError("Invalid configuration target."))}, lambda o: ["configure", p, o["target"], "--yes"]),
        "configure-team": ({"team": lambda v: integer(v, 0, 253)}, lambda o: ["configure-team", p, o["team"], "--yes"]),
        "guac-sync": ({}, lambda o: ["guac-sync", p]),
        "guac-apply": ({}, lambda o: ["guac-apply", p]),
        "team-add": ({"team": lambda v: integer(v, 1, 253)}, lambda o: ["team-add", p, o["team"]]),
        "team-remove": ({"team": lambda v: integer(v, 1, 253)}, lambda o: ["team-remove", p, o["team"]]),
        "network-add": ({"name": name, "team": lambda v: integer(v, 0, 253), "id": lambda v: integer(v, 1, 253), "node": node}, lambda o: ["network-add", p, o["name"], "--team", o["team"], "--id", o["id"], "--node", o["node"]]),
        "network-edit": ({"name": name, "id": lambda v: integer(v, 1, 253), "node": node, "uplink": lambda v: integer(v, 2, 100), "cores": lambda v: integer(v, 1, 16), "memory": lambda v: integer(v, 512, 65536)}, lambda o: ["network-edit", p, o["name"], "--id", o["id"], "--node", o["node"], "--uplink", o["uplink"], "--cores", o["cores"], "--memory", o["memory"]]),
        "network-remove": ({"name": name}, lambda o: ["network-remove", p, o["name"]]),
        "box-add": ({"name": jump_name, "team": lambda v: integer(v, 0, 253), "node": node, "network": lambda v: '' if v == '' else name(v), "cores": lambda v: integer(v, 1, 16), "memory": lambda v: integer(v, 512, 65536)}, lambda o: ["box-add", p, o["name"], "--team", o["team"], "--node", o["node"], "--cores", o["cores"], "--memory", o["memory"], *(["--network", o["network"]] if o["network"] else [])]),
        "box-remove": ({"name": resource_key}, lambda o: ["box-remove", p, o["name"]]),
        "box-state": ({"name": resource_key, "state": lambda v: v if v in ('start', 'stop') else (_ for _ in ()).throw(ValueError('Invalid power state.'))}, lambda o: ["box-state", p, o["name"], o["state"]]),
        "scenario-add": ({"name": name, "team": lambda v: integer(v, 0, 253), "ip": lambda v: integer(v, 2, 100), "template": name, "node": node, "cores": lambda v: integer(v, 1, 16), "memory": lambda v: integer(v, 512, 65536), "health": health, "guac_protocol": guac_protocol}, lambda o: ["scenario-add", p, o["name"], "--team", o["team"], "--ip", o["ip"], "--template", o["template"], "--node", o["node"], "--cores", o["cores"], "--memory", o["memory"], "--health", o["health"], "--guac-protocol", o["guac_protocol"]]),
        "scenario-edit": ({"name": scenario_key, "template": name, "node": node, "cores": lambda v: integer(v, 1, 16), "memory": lambda v: integer(v, 512, 65536), "health": health, "guac_protocol": guac_protocol}, lambda o: ["scenario-edit", p, o["name"], "--template", o["template"], "--node", o["node"], "--cores", o["cores"], "--memory", o["memory"], "--health", o["health"], "--guac-protocol", o["guac_protocol"]]),
        "scenario-remove": ({"name": scenario_key}, lambda o: ["scenario-remove", p, o["name"]]),
        "container-add": ({"name": name, "team": lambda v: integer(v, 0, 253), "ip": lambda v: integer(v, 2, 100), "template": name, "node": node, "network": lambda v: '' if v == '' else name(v), "cores": lambda v: integer(v, 1, 16), "memory": lambda v: integer(v, 512, 65536), "health": health, "guac_protocol": guac_protocol}, lambda o: ["container-add", p, o["name"], "--team", o["team"], "--ip", o["ip"], "--template", o["template"], "--node", o["node"], "--cores", o["cores"], "--memory", o["memory"], "--health", o["health"], "--guac-protocol", o["guac_protocol"], *(["--network", o["network"]] if o["network"] else [])]),
        "container-edit": ({"name": scenario_key, "template": name, "node": node, "ip": lambda v: integer(v, 2, 100), "network": lambda v: '' if v == '' else name(v), "cores": lambda v: integer(v, 1, 16), "memory": lambda v: integer(v, 512, 65536), "health": health, "guac_protocol": guac_protocol}, lambda o: ["container-edit", p, o["name"], "--template", o["template"], "--node", o["node"], "--ip", o["ip"], "--network", o["network"], "--cores", o["cores"], "--memory", o["memory"], "--health", o["health"], "--guac-protocol", o["guac_protocol"]]),
        "container-remove": ({"name": name}, lambda o: ["container-remove", p, o["name"]]),
        "scenario-health": ({"name": lambda v: "" if v == "" else scenario_key(v)}, lambda o: ["scenario-health", p, *([o["name"]] if o["name"] else [])]),
        "container-health": ({"name": lambda v: "" if v == "" else scenario_key(v)}, lambda o: ["container-health", p, *([o["name"]] if o["name"] else [])]),
        "release-capture": ({"version": version, "boot_order": boot_orders}, lambda o: ["release-capture", p, o["version"], *sum((["--boot-order", item] for item in o["boot_order"]), [])]),
        "release-show": ({"version": version}, lambda o: ["release-show", p, o["version"]]),
        "release-test-prepare": ({"version": version}, lambda o: ["release-test-prepare", p, o["version"]]),
        "release-test-check": ({"version": version}, lambda o: ["release-test-check", p, o["version"]]),
        "release-test-cleanup": ({"version": version}, lambda o: ["release-test-cleanup", p, o["version"]]),
        "release-deploy": ({"version": version, "teams": teams}, lambda o: ["release-deploy", p, o["version"], "--teams", *o["teams"]]),
    }
    if operation not in schemas:
        raise ValueError("Unsupported operation.")
    fields, build = schemas[operation]
    if set(options) != set(fields):
        raise ValueError("Unexpected or missing operation fields.")
    validated = {key: validate(options[key]) for key, validate in fields.items()}
    return build(validated)


class JobStore:
    def __init__(self, path):
        self.path = Path(path)
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
            db.execute("INSERT INTO jobs(id,actor,project,operation,options,status,created) VALUES(?,?,?,?,?,'queued',?)",
                       (job, actor, project, operation, json.dumps(options, sort_keys=True), time.time()))
        return job

    def list(self, projects, admin=False, limit=100):
        with self.connect() as db:
            columns = "SELECT id,actor,project,operation,options,status,output,created,finished FROM jobs"
            if admin:
                rows = db.execute(columns + " ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
            elif projects:
                allowed = [int(project) for project in projects]
                marks = ",".join("?" for _ in allowed)
                rows = db.execute(columns + f" WHERE project IN ({marks}) ORDER BY created DESC LIMIT ?", (*allowed, limit)).fetchall()
            else:
                rows = []
        result = []
        for row in rows:
            item = dict(row)
            options = json.loads(item.pop('options'))
            if item['operation'] in ('apply', 'stage-deploy'):
                item['plan_id'] = options.get('plan_id')
            result.append(item)
        return result

    def run_next(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET status='running' WHERE id=?", (row['id'],))
        if row is None:
            return False
        try:
            argv = command(row['operation'], row['project'], json.loads(row['options']))
            proc = subprocess.run([sys.executable, str(ROOT / 'scripts/crange.py'), *argv], cwd=ROOT, capture_output=True, text=True, timeout=3600)
            status = 'succeeded' if proc.returncode == 0 else 'failed'
            output = (proc.stdout + proc.stderr)[-50000:]
            if status == 'succeeded' and row['operation'] in {
                'box-add', 'box-remove', 'scenario-add', 'scenario-edit',
                'scenario-remove', 'container-add', 'container-edit',
                'container-remove', 'team-add', 'team-remove',
            }:
                sync = subprocess.run([sys.executable, str(ROOT / 'scripts/crange.py'),
                                       'guac-sync', str(row['project'])], cwd=ROOT,
                                      capture_output=True, text=True, timeout=120)
                output = (output + '\nGuacamole desired-state refresh:\n' +
                          sync.stdout + sync.stderr)[-50000:]
        except (ValueError, subprocess.TimeoutExpired, OSError) as exc:
            status, output = 'failed', str(exc)
        with self.connect() as db:
            db.execute('UPDATE jobs SET status=?,output=?,finished=? WHERE id=?', (status, output, time.time(), row['id']))
        return True

    def worker(self, stop):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='interrupted',finished=? WHERE status='running'", (time.time(),))
        while not stop.is_set():
            if not self.run_next():
                stop.wait(0.5)

    def start(self):
        stop = threading.Event()
        thread = threading.Thread(target=self.worker, args=(stop,), daemon=True)
        thread.start()
        return stop
