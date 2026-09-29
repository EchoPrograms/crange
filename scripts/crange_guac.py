"""Controlled, ownership-safe Guacamole REST reconciliation for CRANGE."""
from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

class GuacamoleError(RuntimeError):
    pass

def _env_file(path):
    values = {}
    try:
        lines = Path(path).read_text().splitlines()
    except (OSError, UnicodeError):
        return values
    for line in lines:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('"\'')
    return values

class GuacamoleClient:
    def __init__(self, base_url=None, username=None, password=None, opener=urlopen):
        values = _env_file(os.environ.get('CRANGE_GUACAMOLE_ENV', '/home/crange/.config/crange/guacamole.env'))
        self.base_url = (base_url or os.environ.get('GUACAMOLE_URL') or values.get('GUACAMOLE_URL') or 'http://10.60.0.10:8080').rstrip('/')
        self.username = username or os.environ.get('GUACAMOLE_USERNAME') or values.get('GUACAMOLE_USERNAME')
        self.password = password or os.environ.get('GUACAMOLE_PASSWORD') or values.get('GUACAMOLE_PASSWORD')
        self.opener, self.token, self.data_source = opener, None, None

    def _request(self, method, path, data=None, content_type='application/json'):
        body, headers = None, {}
        if data is not None:
            body = urlencode(data).encode() if content_type == 'application/x-www-form-urlencoded' else json.dumps(data).encode()
            headers['Content-Type'] = content_type
        if self.token:
            headers['Guacamole-Token'] = self.token
        try:
            response = self.opener(Request(self.base_url + path, data=body, headers=headers, method=method), timeout=15)
            raw = response.read()
            return json.loads(raw) if raw else {}
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise GuacamoleError(f'Guacamole API returned HTTP {exc.code} for {method} {path}.') from exc
        except (OSError, URLError, ValueError) as exc:
            raise GuacamoleError(f'Guacamole API request failed: {exc}') from exc

    def login(self):
        if not self.username or not self.password:
            raise GuacamoleError('Set GUACAMOLE_USERNAME and GUACAMOLE_PASSWORD in the protected connector environment.')
        result = self._request('POST', '/api/tokens', {'username': self.username, 'password': self.password}, 'application/x-www-form-urlencoded')
        if not result or not result.get('authToken') or not result.get('dataSource'):
            raise GuacamoleError('Guacamole login did not return an authentication token.')
        self.token, self.data_source = result['authToken'], result['dataSource']

    @staticmethod
    def _connection_payload(record):
        protocol = record.get('protocol', 'ssh')
        port = str(record.get('port') or (3389 if protocol == 'rdp' else 22))
        params = {'hostname': record['address'], 'port': port}
        if protocol == 'rdp':
            params.update({'ignore-cert': 'true', 'security': 'any'})
        else:
            params.update({'username': '', 'host-key': ''})
        # Guacamole's REST mapper expects both the parent and attributes keys
        # even when the connection lives at the root and has no attributes.
        return {'parentIdentifier': 'ROOT', 'name': record['name'],
                'protocol': protocol, 'parameters': params, 'attributes': {}}

    def apply(self, manifest, state_path=None):
        self.login()
        source = self.data_source
        users = self._request('GET', f'/api/session/data/{source}/users') or {}
        missing = sorted({u['username'] for u in manifest.get('users', []) if u['username'] not in users})
        if missing:
            raise GuacamoleError('Guacamole users must be provisioned before access can be applied: ' + ', '.join(missing))
        connections = self._request('GET', f'/api/session/data/{source}/connections') or {}
        state_path = Path(state_path) if state_path else None
        old = {}
        if state_path and state_path.is_file():
            try:
                old = json.loads(state_path.read_text())
            except (OSError, ValueError) as exc:
                raise GuacamoleError(f'Cannot read Guacamole ownership state: {exc}') from exc
        owned = {str(item['name']): str(item['identifier']) for item in old.get('connections', []) if item.get('name') and item.get('identifier')}
        desired = {str(item['name']): item for item in manifest.get('connections', [])}
        identifiers, applied = {}, []
        for name, record in desired.items():
            if not record.get('address'):
                raise GuacamoleError(f'Connection {name} has no deployed address.')
            identifier = owned.get(name)
            current = connections.get(identifier) if identifier else None
            if identifier and not current:
                raise GuacamoleError(f'Owned Guacamole connection {name} ({identifier}) no longer exists; review the manifest.')
            if not identifier:
                collisions = [str(item.get('identifier')) for item in connections.values() if item.get('name') == name]
                if collisions:
                    raise GuacamoleError(f'Guacamole connection name is already in use outside CRANGE ownership: {name}.')
                created = self._request('POST', f'/api/session/data/{source}/connections', self._connection_payload(record)) or {}
                identifier = str(created.get('identifier') or '')
                if not identifier:
                    raise GuacamoleError(f'Guacamole did not return an identifier for {name}.')
            else:
                self._request('PUT', f'/api/session/data/{source}/connections/{identifier}', self._connection_payload(record))
            identifiers[name] = identifier
            applied.append(name)
        resource_identifiers = {}
        for name, record in desired.items():
            resource_identifiers[name] = identifiers[name]
            if record.get('key'):
                resource_identifiers[str(record['key'])] = identifiers[name]
        desired_grants = set()
        for user in manifest.get('users', []):
            keys = set(user.get('connection_keys', []))
            # Backward-compatible manifests used default_for_team before explicit keys.
            if not keys:
                team_data = user.get('teams', {})
                if isinstance(team_data, dict):
                    team_values = team_data.get(str(manifest.get('project_id')), [])
                else:
                    team_values = team_data if isinstance(team_data, list) else []
                teams = {str(t) for t in team_values}
                keys.update(record.get('key', record.get('name', '')) for record in manifest.get('connections', [])
                            if record.get('default_for_team') and str(record.get('team_id')) in teams)
            desired_grants.update((user['username'], resource_identifiers[key])
                                  for key in keys if key in resource_identifiers)
        previous_grants = {(g.get('username'), str(g.get('identifier'))) for g in old.get('permissions', []) if g.get('username') and g.get('identifier')}
        for username, identifier in sorted(desired_grants - previous_grants):
            self._request('PATCH', f'/api/session/data/{source}/users/{username}/permissions',
                          [{'op': 'add', 'path': f'/connectionPermissions/{identifier}', 'value': 'READ'}])
        for username, identifier in sorted(previous_grants - desired_grants):
            self._request('PATCH', f'/api/session/data/{source}/users/{username}/permissions',
                          [{'op': 'remove', 'path': f'/connectionPermissions/{identifier}', 'value': 'READ'}])
        for name, identifier in sorted(owned.items()):
            if name in desired:
                continue
            self._request('DELETE', f'/api/session/data/{source}/connections/{identifier}')
        if state_path:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({'schema_version': 1,
                                  'connections': [{'name': n, 'identifier': i} for n, i in sorted(identifiers.items())],
                                  'permissions': [{'username': u, 'identifier': i} for u, i in sorted(desired_grants)]},
                                 indent=2, sort_keys=True) + '\n'
            fd, temporary = tempfile.mkstemp(prefix=state_path.name + '.', dir=state_path.parent)
            try:
                with os.fdopen(fd, 'w') as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, state_path)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        return applied

def apply_manifest(path):
    manifest_path = Path(path)
    return GuacamoleClient().apply(json.loads(manifest_path.read_text()), manifest_path.with_name(manifest_path.stem + '.applied.json'))
