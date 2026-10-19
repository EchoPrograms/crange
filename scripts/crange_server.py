"""Same-origin dashboard HTTP service with password sessions."""
import hmac
import hashlib
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from crange_auth import AuthStore
from crange_jobs import JobStore

ROOT = Path(__file__).resolve().parents[1]


def job_query_params(query, user):
    def integer_parameter(name, default, minimum, maximum):
        values = query.get(name, [str(default)])
        if len(values) != 1 or not values[0].isdigit():
            raise ValueError(f'Invalid {name}.')
        value = int(values[0])
        if value < minimum or value > maximum:
            raise ValueError(f'Invalid {name}.')
        return value
    page = integer_parameter('page', 1, 1, 1_000_000)
    # Preserve the historical unparameterized response size for older callers.
    page_size = integer_parameter('page_size', 100, 10, 100)
    project_values = query.get('project', [])
    if len(project_values) > 1 or (project_values and not project_values[0].isdigit()):
        raise ValueError('Invalid project.')
    project = int(project_values[0]) if project_values else None
    if project is not None and not user['admin'] and str(project) not in user['managed_projects']:
        raise PermissionError('Project manager access required.')
    return page, page_size, project


def serve(host='127.0.0.1', port=8000):
    auth = AuthStore(os.environ.get('CRANGE_AUTH_DB', Path.home() / '.local/share/crange/auth.sqlite3'))
    jobs = JobStore(os.environ.get('CRANGE_JOB_DB', Path.home() / '.local/share/crange/jobs.sqlite3'), auth_path=auth.path)
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
                        from crange_project_lifecycle import state as lifecycle_state, pending
                        try:
                            lifecycle = lifecycle_state(directory)
                        except ValueError as exc:
                            if user['admin']:
                                projects.append({'project_id':int(project),'display_name':f'Project {project}', 'deleted':True, 'lifecycle':{'state':'record_invalid','error':str(exc)}})
                            continue
                        queued = pending(jobs, int(project))
                        if queued:
                            lifecycle = {**lifecycle, 'pending_operation': queued['operation'], 'job_id': queued['id']}
                        inactive = lifecycle['state'] not in ('active','restored') or (queued and queued['operation'] == 'project-delete')
                        if inactive:
                            if user['admin']:
                                from crange_user_management import preferences
                                projects.append({'project_id': int(project), 'display_name': preferences(auth,int(project))['display_name'] or f'Project {project}',
                                    'lifecycle': lifecycle, 'deleted': True,
                                    'release_count': len(list((CRANGE_ROOT / 'releases' / directory.name).glob('*.json')))})
                            continue
                        if not user['admin'] and project not in user['managed_projects']:
                            from crange_user_management import preferences
                            projects.append({'project_id': int(project), 'participant': True,
                                             'display_name': preferences(auth,int(project))['display_name'] or f'Project {project}'})
                            continue
                        try:
                            cfg = json_config(directory)
                            from crange_user_management import preferences
                            settings = preferences(auth,int(project))
                            record = {k: cfg.get(k) for k in ('project_id', 'team_ids', 'jump_boxes', 'custom_networks', 'scenario_boxes', 'scenario_containers', 'release_rollouts')}
                            record['display_name'] = settings['display_name'] or f'Project {project}'
                            record['user_defaults'] = settings
                            desired_file = directory / 'crange.auto.tfvars.json'
                            if not desired_file.is_file():
                                desired_file = directory / 'terraform.tfvars'
                            record['desired_revision'] = hashlib.sha256(desired_file.read_bytes()).hexdigest()
                            record['lifecycle'] = lifecycle
                            record['has_state'] = (directory / 'terraform.tfstate').is_file()
                            bootstrap_status = directory / '.crange-bootstrap.json'
                            if bootstrap_status.is_file():
                                try:
                                    record['bootstrap_status'] = json.loads(bootstrap_status.read_text())
                                except (OSError, ValueError):
                                    record['bootstrap_status'] = {'state': 'record_invalid'}
                            placement_file = directory / 'crange-applied-placements.json'
                            record['applied_placements'] = json.loads(placement_file.read_text()) if placement_file.is_file() else {}
                            record['staged_plans'] = []
                            for summary in sorted((directory / '.crange-plans').glob('*.json')):
                                try:
                                    record['staged_plans'].append(json.loads(summary.read_text()))
                                except (OSError, ValueError):
                                    continue
                            for kind, filename in [('vm_templates', 'approved-scenario-templates.json'), ('container_templates', 'approved-container-templates.json')]:
                                source = directory / filename
                                approved = json.loads(source.read_text()) if source.is_file() else {}
                                from crange_template_selection import template_choices
                                record[kind] = sorted(template_choices(approved))
                                labels = {key: value.get('display_name') or ('ParrotOS Jump Box' if kind == 'vm_templates' and value.get('template_id') == 112 else 'Debian 13 · CRANGE base container' if kind == 'container_templates' and key == 'approved-ct-base' else 'Debian 13 · Proxmox system template' if kind == 'container_templates' and key == 'debian-13' else key) for key, value in approved.items()}
                                record.setdefault('template_labels_by_kind', {})[kind] = labels
                                record.setdefault('template_labels', {}).update(labels)
                            try:
                                from crange_guac_ticket import request_broker
                                record['template_readiness'] = request_broker({
                                    'operation': 'template-readiness', 'project': int(project),
                                    'session': self.token()}).get('templates', {})
                            except Exception:
                                record['template_readiness'] = {}
                                record['template_readiness_error'] = 'Image readiness could not be verified.'
                            from crange_jumpboxes import presets
                            record['jumpbox_templates'] = presets(int(project))
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
            if path == '/api/cluster':
                user = auth.session(self.token())
                if not user:
                    return self.reply(401, {'error': 'Sign in required.'})
                if not user['admin']:
                    return self.reply(403, {'error': 'Administrator access required.'})
                from crange_cluster import inventory_snapshot
                from crange_live_status import overlay_nodes
                from crange_guac_ticket import request_broker
                snapshot = inventory_snapshot(admin=True)
                try:
                    observed = request_broker({'operation':'cluster-status','session':self.token()})
                except (OSError, ValueError, RuntimeError):
                    import time
                    observed = {'captured_at':time.time(),'nodes':[],'error':'Live host status unavailable.'}
                return self.reply(200, overlay_nodes(snapshot, observed))
            if path == '/api/jobs':
                user = auth.session(self.token())
                if not user:
                    return self.reply(401, {'error': 'Sign in required.'})
                if not user['admin'] and not user['managed_projects']:
                    return self.reply(403, {'error': 'Project manager access required.'})
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                try:
                    page, page_size, project = job_query_params(query, user)
                except PermissionError as exc:
                    return self.reply(403, {'error': str(exc)})
                except ValueError as exc:
                    return self.reply(400, {'error': str(exc)})
                result = jobs.list_page(user['managed_projects'], user['admin'], page=page,
                                        page_size=page_size, project=project)
                return self.reply(200, result)
            if path.startswith('/api/jobs/'):
                user = auth.session(self.token())
                if not user:
                    return self.reply(401, {'error': 'Sign in required.'})
                if not user['admin'] and not user['managed_projects']:
                    return self.reply(403, {'error': 'Project manager access required.'})
                job_id = path.removeprefix('/api/jobs/')
                if not re.fullmatch(r'[0-9a-f]{32}', job_id):
                    return self.reply(400, {'error': 'Invalid job ID.'})
                job = jobs.get(job_id, user['managed_projects'], user['admin'])
                if job is None:
                    return self.reply(404, {'error': 'Job not found.'})
                return self.reply(200, {'job': job})
            if path == '/api/users':
                user = auth.session(self.token())
                if not user:
                    return self.reply(401, {'error': 'Sign in required.'})
                if not user['admin'] and not user['managed_projects']:
                    return self.reply(403, {'error': 'Project manager access required.'})
                records = auth.list_users()
                if not user['admin']:
                    allowed = set(user['managed_projects'])
                    records = [record for record in records if allowed.intersection(record['projects'])]
                    records = [{**record,
                                'projects': [p for p in record['projects'] if p in allowed],
                                **{key: {p: values for p, values in record[key].items() if p in allowed}
                                   for key in ('teams', 'connections', 'roles')}} for record in records]
                return self.reply(200, {'users': records})
            if path == '/api/templates':
                user = auth.session(self.token())
                if not user or not user['admin']:
                    return self.reply(403, {'error': 'Administrator access required.'})
                from crange_guac_ticket import request_broker, TicketError
                try:
                    return self.reply(200, request_broker({'operation':'template-options','project':1,'session':self.token(),'refresh':parse_qs(urlsplit(self.path).query).get('refresh')==['1']}))
                except TicketError as exc:
                    return self.reply(503, {'error':str(exc)})
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
                                        'url': os.environ.get('CRANGE_GUAC_PUBLIC_URL', 'https://crguac.brogano.dev'),
                                        'state': status.get('state', 'pending_configuration')})
            if path == '/api/guac-ticket':
                session_token = self.token()
                user = auth.session(session_token)
                if not user:
                    return self.reply(401, {'error': 'Sign in required.'})
                if user['admin']:
                    # Dashboard and native Guacamole administrators are separate
                    # identities. Preserve the browser's native login (guacadmin)
                    # instead of replacing it with a JSON-auth dashboard ticket.
                    origin = os.environ.get('CRANGE_GUAC_PUBLIC_URL', 'https://crguac.brogano.dev').rstrip('/')
                    if not origin.startswith(('https://','http://')):
                        return self.reply(503, {'error':'Guacamole public URL is not configured.'})
                    return self.reply(200, {'url': origin + '/', 'authentication':'native', 'credentials':[]})
                query = parse_qs(urlsplit(self.path).query)
                raw_project = query.get('project', [''])[0]
                if not raw_project.isdecimal() or not 1 <= int(raw_project) <= 255:
                    return self.reply(400, {'error': 'Invalid project.'})
                try:
                    from crange_guac_ticket import request_launch
                    launch = request_launch(session_token, int(raw_project))
                except (OSError, ValueError, RuntimeError) as exc:
                    print(f'crange: Guacamole launch failed: {exc}', flush=True)
                    return self.reply(503, {'error': str(exc) if 'credentials' not in str(exc).lower() else 'Guacamole launch is not configured.'})
                return self.reply(200, launch)
            if path == '/api/guac-user-stats':
                user = auth.session(self.token())
                if not user:return self.reply(401, {'error':'Sign in required.'})
                query=parse_qs(urlsplit(self.path).query)
                try:
                    project=int(query.get('project',[''])[0])
                    if not 1 <= project <= 255:raise ValueError('Invalid project.')
                    if not user['admin'] and str(project) not in user['managed_projects']:
                        return self.reply(403, {'error':'Project manager access required.'})
                    from crange_guac_ticket import request_broker
                    return self.reply(200,request_broker({'operation':'user-stats','project':project,'session':self.token(),'username':query.get('username',[''])[0]}))
                except (ValueError,RuntimeError) as exc:return self.reply(400,{'error':str(exc)})
            if path == '/api/guac-users':
                user = auth.session(self.token())
                if not user:
                    return self.reply(401, {'error': 'Sign in required.'})
                raw_project = parse_qs(urlsplit(self.path).query).get('project', [''])[0]
                if not raw_project.isdecimal() or not 1 <= int(raw_project) <= 255:
                    return self.reply(400, {'error': 'Invalid project.'})
                if not user['admin'] and raw_project.lstrip('0') not in user['managed_projects']:
                    return self.reply(403, {'error': 'Project manager access required.'})
                from crange_guac_users import ProjectUsers
                return self.reply(200, {'users': ProjectUsers(auth).list(int(raw_project))})
            if path == '/api/audit':
                user = auth.session(self.token())
                if not user:
                    return self.reply(401, {'error': 'Sign in required.'})
                if not user['admin']:
                    return self.reply(403, {'error': 'Administrator access required.'})
                return self.reply(200, {'events': auth.list_audit()})
            names = {'/': 'index.html', '/app.js': 'app.js', '/lifecycle.js': 'lifecycle.js', '/accounts.js': 'accounts.js', '/styles.css': 'styles.css', '/users.js': 'users.js', '/templates.js':'templates.js', '/favicon.svg':'favicon.svg', **{f'/fonts/IBMPlexMono-{weight}.woff2': f'fonts/IBMPlexMono-{weight}.woff2' for weight in ('Regular',)}, **{f'/fonts/IBMPlexSans-{weight}.woff2': f'fonts/IBMPlexSans-{weight}.woff2' for weight in ('Regular', 'Medium', 'SemiBold', 'Bold')}}
            if path not in names:
                return self.reply(404, {'error': 'Not found.'})
            file = ROOT / 'gui' / names[path]
            body = file.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', {'html': 'text/html', 'js': 'text/javascript', 'css': 'text/css', 'svg': 'image/svg+xml', 'woff2': 'font/woff2'}[file.suffix[1:]])
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Content-Security-Policy', "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if urlsplit(self.path).path == '/api/guac-bridge':
                # Guacamole calls this directly from its server. Browser
                # sessions, cookies and CSRF tokens have no authority here.
                from crange_guac_bridge import BridgeError, broker_rows, handle, verify
                from crange_guac_ticket import TicketError
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 65536 or self.headers.get('Origin'):
                        raise ValueError('Invalid Guacamole bridge request.')
                    raw = self.rfile.read(size)
                    username, request = verify(raw, self.headers, self.client_address[0])
                    status, result = handle(username, request, auth=auth, jobs=jobs, root=ROOT,
                                            rows_provider=broker_rows)
                    return self.reply(status, result)
                except (ValueError, BridgeError, TicketError) as exc:
                    # Keep the browser response generic, but leave one
                    # actionable line in the controller journal for operators.
                    print(f'crange: guac bridge rejected: {exc}', flush=True)
                    return self.reply(403, {'error': str(exc)})
            origin = self.headers.get('Origin')
            if origin and urlsplit(origin).netloc != self.headers.get('Host'):
                return self.reply(403, {'error': 'Origin rejected.'})
            if urlsplit(self.path).path.startswith('/api/template-media/'):
                user = auth.session(self.token())
                if not user or not user['admin']:
                    return self.reply(403, {'error':'Administrator access required.'})
                if not hmac.compare_digest(user['csrf'],self.headers.get('X-CSRF-Token','')):
                    return self.reply(403, {'error':'Invalid session request.'})
                from crange_template_media import chunk, CHUNK
                try:
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0 < size <= CHUNK:raise ValueError('ISO chunk is too large.')
                    return self.reply(200,chunk(user['username'],urlsplit(self.path).path.rsplit('/',1)[-1],int(self.headers.get('X-Upload-Offset','-1')),self.rfile.read(size)))
                except (ValueError,OSError) as exc:
                    return self.reply(400,{'error':str(exc)})
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
                if self.path in ('/api/custom-jumpboxes','/api/box-remote-access','/api/box-users'):
                    project = data.get('project')
                    if type(project) is not int or not 1 <= project <= 255:
                        raise ValueError('Invalid project.')
                    if not user['admin'] and str(project) not in user['managed_projects']:
                        return self.reply(403, {'error':'Project manager access required.'})
                    from crange_project_lifecycle import require_editable
                    require_editable(jobs, project)
                if self.path == '/api/template-media':
                    if not user['admin']:return self.reply(403,{'error':'Administrator access required.'})
                    from crange_template_media import begin,finish,cancel
                    if data.get('action')=='begin':return self.reply(201,begin(user['username'],data.get('name'),data.get('size')))
                    if data.get('action')=='finish':return self.reply(200,finish(user['username'],data.get('upload_id')))
                    if data.get('action')=='cancel':return self.reply(200,cancel(user['username'],data.get('upload_id')))
                    raise ValueError('Invalid ISO upload action.')
                if self.path == '/api/logout':
                    auth.logout(self.token())
                    return self.reply(200, {}, 'crange_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
                if self.path == '/api/custom-jumpboxes':
                    project=data.get('project')
                    if type(project) is not int or not 1<=project<=255:raise ValueError('Invalid project.')
                    if not user['admin'] and str(project) not in user['managed_projects']:return self.reply(403,{'error':'Project manager access required.'})
                    from crange_jumpboxes import creation
                    definition={**data.get('definition',{}),'credential_ref':'','sealed_credentials':''}
                    creation(definition)
                    if data.get('credentials'):
                        from crange_guest_credentials import validate
                        from crange_guac_ticket import request_broker
                        import uuid
                        ref=uuid.uuid4().hex
                        protected=request_broker({'operation':'seal-guest-credentials','session':self.token(),'project':project,'reference':ref,'credentials':validate(data['credentials'])})
                        definition.update(credential_ref=ref,sealed_credentials=protected['sealed_credentials'])
                    job=jobs.submit(user['username'],project,'custom-jumpbox-create',{'definition':definition})
                    auth.audit(user['username'],'custom-jumpbox-create',definition['name'],{'project':project,'job_id':job})
                    return self.reply(202,{'job_id':job})
                if self.path == '/api/box-remote-access':
                    project = data.get('project')
                    if isinstance(project, bool) or not isinstance(project, int) or not 1 <= project <= 255:
                        raise ValueError('Invalid project.')
                    if not user['admin'] and str(project) not in user['managed_projects']:
                        return self.reply(403, {'error': 'Project manager access required.'})
                    from crange import project_dir, json_config
                    from crange_jumpboxes import FIELDS
                    from crange_jobs import command
                    kind, name = data.get('kind'), data.get('name')
                    if kind not in FIELDS or name not in json_config(project_dir(str(project))).get(FIELDS[kind], {}):
                        raise ValueError('Choose a box declared in this project.')
                    options = {key: data.get(key) for key in ('kind','name','display_name','is_jumpbox','services')}
                    mode = data.get('credentials_mode', 'keep')
                    command('jumpbox-settings', project, {**options, 'credentials_mode': 'keep'})
                    if mode == 'custom':
                        from crange_guest_credentials import validate
                        from crange_guac_ticket import request_broker
                        import uuid
                        credentials = validate(data.get('credentials'))
                        ref = uuid.uuid4().hex
                        protected = request_broker({'operation':'seal-guest-credentials', 'session':self.token(),
                                                    'project':project, 'reference':ref, 'credentials':credentials})
                        options.update(credentials_mode='custom', credential_ref=ref,
                                       sealed_credentials=protected['sealed_credentials'])
                    elif mode in ('keep','default'):
                        options['credentials_mode'] = mode
                    else:
                        raise ValueError('Choose existing, default or custom credentials.')
                    job = jobs.submit(user['username'], project, 'jumpbox-settings', options)
                    auth.audit(user['username'], 'box-remote-access', name, {'project':project,'kind':kind,'credentials_mode':mode,'job':job})
                    return self.reply(202, {'job_id': job})
                if self.path == '/api/box-users':
                    project = data.get('project')
                    if isinstance(project, bool) or not isinstance(project, int) or not 1 <= project <= 255:
                        raise ValueError('Invalid project.')
                    if not user['admin'] and str(project) not in user['managed_projects']:
                        return self.reply(403, {'error': 'Project manager access required.'})
                    from crange_box_users import username, binding, box_key, FIELDS, ACTIONS
                    kind, name, action = data.get('kind'), data.get('name'), data.get('action')
                    if not isinstance(kind, str) or kind not in FIELDS or action not in ACTIONS:
                        raise ValueError('Invalid guest account operation.')
                    name = box_key(name)
                    from crange import project_dir, json_config
                    if name not in json_config(project_dir(str(project))).get(FIELDS[kind], {}):
                        raise ValueError('Choose a box declared in this project.')
                    login = '' if action in ('list', 'access') else username(data.get('username'))
                    sealed = ''
                    if action in ('create', 'password'):
                        from crange_guac_ticket import request_sealed_password
                        password = data.get('password')
                        if not isinstance(password, str) or not 6 <= len(password) <= 1024 or any(c in password for c in ('\n', '\r', '\x00')):
                            raise ValueError('Password must contain 6–1024 characters without line breaks.')
                        sealed = request_sealed_password(self.token(), project, binding(kind, name, login), password)
                    job = jobs.submit(user['username'], project, 'box-users', {'name': name, 'kind': kind, 'action': action, 'username': login, 'sealed_password': sealed})
                    auth.audit(user['username'], 'box-user-' + action, str(project), {'box': name, 'kind': kind, 'username': login, 'job_id': job})
                    return self.reply(202, {'job_id': job})
                if self.path in ('/api/guac-users','/api/project-settings'):
                    project=data.get('project')
                    if isinstance(project,bool) or not isinstance(project,int) or not 1 <= project <= 255:
                        raise ValueError('Invalid project.')
                    if not user['admin'] and str(project) not in user['managed_projects']:
                        return self.reply(403,{'error':'Project manager access required.'})
                    from crange import ENVS,json_config
                    from crange_user_management import manage_users,save_preferences
                    from crange_project_lifecycle import require_editable
                    require_editable(jobs, project)
                    cfg=json_config(ENVS/f'project-{project:03d}')
                    try:
                        if self.path=='/api/project-settings':
                            settings=save_preferences(auth,self.token(),project,data,cfg)
                            auth.audit(user['username'],'project-settings-update',str(project),{'display_name':settings['display_name']})
                            return self.reply(200,{'settings':settings})
                        result=manage_users(auth,jobs,user['username'],self.token(),project,data,cfg)
                        return self.reply(202,result)
                    except __import__('sqlite3').IntegrityError:
                        return self.reply(409,{'error':'A username already exists. No users from this batch were created.'})
                    except ValueError as exc:
                        return self.reply(409 if 'already exists' in str(exc) else 400,{'error':str(exc)})
                    except RuntimeError as exc:
                        return self.reply(400,{'error':str(exc)})
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
                        if (not isinstance(projects, list) or not projects or
                                any(isinstance(p, bool) or not isinstance(p, int) or
                                    p < 1 or p > 255 or p in (60, 61) for p in projects) or
                                len(set(projects)) != len(projects)):
                            raise ValueError('Project access must be a comma-separated list of valid project numbers.')
                        if data.get('role', 'participant') not in ('manager', 'participant'):
                            raise ValueError('Choose manager or participant access.')
                        auth.set_password(username, data['password'], admin=bool(data.get('admin', False)),
                                          projects=projects, role=data.get('role', 'participant'), create_only=True)
                        auth.audit(user['username'], 'user-create', username, {'projects': projects, 'role': data.get('role', 'participant')})
                        return self.reply(200, {'users': auth.list_users()})
                    if data.get('action') == 'delete':
                        if not user['admin'] or username == user['username']:
                            return self.reply(403, {'error': 'Administrator access required; you cannot delete your own account.'})
                        auth.delete_user(username)
                        auth.audit(user['username'], 'user-delete', username)
                        return self.reply(200, {'users': auth.list_users()})
                    if data.get('action') == 'set-role':
                        if not user['admin']:
                            return self.reply(403, {'error': 'Administrator access required.'})
                        if isinstance(project, bool) or not isinstance(project, int):
                            raise ValueError('Invalid project.')
                        auth.set_project_role(username, project, data.get('role'))
                        auth.audit(user['username'], 'project-role-update', username,
                                   {'project': project, 'role': data['role']})
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
                        if isinstance(project, bool) or not isinstance(project, int) or str(project) not in user['managed_projects']:
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
                    if (not isinstance(projects, list) or not isinstance(teams, dict) or
                            any(isinstance(p, bool) or not isinstance(p, int) or p < 1 or p > 255 or p in (60, 61)
                                for p in projects) or len(set(projects)) != len(projects)):
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
                    if data.get('action') == 'cancel':
                        job_id = data.get('job_id')
                        cancelled = jobs.cancel(job_id, user['managed_projects'], user['admin'])
                        if not cancelled:
                            return self.reply(409, {'error': 'This job is no longer running or could not be stopped.'})
                        return self.reply(200, {'cancelled': True})
                    if data.get('action') == 'reconcile-network':
                        if not user['admin']:
                            return self.reply(403, {'error': 'Administrator access required.'})
                        source_job_id = data.get('source_job_id')
                        if not isinstance(source_job_id, str) or not re.fullmatch(r'[a-f0-9]{32}', source_job_id):
                            raise ValueError('Invalid source network job.')
                        source = jobs.get(source_job_id, [], admin=True) if isinstance(source_job_id, str) else None
                        if not source or source.get('operation') != 'network-verify' or source.get('status') != 'reconcile-required':
                            return self.reply(409, {'error': 'Choose a network verification that needs reconciliation.'})
                        job = jobs.submit(user['username'], source['project'], 'network-reconcile',
                                          {'source_job_id': source_job_id})
                        auth.audit(user['username'], 'network-verification-reconcile', source_job_id,
                                   {'project': source['project'], 'job_id': job})
                        return self.reply(202, {'job_id': job})
                    if data.get('action')=='publish-network':
                        if not user['admin']:
                            return self.reply(403,{'error':'Administrator access required.'})
                        source_job_id=data.get('source_job_id')
                        if not isinstance(source_job_id,str) or not re.fullmatch(r'[a-f0-9]{32}',source_job_id):
                            raise ValueError('Invalid source network job.')
                        source=jobs.get(source_job_id,[],admin=True)
                        if not source or source.get('operation')!='network-verify' or source.get('status') not in ('succeeded','failed'):
                            return self.reply(409,{'error':'Choose a completed accepted network verification.'})
                        job=jobs.submit(user['username'],source['project'],'network-publish',{'source_job_id':source_job_id})
                        auth.audit(user['username'],'network-verification-publish',source_job_id,{'project':source['project'],'job_id':job})
                        return self.reply(202,{'job_id':job})
                    project = data.get('project')
                    if isinstance(project, bool) or not isinstance(project, int):
                        raise ValueError('Invalid project.')
                    if (data.get('operation') in ('project-init', 'plan-sdn-repair','network-publish','project-restore','project-purge') or str(data.get('operation','')).startswith('template-')) and not user['admin']:
                        return self.reply(403, {'error': 'Administrator access required.'})
                    if data.get('operation') != 'project-init' and not user['admin'] and str(project) not in user['managed_projects']:
                        return self.reply(403, {'error': 'Project manager access required.'})
                    if data.get('operation') == 'network-verify':
                        from crange import ENVS, json_config
                        from crange_network_worker import declared_vnets
                        cfg = json_config(ENVS / f'project-{project:03d}')
                        network = data.get('options', {}).get('network') if isinstance(data.get('options'), dict) else None
                        allowed = declared_vnets(project, cfg)
                        if network not in allowed:
                            raise ValueError('Choose a declared LAN in this project.')
                    job = jobs.submit(user['username'], project, data.get('operation'), data.get('options', {}))
                    if data.get('operation') in ('project-delete', 'project-restore', 'project-purge'):
                        auth.audit(user['username'], data['operation'] + '-submitted', str(project), {'job_id': job})
                    if data.get('operation') == 'network-verify':
                        options = data.get('options', {})
                        auth.audit(user['username'], 'network-verification-submit', str(project),
                                   {'node': options.get('node'), 'network': options.get('network'), 'job_id': job})
                    return self.reply(202, {'job_id': job})
                return self.reply(404, {'error': 'Not found.'})
            except (ValueError, TypeError) as exc:
                self.reply(400, {'error': str(exc) if self.path in ('/api/users','/api/jobs','/api/project-settings','/api/guac-users') else 'Invalid request.'})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f'CRANGE listening on http://{host}:{port}', flush=True)
    try:
        server.serve_forever()
    finally:
        if jobs_stop:
            jobs_stop.set()
        server.server_close()
