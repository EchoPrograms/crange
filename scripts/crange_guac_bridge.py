"""Authenticated Guacamole-to-controller box management bridge.

The browser speaks only to Guacamole. Guacamole verifies its own session and
signs a short server-to-server request; this module checks current CRANGE box
grants again before reading metadata or queueing a fixed worker operation.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import threading
import time

from crange_guac_ticket import TicketError


class BridgeError(ValueError):
    pass


_seen = {}
_seen_lock = threading.Lock()


def verify(raw: bytes, headers, peer: str, *, key_file=None, now=None):
    if peer not in ('10.60.0.10', '127.0.0.1', '::1'):
        raise BridgeError('Guacamole host required.')
    if len(raw) > 65536:
        raise BridgeError('Request is too large.')
    key_file = Path(key_file or os.environ.get(
        'CRANGE_GUAC_BRIDGE_KEY', '/var/lib/crange-web/.config/crange/guac-bridge.key'))
    try:
        key = bytes.fromhex(key_file.read_text().strip())
    except (OSError, ValueError) as exc:
        raise BridgeError('Guacamole bridge key is unavailable.') from exc
    if len(key) != 32:
        raise BridgeError('Guacamole bridge key is invalid.')
    timestamp = headers.get('X-Crange-Timestamp', '')
    nonce = headers.get('X-Crange-Nonce', '')
    signature = headers.get('X-Crange-Signature', '')
    if not re.fullmatch(r'[0-9]{10,11}', timestamp) or not re.fullmatch(r'[0-9a-f]{32}', nonce):
        raise BridgeError('Invalid Guacamole bridge request.')
    now = int(time.time() if now is None else now)
    if abs(now - int(timestamp)) > 30:
        raise BridgeError('Guacamole bridge request expired.')
    message = timestamp.encode() + b'\n' + nonce.encode() + b'\n' + raw
    expected = hmac.new(key, message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise BridgeError('Invalid Guacamole bridge signature.')
    with _seen_lock:
        for old, expiry in list(_seen.items()):
            if expiry < now:
                del _seen[old]
        if nonce in _seen:
            raise BridgeError('Guacamole bridge request was already used.')
        _seen[nonce] = now + 60
    try:
        document = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise BridgeError('Invalid Guacamole bridge JSON.') from exc
    identity = document.get('identity') if isinstance(document, dict) else None
    request = document.get('request') if isinstance(document, dict) else None
    data_source = document.get('dataSource') if isinstance(document, dict) else None
    if not isinstance(identity, dict) or not isinstance(request, dict):
        raise BridgeError('Invalid Guacamole bridge request.')
    username = identity.get('username')
    if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,95}', username):
        raise BridgeError('Invalid Guacamole identity.')
    if not isinstance(data_source, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', data_source):
        raise BridgeError('Invalid Guacamole data source.')
    request = dict(request)
    request['_authenticated_data_source'] = data_source
    return username, request


def _access(auth, root: Path, project: int, username: str):
    from crange_guac_users import ProjectUsers
    applied_path = root / 'guacamole/access' / f'project-{project:03d}.applied.json'
    try:
        applied = json.loads(applied_path.read_text())
    except (OSError, ValueError) as exc:
        raise BridgeError('Project remote access is not applied.') from exc
    ready, pending = applied.get('connections', []), applied.get('pending_connections', [])
    if not isinstance(ready, list) or not isinstance(pending, list):
        raise BridgeError('Project remote access is invalid.')
    connections = ready + pending
    available = {str(item.get('resource_key')) for item in connections if item.get('resource_key')}
    # Platform administrators are provisioned outside project automation. This
    # protected allowlist also lets the worker recheck native Guacamole admins.
    admin_path = Path(os.environ.get('CRANGE_GUAC_MANAGEMENT_ADMINS',
                                    '/home/crange/.config/crange/guac-management-admins.json'))
    try:
        admins = json.loads(admin_path.read_text())
    except (OSError, ValueError):
        admins = []
    if isinstance(admins, list) and username in admins:
        return connections, available
    dashboard = next((item for item in auth.list_users(project=str(project))
                      if item['username'] == username and item.get('enabled', True)), None)
    if dashboard:
        if dashboard.get('admin'):
            current = available
        else:
            teams = set(map(str, dashboard.get('teams', {}).get(str(project), [])))
            current = set(dashboard.get('connections', {}).get(str(project), [])) | {
                str(item['resource_key']) for item in connections
                if item.get('default_for_team') and str(item.get('team_id')) in teams}
        applied_grants = {str(item['resource_key']) for item in applied.get('permissions', []) + applied.get('pending_permissions', [])
                          if item.get('username') == username and item.get('resource_key')}
        return connections, available & current & applied_grants

    native = next((item for item in ProjectUsers(auth).list(project)
                   if item['login'] == username and item['enabled']), None)
    if not native:
        return connections, set()
    owned_path = root / 'guacamole/access' / f'project-{project:03d}.users-applied.json'
    try:
        owned = json.loads(owned_path.read_text()).get('users', {})
    except (OSError, ValueError):
        owned = {}
    if username not in owned:
        return connections, set()
    teams = set(map(str, native['teams']))
    current = set(native['connections']) | {
        str(item['resource_key']) for item in connections
        if item.get('default_for_team') and str(item.get('team_id')) in teams}
    applied_grants = set(owned[username].get('connection_keys', []))
    return connections, available & current & applied_grants


def _project_rows(auth, root: Path, project: int, username: str, *, include_state=False):
    from crange import CrangeError, ENVS, json_config, templates_for
    from crange_jumpboxes import FIELDS
    path = ENVS / f'project-{project:03d}'
    from crange_project_lifecycle import is_active
    if not path.is_dir() or not is_active(path):
        return []
    connections, allowed = _access(auth, root, project, username)
    if not allowed:
        return []
    cfg = json_config(path)
    try:
        outputs = json.loads((path / 'terraform.tfstate').read_text()).get('outputs', {})
    except (OSError, ValueError):
        outputs = {}
    from crange_user_management import preferences
    display_name = preferences(auth, project)['display_name'] or f'Project {project}'
    project_name = f'{display_name} (p{project:03d})'
    try:
        approvals = templates_for(path)
    except (CrangeError, OSError, ValueError):
        approvals = {}
    try:
        admins=json.loads(Path(os.environ.get('CRANGE_GUAC_MANAGEMENT_ADMINS',
                          '/home/crange/.config/crange/guac-management-admins.json')).read_text())
    except (OSError,ValueError):admins=[]
    console_admin=isinstance(admins,list) and username in admins
    grouped = {}
    for item in connections:
        key = str(item.get('resource_key', ''))
        if key not in allowed or not item.get('address'):
            continue
        kind, _, name = key.partition(':')
        box = cfg.get(FIELDS.get(kind, ''), {}).get(name)
        if not box:
            continue
        profile = approvals.get(box.get('template'), {}).get('profile', {})
        ostype = str(profile.get('os_type', '')).lower()
        guest_os = item.get('guest_os') or ('linux' if kind == 'container' or ostype.startswith(('l2', 'l6')) else 'windows' if ostype.startswith('win') else 'bsd' if 'bsd' in ostype else '')
        field = FIELDS.get(kind, '')
        guest = (outputs.get(field, {}).get('value') or {}).get(name, {})
        network = cfg.get('custom_networks', {}).get(box.get('network_key', ''))
        network_id = int(network['network_id']) if network else int(box['team_id'])
        vnet = f'p{project:03d}{"c" if network else "t"}{network_id:03d}'
        guest_name = f'{vnet}-jump-{int(box.get("jump_number",1)):02d}' if kind == 'jumpbox' else f'{vnet}-{box["name"]}'
        row = grouped.setdefault(key, {
            'project': project, 'project_name': project_name, 'guest_id': guest.get('id'), 'guest_name': guest_name, 'resource_key': key, 'kind': kind,
            'name': box.get('display_name') or name, 'ip': item['address'], 'guest_os': guest_os,
            'team': 0 if int(box['team_id']) == 254 else int(box['team_id']),
            'desired_power': 'running' if box.get('started', True) else 'stopped',
            'protocols': [],
            'console_available':console_admin and kind in ('vm','jumpbox','container'),
        })
        if item.get('access_state') == 'awaiting-initialization':
            row['access_state'] = 'awaiting-initialization'
        else:
            row['protocols'].append({'protocol': item['protocol'], 'connection_name': item['name']})
    rows = list(grouped.values())
    if include_state:
        from crange_live_status import box_states
        rows = box_states(rows)
    return rows


def broker_rows(auth, root, project, username):
    from crange_guac_ticket import request_broker
    return request_broker({'operation': 'guac-box-list', 'username': username,
                           'project': project})['boxes']


def handle(username: str, request: dict, *, auth, jobs, root: Path, rows_provider=None):
    rows_provider = rows_provider or _project_rows
    operation = request.get('operation')
    if operation == 'list':
        from crange import ENVS
        rows = []
        for path in sorted(ENVS.glob('project-[0-9][0-9][0-9]')):
            if not (root / 'guacamole/access' / f'{path.name}.applied.json').is_file():
                continue
            try:
                rows.extend(rows_provider(auth, root, int(path.name[-3:]), username))
            except (OSError, ValueError, TicketError) as exc:
                # A stale or unreadable legacy project must not take down the
                # global Guacamole inventory. Project-specific operations still
                # fail closed below when that project is selected explicitly.
                print(f'crange: skipping unreadable Guacamole project {path.name}: {exc}', flush=True)
        with jobs.connect() as db:
            recent = db.execute("SELECT id,project,options,status,output FROM jobs "
                                "WHERE actor=? AND operation IN ('guac-box-control','guac-snapshot') "
                                "ORDER BY created DESC", (username,)).fetchall()
        latest = {}
        for job in recent:
            options = json.loads(job['options'])
            latest.setdefault((job['project'], options['resource_key']), (job, options))
        for box in rows:
            record = latest.get((box['project'], box['resource_key']))
            box['job'] = None
            if record and record[0]['status'] in ('queued', 'running', 'failed'):
                job, options = record
                box['job'] = {'id': job['id'], 'action': options['action'] if options['action'] in ('start','shutdown','stop','restart') else 'snapshot-' + options['action'],
                              'status': job['status'], 'output': job['output']}
        return 200, {'boxes': rows}

    if operation == 'power-status':
        from crange_guac_ticket import request_broker
        return 200, request_broker({'operation':'guac-power-status','username':username})

    if operation == 'native-inventory':
        from crange_guac_ticket import request_broker
        result = request_broker({'operation': 'guac-native-inventory', 'username': username,
                                 'data_source': request.get('_authenticated_data_source')})
        connections = result.get('connections')
        if not isinstance(connections, list):
            raise BridgeError('Guacamole returned an invalid connection inventory.')
        return 200, {'connections': connections}

    project = request.get('project')
    key = request.get('resource_key')
    if isinstance(project, bool) or not isinstance(project, int) or not 1 <= project <= 255:
        raise BridgeError('Invalid project.')
    if not isinstance(key, str) or not re.fullmatch(r'(jumpbox|vm|container):[a-z][a-z0-9_.-]{0,63}', key):
        raise BridgeError('Invalid box.')
    rows = rows_provider(auth, root, project, username)
    if not any(row['resource_key'] == key for row in rows):
        raise BridgeError('Box access denied.')

    if operation == 'status':
        from crange_guac_ticket import request_broker
        return 200, request_broker({'operation': 'guac-box-status', 'username': username,
                                   'project': project, 'resource_key': key})
    if operation=='console':
        if not any(row['resource_key']==key and row.get('console_available') for row in rows):
            raise BridgeError('Administrator console access denied.')
        from crange_guac_ticket import request_broker
        return 200,request_broker({'operation':'guac-console-ticket','username':username,
                                  'project':project,'resource_key':key})

    if operation == 'submit':
        action = request.get('action')
        if action in ('start', 'shutdown', 'stop', 'restart'):
            job_operation = 'guac-box-control'
            options = {'resource_key': key, 'action': action}
        elif action in ('snapshot-list', 'snapshot-create', 'snapshot-restore', 'snapshot-delete'):
            job_operation = 'guac-snapshot'
            snapshot = request.get('snapshot', '')
            if not isinstance(snapshot, str):
                raise BridgeError('Invalid snapshot name.')
            options = {'resource_key': key, 'action': action.removeprefix('snapshot-'),
                       'snapshot': snapshot}
        else:
            raise BridgeError('Unsupported box action.')
        job_id = jobs.submit(username, project, job_operation, options)
        auth.audit(username, 'guac-box-' + action, key, {'project': project, 'job_id': job_id})
        return 202, {'job_id': job_id}

    if operation == 'job':
        job_id = request.get('job_id')
        if not isinstance(job_id, str) or not re.fullmatch(r'[0-9a-f]{32}', job_id):
            raise BridgeError('Invalid job ID.')
        with jobs.connect() as db:
            row = db.execute('SELECT id,actor,project,operation,options,status,output,created,finished FROM jobs WHERE id=?',
                             (job_id,)).fetchone()
        if not row or row['actor'] != username or row['project'] != project or row['operation'] not in ('guac-box-control', 'guac-snapshot'):
            raise BridgeError('Job access denied.')
        result = dict(row)
        options = json.loads(result.pop('options'))
        if options.get('resource_key') != key:
            raise BridgeError('Job access denied.')
        if row['operation'] == 'guac-snapshot' and options.get('action') == 'list' and row['status'] == 'succeeded':
            try:
                result['snapshots'] = json.loads(row['output'])['snapshots']
            except (ValueError, KeyError, TypeError):
                pass
        return 200, {'job': result}
    raise BridgeError('Unsupported Guacamole request.')
