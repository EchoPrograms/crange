import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_guac import GuacamoleClient, GuacamoleError


class Response:
    def __init__(self, value): self.value = value
    def read(self): return json.dumps(self.value).encode()


class GuacamoleConnectorTests(unittest.TestCase):
    def test_applies_declared_connection_and_permission(self):
        calls = []
        def opener(request, timeout=0):
            path = urlsplit(request.full_url).path; calls.append((request.method, path))
            if path == '/api/tokens': return Response({'authToken': 't', 'dataSource': 'postgresql'})
            if path.endswith('/users'): return Response({'manager': {'username': 'manager'}})
            if path.endswith('/connections') and request.method == 'GET': return Response({})
            if path.endswith('/users/manager/permissions'): return Response({})
            if request.method == 'POST': return Response({'identifier': '42'})
            return Response({})
        client = GuacamoleClient('http://guac', 'admin', 'secret', opener)
        applied = client.apply({'project_id': 3, 'users': [{'username': 'manager', 'teams': {'3': ['1']}}],
                                'connections': [{'name': 'team1-j01', 'team_id': '1', 'address': '10.3.1.11', 'default_for_team': True}]})
        self.assertEqual(applied, ['team1-j01'])
        self.assertIn(('PATCH', '/api/session/data/postgresql/users/manager/permissions'), calls)

    def test_missing_guacamole_user_is_explicit(self):
        def opener(request, timeout=0):
            path = urlsplit(request.full_url).path
            if path == '/api/tokens': return Response({'authToken': 't', 'dataSource': 'postgresql'})
            if path.endswith('/users'): return Response({})
            return Response({})
        client = GuacamoleClient('http://guac', 'admin', 'secret', opener)
        with self.assertRaisesRegex(GuacamoleError, 'provisioned'):
            client.apply({'project_id': 3, 'users': [{'username': 'manager', 'teams': {}}], 'connections': []})

    def test_crange_json_auth_needs_no_database_users_and_retires_old_grants(self):
        calls = []
        def opener(request, timeout=0):
            path = urlsplit(request.full_url).path
            calls.append((request.method, path, request.data))
            if path == '/api/tokens': return Response({'authToken': 't', 'dataSource': 'postgresql'})
            if path.endswith('/connections') and request.method == 'GET':
                return Response({'42': {'identifier': '42', 'name': 'P003 · jump'}})
            return Response({})
        client = GuacamoleClient('http://guac', 'admin', 'secret', opener)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'project-003.applied.json'
            state.write_text(json.dumps({'connections': [{'name': 'P003 · jump', 'identifier': '42'}],
                                         'permissions': [{'username': 'manager', 'identifier': '42'}]}))
            manifest = {'schema_version': 3, 'auth_mode': 'crange-json', 'project_id': 3,
                        'users': [{'username': 'manager', 'connection_keys': ['jumpbox:j1']}],
                        'connections': [{'key': 'jumpbox:j1', 'name': 'P003 · jump', 'address': '10.3.1.11',
                                         'team_id': '1', 'protocol': 'rdp', 'default_for_team': True}]}
            client.apply(manifest, state)
            saved = json.loads(state.read_text())
        self.assertFalse(any(path.endswith('/users') for _, path, _ in calls))
        patches = [body for method, path, body in calls if method == 'PATCH' and path.endswith('/permissions')]
        self.assertEqual(len(patches), 1)
        self.assertIn(b'remove', patches[0])
        self.assertEqual(saved['connections'][0]['resource_key'], 'jumpbox:j1')
        self.assertEqual(saved['connections'][0]['address'], '10.3.1.11')
        self.assertEqual(saved['permissions'][0]['resource_key'], 'jumpbox:j1')

    def test_removes_only_owned_connections_and_grants(self):
        calls = []
        def opener(request, timeout=0):
            path = urlsplit(request.full_url).path
            calls.append((request.method, path))
            if path == '/api/tokens': return Response({'authToken': 't', 'dataSource': 'postgresql'})
            if path.endswith('/users'): return Response({'manager': {'username': 'manager'}})
            if path.endswith('/connections'): return Response({'42': {'identifier': '42', 'name': 'P003 · old'}, '99': {'identifier': '99', 'name': 'unrelated'}})
            return Response({})
        client = GuacamoleClient('http://guac', 'admin', 'secret', opener)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'project-003.applied.json'
            state.write_text(json.dumps({'connections': [{'name': 'P003 · old', 'identifier': '42'}],
                                           'permissions': [{'username': 'manager', 'identifier': '42'}]}))
            client.apply({'project_id': 3, 'users': [{'username': 'manager', 'connection_keys': []}], 'connections': []}, state)
        self.assertIn(('DELETE', '/api/session/data/postgresql/connections/42'), calls)
        self.assertNotIn(('DELETE', '/api/session/data/postgresql/connections/99'), calls)
        self.assertIn(('PATCH', '/api/session/data/postgresql/users/manager/permissions'), calls)

class PendingWindowsAccessTests(unittest.TestCase):
    def test_uninitialized_windows_does_not_block_ready_connections_or_get_a_default_password(self):
        calls = []
        def opener(request, timeout=0):
            path = urlsplit(request.full_url).path
            calls.append((request.method, path))
            if path == '/api/tokens':return Response({'authToken':'fixture','dataSource':'postgresql'})
            if path.endswith('/connections') and request.method == 'GET':
                return Response({'42':{'name':'pending','identifier':'42'}, '43':{'name':'ready','identifier':'43'}})
            return Response({})
        client = GuacamoleClient('http://guac','fixture','fixture',opener)
        with tempfile.TemporaryDirectory() as directory:
            state=Path(directory)/'project-100.applied.json'
            state.write_text(json.dumps({'connections':[{'name':'pending','identifier':'42'},{'name':'ready','identifier':'43'}]}))
            manifest={'project_id':100,'auth_mode':'crange-json','users':[{'username':'alice','connection_keys':['vm:new']}],
                      'connections':[{'key':'vm:new','name':'pending','guest_os':'windows','protocol':'rdp','address':'10.100.2.12'},
                                     {'key':'container:ready','name':'ready','protocol':'ssh','address':'10.100.2.40'}]}
            result=client.apply(manifest,state)
            saved=json.loads(state.read_text())
        self.assertEqual(result,['ready'])
        self.assertIn(('PUT','/api/session/data/postgresql/connections/43'),calls)
        self.assertIn(('DELETE','/api/session/data/postgresql/connections/42'),calls)
        self.assertNotIn(('PUT','/api/session/data/postgresql/connections/42'),calls)
        self.assertEqual(saved['pending_connections'][0]['resource_key'],'vm:new')
        self.assertNotIn('identifier',saved['pending_connections'][0])
        self.assertEqual(saved['pending_permissions'],[{'username':'alice','resource_key':'vm:new'}])
        self.assertEqual([r['name'] for r in saved['connections']],['ready'])

class GuacamoleApplyLockTests(unittest.TestCase):
    def test_same_project_updates_serialize_before_reading_native_ownership(self):
        import threading
        from unittest.mock import patch
        first = threading.Event()
        release = threading.Event()
        second = threading.Event()
        errors = []
        def run(client, name):
            try:client.apply({'name':name},state)
            except Exception as exc:errors.append(exc)
        def apply(client, manifest, state_path):
            if manifest['name'] == 'first':
                first.set()
                if not release.wait(2):raise AssertionError('First transaction was not released.')
            else:second.set()
        with tempfile.TemporaryDirectory() as directory:
            state=Path(directory)/'project-003.applied.json'
            with patch.object(GuacamoleClient,'_apply',apply):
                one=threading.Thread(target=run,args=(GuacamoleClient(),'first'))
                two=threading.Thread(target=run,args=(GuacamoleClient(),'second'))
                one.start();self.assertTrue(first.wait(1));two.start()
                try:self.assertFalse(second.wait(.05))
                finally:release.set();one.join(2);two.join(2)
        self.assertFalse(errors)
        self.assertTrue(second.is_set())

class GuacamoleAuthorityTests(unittest.TestCase):
    def test_database_identity_cannot_silently_change(self):
        from types import SimpleNamespace
        from crange_guac import auth_database_identity
        recorded={'auth_database':'/fixture/authoritative.sqlite3'}
        self.assertEqual(auth_database_identity(SimpleNamespace(path=Path('/fixture/authoritative.sqlite3')),(recorded,)),recorded['auth_database'])
        with self.assertRaisesRegex(ValueError,'recorded authority'):
            auth_database_identity(SimpleNamespace(path=Path('/fixture/default.sqlite3')),(recorded,))

    def test_native_user_reconciliation_rejects_wrong_database_before_any_api_call(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from crange_guac_users import apply_users
        client=Mock()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);access=root/'guacamole/access';access.mkdir(parents=True)
            (access/'project-003.users-applied.json').write_text(json.dumps({'auth_database':'/fixture/authoritative.sqlite3','users':{'participant':{}}}))
            with self.assertRaisesRegex(ValueError,'recorded authority'):
                apply_users(3,SimpleNamespace(path=Path('/fixture/default.sqlite3')),client,root,'unused-fixture')
        client.login.assert_not_called()
        client._request.assert_not_called()
