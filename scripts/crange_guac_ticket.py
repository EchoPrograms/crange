"""Short-lived, per-session Guacamole JSON-auth launch tickets."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import struct
import time
from pathlib import Path
from urllib.parse import urlencode

MAX_BROKER_RESPONSE_BYTES = 1024 * 1024


class TicketError(RuntimeError):
    pass


def encrypt_ticket(payload: dict, secret_hex: str) -> str:
    """Encode Apache Guacamole's signed AES-CBC JSON-auth format."""
    try:
        secret = bytes.fromhex(secret_hex.strip())
    except ValueError as exc:
        raise TicketError('The Guacamole JSON authentication key is invalid.') from exc
    if len(secret) != 16:
        raise TicketError('The Guacamole JSON authentication key must be 128 bits.')

    cleartext = json.dumps(payload, separators=(',', ':'), ensure_ascii=True).encode()
    signed = hmac.digest(secret, cleartext, 'sha256') + cleartext
    pad_length = 16 - len(signed) % 16
    signed += bytes([pad_length]) * pad_length
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        encryptor = Cipher(algorithms.AES(secret), modes.CBC(bytes(16))).encryptor()
        ciphertext = encryptor.update(signed) + encryptor.finalize()
    except ImportError as exc:
        raise TicketError('Install the Python cryptography package to enable Guacamole launch tickets.') from exc
    return base64.b64encode(ciphertext).decode('ascii')


def issue_ticket(session_token: str, project_id: int, *, auth, secret_hex: str,
                 repo_root: Path, status_file=Path('/var/lib/crange-jobs/guacamole-status.json'),
                 return_details=False):
    session = auth.session(session_token)
    if not session:
        raise TicketError('Sign in required.')
    project = str(project_id)
    if not session['admin'] and project not in session['projects']:
        raise TicketError('Project access denied.')

    from crange_project_lifecycle import require_active
    try:
        require_active(repo_root / 'terraform/environments' / f'project-{project_id:03d}')
    except ValueError as exc:
        raise TicketError(str(exc)) from exc
    sidecar = repo_root / 'guacamole' / 'access' / f'project-{project_id:03d}.applied.json'
    try:
        applied = json.loads(sidecar.read_text())
        status = json.loads(Path(status_file).read_text())
    except (OSError, ValueError) as exc:
        raise TicketError('Guacamole access has not been applied for this project.') from exc
    connections = applied.get('connections', [])
    permissions = applied.get('permissions', [])
    if not connections:
        raise TicketError('No remote-access boxes are deployed for this project yet. Deploy the boxes before opening Guacamole.')
    if any(not item.get('resource_key') for item in connections):
        raise TicketError('Reapply this project’s Guacamole access manifest to enable launch tickets.')

    user = next((item for item in auth.list_users(project=project)
                 if item['username'] == session['username']), None)
    if not user or not user.get('enabled', True):
        raise TicketError('This account has no active access to the project.')

    active_keys = {str(item['resource_key']) for item in permissions
                   if item.get('username') == session['username'] and item.get('resource_key')}
    if user.get('admin'):
        current_keys = {str(item['resource_key']) for item in connections}
    else:
        teams = {str(value) for value in user.get('teams', {}).get(project, [])}
        selected = {str(value) for value in user.get('connections', {}).get(project, [])}
        current_keys = selected | {
            str(item['resource_key']) for item in connections
            if item.get('default_for_team') and str(item.get('team_id')) in teams
        }

    allowed = active_keys & current_keys
    rendered = {}
    notices = {}
    for item in connections:
        key = str(item.get('resource_key', ''))
        if key not in allowed or not item.get('address'):
            continue
        from crange_guac import guest_parameters
        protocol = item.get('protocol', 'ssh')
        params = guest_parameters(item)
        rendered[str(item['name'])] = {'protocol': protocol, 'parameters': params}
        if return_details and (item.get('is_jumpbox') or item.get('kind') == 'jumpbox' or item.get('credential_ref')):
            from crange_guest_credentials import resolve
            custom = resolve(item)
            if (not custom or custom['show_credentials']) and params.get('password'):
                notices[key] = {'name': item['name'], 'address': item['address'],
                                'username': custom['username'] if custom else params.get('username', ''),
                                'password': params['password']}
    if not rendered:
        raise TicketError('No currently applied Guacamole connections are assigned to this account.')

    try:
        url = os.environ.get('CRANGE_GUAC_PUBLIC_URL', 'https://range.brogano.dev').rstrip('/')
    except (KeyError, TypeError):
        raise TicketError('Guacamole connector is not configured.')
    if not status.get('configured') or not url.startswith(('http://', 'https://')):
        raise TicketError('Guacamole connector is not configured.')
    payload = {'username': session['username'],
               'expires': int((time.time() + 60) * 1000),
               'connections': rendered}
    token = encrypt_ticket(payload, secret_hex)
    # Override Guacamole's cached token so each dashboard launch consumes the
    # new grant set, including changes made since a previous browser login.
    # Route parameters are read by Guacamole's client and POSTed to /api/tokens.
    # Keep the encrypted grant set out of the HTTP request line: large projects
    # exceed proxy/Tomcat header limits if it is sent as a normal URL query.
    url = url + '/#/?' + urlencode({'data': token, 'token': ''})
    return {'url': url, 'credentials': list(notices.values())} if return_details else url


def request_broker(data, *, socket_path=None):
    path = socket_path or os.environ.get('CRANGE_GUAC_TICKET_SOCKET', '/run/crange-guac/ticket.sock')
    request = json.dumps(data).encode() + b'\n'
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(45)
            client.connect(path)
            client.sendall(request)
            chunks, size = [], 0
            while size <= MAX_BROKER_RESPONSE_BYTES:
                block = client.recv(min(4096, MAX_BROKER_RESPONSE_BYTES + 1 - size))
                if not block:
                    break
                chunks.append(block)
                size += len(block)
                if b'\n' in block:
                    break
        raw = b''.join(chunks)
        if size > MAX_BROKER_RESPONSE_BYTES or b'\n' not in raw:
            raise ValueError('Broker response exceeded its size limit or was incomplete.')
        response = json.loads(raw.split(b'\n', 1)[0])
    except (OSError, ValueError) as exc:
        raise TicketError('Guacamole launch service is unavailable.') from exc
    if response.get('error'):
        raise TicketError(response['error'])
    return response


def request_ticket(session_token: str, project_id: int, *, socket_path=None) -> str:
    response = request_broker({'session': session_token, 'project': project_id}, socket_path=socket_path)
    if not isinstance(response.get('url'), str):
        raise TicketError('Guacamole launch service returned an invalid response.')
    return response['url']


def request_launch(session_token, project_id):
    response = request_broker({'session': session_token, 'project': project_id})
    if not isinstance(response.get('url'), str) or not isinstance(response.get('credentials', []), list):
        raise TicketError('Guacamole launch service returned an invalid response.')
    return response


def request_sealed_password(session_token, project_id, username, password):
    response = request_broker({'operation': 'seal-password', 'session': session_token,
                               'project': project_id, 'username': username, 'password': password})
    if not isinstance(response.get('sealed_password'), str):
        raise TicketError('Guacamole password service returned an invalid response.')
    return response['sealed_password']
