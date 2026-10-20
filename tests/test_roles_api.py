import http.cookiejar
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, HTTPCookieProcessor, Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from crange_auth import AuthStore
from crange_jobs import JobStore


class ProjectRoleAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.auth = AuthStore(root / 'auth.sqlite3')
        for name in ('participant', 'manager'):
            self.auth.set_password(name, 'a long role test password', projects=['3', '4'])
        self.auth.set_project_role('manager', 3, 'manager')
        self.auth.set_password('platform-admin', 'a long role test password', admin=True)
        for number in (3, 4):
            (root / f'environments/project-{number:03d}').mkdir(parents=True)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        env = {**os.environ, 'CRANGE_AUTH_DB': str(self.auth.path),
               'CRANGE_JOB_DB': str(root / 'jobs.sqlite3'), 'CRANGE_WORKER_EXTERNAL': '1'}
        code = (f'import sys;sys.path.insert(0,{str(ROOT / "scripts")!r});'
                f'from pathlib import Path;import crange;crange.ENVS=Path({str(root / "environments")!r});'
                f'from crange_server import serve;serve("127.0.0.1",{port})')
        self.process = subprocess.Popen([sys.executable, '-c', code], env=env,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop)
        self.url = f'http://127.0.0.1:{port}'
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                self.request('/api/session')
                break
            except URLError:
                time.sleep(.01)
        else:
            self.fail('Test API did not start')

    def stop(self):
        self.process.terminate()
        self.process.wait(timeout=5)

    def request(self, path, body=None):
        headers = {'Content-Type': 'application/json', 'X-CSRF-Token': getattr(self, 'csrf', '')}
        request = Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                          headers=headers)
        try:
            response = self.opener.open(request, timeout=3)
        except HTTPError as error:
            response = error
        return response.code, json.loads(response.read())

    def login(self, name):
        status, body = self.request('/api/login', {'username': name, 'password': 'a long role test password'})
        self.assertEqual(status, 200)
        self.csrf = body['csrf']

    def test_dashboard_login_canonicalizes_mixed_case_created_username(self):
        password = 'a distinct long test password'
        self.auth.set_password('CIG-Lead-Test', password, projects=['100'],
                               role='manager', create_only=True)
        for typed_name in ('CIG-Lead-Test', 'cig-lead-test'):
            status, body = self.request('/api/login', {'username': typed_name, 'password': password})
            self.assertEqual(status, 200)
            self.csrf = body['csrf']
            status, session = self.request('/api/session')
            self.assertEqual(status, 200)
            self.assertEqual(session['username'], 'cig-lead-test')

    def test_admin_cluster_reads_cached_snapshot_and_queues_fixed_refresh(self):
        self.login('platform-admin')
        status, snapshot = self.request('/api/cluster')
        self.assertEqual(status, 200)
        self.assertIn('nodes', snapshot)
        self.assertIn('freshness', snapshot)
        status, _ = self.request('/api/jobs', {'project': 0, 'operation': 'template-cluster-refresh', 'options': {}})
        self.assertEqual(status, 202)

    def test_participant_cannot_manage_or_read_management_data(self):
        self.login('participant')
        status, data = self.request('/api/projects')
        self.assertEqual(status, 200)
        self.assertEqual(data['projects'], [{'project_id': 3, 'participant': True, 'display_name': 'Project 3'},
                                           {'project_id': 4, 'participant': True, 'display_name': 'Project 4'}])
        for path in ('/api/jobs', '/api/users', '/api/audit', '/api/guac-users?project=3','/api/templates','/api/cluster'):
            self.assertEqual(self.request(path)[0], 403)
        for operation in ('plan', 'configure-team', 'guac-apply', 'project-init','template-create','template-publish','template-upload','template-memory-reserve-set','network-verify'):
            self.assertEqual(self.request('/api/jobs', {'project': 3, 'operation': operation})[0], 403)
        for action in ('set-role', 'set-connections', 'set-enabled'):
            self.assertEqual(self.request('/api/users', {'action': action, 'username': 'participant',
                                                        'project': 3, 'role': 'manager'})[0], 403)
        self.assertEqual(self.request('/api/guac-users', {'project': 3, 'action': 'create',
                                                       'username': 'alice', 'password': 'a private password'})[0], 403)
        self.assertEqual(self.request('/api/guac-users', {'project': 4, 'action': 'delete',
                                                       'username': 'alice'})[0], 403)

    def test_manager_permission_is_checked_per_project_and_on_demotion(self):
        self.login('manager')
        self.assertEqual(self.request('/api/templates')[0],403)
        self.assertEqual(self.request('/api/cluster')[0],403)
        self.assertEqual(self.request('/api/jobs',{'project':0,'operation':'template-cluster-refresh','options':{}})[0],403)
        for op in ('template-create','template-publish','template-upload'):
            self.assertEqual(self.request('/api/jobs',{'project':0,'operation':op,'options':{}})[0],403)
        request = {'project': 3, 'operation': 'plan', 'options': {}}
        self.assertEqual(self.request('/api/jobs', request)[0], 202)
        self.assertEqual(self.request('/api/jobs', {**request, 'project': 4})[0], 403)
        self.auth.set_project_role('manager', 3, 'participant')
        self.assertEqual(self.request('/api/jobs', request)[0], 403)

    def test_worker_rejects_queued_job_after_demotion(self):
        store = JobStore(Path(self.tmp.name) / 'queued.sqlite3', auth_path=self.auth.path)
        store.submit('manager', 3, 'plan', {})
        self.auth.set_project_role('manager', 3, 'participant')
        with patch.dict(os.environ, {'CRANGE_AUTH_DB': str(self.auth.path)}), \
                patch('crange_jobs.subprocess.run') as execute:
            self.assertTrue(store.run_next())
            execute.assert_not_called()
        job = store.list([], admin=True)[0]
        self.assertEqual(job['status'], 'failed')
        self.assertIn('access was revoked', job['output'])

    def test_manager_can_queue_only_a_declared_project_network_verification(self):
        config = {'project_id': 3, 'team_ids': ['1'], 'jump_boxes': {},
                  'scenario_boxes': {}, 'scenario_containers': {},
                  'custom_networks': {'test': {'owner_team_id':'254','network_id':100,'uplink_last_octet':2}}}
        (Path(self.tmp.name) / 'environments/project-003/crange.auto.tfvars.json').write_text(json.dumps(config))
        self.login('manager')
        request = {'project': 3, 'operation': 'network-verify',
                   'options': {'node': 'proxmox2', 'network': 'p003t001'}}
        status, body = self.request('/api/jobs', request)
        self.assertEqual(status, 202)
        self.assertEqual(self.request('/api/jobs', {**request, 'options': {**request['options'], 'network': 'p003t002'}})[0], 400)
        self.assertEqual(self.request('/api/jobs', {**request, 'options': {**request['options'], 'network': 'p003c100'}})[0], 202)
        self.assertEqual(self.request('/api/jobs', {**request, 'options': {**request['options'], 'network': 'p003c101'}})[0], 400)
        self.assertEqual(self.request('/api/jobs', {**request, 'project': 4})[0], 403)
        self.assertTrue(body['job_id'])

    def test_admin_publication_derives_project_from_only_completed_source_job(self):
        store=JobStore(Path(self.tmp.name)/'jobs.sqlite3')
        source=store.submit('manager',3,'network-verify',{'node':'proxmox2','network':'p003t001'})
        with store.connect() as db:db.execute("UPDATE jobs SET status='succeeded' WHERE id=?",(source,))
        request={'action':'publish-network','source_job_id':source,'project':240}
        self.login('manager');self.assertEqual(self.request('/api/jobs',request)[0],403)
        self.login('platform-admin');status,response=self.request('/api/jobs',request)
        self.assertEqual(status,202)
        queued=store.get(response['job_id'],[],admin=True)
        self.assertEqual(queued['operation'],'network-publish')
        self.assertEqual(queued['project'],3)
        with store.connect() as db:
            options=json.loads(db.execute('SELECT options FROM jobs WHERE id=?',(response['job_id'],)).fetchone()[0])
        self.assertEqual(options,{'source_job_id':source})
        self.assertEqual(self.request('/api/jobs',{'action':'publish-network','source_job_id':'../bundle'})[0],400)

    def test_only_admin_can_queue_fixed_network_reconciliation(self):
        store=JobStore(Path(self.tmp.name)/'jobs.sqlite3')
        source=store.submit('manager',3,'network-verify',
                            {'node':'proxmox2','network':'p003t001'})
        with store.connect() as db:
            db.execute("UPDATE jobs SET status='reconcile-required' WHERE id=?",(source,))
        self.login('manager')
        request={'action':'reconcile-network','source_job_id':source}
        self.assertEqual(self.request('/api/jobs',request)[0],403)
        self.login('platform-admin')
        status,response=self.request('/api/jobs',request)
        self.assertEqual(status,202)
        queued=store.get(response['job_id'],[],admin=True)
        self.assertEqual(queued['operation'],'network-reconcile')

    def test_admin_can_promote_but_manager_cannot_promote_itself(self):
        self.login('platform-admin')
        self.assertEqual(self.request('/api/users', {'action': 'set-role', 'username': 'participant',
                                                   'project': 3, 'role': 'manager'})[0], 200)
        self.login('manager')
        self.assertEqual(self.request('/api/users', {'action': 'set-role', 'username': 'manager',
                                                   'project': 4, 'role': 'manager'})[0], 403)

    def test_admin_user_project_access_rejects_invalid_ids_and_can_delete_test_user(self):
        self.login('platform-admin')
        for project_value in ([None], [float('nan')], ['3'], [True], [0], [60], [3, 3]):
            status, body = self.request('/api/users', {'action': 'create', 'username': 'badaccess',
                'password': 'a long role test password', 'projects': project_value, 'role': 'participant'})
            self.assertEqual(status, 400, body)
            self.assertNotIn('badaccess', [item['username'] for item in self.auth.list_users()])
        status, body = self.request('/api/users', {'action': 'create', 'username': 'CIG-Short',
            'password': 'short123', 'projects': [100], 'role': 'manager', 'admin': False})
        self.assertEqual(status, 400)
        self.assertIn('12–1024 characters', body['error'])
        status, _ = self.request('/api/users', {'action': 'create', 'username': 'CIG-Lead',
            'password': 'a long role test password', 'projects': [100], 'role': 'manager', 'admin': False})
        self.assertEqual(status, 200)
        record = next(item for item in self.auth.list_users() if item['username'] == 'cig-lead')
        self.assertEqual(record['projects'], ['100'])
        self.assertEqual(record['roles']['100'], 'manager')
        status, _ = self.request('/api/users', {'action': 'delete', 'username': 'cig-lead'})
        self.assertEqual(status, 200)
        self.assertNotIn('cig-lead', [item['username'] for item in self.auth.list_users()])

    def test_manager_directory_omits_other_project_assignments(self):
        self.login('manager')
        status, data = self.request('/api/users')
        self.assertEqual(status, 200)
        for user in data['users']:
            self.assertNotIn('4', user['projects'])
            self.assertNotIn('4', user['roles'])

    def test_local_font_and_static_allowlist(self):
        for font in ('IBMPlexSans-Regular', 'IBMPlexSans-Medium', 'IBMPlexSans-SemiBold', 'IBMPlexSans-Bold', 'IBMPlexMono-Regular'):
            with self.opener.open(self.url + '/fonts/' + font + '.woff2', timeout=3) as response:
                self.assertEqual(response.headers['Content-Type'], 'font/woff2')
                self.assertEqual(response.read(4), b'wOF2')
        for path in ('/fonts/unknown.woff2', '/fonts/../scripts/crange_server.py', '/fonts/OFL-IBM-Plex.txt'):
            with self.assertRaises(HTTPError) as result:
                self.opener.open(self.url + path, timeout=3)
            self.assertEqual(result.exception.code, 404)
            result.exception.close()

    def test_lifecycle_manager_delete_and_admin_restore_authorization(self):
        self.login('manager')
        for op in ('project-restore','project-purge'):
            self.assertEqual(self.request('/api/jobs',{'project':3,'operation':op,'options':{'confirmation':'p003'}})[0],403)
        self.assertEqual(self.request('/api/jobs',{'project':4,'operation':'project-delete','options':{'confirmation':'p004'}})[0],403)
        self.assertEqual(self.request('/api/jobs',{'project':3,'operation':'project-delete','options':{'confirmation':'p003'}})[0],202)
        code,body=self.request('/api/jobs',{'project':3,'operation':'plan','options':{}})
        self.assertEqual(code,400);self.assertIn('queued',body['error'])
        self.assertEqual(self.request('/api/project-settings',{'project':3,'display_name':'Race'})[0],400)
        self.assertEqual(self.request('/api/projects')[1]['projects'],[{'project_id':4,'participant':True,'display_name':'Project 4'}])
        self.login('platform-admin')
        records=self.request('/api/projects')[1]['projects']
        deleted=next(r for r in records if r['project_id']==3)
        self.assertTrue(deleted['deleted']);self.assertEqual(deleted['lifecycle']['pending_operation'],'project-delete')

    def test_deleted_project_hidden_from_members_and_restorable_by_admin(self):
        directory=Path(self.tmp.name)/'environments/project-003'
        (directory/'.crange-lifecycle.json').write_text(json.dumps({'state':'deleted','deleted_at':123}))
        self.login('participant')
        self.assertEqual([r['project_id'] for r in self.request('/api/projects')[1]['projects']],[4])
        self.login('manager')
        self.assertEqual(self.request('/api/jobs',{'project':3,'operation':'plan','options':{}})[0],400)
        self.login('platform-admin')
        self.assertEqual(self.request('/api/jobs',{'project':3,'operation':'project-restore','options':{'confirmation':'p003'}})[0],202)

    def test_admin_guacamole_launch_preserves_native_identity_without_a_ticket(self):
        self.login('platform-admin')
        code, result=self.request('/api/guac-ticket?project=0')
        self.assertEqual(code,200)
        self.assertEqual(result['authentication'],'native')
        self.assertEqual(result['url'],'https://range.brogano.dev/')
        self.assertEqual(result['credentials'],[])
        self.assertNotIn('data=', result['url'])
        self.assertNotIn('token=', result['url'])
