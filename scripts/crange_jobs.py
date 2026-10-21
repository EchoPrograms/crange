"""Persistent, typed dashboard jobs. No client-controlled command lines."""
from __future__ import annotations

import json
import codecs
import selectors
import ipaddress
import math
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
POWER_OPERATIONS = ('guac-box-control','box-state','machine-state')


def job_guest_scope(row):
    """None means the whole project; a set identifies exact guest mutations."""
    operation=row['operation'];options=json.loads(row['options'])
    if operation in ('guac-box-control','guac-snapshot'):
        return {options['resource_key']}
    if operation=='box-state':
        return {'jumpbox:'+options['name']}
    if operation=='machine-state':
        return {options['kind']+':'+options['name']}
    if operation in ('inventory','guac-sync','guac-refresh','network-verify','network-reconcile','network-publish'):
        return set()
    if operation in ('apply','stage-deploy','release-apply'):
        try:
            from crange_apply_scope import guest_key
            record=json.loads((ROOT/'terraform/environments'/f'project-{row["project"]:03d}'/'.crange-plans'/(options['plan_id']+'.json')).read_text())
            keys=set()
            for change in record['changes']:
                if change['actions']==['no-op']:continue
                key=guest_key(change['address'])
                if not key or key.startswith('router:'):return None
                keys.add(key)
            return keys
        except (OSError,ValueError,KeyError,TypeError):
            return None
    if operation=='release-capture' and options.get('machines'):
        return {kind+':'+name for name in options['machines'] for kind in ('vm','container','jumpbox')}
    return None


def power_conflict(candidate, running):
    if candidate['project']!=running['project']:
        return False
    if candidate['operation'] not in POWER_OPERATIONS and running['operation'] not in POWER_OPERATIONS:
        return False
    left,right=job_guest_scope(candidate),job_guest_scope(running)
    return (left is None and right!=set()) or (right is None and left!=set()) or bool((left or set()) & (right or set()))


def choice(value, allowed):
    if value not in allowed:
        raise ValueError('Choose an approved option.')
    return value


def integer(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"Expected a number from {minimum} to {maximum}.")
    return str(value)


def capacity_ratio(value, maximum):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or not 1 <= value <= maximum or value * 4 != round(value * 4)):
        raise ValueError(f'Choose a commitment ratio from 1 to {maximum} in quarter steps.')
    return f'{float(value):g}'


def memory_reserve_fraction(value):
    if type(value) not in (int,float) or not math.isfinite(value) or not .1 <= value <= .5:
        raise ValueError('Choose a host RAM reserve from 10% to 50%.')
    return f'{value:g}'


def name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError("Use a lowercase name with letters, digits or hyphens.")
    return value


def template_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_.-]{0,127}', value):
        raise ValueError('Choose an approved template identifier.')
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
        raise ValueError("Choose a Proxmox node from 1 to 6.")
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

def guac_protocols(value):
    from crange_jumpboxes import services
    return json.dumps(services(value), separators=(',', ':'))


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
    if operation == 'release-capture':
        options = {**options, 'machines': options.get('machines', [])}
    if operation in ("scenario-health", "container-health"):
        options = {**options, "name": options.get("name", "")}
    if not isinstance(project, int) or isinstance(project, bool) or not (1 <= project <= 255 or (project == 0 and isinstance(operation, str) and operation.startswith('template-'))):
        raise ValueError("Invalid project.")
    if operation == 'network-verify' and project in (60, 61):
        raise ValueError('This project is outside network verification scope.')
    if operation == 'jumpbox-settings':
        options = {'credentials_mode': 'keep', 'credential_ref': '', 'sealed_credentials': '', **options}
    if operation in {'scenario-add', 'scenario-edit', 'container-add', 'container-edit'} and 'guac_protocol' in options and 'guac_protocols' not in options:
        options = {**options, 'guac_protocols': [options['guac_protocol']]}
        options.pop('guac_protocol')
    if operation in {'project-delete', 'project-restore', 'project-purge'}:
        from crange_project_lifecycle import project_id
        project_id(project)
        if set(options) != {'confirmation'} or options['confirmation'] != f'p{project:03d}':
            raise ValueError(f'Type p{project:03d} to confirm this action.')
        return [operation, str(project), '--confirm', options['confirmation']]
    p = str(project)
    schemas = {
        "reconcile-project-reservations": ({}, lambda o: ['reconcile-project-reservations', p]),
        "capture-inspect": ({"version":version,"name":scenario_key},lambda o:['capture-inspect',p,o['version'],o['name']]),
        "template-capture-network-prepare": ({"node":node},lambda o:['capture-network-prepare','--node',o['node']]),
        "jumpbox-template-save": ({"definition": lambda v: json.dumps(__import__('crange_jumpboxes').preset_settings(v))}, lambda o: ['jumpbox-template-save',p,'--definition',o['definition']]),
        "jumpbox-template-remove": ({"key": name},lambda o: ['jumpbox-template-remove',p,o['key']]),
        "custom-jumpbox-create": ({"definition": lambda v: json.dumps(__import__('crange_jumpboxes').creation(v))}, lambda o: ['custom-jumpbox-create',p,'--definition',o['definition']]),
        "template-cache-gc-review": ({"image": lambda v: v if isinstance(v,str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}',v) else (_ for _ in ()).throw(ValueError('Invalid image ID')), "node": node, "template_id": lambda v: integer(v,100,999999999)}, lambda o: ['cache-gc-review',o['image'],'--node',o['node'],'--template-id',o['template_id']]),
        "template-cache-gc-apply": ({"plan_id": lambda v:v if isinstance(v,str) and re.fullmatch(r'[0-9a-f]{32}',v) else (_ for _ in ()).throw(ValueError('Invalid cache GC plan ID'))}, lambda o:['cache-gc-apply',o['plan_id']]),
        "template-capture-settings": ({"bridge":name,"storage":lambda v:choice(v,('local-lvm','ssd-lvm','hdd-lvm','crange-thin','local-hdd')),"backup":name},lambda o:['capture-settings','--bridge',o['bridge'],'--storage',o['storage'],'--backup',o['backup']]),
        "template-cluster-refresh": ({}, lambda o: ["cluster-refresh"]),
        "template-capacity-policy-set": ({"cpu_ratio": lambda v: capacity_ratio(v, 6),
                                           "memory_ratio": lambda v: capacity_ratio(v, 2),
                                           "disk_ratio": lambda v: capacity_ratio(v, 2)},
                                          lambda o: ['cluster-capacity-policy-set', '--cpu-ratio', o['cpu_ratio'],
                                                     '--memory-ratio', o['memory_ratio'], '--disk-ratio', o['disk_ratio']]),
        "template-container-cache-acceptance": ({"project_id":lambda v:integer(v,1,255),"image":template_key,"node":node,
                                               "storage":lambda v:choice(v,('local-lvm','crange-thin','local-hdd'))},
                                              lambda o:['container-cache-acceptance','--project-id',o['project_id'],'--image',o['image'],'--node',o['node'],'--storage',o['storage']]),
        "template-library-recover": ({}, lambda o:["library-resources","--recover"]),
        "template-node-backup-prepare": ({"node":lambda v:choice(v,tuple(f'proxmox{i}' for i in range(1,7)))}, lambda o:['node-backup-prepare','--node',o['node']]),
        "template-library-grow": ({}, lambda o:["library-grow"]),
        "template-library-resources": ({}, lambda o:["library-resources"]),
        "template-range-storage-review": ({"size_gb":lambda v:choice(v,(256,512))}, lambda o:['range-storage-review','--size-gb',str(o['size_gb'])]),
        "template-range-storage-apply": ({"plan_id":lambda v:v if isinstance(v,str) and re.fullmatch('[a-f0-9]{32}',v) else (_ for _ in ()).throw(ValueError('Invalid storage review identity')),
                                           "review_sha256":lambda v:v if isinstance(v,str) and re.fullmatch('[a-f0-9]{64}',v) else (_ for _ in ()).throw(ValueError('Invalid storage review digest'))},
                                          lambda o:['range-storage-apply','--plan-id',o['plan_id'],'--review-sha256',o['review_sha256']]),
        "template-memory-reserve-set": ({"node":node,"minimum_mb":lambda v:integer(v,2048,32768),
                                          "fraction":memory_reserve_fraction,
                                          "expected_revision":lambda v:v if isinstance(v,str) and re.fullmatch(r'[A-Za-z0-9_.+:-]{1,8192}',v) else (_ for _ in ()).throw(ValueError('Refresh the cluster policy revision.'))},
                                         lambda o:['cluster-memory-reserve-set','--node',o['node'],
                                                   '--minimum-mb',o['minimum_mb'],'--fraction',o['fraction'],
                                                   '--expected-revision',o['expected_revision']]),
        "network-verify": ({"node": lambda v: choice(v, tuple(f'proxmox{i}' for i in range(1, 7))), "network": lambda v: v if isinstance(v, str) and re.fullmatch(fr"p{project:03d}[tc](?:00[1-9]|0[1-9][0-9]|1[0-9]{{2}}|2[0-4][0-9]|25[0-4])", v) else (_ for _ in ()).throw(ValueError("Choose a declared team, staging or custom LAN."))}, lambda o: ["network-verify", p, "--node", o["node"], "--network", o["network"]]),
        "network-reconcile": ({"source_job_id": lambda v: v if isinstance(v, str) and re.fullmatch(r"[a-f0-9]{32}", v) else (_ for _ in ()).throw(ValueError("Invalid source network job."))}, lambda o: ["network-reconcile", o["source_job_id"]]),
        "network-publish": ({"source_job_id": lambda v: v if isinstance(v,str) and re.fullmatch(r"[a-f0-9]{32}",v) else (_ for _ in ()).throw(ValueError("Invalid source network job."))}, lambda o: ["network-publish",o["source_job_id"]]),
        "template-distribution-inventory": ({}, lambda o: ['distribution-inventory']),
        "template-distribution-prepare": ({"image": lambda v: v if isinstance(v, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}',v) else (_ for _ in ()).throw(ValueError('Invalid immutable image ID')), "node": node, "storage": lambda v: choice(v, ('local-lvm','ssd-lvm','hdd-lvm','crange-thin','local-hdd'))}, lambda o: ['distribution-prepare',o['image'],'--project',p,'--node',o['node'],'--storage',o['storage']]),
        "template-cache-snapshot-retry": ({"source_job_id": lambda v: v if isinstance(v,str) and re.fullmatch(r'cache-[a-f0-9]{32}',v) else (_ for _ in ()).throw(ValueError('Choose an exact snapshot cache job.'))}, lambda o: ['cache-snapshot-probe-retry',o['source_job_id']]),
        "template-cache-restore-resume": ({"source_job_id": lambda v: v if isinstance(v, str) and re.fullmatch(r'cache-[a-zA-Z0-9-]{1,64}', v) else (_ for _ in ()).throw(ValueError('Choose a protected cache restore job.'))}, lambda o: ['cache-restore-resume', o['source_job_id']]),
        "template-cache-recovery": ({"image": lambda v: v if v == 'mhl-windows-server-2025-base-p100-v1' else (_ for _ in ()).throw(ValueError('Choose the reviewed P100 Windows image.')), "source_job_id": lambda v: v if isinstance(v,str) and re.fullmatch(r'cache-[a-zA-Z0-9-]{1,64}',v) else (_ for _ in ()).throw(ValueError('Choose a protected cache job.')), "action": lambda v: choice(v, ('resume','cleanup'))}, lambda o: ['cache-windows-p100-recovery',o['image'],'--source-job-id',o['source_job_id'],'--action',o['action']]),
        "template-base-prepare": ({"base": lambda v: __import__("crange_platform_bases").base_key(v)}, lambda o: ["template-base-prepare", o["base"]]),
        "template-create": ({"definition": lambda v: json.dumps(__import__('crange_templates').validate(v))}, lambda o: ['template-create', '--definition', o['definition']]),
        "template-approve-image": ({"image_id": lambda v: v if isinstance(v, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', v) else (_ for _ in ()).throw(ValueError('Invalid logical image ID')), "projects": template_projects}, lambda o: ['template-approve-image', o['image_id'], '--projects', *o['projects']]),
        "template-cancel": ({"build_id": lambda v: __import__('crange_templates').identifier(v)}, lambda o: ['template-cancel', o['build_id']]),
        "template-start-installer": ({"build_id": lambda v: __import__('crange_templates').identifier(v)}, lambda o: ['template-start-installer', o['build_id']]),
        "template-publish": ({"build_id": lambda v: __import__('crange_templates').identifier(v), "projects": template_projects,
                              "prepared": template_prepared}, lambda o: ['template-publish', o['build_id'], '--projects', *o['projects'], '--prepared']),
        "template-publish-sealed-windows-p100": ({}, lambda o: ['windows-p100-sealed-publish']),
        "template-windows-base-prepare": ({"build_id": lambda v: __import__('crange_templates').identifier(v)},
                                           lambda o: ['template-windows-base-prepare', o['build_id']]),
        "template-windows-base-seal": ({"build_id": lambda v: __import__('crange_templates').identifier(v)},
                                        lambda o: ['template-windows-base-seal', o['build_id']]),
        "template-upload": ({"upload_id": lambda v: __import__('crange_templates').identifier(v), "node": node}, lambda o: ['template-upload', o['upload_id'], '--node', o['node']]),
        "project-init": ({}, lambda o: ["project-init", p]),
        "bootstrap-plan": ({}, lambda o: ["bootstrap-plan", p]),
        "bootstrap-apply": ({"plan_id": lambda v: v if isinstance(v, str) and re.fullmatch(r"[0-9a-f]{32}", v) else (_ for _ in ()).throw(ValueError("Invalid bootstrap plan ID."))}, lambda o: ["infra-bootstrap-apply", p, "--yes", "--plan-id", o["plan_id"]]),
        "bootstrap-configure": ({}, lambda o: ["bootstrap-configure", p]),
        "release-configure": ({"teams": teams}, lambda o: ["release-configure", p, "--teams", *o["teams"], "--yes"]),
        "release-stage": ({"teams": teams}, lambda o: ["release-stage", p, "--teams", *o["teams"]]),
        "stage-changes": ({}, lambda o: ["stage-changes", p]),
        "plan": ({}, lambda o: ["infra-plan", p, "--retain"]),
        "plan-sdn-repair": ({}, lambda o: ["infra-plan", p, "--retain", "--replace-sdn-applier"]),
        "apply": ({"plan_id": lambda v: v if isinstance(v, str) and re.fullmatch(r"[0-9a-f]{32}", v) else (_ for _ in ()).throw(ValueError("Invalid plan ID.")), "allow_destroy": lambda v: v if isinstance(v, bool) else (_ for _ in ()).throw(ValueError("Invalid destroy setting.")), "allow_replace": lambda v: v if isinstance(v, bool) else (_ for _ in ()).throw(ValueError("Invalid replace setting."))}, lambda o: ["infra-apply", p, "--yes", "--plan-id", o["plan_id"], *(["--allow-destroy"] if o["allow_destroy"] else []), *(["--allow-replace"] if o["allow_replace"] else [])]),
        "release-apply": ({"teams": teams, "plan_id": lambda v: v if isinstance(v, str) and re.fullmatch(r"[0-9a-f]{32}", v) else (_ for _ in ()).throw(ValueError("Invalid plan ID.")), "allow_destroy": lambda v: v if isinstance(v, bool) else (_ for _ in ()).throw(ValueError("Invalid destroy setting.")), "allow_replace": lambda v: v if isinstance(v, bool) else (_ for _ in ()).throw(ValueError("Invalid replace setting."))}, lambda o: ["release-apply", p, "--teams", *o["teams"], "--yes", "--plan-id", o["plan_id"], *(["--allow-destroy"] if o["allow_destroy"] else []), *(["--allow-replace"] if o["allow_replace"] else [])]),
        "stage-deploy": ({"plan_id": lambda v: v if isinstance(v, str) and re.fullmatch(r"[0-9a-f]{32}", v) else (_ for _ in ()).throw(ValueError("Invalid plan ID.")), "allow_destroy": lambda v: v if isinstance(v, bool) else (_ for _ in ()).throw(ValueError("Invalid destroy setting.")), "allow_replace": lambda v: v if isinstance(v, bool) else (_ for _ in ()).throw(ValueError("Invalid replace setting."))}, lambda o: ["stage-deploy", p, "--yes", "--plan-id", o["plan_id"], *(["--allow-destroy"] if o["allow_destroy"] else []), *(["--allow-replace"] if o["allow_replace"] else [])]),
        "inventory": ({}, lambda o: ["inventory", p]),
        "route-sync": ({}, lambda o: ["route-sync", p]),
        "configure": ({"target": lambda v: v if v in ("routes", "routers", "boxes", "windows", "all") else (_ for _ in ()).throw(ValueError("Invalid configuration target."))}, lambda o: ["configure", p, o["target"], "--yes"]),
        "configure-team": ({"team": lambda v: integer(v, 0, 253)}, lambda o: ["configure-team", p, o["team"], "--yes"]),
        "mhl-business-configure": ({"network_id": lambda v: choice(v, (254, 1, 2, 3, 4, 5))},
                                    lambda o: ["mhl-business-configure", p, "--network-id", str(o["network_id"]), "--yes"]),
        "mhl-windows-configure": ({"network_id": lambda v: choice(v, (254, 1, 2, 3, 4, 5))},
                                   lambda o: ["mhl-windows-configure", p, "--network-id", str(o["network_id"]), "--yes"]),
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
        "scenario-add": ({"name": name, "team": lambda v: integer(v, 0, 253), "ip": lambda v: integer(v, 2, 100), "network": lambda v: "" if v == "" else name(v), "template": template_key, "node": node, "cores": lambda v: integer(v, 1, 16), "memory": lambda v: integer(v, 512, 65536), "health": health, "guac_protocols": guac_protocols}, lambda o: ["scenario-add", p, o["name"], "--team", o["team"], "--network", o["network"], "--ip", o["ip"], "--template", o["template"], "--node", o["node"], "--cores", o["cores"], "--memory", o["memory"], "--health", o["health"], "--guac-protocols", o["guac_protocols"]]),
        "scenario-edit": ({"name": scenario_key, "ip": lambda v: integer(v, 2, 100), "network": lambda v: "" if v == "" else name(v), "template": template_key, "node": node, "cores": lambda v: integer(v, 1, 16), "memory": lambda v: integer(v, 512, 65536), "health": health, "guac_protocols": guac_protocols}, lambda o: ["scenario-edit", p, o["name"], "--network", o["network"], "--ip", o["ip"], "--template", o["template"], "--node", o["node"], "--cores", o["cores"], "--memory", o["memory"], "--health", o["health"], "--guac-protocols", o["guac_protocols"]]),
        "scenario-remove": ({"name": scenario_key}, lambda o: ["scenario-remove", p, o["name"]]),
        "container-add": ({"name": name, "team": lambda v: integer(v, 0, 253), "ip": lambda v: integer(v, 2, 100), "template": template_key, "node": node, "network": lambda v: '' if v == '' else name(v), "cores": lambda v: integer(v, 1, 16), "memory": lambda v: integer(v, 512, 65536), "health": health, "guac_protocols": guac_protocols}, lambda o: ["container-add", p, o["name"], "--team", o["team"], "--ip", o["ip"], "--template", o["template"], "--node", o["node"], "--cores", o["cores"], "--memory", o["memory"], "--health", o["health"], "--guac-protocols", o["guac_protocols"], *(["--network", o["network"]] if o["network"] else [])]),
        "container-edit": ({"name": scenario_key, "template": template_key, "node": node, "ip": lambda v: integer(v, 2, 100), "network": lambda v: '' if v == '' else name(v), "cores": lambda v: integer(v, 1, 16), "memory": lambda v: integer(v, 512, 65536), "health": health, "guac_protocols": guac_protocols}, lambda o: ["container-edit", p, o["name"], "--template", o["template"], "--node", o["node"], "--ip", o["ip"], "--network", o["network"], "--cores", o["cores"], "--memory", o["memory"], "--health", o["health"], "--guac-protocols", o["guac_protocols"]]),
        "container-remove": ({"name": name}, lambda o: ["container-remove", p, o["name"]]),
        "machine-state": ({"name": scenario_key, "kind": lambda v: choice(v, ("vm", "container")), "state": lambda v: choice(v, ("start", "stop"))}, lambda o: ["machine-state", p, o["name"], "--kind", o["kind"], "--state", o["state"]]),
        "scenario-health": ({"name": lambda v: "" if v == "" else scenario_key(v)}, lambda o: ["scenario-health", p, *([o["name"]] if o["name"] else [])]),
        "container-health": ({"name": lambda v: "" if v == "" else scenario_key(v)}, lambda o: ["container-health", p, *([o["name"]] if o["name"] else [])]),
        "release-capture": ({"version": version, "boot_order": boot_orders, "machines": lambda v: [scenario_key(x) for x in v] if isinstance(v,list) and len(v)<=128 and all(isinstance(x,str) for x in v) and len(v)==len(set(v)) else (_ for _ in ()).throw(ValueError('Choose distinct staging machines.'))}, lambda o: ["release-capture", p, o["version"], *sum((["--boot-order", item] for item in o["boot_order"]), []), *sum((["--machine",item] for item in o["machines"]), [])]),
        "release-show": ({"version": version}, lambda o: ["release-show", p, o["version"]]),
        "release-recipe": ({"version": version, "parent_version": version}, lambda o: ["release-recipe", p, o["version"], '--parent-version', o['parent_version']]),
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
    if operation == 'release-recipe' and int(p) != 100:
        raise ValueError('Fixed service-recipe updates are currently available for Project 100.')
    if operation == 'mhl-business-configure' and project != 100:
        raise ValueError('MHL business services are only available for Project 100.')
    if operation == 'mhl-windows-configure' and project != 100:
        raise ValueError('MHL Windows services are only available for Project 100.')
    if operation == 'template-distribution-prepare' and int(p) == 0:
        raise ValueError('Choose a real project for cache preparation quota and approval scope.')
    if operation in {'template-cancel', 'template-start-installer', 'template-windows-base-prepare', 'template-windows-base-seal', 'template-cache-recovery', 'template-cache-restore-resume', 'template-cache-snapshot-retry'} and int(p) != 0:
        raise ValueError('Windows base preparation is administrator-only.')
    if operation in ('template-library-recover','template-library-resources','template-library-grow','template-node-backup-prepare','template-base-prepare','template-container-cache-acceptance','template-range-storage-review','template-range-storage-apply','template-capacity-policy-set','template-memory-reserve-set','template-capture-settings','template-capture-network-prepare') and int(p) != 0:
        raise ValueError('Capacity policy changes are administrator-only.')
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
        self._waiting_check = 0
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shared = os.environ.get('CRANGE_SHARED_DB') == '1'
        mode = 0o660 if shared else 0o600
        self.path.touch(mode=mode, exist_ok=True)
        if self.path.stat().st_uid == os.geteuid():
            self.path.chmod(mode)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, actor TEXT NOT NULL, project INTEGER NOT NULL, operation TEXT NOT NULL, options TEXT NOT NULL, status TEXT NOT NULL, output TEXT NOT NULL DEFAULT '', created REAL NOT NULL, finished REAL)")
            columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
            if 'waiting_nodes' not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN waiting_nodes TEXT NOT NULL DEFAULT '[]'")
            if 'archived' not in columns:
                db.execute('ALTER TABLE jobs ADD COLUMN archived INTEGER NOT NULL DEFAULT 0')
            if 'cancel_requested' not in columns:
                db.execute('ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0')

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
        from crange_project_lifecycle import OPERATIONS, path_for, validate_action, require_active
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            active = db.execute("SELECT operation FROM jobs WHERE project=? AND status IN ('queued','running','waiting-for-node','reconcile-required')", (project,)).fetchall()
            if operation in OPERATIONS:
                if active:
                    raise ValueError('Wait for this project’s queued or running jobs to finish before changing its lifecycle.')
                validate_action(path_for(project), operation)
            elif project and operation != 'project-init':
                if any(row['operation'] in OPERATIONS for row in active):
                    raise ValueError('Project deletion or restoration is queued; wait for its job to finish.')
                require_active(path_for(project))
            if operation == 'guac-box-control'  and options['action'] == 'stop':
                db.execute("UPDATE jobs SET status='interrupted',output='Superseded by force Stop.',finished=? "
                           "WHERE project=? AND operation='guac-box-control' AND status IN ('queued','running') "
                           "AND json_extract(options,'$.resource_key')=? AND json_extract(options,'$.action') IN ('shutdown','restart','start')",
                           (time.time(), project, options['resource_key']))
            db.execute("INSERT INTO jobs(id,actor,project,operation,options,status,created) VALUES(?,?,?,?,?,'queued',?)",
                       (job, actor, project, operation, json.dumps(options, sort_keys=True), time.time()))
        return job

    def list(self, projects, admin=False, limit=100):
        with self.connect() as db:
            columns = "SELECT id,actor,project,operation,options,status,output,created,finished FROM jobs"
            if admin:
                active = db.execute(columns + " WHERE status IN ('queued','running','waiting-for-node','reconcile-required') ORDER BY created DESC").fetchall()
                recent = db.execute(columns + " WHERE status NOT IN ('queued','running','waiting-for-node','reconcile-required') ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
            elif projects:
                allowed = [int(project) for project in projects]
                marks = ",".join("?" for _ in allowed)
                active = db.execute(columns + f" WHERE archived=0 AND project IN ({marks}) AND status IN ('queued','running','waiting-for-node','reconcile-required') ORDER BY created DESC", allowed).fetchall()
                recent = db.execute(columns + f" WHERE archived=0 AND project IN ({marks}) AND status NOT IN ('queued','running','waiting-for-node','reconcile-required') ORDER BY created DESC LIMIT ?", (*allowed, limit)).fetchall()
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

    def get(self, job_id, projects, admin=False):
        """Return one job only when it is within the caller's project scope."""
        with self.connect() as db:
            row = db.execute("SELECT id,actor,project,operation,options,status,output,created,finished FROM jobs WHERE id=? AND (archived=0 OR ?)",
                             (job_id, int(admin))).fetchone()
        if row is None or (not admin and str(row['project']) not in {str(value) for value in projects}):
            return None
        item = dict(row)
        options = json.loads(item.pop('options'))
        if item['operation'] in ('apply', 'stage-deploy', 'release-apply'):
            item['plan_id'] = options.get('plan_id')
        return item

    def list_page(self, projects, admin=False, *, page=1, page_size=25, project=None):
        """Return all authorized active jobs and one authorized terminal-history page."""
        allowed = None if admin else [int(value) for value in projects]
        if project is not None and allowed is not None and project not in allowed:
            allowed = []
        scope = [] if admin else [('archived=0', [])]
        if project is not None:
            scope.append(('project=?', [project]))
        elif allowed is not None:
            if allowed:
                scope.append(('project IN (' + ','.join('?' for _ in allowed) + ')', allowed))
            else:
                scope.append(('0', []))
        where = ' AND '.join(part for part, _ in scope) or '1'
        params = [value for _, values in scope for value in values]
        offset = (page - 1) * page_size
        columns = "SELECT id,actor,project,operation,options,status,output,created,finished FROM jobs"
        with self.connect() as db:
            # Active work remains available on every page for notifications and task tracking.
            if admin:
                active_where, active_params = "status IN ('queued','running','waiting-for-node','reconcile-required')", []
            elif allowed:
                active_where = "archived=0 AND project IN (" + ','.join('?' for _ in allowed) + ") AND status IN ('queued','running','waiting-for-node','reconcile-required')"
                active_params = allowed
            else:
                active_where, active_params = '0', []
            active = db.execute(columns + ' WHERE ' + active_where + ' ORDER BY created DESC,id DESC', active_params).fetchall()
            total = db.execute('SELECT COUNT(*) FROM jobs WHERE ' + where + " AND status NOT IN ('queued','running','waiting-for-node','reconcile-required')", params).fetchone()[0]
            terminal = db.execute(columns + ' WHERE ' + where + " AND status NOT IN ('queued','running','waiting-for-node','reconcile-required') ORDER BY created DESC,id DESC LIMIT ? OFFSET ?", (*params, page_size, offset)).fetchall()
        def present(rows):
            result = []
            for row in rows:
                item = dict(row)
                options = json.loads(item.pop('options'))
                if item['operation'] in ('apply', 'stage-deploy', 'release-apply'):
                    item['plan_id'] = options.get('plan_id')
                result.append(item)
            return result
        page_count = (total + page_size - 1) // page_size
        return {'jobs': present(active + terminal), 'total': total, 'page': page,
                'page_size': page_size, 'page_count': page_count,
                'has_previous': page > 1, 'has_next': page < page_count}

    def cancel(self, job_id, projects, admin=False):
        """Cancel a queued job or terminate the subprocess for a running job."""
        if not isinstance(job_id, str) or not re.fullmatch(r'[0-9a-f]{32}', job_id):
            raise ValueError('Invalid job ID.')
        with self.connect() as db:
            row = db.execute('SELECT project,status,operation FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row or (not admin and str(row['project']) not in set(map(str, projects))):
                return False
            if row['status'] == 'queued':
                db.execute("UPDATE jobs SET status='interrupted',output='Cancelled before starting.',finished=? WHERE id=? AND status='queued'", (time.time(), job_id))
                return db.total_changes > 0
            if row['operation'] == 'network-verify' and row['status'] == 'running':
                db.execute("UPDATE jobs SET cancel_requested=1,output=substr(output || '\\nCooperative cancellation requested; waiting for owned cleanup.', -50000) WHERE id=? AND status='running'", (job_id,))
                return db.total_changes > 0
            if row['operation'] in ('network-reconcile','network-publish') and row['status'] == 'running':
                return False
            if row['status'] == 'reconcile-required':
                return False
            if row['status'] != 'running':
                return False
            # The dashboard and external worker have independent process maps.
            # Persist the stop state; the owning worker observes it in its stream loop.
            db.execute("UPDATE jobs SET status='interrupted',cancel_requested=1,output=substr(output || '\nStop requested; waiting for the current command to exit.', -50000),finished=? WHERE id=? AND status='running'", (time.time(), job_id))
            if not db.total_changes:
                return False
            with self._process_lock:
                proc = self._processes.get(job_id)
                if proc is not None:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
        return True

    def _run_streamed(self, job_id, args, env, output, timeout=3600):
        """Merge both streams at the child pipe and persist bounded progress."""
        child = subprocess.Popen(args, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        deadline = time.monotonic() + timeout
        last_flush = 0.0
        dirty = False
        stopping = None
        timed_out = False
        with self.connect() as db:
            row = db.execute('SELECT operation FROM jobs WHERE id=?', (job_id,)).fetchone()
            cooperative = bool(row and row['operation'] in ('network-verify', 'network-reconcile', 'network-publish'))
        with self._process_lock:
            self._processes[job_id] = child
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while selector.get_map() or child.poll() is None:
                    now = time.monotonic()
                    if now >= deadline and stopping is None:
                        if not cooperative:
                            timed_out = True
                            stopping = now
                    with self.connect() as db:
                        state = db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
                    if state and state['status'] == 'interrupted' and stopping is None and not cooperative:
                        stopping = now
                    if stopping is not None:
                        try:
                            os.killpg(child.pid, signal.SIGKILL if now - stopping >= 2 else signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    for key, _ in selector.select(0.1):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if chunk:
                            output = (output + decoder.decode(chunk))[-50000:]
                            dirty = True
                        else:
                            output = (output + decoder.decode(b'', final=True))[-50000:]
                            selector.unregister(key.fileobj)
                    if dirty and now - last_flush >= 0.25:
                        with self.connect() as db:
                            db.execute('UPDATE jobs SET output=? WHERE id=?', (output, job_id))
                        last_flush = now
                        dirty = False
            child.wait()
            if timed_out:
                output = (output + '\ncrange: Operation exceeded its time limit.')[-50000:]
            with self.connect() as db:
                db.execute('UPDATE jobs SET output=? WHERE id=?', (output, job_id))
                state = db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
            returncode = 124 if timed_out else (130 if state and state['status'] == 'interrupted' else child.returncode)
            return subprocess.CompletedProcess(args, returncode, output, '')
        finally:
            child.stdout.close()
            with self._process_lock:
                self._processes.pop(job_id, None)

    def cancel_requested(self, job_id):
        with self.connect() as db:
            row = db.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job_id,)).fetchone()
        return bool(row and row['cancel_requested'])

    def _network_journal_state(self, job_id):
        try:
            path = Path('/var/lib/crange-network-probes') / ('network-' + job_id) / 'journal.json'
            return json.loads(path.read_text()).get('state')
        except (OSError, ValueError, TypeError):
            return None

    def resume_waiting(self, *, api=None):
        """Reconnect deferred deletions without occupying an execution lane."""
        if api is None and time.monotonic() < self._waiting_check:
            return
        self._waiting_check = time.monotonic() + 15
        with self.connect() as db:
            rows=db.execute("SELECT id,operation,waiting_nodes FROM jobs WHERE status='waiting-for-node' AND operation IN ('project-delete','project-purge')").fetchall()
        if not rows:
            return
        try:
            if api is None:
                from crange import ProxmoxAPI
                api=ProxmoxAPI();api.request_timeout=3;api.read_attempts=1
            online={row['node'] for row in api.request('GET','/nodes') if row.get('status')=='online'}
        except Exception:
            return
        with self.connect() as db:
            for row in rows:
                required=json.loads(row['waiting_nodes'])
                ready = bool(set(required) & online) if row['operation'] == 'project-delete' else set(required) <= online
                if required and ready:
                    db.execute("UPDATE jobs SET status='queued',waiting_nodes='[]',finished=NULL WHERE id=? AND status='waiting-for-node'",(row['id'],))

    def run_next(self, force_only=None, *, network_only=False, power_only=None):
        if not network_only and force_only is not True and power_only is not True:
            self.resume_waiting()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if network_only:
                # This lane is serialized under the root worker lock; replay only its idempotent publisher.
                db.execute("UPDATE jobs SET status='queued',finished=NULL WHERE status='running' AND operation='network-publish'")
            force = "operation='guac-box-control' AND json_extract(options,'$.action')='stop'"
            lane = '' if force_only is None else (' AND (' + force + ')' if force_only else ' AND NOT (' + force + ')')
            lane += " AND operation IN ('network-verify','network-reconcile','network-publish')" if network_only else " AND operation NOT IN ('network-verify','network-reconcile','network-publish')"
            if power_only is not None:
                lane += " AND operation " + ("IN" if power_only else "NOT IN") + " ('guac-box-control','box-state','machine-state')"
            running=db.execute("SELECT * FROM jobs WHERE status='running'").fetchall()
            candidates=db.execute("SELECT * FROM jobs WHERE status='queued'" + lane + " ORDER BY created LIMIT 100").fetchall()
            row=next((candidate for candidate in candidates if force_only is True or not any(power_conflict(candidate,active) for active in running)),None)
            if row:
                db.execute("UPDATE jobs SET status='running',output='Worker picked up this action; starting…\n' WHERE id=?", (row['id'],))
        if row is None:
            return False
        output = 'Worker picked up this action; starting…\n'
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
                    if not actor or not actor['enabled'] or (row['operation'] in ('network-reconcile','network-publish') and not actor['admin']) or (not actor['admin'] and (
                            (row['operation'] in ('project-init', 'plan-sdn-repair', 'project-restore', 'project-purge') or row['operation'].startswith('template-')) or
                            actor['roles'].get(str(row['project'])) != 'manager')):
                        raise ValueError('Project manager access was revoked before this job started.')
            from crange_project_lifecycle import OPERATIONS, require_active, path_for
            if row['project'] and row['operation'] not in OPERATIONS | {'project-init'}:
                require_active(path_for(row['project']))
            argv = command(row['operation'], row['project'], json.loads(row['options']))
            if row['operation'] == 'network-verify':
                if not network_only:
                    raise ValueError('Network verification requires the privileged worker lane.')
                args = [sys.executable, str(ROOT / 'scripts/crange_network_worker.py'), 'run', row['id']]
            elif row['operation'] == 'network-reconcile':
                if not network_only:
                    raise ValueError('Network reconciliation requires the privileged worker lane.')
                source_job_id = json.loads(row['options'])['source_job_id']
                args = [sys.executable, str(ROOT / 'scripts/crange_network_worker.py'), 'reconcile', source_job_id]
            elif row['operation'] == 'network-publish':
                if not network_only:
                    raise ValueError('Network publication requires the privileged worker lane.')
                args=[sys.executable,str(ROOT/'scripts/crange_network_publish.py'),
                      json.loads(row['options'])['source_job_id'],'--project',str(row['project'])]
            else:
                args = [sys.executable, str(ROOT / 'scripts/crange.py'), *argv]
            worker_env = {**os.environ, 'CRANGE_ACTOR': row['actor'], 'CRANGE_JOB_ID': row['id'], 'PYTHONUNBUFFERED': '1'}
            # Capture/staging verify images; release applies initialize and configure
            # multiple teams serially. Bound these long jobs without extending
            # ordinary actions beyond their single-job hour.
            timeout = (12 * 3600 if row['operation'] in ('project-delete', 'project-purge', 'apply', 'stage-deploy', 'release-capture', 'template-base-prepare', 'stage-changes', 'release-stage', 'release-apply', 'release-configure') else
                       7200 if row['operation'] in ('network-verify', 'network-reconcile', 'network-publish') else 3600)
            if subprocess.run.__module__ != 'subprocess':
                # Preserve the conventional mock seam used by worker tests.
                proc = subprocess.run(args, cwd=ROOT, env=worker_env, capture_output=True,
                                      text=True, timeout=timeout)
            else:
                proc = self._run_streamed(row['id'], args, worker_env, output,
                                          timeout=timeout)
            status = 'succeeded' if proc.returncode == 0 else 'failed'
            if proc.returncode == 75 and row['operation'] in ('project-delete','project-purge'):
                from crange_project_lifecycle import state
                marker=state(path_for(row['project']))
                waiting=marker.get('waiting_nodes',[])
                if marker.get('job_id')==row['id'] and waiting and all(node in NODES for node in waiting):
                    with self.connect() as db:
                        db.execute("UPDATE jobs SET status='waiting-for-node',waiting_nodes=?,output=?,finished=NULL WHERE id=? AND status='running'",(json.dumps(waiting),(output+proc.stdout+proc.stderr)[-50000:],row['id']))
                    return True
            output = ((output if subprocess.run.__module__ != 'subprocess' else '') + proc.stdout + proc.stderr)[-50000:]
            if status == 'succeeded' and row['operation']=='network-verify':
                if self._network_journal_state(row['id'])!='accepted_cleaned':
                    status='failed'
                    output+='\nNetwork verification lacks an accepted owned receipt.'
                else:
                    publication=subprocess.run([sys.executable,str(ROOT/'scripts/crange_network_publish.py'),
                        row['id'],'--project',str(row['project'])],cwd=ROOT,env=worker_env,
                        capture_output=True,text=True,timeout=60)
                    output=(output+'\nPublishing the accepted network for this exact placement scope.\n'+
                            publication.stdout+publication.stderr)[-50000:]
                    if publication.returncode:status='failed'
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
                'apply', 'stage-deploy', 'release-apply', 'release-configure', 'configure', 'configure-team',
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
        except (ValueError, TypeError, subprocess.TimeoutExpired, OSError) as exc:
            status = 'failed'
            message = 'Operation exceeded its time limit.' if isinstance(exc, subprocess.TimeoutExpired) else str(exc)
            output = (output + '\ncrange: ' + message)[-50000:]
        if status == 'failed' and row['operation'] in ('project-delete','project-purge'):
            from crange_project_lifecycle import state, write_state
            marker=state(path_for(row['project']))
            if marker.get('job_id')==row['id'] and marker.get('waiting_nodes'):
                marker.pop('waiting_nodes',None)
                marker.update(state='delete_failed' if row['operation']=='project-delete' else 'purge_failed',error='The resumed deletion failed. Open its job for details.')
                write_state(path_for(row['project']),marker)
        with self.connect() as db:
            current = db.execute('SELECT status,cancel_requested FROM jobs WHERE id=?', (row['id'],)).fetchone()
            if current and current['status'] == 'interrupted':
                status = 'interrupted'
                output = (output + '\ncrange: Job was stopped by a user; the operation may have partially completed.')[-50000:]
            elif row['operation'] == 'network-verify':
                journal_state = self._network_journal_state(row['id'])
                if journal_state and journal_state not in ('failed_cleaned', 'accepted_cleaned'):
                    status = 'reconcile-required'
                    output = (output + '\nOwned cleanup needs administrator reconciliation; reservation remains held.')[-50000:]
                    db.execute('UPDATE jobs SET status=?,output=?,finished=NULL WHERE id=?', (status, output, row['id']))
                    return True
                if journal_state == 'accepted_cleaned' and status == 'succeeded':
                    pass
                elif journal_state == 'failed_cleaned':
                    status = 'failed'
                elif journal_state is None and not (current and current['cancel_requested']):
                    bundle = Path('/var/lib/crange-network-probes') / ('network-' + row['id'])
                    if bundle.exists() and any((bundle / name).exists()
                                               for name in ('terraform.tfstate', 'terraform.tfstate.backup')):
                        status = 'reconcile-required'
                        output = (output + '\nProbe state exists without its durable lifecycle journal; administrator reconciliation is required.')[-50000:]
                        db.execute('UPDATE jobs SET status=?,output=?,finished=NULL WHERE id=?', (status, output, row['id']))
                        return True
                    status = 'failed'
                    output = (output + '\nNo terminal owned acceptance journal was produced.')[-50000:]
                if current and current['cancel_requested'] and journal_state in ('failed_cleaned', 'accepted_cleaned'):
                    status = 'interrupted'
                    output = (output + '\nCancellation completed after owned cleanup was confirmed.')[-50000:]
                elif current and current['cancel_requested'] and journal_state is None:
                    status = 'interrupted'
                    output = (output + '\nCancelled before probe creation; no owned guest journal exists.')[-50000:]
            db.execute('UPDATE jobs SET status=?,output=?,finished=? WHERE id=?', (status, output, time.time(), row['id']))
        return True

    def worker(self, stop, *, network_only=False):
        with self.connect() as db:
            if network_only:
                # Network jobs may own a guest, router rule, and reservation.
                # Their startup recovery is handled by the privileged worker.
                pass
            else:
                db.execute("UPDATE jobs SET status='interrupted',finished=? WHERE status='running' AND operation NOT IN ('network-verify','network-reconcile','network-publish')", (time.time(),))
        # Force Stop must remain available while a graceful shutdown waits.
        def emergency_worker():
            while not stop.is_set():
                if not network_only and not self.run_next(force_only=True):
                    stop.wait(0.5)
        emergency = threading.Thread(target=emergency_worker, daemon=True)
        emergency.start()
        def power_worker():
            while not stop.is_set():
                if not self.run_next(force_only=False,power_only=True):
                    stop.wait(0.25)
        controls=[threading.Thread(target=power_worker,daemon=True) for _ in range(4)] if not network_only else []
        for thread in controls:thread.start()
        try:
            while not stop.is_set():
                if not self.run_next(force_only=False, network_only=network_only,power_only=False):
                    stop.wait(0.5)
        finally:
            emergency.join(timeout=5)
            for thread in controls:thread.join(timeout=5)

    def start(self):
        stop = threading.Event()
        thread = threading.Thread(target=self.worker, args=(stop,), daemon=True)
        thread.start()
        return stop
