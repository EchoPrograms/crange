import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_auth import AuthStore


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auth = AuthStore(Path(self.tmp.name) / 'auth.sqlite3')
        self.auth.set_password('manager', 'a long test password', projects=['003'])

    def test_login_logout_and_no_plaintext_storage(self):
        self.assertIsNone(self.auth.login('manager', 'incorrect'))
        token, csrf = self.auth.login('manager', 'a long test password')
        self.assertEqual(self.auth.session(token)['projects'], ['3'])
        self.assertEqual(self.auth.session(token)['csrf'], csrf)
        self.assertNotIn(b'a long test password', self.auth.path.read_bytes())
        self.assertNotIn(token.encode(), self.auth.path.read_bytes())
        self.auth.logout(token)
        self.assertIsNone(self.auth.session(token))

    def test_password_reset_revokes_sessions(self):
        token, _ = self.auth.login('manager', 'a long test password')
        self.auth.set_password('manager', 'another long password', projects=['3'])
        self.assertIsNone(self.auth.session(token))

    def test_expiry_and_disabled_user(self):
        token, _ = self.auth.login('manager', 'a long test password')
        with self.auth.connect() as db:
            db.execute('UPDATE sessions SET expires=0')
        self.assertIsNone(self.auth.session(token))
        with self.auth.connect() as db:
            db.execute('UPDATE users SET enabled=0')
        self.assertIsNone(self.auth.login('manager', 'a long test password'))

    def test_throttles_password_guessing(self):
        for _ in range(5):
            self.assertIsNone(self.auth.login('manager', 'incorrect'))
        self.assertIsNone(self.auth.login('manager', 'a long test password'))

    def test_project_and_team_assignments_are_structured(self):
        self.auth.set_password('second', 'another long password', projects=['4'])
        self.auth.set_access('manager', ['3', '4'], {'3': [1, 2], '4': [0]})
        users = {u['username']: u for u in self.auth.list_users()}
        self.assertEqual(users['manager']['projects'], ['3', '4'])
        self.assertEqual(users['manager']['teams'], {'3': ['1', '2'], '4': ['0']})
        with self.assertRaises(ValueError):
            self.auth.set_access('manager', ['3'], {'4': [1]})
        self.auth.set_team_access('manager', 3, [2])
        self.assertEqual(self.auth.list_users()[0]['teams'], {'3': ['2'], '4': ['0']})

    def test_disabling_user_revokes_sessions(self):
        token, _ = self.auth.login('manager', 'a long test password')
        self.auth.set_enabled('manager', False)
        self.assertIsNone(self.auth.session(token))
        self.auth.set_enabled('manager', True)

    def test_audit_events_are_structured(self):
        self.auth.audit('admin', 'user-create', 'manager', {'projects': ['3']})
        event = self.auth.list_audit()[0]
        self.assertEqual((event['actor'], event['action'], event['target']), ('admin', 'user-create', 'manager'))
        self.assertEqual(event['detail']['projects'], ['3'])

    def test_connection_assignments_are_project_scoped(self):
        self.auth.set_connection_access('manager', 3, ['jumpbox:team1-j01', 'vm:t254-web'])
        record = self.auth.list_users(project='3')[0]
        self.assertEqual(record['connections'], {'3': ['jumpbox:team1-j01', 'vm:t254-web']})
        with self.assertRaises(ValueError):
            self.auth.set_connection_access('manager', 4, ['jumpbox:other'])
        self.auth.set_access('manager', ['4'], {})
        self.assertEqual(self.auth.list_users()[0]['connections'], {})
