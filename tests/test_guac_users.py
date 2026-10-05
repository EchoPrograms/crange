import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_auth import AuthStore
from crange_guac_users import ProjectUsers, apply_users, seal_password, open_password


class FakeGuacamole:
    data_source = 'postgresql'

    def __init__(self):
        self.users = {'unrelated': {'username': 'unrelated', 'attributes': {}}}
        self.permissions = {'unrelated': {'connectionPermissions': {'99': ['READ']}}}
        self.calls = []

    def login(self):
        pass

    def _request(self, method, path, data=None):
        self.calls.append((method, path, copy.deepcopy(data)))
        tail = path.split('/users', 1)[1].strip('/')
        if not tail:
            if method == 'GET':
                return copy.deepcopy(self.users)
            name = data['username']
            self.users[name] = copy.deepcopy(data)
            self.permissions[name] = {'connectionPermissions': {}}
            return {}
        name = tail.split('/')[0]
        if tail.endswith('/permissions'):
            if method == 'GET':
                return copy.deepcopy(self.permissions[name])
            for item in data:
                identifier = item['path'].split('/')[-1]
                field=item['path'].split('/')[1]
                values = self.permissions[name].setdefault(field,{})
                if item['op'] == 'remove':
                    values[identifier].remove(item['value'])
                    if not values[identifier]:
                        del values[identifier]
                else:
                    values.setdefault(identifier, []).append(item['value'])
            return {}
        if method == 'DELETE':
            self.users.pop(name, None)
            return {}
        self.users[name].update(copy.deepcopy(data))
        return {}


class ProjectGuacamoleUsersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.auth = AuthStore(self.root / 'auth.sqlite3')
        self.users = ProjectUsers(self.auth)
        self.secret = 'a1' * 16
        self.password = 'a private lab password'
        self.sealed = seal_password(self.password, self.secret, 3, 'alice')
        self.client = FakeGuacamole()
        self.sidecar = self.root / 'guacamole/access/project-003.applied.json'
        self.sidecar.parent.mkdir(parents=True)
        self.sidecar.write_text(json.dumps({'connections': [
            {'identifier': '42', 'resource_key': 'jumpbox:one', 'team_id': '1', 'default_for_team': True},
            {'identifier': '43', 'resource_key': 'jumpbox:two', 'team_id': '2', 'default_for_team': True},
            {'identifier': '44', 'resource_key': 'vm:web', 'team_id': '1', 'default_for_team': False},
        ]}))

    def apply(self):
        apply_users(3, self.auth, self.client, self.root, self.secret)

    def test_native_accounts_are_separate_and_passwords_are_not_public(self):
        self.users.create(3, 'alice', self.sealed, [1], ['vm:web'])
        self.assertEqual(self.auth.list_users(), [])
        self.assertIsNone(self.auth.login('alice', self.password))
        public = self.users.list(3)[0]
        self.assertEqual(public['login'], 'p003-alice')
        self.assertNotIn('sealed_password', public)
        self.assertNotIn(self.password.encode(), self.auth.path.read_bytes())
        self.apply()
        self.assertEqual(self.client.users['p003-alice']['password'], self.password)
        state = (self.root / 'guacamole/access/project-003.users-applied.json').read_text()
        self.assertNotIn(self.password, state)
        self.assertNotIn(self.sealed, state)
        self.assertEqual(self.client.permissions['p003-alice']['connectionPermissions'],
                         {'42': ['READ'], '44': ['READ']})

    def test_password_cipher_is_bound_to_project_and_username(self):
        self.assertEqual(open_password(self.sealed, self.secret, 3, 'alice'), self.password)
        for project, name in ((4, 'alice'), (3, 'bob')):
            with self.assertRaises(ValueError):
                open_password(self.sealed, self.secret, project, name)
        self.assertNotEqual(self.sealed, seal_password(self.password, self.secret, 3, 'alice'))

    def test_update_revokes_grants_and_does_not_reset_password_unnecessarily(self):
        self.users.create(3, 'alice', self.sealed, [1], ['vm:web'])
        self.apply()
        self.client.calls.clear()
        self.users.update(3, 'alice', teams=[2], connections=[], enabled=True)
        self.apply()
        self.assertEqual(self.client.permissions['p003-alice']['connectionPermissions'], {'43': ['READ']})
        payload = next(data for method, path, data in self.client.calls if method == 'PUT')
        self.assertNotIn('password', payload)
        self.users.update(3, 'alice', teams=[2], connections=[], enabled=False)
        self.apply()
        self.assertEqual(self.client.users['p003-alice']['disabled'], True)
        self.assertEqual(self.client.permissions['p003-alice']['connectionPermissions'], {})

    def test_remove_only_owned_account_and_refuse_unmanaged_collision(self):
        self.users.create(3, 'alice', self.sealed, [1], [])
        self.client.users['p003-alice'] = {'username': 'p003-alice'}
        with self.assertRaisesRegex(ValueError, 'outside project ownership'):
            self.apply()
        self.assertFalse(any(method != 'GET' for method, _, _ in self.client.calls))
        del self.client.users['p003-alice']
        self.apply()
        self.users.delete(3, 'alice')
        self.apply()
        self.assertNotIn('p003-alice', self.client.users)
        self.assertIn('unrelated', self.client.users)
        self.assertEqual(self.client.permissions['unrelated']['connectionPermissions'], {'99': ['READ']})

    def test_reserved_dashboard_namespace_and_duplicate_native_user(self):
        with self.assertRaisesRegex(ValueError, 'reserved'):
            self.auth.set_password('p003-alice', self.password, projects=['3'])
        self.users.create(3, 'alice', self.sealed, [], [])
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.users.create(3, 'alice', self.sealed, [], [])
