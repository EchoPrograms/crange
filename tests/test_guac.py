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
