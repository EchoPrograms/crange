"""Same-origin dashboard HTTP service with password sessions."""
import hmac
import hashlib
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from crange_auth import AuthStore
from crange_jobs import JobStore

ROOT = Path(__file__).resolve().parents[1]


def serve(host='127.0.0.1', port=8000):
    auth = AuthStore(Path.home() / '.local/share/crange/auth.sqlite3')
    jobs = JobStore(os.environ.get('CRANGE_JOB_DB', Path.home() / '.local/share/crange/jobs.sqlite3'))
    jobs_stop = None if os.environ.get('CRANGE_WORKER_EXTERNAL') == '1' else jobs.start()

    class Handler(BaseHTTPRequestHandler):
        def handle(self):
            try:
                super().handle()
            except Exception as exc:
                # Keep service logs actionable and free of Python tracebacks.
                print(f'crange: request failed: {exc}', flush=True)
                try:
                    self.reply(500, {'error': 'Internal dashboard error.'})
                except Exception:
                    pass

        def token(self):
            jar = cookies.SimpleCookie()
            try:
                jar.load(self.headers.get('Cookie', ''))
                return jar['crange_session'].value if 'crange_session' in jar else ''
            except cookies.CookieError:
                return ''

        def reply(self, status, data, cookie=None):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if cookie:
                self.send_header('Set-Cookie', cookie)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/api/session':
                user = auth.session(self.token())
                return self.reply(200 if user else 401, user or {'error': 'Sign in required.'})
            if path == '/api/projects':
                user = auth.session(self.token())
                if not user:
                    return self.reply(401, {'error': 'Sign in required.'})
                from crange import ENVS, ROOT as CRANGE_ROOT, json_config, project_quota, CrangeError
                projects = []
                for directory in sorted(ENVS.glob('project-[0-9][0-9][0-9]')):
                    project = str(int(directory.name.split('-')[-1]))
                    if user['admin'] or project in user['projects']:
                        try:
                            cfg = json_config(directory)
                            record = {k: cfg.get(k) for k in ('project_id', 'team_ids', 'jump_boxes', 'custom_networks', 'scenario_boxes', 'scenario_containers')}
                            desired_file = directory / 'crange.auto.tfvars.json'
                            if not desired_file.is_file():
                                desired_file = directory / 'terraform.tfvars'
                            record['desired_revision'] = hashlib.sha256(desired_file.read_bytes()).hexdigest()
                            for kind, filename in [('vm_templates', 'approved-scenario-templates.json'), ('container_templates', 'approved-container-templates.json')]:
                                source = directory / filename
                                record[kind] = sorted(json.loads(source.read_text())) if source.is_file() else []
                            record['quota'] = project_quota(directory)
                            machines = [box for field in ('jump_boxes', 'scenario_boxes', 'scenario_containers') for box in (cfg.get(field) or {}).values()]
                            record['usage'] = {
                                'cores': sum(int(box.get('cores', 1)) for box in machines),
                                'memory_mb': sum(int(box.get('memory_mb', 512)) for box in machines),
                            }
                            manifest = ROOT / 'guacamole' / 'access' / f"project-{int(project):03d}.json"
                            if manifest.is_file():
                                try:
                                    access = json.loads(manifest.read_text())
                                    record['guacamole'] = {'status': access.get('status', 'unknown'),
                                                           'generated_at': access.get('generated_at'),
                                                           'applied_at': access.get('applied_at'),
                                                           'last_error': access.get('last_error'),
                                                           'connection_count': len(access.get('connections', [])),
                                                           'user_count': len(access.get('users', []))}
                                except (OSError, ValueError):
                                    record['guacamole'] = {'status': 'invalid'}
                            releases = CRANGE_ROOT / 'releases' / directory.name
                            record['releases'] = []
                            for manifest in sorted(releases.glob('*.json')) if releases.is_dir() else []:
                                try:
                                    release = json.loads(manifest.read_text())
                                    record['releases'].append({'version': release['version'],
                                                               'created_at': release.get('created_at'),
                                                               'release_id': release.get('release_id')})
                                except (OSError, ValueError, KeyError):
                                    continue
                            projects.append(record)
                        except CrangeError as exc:
                            projects.append({'project_id': int(project), 'error': str(exc)})
                return self.reply(200, {'projects': projects})
            if path == '/api/jobs':
                user = auth.session(self.token())
                if not user:
                    return self.reply(401, {'error': 'Sign in required.'})
                return self.reply(200, {'jobs': jobs.list(user['projects'], user['admin'])})
            if path == '/api/users':
                user = auth.session(self.token())
                if not user:
                    return self.reply(401, {'error': 'Sign in required.'})
                records = auth.list_users()
                if not user['admin']:
                    allowed = set(user['projects'])
                    records = [record for record in records if allowed.intersection(record['projects'])]
                return self.reply(200, {'users': records})
            if path == '/api/connector':
                user = auth.session(self.token())
                if not user:
                    return self.reply(401, {'error': 'Sign in required.'})
                status_path = Path('/var/lib/crange-jobs/guacamole-status.json')
                try:
                    status = json.loads(status_path.read_text()) if status_path.is_file() else {}
                except (OSError, ValueError):
                    status = {}
                return self.reply(200, {'configured': bool(status.get('configured', False)),
                                        'url': status.get('url', 'http://10.60.0.10:8080'),
                                        'state': status.get('state', 'pending_configuration')})
            if path == '/api/audit':
                user = auth.session(self.token())
                if not user:
                    return self.reply(401, {'error': 'Sign in required.'})
                if not user['admin']:
                    return self.reply(403, {'error': 'Administrator access required.'})
                return self.reply(200, {'events': auth.list_audit()})
            names = {'/': 'index.html', '/app.js': 'app.js', '/styles.css': 'styles.css'}
            if path not in names:
                return self.reply(404, {'error': 'Not found.'})
            file = ROOT / 'gui' / names[path]
            body = file.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', {'html': 'text/html', 'js': 'text/javascript', 'css': 'text/css'}[file.suffix[1:]])
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Content-Security-Policy', "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            origin = self.headers.get('Origin')
            if origin and urlsplit(origin).netloc != self.headers.get('Host'):
                return self.reply(403, {'error': 'Origin rejected.'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 65536:
                    raise ValueError('Invalid request size.')
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError('Expected an object.')
                if self.path == '/api/login':
                    username, password = data.get('username'), data.get('password')
                    if not isinstance(username, str) or not isinstance(password, str):
                        raise ValueError('Username and password required.')
                    result = auth.login(username, password)
                    if not result:
                        return self.reply(401, {'error': 'Invalid login or too many attempts.'})
                    token, csrf = result
                    return self.reply(200, {'csrf': csrf}, f'crange_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800')
                user = auth.session(self.token())
                if not user:
                    return self.reply(401, {'error': 'Sign in required.'})
                if not hmac.compare_digest(user['csrf'], self.headers.get('X-CSRF-Token', '')):
                    return self.reply(403, {'error': 'Invalid session request.'})
                if self.path == '/api/logout':
                    auth.logout(self.token())
                    return self.reply(200, {}, 'crange_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
                if self.path == '/api/users':
                    username = data.get('username')
                    projects = data.get('projects', [])
                    teams = data.get('teams', {})
                    project = data.get('project')
                    team_ids = data.get('team_ids', [])
                    if not isinstance(username, str):
                        raise ValueError('Invalid user assignment.')
                    if data.get('action') == 'create':
                        if not user['admin'] or not isinstance(data.get('password'), str):
                            return self.reply(403, {'error': 'Administrator access required.'})
                        auth.set_password(username, data['password'], admin=bool(data.get('admin', False)), projects=projects)
                        auth.audit(user['username'], 'user-create', username, {'projects': projects})
                        return self.reply(200, {'users': auth.list_users()})
                    if data.get('action') == 'set-enabled':
                        if not user['admin'] or username == user['username']:
                            return self.reply(403, {'error': 'Administrator access required; you cannot disable your own account.'})
                        target = next((record for record in auth.list_users() if record['username'] == username), None)
                        if not target:
                            return self.reply(404, {'error': 'User not found.'})
                        auth.set_enabled(username, bool(data.get('enabled', False)))
                        auth.audit(user['username'], 'user-enable' if data.get('enabled', False) else 'user-disable', username)
                        sync_jobs = [jobs.submit(user['username'], int(p), 'guac-sync', {}) for p in target['projects']]
                        return self.reply(200, {'users': auth.list_users(), 'sync_jobs': sync_jobs})
                    if not user['admin']:
                        if isinstance(project, bool) or not isinstance(project, int) or str(project) not in user['projects']:
                            return self.reply(403, {'error': 'Project access denied.'})
                        if not isinstance(team_ids, list) and data.get('action') != 'set-connections':
                            raise ValueError('Invalid team assignment.')
                        target = next((record for record in auth.list_users(project=str(project)) if record['username'] == username), None)
                        if not target:
                            return self.reply(403, {'error': 'User is not assigned to this project.'})
                        if data.get('action') == 'set-connections':
                            connection_keys = data.get('connection_keys')
                            if not isinstance(connection_keys, list) or any(not isinstance(key, str) for key in connection_keys):
                                raise ValueError('Invalid connection assignment.')
                            from crange import ENVS, json_config
                            cfg = json_config(ENVS / f'project-{project:03d}')
                            allowed = ({f'jumpbox:{key}' for key in cfg.get('jump_boxes', {})} |
                                       {f'vm:{key}' for key in cfg.get('scenario_boxes', {})} |
                                       {f'container:{key}' for key in cfg.get('scenario_containers', {})})
                            if not set(connection_keys) <= allowed:
                                raise ValueError('Choose declared project connections only.')
                            auth.set_connection_access(username, project, connection_keys)
                            auth.audit(user['username'], 'guac-connection-access-update', username,
                                       {'project': project, 'connections': sorted(set(connection_keys))})
                            sync_jobs = [jobs.submit(user['username'], project, 'guac-sync', {})]
                            return self.reply(200, {'users': auth.list_users(project=str(project)), 'sync_jobs': sync_jobs})
                        from crange import ENVS, json_config
                        cfg = json_config(ENVS / f'project-{project:03d}')
                        valid_teams = set(map(str, cfg.get('team_ids', [])))
                        if any(isinstance(team, bool) or not isinstance(team, int) or str(team) not in valid_teams for team in team_ids):
                            raise ValueError('Choose configured team numbers only; staging is not a user team.')
                        auth.set_team_access(username, project, team_ids)
                        auth.audit(user['username'], 'team-access-update', username, {'project': project, 'teams': team_ids})
                        sync_jobs = [jobs.submit(user['username'], project, 'guac-sync', {})]
                        return self.reply(200, {'users': auth.list_users(project=str(project)), 'sync_jobs': sync_jobs})
                    if not isinstance(projects, list) or not isinstance(teams, dict):
                        raise ValueError('Invalid user assignment.')
                    previous = next((record for record in auth.list_users() if record['username'] == username), None)
                    previous_projects = previous['projects'] if previous else []
                    auth.set_access(username, projects, teams)
                    auth.audit(user['username'], 'project-access-update', username, {'projects': projects, 'teams': teams})
                    sync_jobs = []
                    for project in sorted(set(map(int, projects)) | set(map(int, previous_projects))):
                        sync_jobs.append(jobs.submit(user['username'], int(project), 'guac-sync', {}))
                    return self.reply(200, {'users': auth.list_users(), 'sync_jobs': sync_jobs})
                if self.path == '/api/jobs':
                    project = data.get('project')
                    if isinstance(project, bool) or not isinstance(project, int):
                        raise ValueError('Invalid project.')
                    if data.get('operation') in ('project-init', 'plan-sdn-repair') and not user['admin']:
                        return self.reply(403, {'error': 'Administrator access required.'})
                    if data.get('operation') != 'project-init' and not user['admin'] and str(project) not in user['projects']:
                        return self.reply(403, {'error': 'Project access denied.'})
                    job = jobs.submit(user['username'], project, data.get('operation'), data.get('options', {}))
                    return self.reply(202, {'job_id': job})
                return self.reply(404, {'error': 'Not found.'})
            except (ValueError, TypeError):
                self.reply(400, {'error': 'Invalid request.'})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f'CRANGE listening on http://{host}:{port}', flush=True)
    try:
        server.serve_forever()
    finally:
        if jobs_stop:
            jobs_stop.set()
        server.server_close()
