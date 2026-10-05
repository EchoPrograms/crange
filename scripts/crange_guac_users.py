"""Project-owned Guacamole accounts, separate from dashboard accounts."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid


def valid_username(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,95}', name):
        raise ValueError('Use 1–96 letters, numbers, dots, underscores, @ or hyphens.')
    return name


def login_name(project, name):
    return f'p{int(project):03d}-{valid_username(name)}'


def seal_password(password, secret_hex, project, name):
    if not isinstance(password, str) or not 6 <= len(password) <= 1024:
        raise ValueError('Password must contain 6–1024 characters.')
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = hashlib.sha256(bytes.fromhex(secret_hex) + b'crange-guacamole-user-password').digest()
    nonce = os.urandom(12)
    associated = login_name(project, name).encode()
    return base64.urlsafe_b64encode(nonce + AESGCM(key).encrypt(nonce, password.encode(), associated)).decode()


def open_password(sealed, secret_hex, project, name):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = hashlib.sha256(bytes.fromhex(secret_hex) + b'crange-guacamole-user-password').digest()
    try:
        value = base64.urlsafe_b64decode(sealed)
        return AESGCM(key).decrypt(value[:12], value[12:], login_name(project, name).encode()).decode()
    except Exception as exc:
        raise ValueError('Cannot decrypt the project user password.') from exc


class ProjectUsers:
    def __init__(self, auth):
        self.auth = auth

    def list(self, project, *, private=False):
        with self.auth.connect() as db:
            rows = db.execute('SELECT * FROM guacamole_users WHERE project=? ORDER BY name', (int(project),)).fetchall()
        result = []
        for row in rows:
            item = {'project_id': row['project'], 'username': row['name'],
                    'login': row['login'], 'enabled': bool(row['enabled']),
                    'allow_password_reset': bool(row['allow_password_reset']),
                    'user_number': row['user_number'], 'home_team': row['home_team'],
                    'teams': json.loads(row['teams']), 'connections': json.loads(row['connections']),
                    'password_revision': row['password_revision']}
            if private:
                item['sealed_password'] = row['sealed_password']
            result.append(item)
        return result

    def create(self, project, name, sealed, teams, connections, *, login=None,
               allow_password_reset=False, user_number=None, home_team=None):
        valid_username(name)
        if not isinstance(sealed, str) or not sealed:
            raise ValueError('A protected password is required.')
        login = valid_username(login) if login is not None else login_name(project, name)
        with self.auth.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM guacamole_users WHERE login=? COLLATE NOCASE', (login,)).fetchone():
                raise ValueError('This Guacamole username already exists.')
            if db.execute('SELECT 1 FROM users WHERE name=? COLLATE NOCASE', (login,)).fetchone():
                raise ValueError('This username belongs to a dashboard account.')
            if db.execute('SELECT 1 FROM guacamole_users WHERE project=? AND name=?', (int(project), name)).fetchone():
                raise ValueError('This project user already exists.')
            db.execute('INSERT INTO guacamole_users(project,name,enabled,teams,connections,password_revision,sealed_password,login,allow_password_reset,user_number,home_team) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (int(project), name, 1, json.dumps(sorted(set(map(str, teams)))),
                        json.dumps(sorted(set(connections))), uuid.uuid4().hex, sealed, login,
                        int(allow_password_reset), user_number, home_team))

    def update(self, project, name, *, teams, connections, enabled, sealed=None, login=None, allow_password_reset=None):
        valid_username(name)
        with self.auth.connect() as db:
            row = db.execute('SELECT 1 FROM guacamole_users WHERE project=? AND name=?', (int(project), name)).fetchone()
            if not row:
                raise ValueError('Project user not found.')
            db.execute('UPDATE guacamole_users SET enabled=?,teams=?,connections=? WHERE project=? AND name=?',
                       (int(enabled), json.dumps(sorted(set(map(str, teams)))),
                        json.dumps(sorted(set(connections))), int(project), name))
            if login is not None:
                valid_username(login)
                if db.execute('SELECT 1 FROM guacamole_users WHERE login=? COLLATE NOCASE AND NOT(project=? AND name=?)', (login,int(project),name)).fetchone():
                    raise ValueError('This Guacamole username already exists.')
                if db.execute('SELECT 1 FROM users WHERE name=? COLLATE NOCASE', (login,)).fetchone():
                    raise ValueError('This username belongs to a dashboard account.')
                db.execute('UPDATE guacamole_users SET login=? WHERE project=? AND name=?', (login,int(project),name))
            if allow_password_reset is not None:
                db.execute('UPDATE guacamole_users SET allow_password_reset=? WHERE project=? AND name=?', (int(allow_password_reset),int(project),name))
            if sealed is not None:
                db.execute('UPDATE guacamole_users SET sealed_password=?,password_revision=? WHERE project=? AND name=?',
                           (sealed, uuid.uuid4().hex, int(project), name))

    def delete(self, project, name):
        with self.auth.connect() as db:
            if not db.execute('DELETE FROM guacamole_users WHERE project=? AND name=?',
                              (int(project), valid_username(name))).rowcount:
                raise ValueError('Project user not found.')


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name+'.')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def apply_users(project, auth, client, repo_root, secret_hex):
    project = int(project)
    directory = Path(repo_root) / 'guacamole/access'
    connections_file = directory / f'project-{project:03d}.applied.json'
    state_file = directory / f'project-{project:03d}.users-applied.json'
    connections = json.loads(connections_file.read_text())['connections'] if connections_file.exists() else []
    state = json.loads(state_file.read_text()) if state_file.exists() else {'users': {}}
    owned = state['users']
    desired = ProjectUsers(auth).list(project, private=True)
    client.login()
    base = f'/api/session/data/{client.data_source}/users'
    current = client._request('GET', base) or {}
    for user in desired:
        if user['login'] in current and user['login'] not in owned:
            raise ValueError(f"Guacamole login {user['login']} already exists outside project ownership.")
    from crange_jumpboxes import resource_identifiers
    identifiers = resource_identifiers(connections)
    for user in desired:
        login = user['login']
        prior = owned.get(login, {})
        attributes = dict(current.get(login, {}).get('attributes') or {})
        payload = {'username': login, 'attributes': attributes, 'disabled': not user['enabled']}
        if login not in current or prior.get('password_revision') != user['password_revision']:
            payload['password'] = open_password(user['sealed_password'], secret_hex, project, user['username'])
        client._request('PUT' if login in current else 'POST', base+'/'+login if login in current else base, payload)
        # Record ownership immediately so a later API failure is safely retryable.
        owned[login] = {'password_revision': user['password_revision'], 'user_key': user['username']}
        save_state(state_file, state)
        keys = set(user['connections']) | {
            item['resource_key'] for item in connections
            if item.get('default_for_team') and str(item['team_id']) in user['teams']}
        allowed = {identifier for key in keys for identifier in identifiers.get(key, set())} if user['enabled'] else set()
        permissions = client._request('GET', base+'/'+login+'/permissions') or {}
        patches = [{'op': 'remove', 'path': f'/connectionPermissions/{identifier}', 'value': value}
                   for identifier, values in (permissions.get('connectionPermissions') or {}).items()
                   for value in values if identifier not in allowed or value != 'READ']
        patches += [{'op': 'add', 'path': f'/connectionPermissions/{identifier}', 'value': 'READ'}
                    for identifier in sorted(allowed)
                    if 'READ' not in (permissions.get('connectionPermissions') or {}).get(identifier, [])]
        self_updates = (permissions.get('userPermissions') or {}).get(login, [])
        if user['allow_password_reset'] and 'UPDATE' not in self_updates:
            patches.append({'op':'add','path':f'/userPermissions/{login}','value':'UPDATE'})
        if not user['allow_password_reset'] and 'UPDATE' in self_updates:
            patches.append({'op':'remove','path':f'/userPermissions/{login}','value':'UPDATE'})
        if patches:
            client._request('PATCH', base+'/'+login+'/permissions', patches)
        # The bridge may manage only grants that this apply actually finished.
        # Ownership is recorded earlier for retryability, so it alone is not
        # evidence that the permission PATCH succeeded.
        owned[login]['connection_keys'] = sorted(keys) if user['enabled'] else []
        save_state(state_file, state)
        print(f"{login}: {'enabled' if user['enabled'] else 'disabled'}, {len(allowed)} connection(s).")
    wanted = {user['login'] for user in desired}
    with auth.connect() as db:
        other_users = [dict(row) for row in db.execute('SELECT login FROM guacamole_users WHERE project<>?', (project,))]
    for login in sorted(set(owned)-wanted):
        if any(item['login'].lower() == login.lower() for item in other_users):
            raise ValueError('Refusing to remove a login now owned by another project.')
        client._request('DELETE', base+'/'+login)
        del owned[login]
        save_state(state_file, state)
        print(f'{login}: removed.')
