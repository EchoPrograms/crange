import base64
import hashlib
import hmac
import json
import multiprocessing
import os
import pwd
import socketserver
import sys
import tempfile
import unittest
import time
import threading
from unittest.mock import patch
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_auth import AuthStore
from crange_guac_ticket import TicketError, issue_ticket, request_ticket, request_broker
import crange_guac_broker


@unittest.skipUnless(sys.platform == 'linux', 'Requires Linux peer credentials')
class GuacamoleBrokerTests(unittest.TestCase):
    def test_broker_response_accepts_large_template_catalog_and_stays_bounded(self):
        class ResponseHandler(socketserver.StreamRequestHandler):
            def handle(self):
                self.rfile.readline(65537)
                self.wfile.write(json.dumps({'catalog': 'x' * 72_000}).encode() + b'\n')

        with tempfile.TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / 'response.sock')
            server = socketserver.UnixStreamServer(socket_path, ResponseHandler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                self.assertEqual(len(request_broker({'operation': 'template-options'}, socket_path=socket_path)['catalog']), 72_000)
            finally:
                server.shutdown(); server.server_close(); worker.join(5)

        class OversizeHandler(socketserver.StreamRequestHandler):
            def handle(self):
                self.rfile.readline(65537)
                self.wfile.write(json.dumps({'catalog': 'x' * (1024 * 1024 + 1)}).encode() + b'\n')

        with tempfile.TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / 'oversize.sock')
            server = socketserver.UnixStreamServer(socket_path, OversizeHandler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with self.assertRaisesRegex(TicketError, 'unavailable'):
                    request_broker({'operation': 'template-options'}, socket_path=socket_path)
            finally:
                server.shutdown(); server.server_close(); worker.join(5)

    def check_broker(self, expected_user, permitted):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            key = path / 'key'
            key.write_text('a1' * 16)
            socket_path = path / 'ticket.sock'
            auth = AuthStore(path / 'auth.sqlite3')
            auth.set_password('manager', 'a long manager password', projects=['3'], role='manager')
            manager_token, _ = auth.login('manager', 'a long manager password')
            auth.set_password('participant', 'a long participant password', projects=['3'])
            participant_token, _ = auth.login('participant', 'a long participant password')
            env = {'CRANGE_GUAC_TICKET_SOCKET': str(socket_path),
                   'CRANGE_GUAC_JSON_KEY': str(key),
                   'CRANGE_AUTH_DB': str(path / 'auth.sqlite3'),
                   'CRANGE_WEB_USER': expected_user}
            with patch.dict(os.environ, env), patch.object(
                    crange_guac_broker, 'issue_ticket', return_value='https://guac.example/'):
                worker = multiprocessing.get_context('fork').Process(target=crange_guac_broker.serve)
                worker.start()
                try:
                    deadline = time.monotonic() + 5
                    while not socket_path.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(socket_path.exists(), 'Broker failed to start')
                    if permitted:
                        self.assertEqual(request_ticket('x' * 40, 3, socket_path=str(socket_path)),
                                         'https://guac.example/')
                        from crange_guac_users import open_password
                        request = {'operation': 'seal-password', 'session': manager_token, 'project': 3,
                                   'username': 'alice', 'password': 'a private native password'}
                        reply = request_broker(request, socket_path=str(socket_path))
                        self.assertEqual(open_password(reply['sealed_password'], 'a1' * 16, 3, 'alice'),
                                         'a private native password')
                        for overrides in ({'session': participant_token}, {'project': 4}, {'session': 'x' * 40}):
                            with self.assertRaisesRegex(TicketError, 'Project manager access required'):
                                request_broker(dict(request, **overrides), socket_path=str(socket_path))
                    else:
                        with self.assertRaisesRegex(TicketError, 'Caller is not authorized'):
                            request_ticket('x' * 40, 3, socket_path=str(socket_path))
                finally:
                    worker.terminate()
                    worker.join(5)

    def test_real_socket_accepts_configured_uid(self):
        self.check_broker(pwd.getpwuid(os.getuid()).pw_name, True)

    def test_real_socket_rejects_other_uid(self):
        other = next(user for user in pwd.getpwall() if user.pw_uid != os.getuid())
        self.check_broker(other.pw_name, False)


class GuacamoleTicketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.auth = AuthStore(self.root / 'auth.sqlite3')
        self.auth.set_password('manager', 'long manager test password', projects=['3'])
        self.auth.set_team_access('manager', 3, [1])
        self.auth.set_connection_access('manager', 3, ['vm:web'])
        self.secret = 'a1' * 16
        self.sidecar = self.root / 'guacamole/access/project-003.applied.json'
        self.sidecar.parent.mkdir(parents=True)
        self.connections = [
            {'resource_key': 'jumpbox:j1', 'name': 'Team 1 jump', 'address': '10.3.1.11',
             'protocol': 'rdp', 'port': 3389, 'team_id': '1', 'default_for_team': True},
            {'resource_key': 'jumpbox:j2', 'name': 'Team 2 jump', 'address': '10.3.2.11',
             'protocol': 'rdp', 'port': 3389, 'team_id': '2', 'default_for_team': True},
            {'resource_key': 'vm:web', 'name': 'Team 1 web', 'address': '10.3.1.20',
             'protocol': 'ssh', 'port': 22, 'team_id': '1', 'default_for_team': False},
        ]
        self.sidecar.write_text(json.dumps({
            'connections': self.connections,
            'permissions': [{'username': 'manager', 'resource_key': item['resource_key']}
                            for item in (self.connections[0], self.connections[2])],
        }))
        self.status = self.root / 'status.json'
        self.status.write_text(json.dumps({'configured': True, 'url': 'https://guac.example'}))

    def decode(self, url):
        token = parse_qs(urlsplit(url).query)['data'][0]
        raw = base64.b64decode(token)
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        decryptor = Cipher(algorithms.AES(bytes.fromhex(self.secret)), modes.CBC(bytes(16))).decryptor()
        signed = decryptor.update(raw) + decryptor.finalize()
        clear_length = len(signed) - signed[-1]
        signature, payload = signed[:32], signed[32:clear_length]
        self.assertTrue(hmac.compare_digest(signature, hmac.digest(bytes.fromhex(self.secret), payload, 'sha256')))
        return json.loads(payload)

    def test_ticket_contains_only_current_team_default_and_explicit_connection(self):
        token, _ = self.auth.login('manager', 'long manager test password')
        url = issue_ticket(token, 3, auth=self.auth, secret_hex=self.secret,
                           repo_root=self.root, status_file=self.status)
        payload = self.decode(url)
        self.assertEqual(parse_qs(urlsplit(url).query, keep_blank_values=True)['token'], [''])
        self.assertEqual(payload['username'], 'manager')
        self.assertEqual(set(payload['connections']), {'Team 1 jump', 'Team 1 web'})
        self.assertEqual(payload['connections']['Team 1 jump']['parameters']['hostname'], '10.3.1.11')
        self.assertLessEqual(payload['expires'] - 1000 * __import__('time').time(), 61000)

    def test_guest_credentials_disclosure_requires_authorized_box_and_opt_in(self):
        self.connections[0].update(kind='jumpbox',credential_ref='a'*32)
        state=json.loads(self.sidecar.read_text());state['connections']=self.connections
        self.sidecar.write_text(json.dumps(state))
        token,_=self.auth.login('manager','long manager test password')
        credentials={'username':'custom-user','password':'custom-secret','show_credentials':False}
        with patch('crange_guac.guest_parameters',return_value={'username':'custom-user','password':'custom-secret'}), patch('crange_guest_credentials.resolve',return_value=credentials):
            result=issue_ticket(token,3,auth=self.auth,secret_hex=self.secret,repo_root=self.root,status_file=self.status,return_details=True)
            self.assertEqual(result['credentials'],[])
            credentials['show_credentials']=True
            result=issue_ticket(token,3,auth=self.auth,secret_hex=self.secret,repo_root=self.root,status_file=self.status,return_details=True)
            self.assertEqual([v['name'] for v in result['credentials']],['Team 1 jump'])
            self.auth.set_team_access('manager',3,[])
            result=issue_ticket(token,3,auth=self.auth,secret_hex=self.secret,repo_root=self.root,status_file=self.status,return_details=True)
            self.assertEqual(result['credentials'],[])

    def test_removed_team_access_takes_effect_even_before_next_apply(self):
        token, _ = self.auth.login('manager', 'long manager test password')
        self.auth.set_team_access('manager', 3, [2])
        url = issue_ticket(token, 3, auth=self.auth, secret_hex=self.secret,
                           repo_root=self.root, status_file=self.status)
        self.assertEqual(set(self.decode(url)['connections']), {'Team 1 web'})

    def test_project_scope_and_disabled_sessions_are_enforced(self):
        token, _ = self.auth.login('manager', 'long manager test password')
        with self.assertRaisesRegex(TicketError, 'Project access denied'):
            issue_ticket(token, 4, auth=self.auth, secret_hex=self.secret,
                         repo_root=self.root, status_file=self.status)
        self.auth.set_enabled('manager', False)
        with self.assertRaisesRegex(TicketError, 'Sign in required'):
            issue_ticket(token, 3, auth=self.auth, secret_hex=self.secret,
                         repo_root=self.root, status_file=self.status)

    def test_platform_admin_gets_project_manifest_scope(self):
        self.auth.set_password('admin', 'long admin test password', admin=True)
        token, _ = self.auth.login('admin', 'long admin test password')
        with self.assertRaisesRegex(TicketError, 'No currently applied'):
            issue_ticket(token, 3, auth=self.auth, secret_hex=self.secret,
                         repo_root=self.root, status_file=self.status)
        state = json.loads(self.sidecar.read_text())
        state['permissions'].extend({'username': 'admin', 'resource_key': item['resource_key']}
                                    for item in self.connections)
        self.sidecar.write_text(json.dumps(state))
        url = issue_ticket(token, 3, auth=self.auth, secret_hex=self.secret,
                           repo_root=self.root, status_file=self.status)
        self.assertEqual(set(self.decode(url)['connections']), {'Team 1 jump', 'Team 2 jump', 'Team 1 web'})

    def test_platform_admin_gets_each_enabled_protocol_for_every_box(self):
        self.auth.set_password('admin', 'long admin test password', admin=True)
        token, _ = self.auth.login('admin', 'long admin test password')
        extra_services = [
            {**self.connections[0], 'name': 'Team 1 jump · SSH', 'protocol': 'ssh', 'port': 22},
            {**self.connections[0], 'name': 'Team 1 jump · VNC', 'protocol': 'vnc', 'port': 5900},
            {**self.connections[2], 'name': 'Team 1 web · RDP', 'protocol': 'rdp', 'port': 3389},
        ]
        state = json.loads(self.sidecar.read_text())
        state['connections'].extend(extra_services)
        state['permissions'] = [
            {'username': 'admin', 'resource_key': item['resource_key']}
            for item in state['connections']
        ]
        self.sidecar.write_text(json.dumps(state))

        url = issue_ticket(token, 3, auth=self.auth, secret_hex=self.secret,
                           repo_root=self.root, status_file=self.status)
        connections = self.decode(url)['connections']
        self.assertEqual(set(connections), {
            'Team 1 jump', 'Team 1 jump · SSH', 'Team 1 jump · VNC',
            'Team 2 jump', 'Team 1 web', 'Team 1 web · RDP',
        })
        self.assertEqual({item['protocol'] for item in connections.values()}, {'rdp', 'ssh', 'vnc'})


if __name__ == '__main__':
    unittest.main()
