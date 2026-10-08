"""Local-only broker for authenticated, least-privilege Guacamole tickets."""
from __future__ import annotations

import json
import ipaddress
import os
from pathlib import Path
import pwd
import socket
import socketserver
import struct
import threading
import time
from urllib.parse import urlsplit

from crange_auth import AuthStore
from crange_guac_ticket import TicketError, issue_ticket


DEFAULT_PROXMOX_PUBLIC_URL = 'https://proxmox.brogano.dev'
_native_guest_cache = {'expires': 0.0, 'addresses': {}}
_native_guest_cache_lock = threading.Lock()


def public_proxmox_origin(configured=None):
    value = os.environ.get('CRANGE_PROXMOX_PUBLIC_URL') if configured is None else configured
    if value is None or not str(value).strip():
        return DEFAULT_PROXMOX_PUBLIC_URL
    try:
        parsed = urlsplit(str(value).strip())
        port = parsed.port
        host = parsed.hostname
        if (parsed.scheme != 'https' or not host or parsed.username or parsed.password or
                parsed.path not in ('', '/') or parsed.query or parsed.fragment or port == 8006):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
        if address is None and ('.' not in host or host.lower() == 'localhost' or
                                host.lower().endswith(('.local', '.localhost', '.internal', '.lan'))):
            raise ValueError
        netloc = host.lower()
        if ':' in netloc:
            netloc = f'[{netloc}]'
        if port is not None:
            netloc += f':{port}'
        return f'https://{netloc}'
    except (TypeError, ValueError):
        raise TicketError('Configured public Proxmox URL must be a public HTTPS origin.')


def proxmox_console_url(origin, vmid):
    if isinstance(vmid, bool) or not isinstance(vmid, int) or vmid < 1:
        raise ValueError('Invalid installer VM ID.')
    base = public_proxmox_origin(origin).rstrip('/')
    # Proxmox StateProvider route: select qemu/<vmid>, then its kvmtab console.
    return f'{base}/#v1:0:=qemu%2F{vmid}:4:::::=console'


def box_status(data, *, auth, root):
    """Called only after the Unix socket verifies the web service's peer UID."""
    from crange_guac_bridge import _project_rows
    from crange_guac_boxes import resource_key, resolve
    from crange_guac_users import valid_username
    project = data.get('project')
    if isinstance(project, bool) or not isinstance(project, int) or not 1 <= project <= 255:
        raise TicketError('Invalid project.')
    username = valid_username(data.get('username'))
    key = resource_key(data.get('resource_key'))
    if not any(row['resource_key'] == key for row in _project_rows(auth, root, project, username)):
        raise TicketError('Box access denied.')
    _path, _cfg, _box, api, kind, node, vmid, _address = resolve(project, key)
    state = (api.container_status if kind == 'container' else api.status)(node, vmid)
    return {'resource_key': key, 'state': state}


def native_inventory(username, data_source):
    """Describe only Guacamole connections currently granted to this identity.

    Native connection metadata comes from Guacamole itself. A Proxmox guest is
    attached only when its current configured or guest-agent address uniquely
    matches that hostname; this endpoint never grants management operations.
    """
    from crange_guac import GuacamoleClient
    from crange_guac_users import valid_username
    username = valid_username(username)
    client = GuacamoleClient()
    client.login()
    if not isinstance(data_source, str) or data_source != client.data_source:
        raise TicketError('Native inventory data source is unavailable.')
    source = client.data_source
    users = client._request('GET', f'/api/session/data/{source}/users') or {}
    if username not in users:
        raise TicketError('Guacamole account not found.')
    permissions = client._request('GET', f'/api/session/data/{source}/users/{username}/permissions') or {}
    granted = permissions.get('connectionPermissions', {})
    if not isinstance(granted, dict):
        granted = {}
    connections = client._request('GET', f'/api/session/data/{source}/connections') or {}
    rows = []
    for identifier, connection in connections.items():
        value = next((permission for key, permission in granted.items()
                      if str(identifier) == str(key)), [])
        levels = {value} if isinstance(value, str) else set(value) if isinstance(value, list) else set()
        if not levels.intersection(('READ', 'UPDATE', 'ADMINISTER')):
            continue
        params = client._request('GET', f'/api/session/data/{source}/connections/{identifier}/parameters') or {}
        hostname = str(params.get('hostname') or '').strip()
        rows.append({'data_source': source, 'identifier': str(identifier), 'name': str(connection.get('name') or ''),
                     'protocol': str(connection.get('protocol') or ''), 'hostname': hostname})

    if not rows:
        return rows

    from crange import ProxmoxAPI
    api = ProxmoxAPI()
    by_address = _live_native_guest_index(api)
    for row in rows:
        matches = by_address.get(row['hostname'], [])
        if len(matches) == 1:
            guest = matches[0]
            prefix = 'qemu' if guest['kind'] == 'qemu' else 'lxc'
            status = (api.request('GET', f"/nodes/{guest['node']}/{prefix}/{guest['vmid']}/status/current") or {}).get('status')
            row.update({'guest_type': 'VM' if guest['kind'] == 'qemu' else 'Container',
                        'state': status if status in ('running', 'stopped') else 'unknown',
                        'guest_name': guest['guest_name']})
            row['guest_matched'] = True
        else:
            row.update({'guest_type': 'Connection', 'state': 'unmanaged', 'guest_name': '',
                        'guest_matched': False})
    return rows


def _live_native_guest_index(api):
    """Cache read-only PVE address discovery briefly; refresh power on each poll."""
    import re
    now = time.monotonic()
    with _native_guest_cache_lock:
        if _native_guest_cache['expires'] > now:
            return _native_guest_cache['addresses']
        resources = api.request('GET', '/cluster/resources', {'type': 'vm'}) or []
        records = []
        for resource in resources:
            node, vmid, kind = resource.get('node'), int(resource.get('vmid', 0)), resource.get('type')
            if kind not in ('qemu', 'lxc') or not node or not vmid:
                continue
            prefix = 'qemu' if kind == 'qemu' else 'lxc'
            config = api.request('GET', f'/nodes/{node}/{prefix}/{vmid}/config') or {}
            if int(config.get('template', 0)) == 1:
                continue
            guest_name = str(config.get('name' if kind == 'qemu' else 'hostname') or resource.get('name') or '')
            tags = set(filter(None, str(config.get('tags') or '').split(';')))
            if (vmid in range(8910, 8915) or
                    guest_name.startswith(('crange-win-acceptance-', 'crange-win-locale-proof-',
                                           'crange-image-', 'crange-cache-')) or
                    tags.intersection({'crange-image', 'crange-cache', 'crange-probe', 'crange-acceptance'}) or
                    any(tag.startswith(('crange-image-', 'crange-cache-', 'crange-probe-')) for tag in tags)):
                continue
            addresses = set()
            if kind == 'lxc':
                for key, value in config.items():
                    if key.startswith('net') and isinstance(value, str):
                        addresses.update(re.findall(r'(?:^|,)ip=([0-9.]+)(?:/|,|$)', value))
            else:
                for key, value in config.items():
                    if key.startswith('ipconfig') and isinstance(value, str):
                        addresses.update(re.findall(r'(?:^|,)ip=([0-9.]+)(?:/|,|$)', value))
            records.append((node, vmid, kind, guest_name, addresses))
        for node, vmid, kind, _guest_name, addresses in records:
            if kind == 'qemu':
                try:
                    status = (api.request('GET', f'/nodes/{node}/qemu/{vmid}/status/current') or {}).get('status')
                    if status != 'running':
                        continue
                    agent = api.request('GET', f'/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces') or {}
                    for iface in agent.get('result', []):
                        for address in iface.get('ip-addresses', []):
                            value = address.get('ip-address', '')
                            if address.get('ip-address-type') == 'ipv4' and value and not value.startswith('127.'):
                                addresses.add(value)
                except Exception:
                    pass
        index = {}
        for node, vmid, kind, guest_name, addresses in records:
            for address in addresses:
                index.setdefault(address, []).append({
                    'kind': kind, 'node': node, 'vmid': vmid, 'guest_name': guest_name})
        _native_guest_cache.update({'expires': now + 15.0, 'addresses': index})
        return index


def serve():
    socket_path = Path(os.environ.get('CRANGE_GUAC_TICKET_SOCKET', '/run/crange-guac/ticket.sock'))
    key_path = Path(os.environ.get('CRANGE_GUAC_JSON_KEY', '/home/crange/.config/crange/guacamole-json.secret'))
    auth_path = Path(os.environ.get('CRANGE_AUTH_DB', '/var/lib/crange-web/.local/share/crange/auth.sqlite3'))
    repo_root = Path(__file__).resolve().parents[1]
    try:
        secret = key_path.read_text().strip()
    except OSError as exc:
        raise RuntimeError('Guacamole JSON authentication key is not installed.') from exc
    if len(secret) != 32 or any(char not in '0123456789abcdefABCDEF' for char in secret):
        raise RuntimeError('Guacamole JSON authentication key must be 32 hexadecimal characters.')
    expected_uid = pwd.getpwnam(os.environ.get('CRANGE_WEB_USER', 'crange-web')).pw_uid
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
        allow_reuse_address = True

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                credentials = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
                _, peer_uid, _ = struct.unpack('3i', credentials)
                if peer_uid != expected_uid:
                    raise TicketError('Caller is not authorized.')
                raw = self.rfile.readline(65537)
                if not raw or len(raw) > 65536:
                    raise TicketError('Invalid launch request.')
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise TicketError('Invalid launch request.')
                operation = data.get('operation', 'ticket')
                if operation == 'guac-native-inventory':
                    reply = {'connections': native_inventory(data.get('username'), data.get('data_source'))}
                    self.wfile.write(json.dumps(reply, separators=(',', ':')).encode() + b'\n')
                    return
                project_id = data.get('project')
                if isinstance(project_id, bool) or not isinstance(project_id, int) or not 1 <= project_id <= 255:
                    raise TicketError('Invalid project.')
                auth = AuthStore(auth_path)
                if operation in ('guac-box-status', 'guac-box-list'):
                    if operation == 'guac-box-list':
                        from crange_guac_bridge import _project_rows
                        from crange_guac_users import valid_username
                        username = valid_username(data.get('username'))
                        reply = {'boxes': _project_rows(auth, repo_root, project_id, username)}
                    else:
                        reply = box_status(data, auth=auth, root=repo_root)
                    self.wfile.write(json.dumps(reply, separators=(',', ':')).encode() + b'\n')
                    return
                session_token = data.get('session')
                if not isinstance(session_token, str) or not 32 <= len(session_token) <= 256:
                    raise TicketError('Sign in required.')
                if operation == 'template-readiness':
                    session = auth.session(session_token)
                    if not session or (not session['admin'] and str(project_id) not in session['managed_projects']):
                        raise TicketError('Project manager access required.')
                    from crange_templates import project_image_readiness
                    reply = {'templates': project_image_readiness(project_id)}
                    self.wfile.write(json.dumps(reply, separators=(',', ':')).encode() + b'\n')
                    return
                if operation == 'template-options':
                    session = auth.session(session_token)
                    if not session or not session['admin']:raise TicketError('Administrator access required.')
                    from crange_templates import options
                    reply = options()
                    from crange_template_media import pending
                    reply['uploads'] = pending(session['username'])
                    reply['console_base'] = public_proxmox_origin()
                    for build in reply['builds']:
                        build['console_url'] = (proxmox_console_url(reply['console_base'], build['vmid'])
                                                if build.get('console_available') else None)
                    self.wfile.write(json.dumps(reply,separators=(',',':')).encode()+b'\n')
                    return
                if operation in ('seal-password','seal-guest-credentials','prepare-users','available-users','user-stats'):
                    session = auth.session(session_token)
                    if not session or (not session['admin'] and str(project_id) not in session['managed_projects']):
                        raise TicketError('Project manager access required.')
                    from crange_guac_users import seal_password, open_password, ProjectUsers, valid_username
                    from crange_user_management import preferences, expand_format, user_stats
                    from crange_guac import GuacamoleClient
                    if operation == 'seal-guest-credentials':
                        from crange_guest_credentials import seal
                        reply = {'sealed_credentials': seal(data.get('credentials'), secret, project_id, data.get('reference'))}
                    elif operation == 'seal-password':
                        reply = {'sealed_password': seal_password(data.get('password'), secret, project_id, data.get('username'))}
                    elif operation == 'prepare-users':
                        users=data.get('users')
                        if not isinstance(users,list) or not 1 <= len(users) <= 100:
                            raise TicketError('Choose 1–100 users.')
                        prefs=preferences(auth,project_id,private=True)
                        pattern=data.get('password_template')
                        if not pattern and prefs.get('sealed_password_template'):
                            pattern=open_password(prefs['sealed_password_template'],secret,project_id,'defaults')
                        prepared=[]
                        for item in users:
                            name=valid_username(item['username'])
                            password=data.get('password') or (expand_format(pattern,project_id,item['home_team'],item['user_number']) if pattern else valid_username(item.get('login',name)))
                            prepared.append({'sealed_password':seal_password(password,secret,project_id,name)})
                        reply={'users':prepared}
                    elif operation == 'available-users':
                        names=data.get('names')
                        if not isinstance(names,list) or not 1 <= len(names) <= 100:
                            raise TicketError('Choose 1–100 usernames.')
                        names=[valid_username(name) for name in names]
                        ignored=next((r['login'] for r in ProjectUsers(auth).list(project_id) if r['username']==data.get('ignore')),None)
                        client=GuacamoleClient();client.login()
                        current=client._request('GET',f'/api/session/data/{client.data_source}/users') or {}
                        occupied={name.lower() for name in current if name!=ignored}
                        for name in names:
                            if name.lower() in occupied:raise TicketError(f'Username {name} already exists in Guacamole.')
                        reply={'available':True}
                    else:
                        reply=user_stats(auth,project_id,data.get('username'),GuacamoleClient(),repo_root)
                elif data.get('operation', 'ticket') == 'ticket':
                    ticket = issue_ticket(session_token, project_id, auth=auth,
                                          secret_hex=secret, repo_root=repo_root, return_details=True)
                    # Keep compatibility with callers and test doubles that still
                    # return the original URL-only response.
                    reply = ticket if isinstance(ticket, dict) else {"url": ticket}
                else:
                    raise TicketError('Invalid launch request.')
            except (TicketError, ValueError, TypeError, OSError, RuntimeError, KeyError) as exc:
                print(f'crange: guac broker request failed: {exc}', flush=True)
                reply = {'error': str(exc) if isinstance(exc, (TicketError, ValueError)) else 'Guacamole service request failed.'}
            self.wfile.write(json.dumps(reply, separators=(',', ':')).encode() + b'\n')

    with Server(str(socket_path), Handler) as server:
        os.chmod(socket_path, 0o660)
        server.serve_forever(poll_interval=0.5)


if __name__ == '__main__':
    serve()
