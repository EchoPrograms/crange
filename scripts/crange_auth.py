"""Password and session storage for the controller dashboard."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time
from contextlib import contextmanager


class AuthStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shared = os.environ.get('CRANGE_SHARED_DB') == '1'
        # The dashboard and worker deliberately share this database.  Keep an
        # already shared file group-readable even when a maintenance command
        # is invoked without the service environment; otherwise that command
        # could silently downgrade the file to 0600 and break Guacamole.
        try:
            existing_mode = stat.S_IMODE(self.path.stat().st_mode)
        except FileNotFoundError:
            existing_mode = 0
        shared = shared or bool(existing_mode & 0o060 == 0o060)
        mode = 0o660 if shared else 0o600
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, mode)
        os.close(fd)
        if self.path.stat().st_uid == os.geteuid():
            self.path.chmod(mode)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    name TEXT PRIMARY KEY, salt TEXT NOT NULL,
                    password TEXT NOT NULL, admin INTEGER NOT NULL,
                    projects TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS sessions (
                    digest TEXT PRIMARY KEY, username TEXT NOT NULL,
                    csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    name TEXT PRIMARY KEY, failures INTEGER NOT NULL,
                    reset REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS user_teams (
                    username TEXT NOT NULL, project TEXT NOT NULL,
                    team TEXT NOT NULL, PRIMARY KEY(username, project, team),
                    FOREIGN KEY(username) REFERENCES users(name) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS user_connections (
                    username TEXT NOT NULL, project TEXT NOT NULL,
                    resource_key TEXT NOT NULL,
                    PRIMARY KEY(username, project, resource_key),
                    FOREIGN KEY(username) REFERENCES users(name) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL,
                    actor TEXT NOT NULL, action TEXT NOT NULL,
                    target TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS guacamole_users (
                    project INTEGER NOT NULL, name TEXT NOT NULL,
                    enabled INTEGER NOT NULL, teams TEXT NOT NULL,
                    connections TEXT NOT NULL, password_revision TEXT NOT NULL,
                    sealed_password TEXT NOT NULL, PRIMARY KEY(project,name));
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(guacamole_users)')}
            for field, definition in [('login', 'TEXT'), ('allow_password_reset', 'INTEGER NOT NULL DEFAULT 0'),
                                      ('user_number', 'INTEGER'), ('home_team', 'INTEGER'), ('history_archive', "TEXT NOT NULL DEFAULT '[]'")]:
                if field not in columns:
                    db.execute(f'ALTER TABLE guacamole_users ADD COLUMN {field} {definition}')
            if db.execute('SELECT 1 FROM guacamole_users WHERE login IS NULL LIMIT 1').fetchone():
                db.execute("UPDATE guacamole_users SET login=printf('p%03d-',project)||name WHERE login IS NULL")
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS guacamole_unique_login ON guacamole_users(login COLLATE NOCASE)')
            db.executescript("""
                CREATE TABLE IF NOT EXISTS guacamole_project_settings (
                    project INTEGER PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '',
                    username_template TEXT NOT NULL, sealed_password_template TEXT,
                    jumpboxes TEXT NOT NULL DEFAULT '[]');
                CREATE TABLE IF NOT EXISTS guacamole_enrollments (
                    id TEXT PRIMARY KEY, project INTEGER NOT NULL, boxes TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending');
            """)
            # Preserve existing managers once when upgrading the old membership
            # model. New memberships have participant access unless promoted.
            db.execute('BEGIN IMMEDIATE')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_managers'").fetchone():
                db.execute('CREATE TABLE project_managers (username TEXT NOT NULL, project TEXT NOT NULL, '
                           'PRIMARY KEY(username,project))')
                for row in db.execute('SELECT name,projects FROM users').fetchall():
                    db.executemany('INSERT INTO project_managers VALUES (?,?)',
                                   [(row['name'], str(project)) for project in json.loads(row['projects'])])

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def password_hash(password, salt):
        return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                              n=16384, r=8, p=1).hex()

    def set_password(self, username, password, *, admin=False, projects=(), role=None):
        import re
        if not re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}', username):
            raise ValueError('Use a lowercase username of up to 64 characters.')
        if not 12 <= len(password) <= 1024:
            raise ValueError('Password must contain 12–1024 characters.')
        projects = sorted({str(int(p)) for p in projects})
        if role not in (None, 'manager', 'participant'):
            raise ValueError('Choose manager or participant access.')
        salt = secrets.token_hex(16)
        hashed = self.password_hash(password, salt)
        with self.connect() as db:
            if db.execute('SELECT 1 FROM guacamole_users WHERE login=? COLLATE NOCASE', (username,)).fetchone():
                raise ValueError('This username belongs to a Guacamole account.')
            if re.match(r'^p[0-9]{3}-', username) and not db.execute('SELECT 1 FROM users WHERE name=?', (username,)).fetchone():
                raise ValueError('Project-prefixed usernames are reserved for Guacamole accounts.')
            db.execute('INSERT INTO users VALUES (?,?,?,?,?,1) '
                       'ON CONFLICT(name) DO UPDATE SET salt=excluded.salt, '
                       'password=excluded.password, admin=excluded.admin, '
                       'projects=excluded.projects, enabled=1',
                       (username, salt, hashed, int(admin), json.dumps(projects)))
            db.execute('DELETE FROM sessions WHERE username=?', (username,))
            db.execute('DELETE FROM project_managers WHERE username=? AND project NOT IN (' +
                       ','.join('?' for _ in projects) + ')', (username, *projects)) if projects else \
                db.execute('DELETE FROM project_managers WHERE username=?', (username,))
            if role is not None:
                db.execute('DELETE FROM project_managers WHERE username=?', (username,))
                if role == 'manager':
                    db.executemany('INSERT INTO project_managers VALUES (?,?)', [(username, p) for p in projects])

    def login(self, username, password):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            attempt = db.execute('SELECT * FROM attempts WHERE name=?', (username,)).fetchone()
            if attempt and attempt['reset'] > now and attempt['failures'] >= 5:
                return None
            user = db.execute('SELECT * FROM users WHERE name=?', (username,)).fetchone()
            salt = user['salt'] if user else '00' * 16
            digest = self.password_hash(password[:1024], salt)
            valid = user and user['enabled'] and len(password) <= 1024 and hmac.compare_digest(digest, user['password'])
            if not valid:
                failures = attempt['failures'] + 1 if attempt and attempt['reset'] > now else 1
                reset = attempt['reset'] if attempt and attempt['reset'] > now else now + 300
                db.execute('INSERT OR REPLACE INTO attempts VALUES (?,?,?)', (username, failures, reset))
                return None
            db.execute('DELETE FROM attempts WHERE name=?', (username,))
            db.execute('DELETE FROM sessions WHERE expires<?', (now,))
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)',
                       (hashlib.sha256(token.encode()).hexdigest(), username, csrf, now + 28800))
            return token, csrf

    def session(self, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            row = db.execute('SELECT u.name,u.admin,u.projects,s.csrf FROM sessions s '
                             'JOIN users u ON u.name=s.username WHERE s.digest=? '
                             'AND s.expires>? AND u.enabled=1', (digest, time.time())).fetchone()
            managed = ([item['project'] for item in db.execute(
                'SELECT project FROM project_managers WHERE username=? ORDER BY project', (row['name'],))]
                       if row else [])
        if not row:
            return None
        return dict(username=row['name'], admin=bool(row['admin']),
                    projects=json.loads(row['projects']), managed_projects=managed, csrf=row['csrf'])

    def logout(self, token):
        with self.connect() as db:
            db.execute('DELETE FROM sessions WHERE digest=?',
                       (hashlib.sha256(token.encode()).hexdigest(),))

    def list_users(self, project=None):
        with self.connect() as db:
            rows = db.execute('SELECT name,admin,projects,enabled FROM users ORDER BY name').fetchall()
            teams = db.execute('SELECT username,project,team FROM user_teams ORDER BY project,team,username').fetchall()
            connections = db.execute('SELECT username,project,resource_key FROM user_connections '
                                     'ORDER BY project,resource_key,username').fetchall()
            managers = {(row['username'], row['project']) for row in db.execute('SELECT * FROM project_managers')}
        assignments = {}
        for row in teams:
            assignments.setdefault(row['username'], {}).setdefault(row['project'], []).append(row['team'])
        connection_assignments = {}
        for row in connections:
            connection_assignments.setdefault(row['username'], {}).setdefault(row['project'], []).append(row['resource_key'])
        result = []
        for row in rows:
            projects = json.loads(row['projects'])
            if project is not None and not bool(row['admin']) and str(project) not in projects:
                continue
            result.append({'username': row['name'], 'admin': bool(row['admin']),
                           'enabled': bool(row['enabled']), 'projects': projects,
                           'roles': {p: 'manager' if (row['name'], p) in managers else 'participant' for p in projects},
                           'teams': assignments.get(row['name'], {}),
                           'connections': connection_assignments.get(row['name'], {})})
        return result

    def set_access(self, username, projects, teams=None):
        projects = sorted({str(int(p)) for p in projects})
        teams = teams or {}
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM users WHERE name=?', (username,)).fetchone():
                raise ValueError('Unknown dashboard user.')
            db.execute('UPDATE users SET projects=? WHERE name=?', (json.dumps(projects), username))
            db.execute('DELETE FROM user_teams WHERE username=?', (username,))
            db.execute('DELETE FROM project_managers WHERE username=? AND project NOT IN (' +
                       ','.join('?' for _ in projects) + ')', (username, *projects)) if projects else \
                db.execute('DELETE FROM project_managers WHERE username=?', (username,))
            db.execute('DELETE FROM user_connections WHERE username=? AND project NOT IN (' +
                       ','.join('?' for _ in projects) + ')', (username, *projects)) if projects else \
                db.execute('DELETE FROM user_connections WHERE username=?', (username,))
            for project, team_ids in teams.items():
                if str(project) not in projects:
                    raise ValueError('Team assignments must belong to an assigned project.')
                for team in sorted({str(int(t)) for t in team_ids}):
                    db.execute('INSERT INTO user_teams(username,project,team) VALUES (?,?,?)',
                               (username, str(int(project)), team))

    def set_project_role(self, username, project, role):
        project = str(int(project))
        if role not in ('manager', 'participant'):
            raise ValueError('Choose manager or participant access.')
        with self.connect() as db:
            row = db.execute('SELECT projects FROM users WHERE name=?', (username,)).fetchone()
            if not row or project not in json.loads(row['projects']):
                raise ValueError('User is not assigned to this project.')
            db.execute('DELETE FROM project_managers WHERE username=? AND project=?', (username, project))
            if role == 'manager':
                db.execute('INSERT INTO project_managers VALUES (?,?)', (username, project))

    def set_team_access(self, username, project, team_ids):
        project = str(int(project))
        teams = sorted({str(int(team)) for team in team_ids})
        with self.connect() as db:
            row = db.execute('SELECT projects FROM users WHERE name=?', (username,)).fetchone()
            if not row:
                raise ValueError('Unknown dashboard user.')
            if project not in json.loads(row['projects']):
                raise ValueError('User is not assigned to this project.')
            db.execute('DELETE FROM user_teams WHERE username=? AND project=?', (username, project))
            for team in teams:
                    db.execute('INSERT INTO user_teams(username,project,team) VALUES (?,?,?)',
                               (username, project, team))

    def set_connection_access(self, username, project, resource_keys):
        project = str(int(project))
        keys = sorted(set(resource_keys))
        if any(not isinstance(key, str) or len(key) > 128 for key in keys):
            raise ValueError('Invalid Guacamole connection assignment.')
        with self.connect() as db:
            row = db.execute('SELECT projects FROM users WHERE name=?', (username,)).fetchone()
            if not row:
                raise ValueError('Unknown dashboard user.')
            if project not in json.loads(row['projects']):
                raise ValueError('User is not assigned to this project.')
            db.execute('DELETE FROM user_connections WHERE username=? AND project=?', (username, project))
            for key in keys:
                db.execute('INSERT INTO user_connections(username,project,resource_key) VALUES (?,?,?)',
                           (username, project, key))

    def set_enabled(self, username, enabled):
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM users WHERE name=?', (username,)).fetchone():
                raise ValueError('Unknown dashboard user.')
            db.execute('UPDATE users SET enabled=? WHERE name=?', (int(bool(enabled)), username))
            if not enabled:
                db.execute('DELETE FROM sessions WHERE username=?', (username,))

    def audit(self, actor, action, target, detail=None):
        with self.connect() as db:
            db.execute('INSERT INTO audit_events(created,actor,action,target,detail) VALUES (?,?,?,?,?)',
                       (time.time(), actor, action, target, json.dumps(detail or {}, sort_keys=True)))

    def list_audit(self, limit=100):
        with self.connect() as db:
            rows = db.execute('SELECT id,created,actor,action,target,detail FROM audit_events '
                              'ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        result = []
        for row in rows:
            result.append({'id': row['id'], 'created': row['created'], 'actor': row['actor'],
                           'action': row['action'], 'target': row['target'],
                           'detail': json.loads(row['detail'])})
        return result
