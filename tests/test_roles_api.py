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
        for operation in ('plan', 'configure-team', 'guac-apply', 'project-init','template-create','template-publish','template-upload'):
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

    def test_admin_can_promote_but_manager_cannot_promote_itself(self):
        self.login('platform-admin')
        self.assertEqual(self.request('/api/users', {'action': 'set-role', 'username': 'participant',
                                                   'project': 3, 'role': 'manager'})[0], 200)
        self.login('manager')
        self.assertEqual(self.request('/api/users', {'action': 'set-role', 'username': 'manager',
                                                   'project': 4, 'role': 'manager'})[0], 403)

    def test_manager_directory_omits_other_project_assignments(self):
        self.login('manager')
        status, data = self.request('/api/users')
        self.assertEqual(status, 200)
        for user in data['users']:
            self.assertNotIn('4', user['projects'])
            self.assertNotIn('4', user['roles'])
