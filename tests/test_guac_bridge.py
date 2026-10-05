import hashlib
import hmac
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from crange_guac_bridge import BridgeError, _access, handle, verify
from crange_jobs import command
from crange_jobs import JobStore


class _Auth:
    def __init__(self):
        self.users = [{'username': 'admin', 'enabled': True, 'admin': True},
                      {'username': 'alice', 'enabled': True, 'admin': False,
                       'teams': {'3': ['1']}, 'connections': {'3': []}}]
        self.events = []

    def list_users(self, project=None):
        return self.users

    def audit(self, *args):
        self.events.append(args)


class _Jobs:
    def __init__(self):
        self.calls = []
        self._temp = tempfile.TemporaryDirectory()
        self.connect = JobStore(Path(self._temp.name) / "jobs.sqlite3").connect

    def __del__(self):
        self._temp.cleanup()

    def submit(self, *args):
        self.calls.append(args)
        command(args[2], args[1], args[3])
        return 'a' * 32


class BridgeTests(unittest.TestCase):
    def test_status_is_delegated_and_broker_rechecks_access(self):
        from crange_guac_broker import box_status
        from crange_guac_ticket import TicketError
        request = {'operation': 'status', 'project': 3, 'resource_key': 'vm:web'}
        with patch('crange_guac_bridge._project_rows', return_value=[{'resource_key': 'vm:web'}]), \
                patch('crange_guac_ticket.request_broker', return_value={'state': 'running'}) as broker:
            self.assertEqual(handle('alice', request, auth=_Auth(), jobs=_Jobs(),
                                    root=Path('/repo')), (200, {'state': 'running'}))
        data = {'operation': 'guac-box-status', 'project': 3,
                'resource_key': 'vm:web', 'username': 'alice'}
        broker.assert_called_once_with(data)
        api = Mock()
        api.status.return_value = 'running'
        with patch('crange_guac_bridge._project_rows', return_value=[{'resource_key': 'vm:web'}]), \
                patch('crange_guac_boxes.resolve', return_value=(None, None, None, api, 'vm', 'node', 123, None)):
            self.assertEqual(box_status(data, auth=_Auth(), root=Path('/repo')),
                             {'resource_key': 'vm:web', 'state': 'running'})
        with patch('crange_guac_bridge._project_rows', return_value=[]), \
                patch('crange_guac_boxes.resolve') as resolve:
            with self.assertRaisesRegex(TicketError, 'access denied'):
                box_status(data, auth=_Auth(), root=Path('/repo'))
        resolve.assert_not_called()

    def test_list_skips_projects_without_applied_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            envs = root / 'terraform/environments'
            (envs / 'project-003').mkdir(parents=True)
            with patch('crange.ENVS', envs), patch('crange_guac_bridge._project_rows') as rows:
                self.assertEqual(handle('alice', {'operation': 'list'}, auth=_Auth(),
                                        jobs=_Jobs(), root=root)[1], {'boxes': []})
            rows.assert_not_called()

    def test_native_account_requires_current_and_applied_grants(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            access = root / 'guacamole/access'
            access.mkdir(parents=True)
            (access / 'project-003.applied.json').write_text(json.dumps({
                'connections': [{'resource_key': 'vm:web', 'team_id': '1',
                                 'default_for_team': True},
                                {'resource_key': 'vm:private', 'team_id': '2'}]}))
            (access / 'project-003.users-applied.json').write_text(json.dumps({
                'users': {'p003-bob': {'connection_keys': ['vm:web', 'vm:private']}}}))
            native = {'login': 'p003-bob', 'enabled': True,
                      'teams': ['1'], 'connections': []}
            with patch('crange_guac_users.ProjectUsers.list', return_value=[native]):
                self.assertEqual(_access(_Auth(), root, 3, 'p003-bob')[1], {'vm:web'})
                native['teams'] = []
                self.assertEqual(_access(_Auth(), root, 3, 'p003-bob')[1], set())
                native['connections'] = ['vm:private']
                self.assertEqual(_access(_Auth(), root, 3, 'p003-bob')[1], {'vm:private'})
                native['enabled'] = False
                self.assertEqual(_access(_Auth(), root, 3, 'p003-bob')[1], set())

    def test_signature_peer_expiry_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            key = bytes(range(32))
            key_file = Path(directory) / 'key'
            key_file.write_text(key.hex())
            body = json.dumps({'identity': {'username': 'alice'},
                               'request': {'operation': 'list'}}).encode()
            nonce = 'a' * 32
            timestamp = '1790899200'
            signature = hmac.new(key, timestamp.encode()+b'\n'+nonce.encode()+b'\n'+body,
                                 hashlib.sha256).hexdigest()
            headers = {'X-Crange-Timestamp': timestamp, 'X-Crange-Nonce': nonce,
                       'X-Crange-Signature': signature}
            self.assertEqual(verify(body, headers, '10.60.0.10', key_file=key_file,
                                    now=int(timestamp))[0], 'alice')
            with self.assertRaises(BridgeError):
                verify(body, headers, '10.60.0.10', key_file=key_file, now=int(timestamp))
            with self.assertRaises(BridgeError):
                verify(body, {**headers, 'X-Crange-Nonce': 'b' * 32}, '10.60.0.10',
                       key_file=key_file, now=int(timestamp))
            with self.assertRaises(BridgeError):
                verify(body, headers, '10.60.0.11', key_file=key_file, now=int(timestamp))

    def test_only_current_applied_grants_can_submit_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            envs = root / 'terraform/environments'
            project = envs / 'project-003'
            project.mkdir(parents=True)
            applied = root / 'guacamole/access/project-003.applied.json'
            applied.parent.mkdir(parents=True)
            applied.write_text(json.dumps({
                'connections': [
                    {'resource_key': 'vm:web', 'address': '10.3.1.20', 'protocol': 'ssh',
                     'name': 'Web SSH', 'team_id': '1', 'default_for_team': True},
                    {'resource_key': 'vm:web', 'address': '10.3.1.20', 'protocol': 'vnc',
                     'name': 'Web VNC', 'team_id': '1', 'default_for_team': True},
                    {'resource_key': 'vm:secret', 'address': '10.3.2.20', 'protocol': 'ssh',
                     'name': 'Secret SSH', 'team_id': '2', 'default_for_team': False},
                ], 'permissions': [{'username': 'alice', 'resource_key': 'vm:web'},
                                   {'username': 'admin', 'resource_key': 'vm:web'},
                                   {'username': 'admin', 'resource_key': 'vm:secret'}]}))
            cfg = {'scenario_boxes': {'web': {'name': 'web', 'team_id': '1'},
                                      'secret': {'name': 'secret', 'team_id': '2'}}}
            auth, jobs = _Auth(), _Jobs()
            with patch('crange.ENVS', envs), patch('crange.json_config', return_value=cfg):
                status, payload = handle('alice', {'operation': 'list'}, auth=auth,
                                         jobs=jobs, root=root)
                self.assertEqual(status, 200)
                self.assertEqual(len(payload['boxes']), 1)
                self.assertEqual({p['protocol'] for p in payload['boxes'][0]['protocols']},
                                 {'ssh', 'vnc'})
                with self.assertRaisesRegex(BridgeError, 'access denied'):
                    handle('alice', {'operation': 'submit', 'project': 3,
                                     'resource_key': 'vm:secret', 'action': 'stop'},
                           auth=auth, jobs=jobs, root=root)
                self.assertEqual(handle('alice', {'operation': 'submit', 'project': 3,
                                                'resource_key': 'vm:web', 'action': 'start'},
                                        auth=auth, jobs=jobs, root=root)[0], 202)
                self.assertEqual(len(jobs.calls), 1)
                self.assertEqual(len(handle('admin', {'operation': 'list'}, auth=auth,
                                           jobs=jobs, root=root)[1]['boxes']), 2)
                auth.users[1]['teams']['3'] = []
                self.assertEqual(handle('alice', {'operation': 'list'}, auth=auth,
                                        jobs=jobs, root=root)[1]['boxes'], [])

    def test_list_restores_only_current_users_latest_unfinished_or_failed_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / 'terraform/environments/project-003'; env.mkdir(parents=True)
            sidecar = root / 'guacamole/access/project-003.applied.json'; sidecar.parent.mkdir(parents=True); sidecar.write_text('{}')
            jobs = JobStore(root / 'jobs.sqlite3')
            job = jobs.submit('alice', 3, 'guac-box-control', {'resource_key': 'vm:web', 'action': 'shutdown'})
            jobs.submit('bob', 3, 'guac-box-control', {'resource_key': 'vm:web', 'action': 'start'})
            with patch('crange.ENVS', env.parent):
                def listed():
                    return handle('alice', {'operation': 'list'}, auth=_Auth(), jobs=jobs, root=root,
                                  rows_provider=lambda *args: [{'project': 3, 'resource_key': 'vm:web'}])[1]['boxes'][0]['job']
                self.assertEqual(listed()['id'], job)
                for status in ('running', 'failed'):
                    with jobs.connect() as db: db.execute('UPDATE jobs SET status=? WHERE id=?', (status, job))
                    self.assertEqual(listed()['status'], status)
                with jobs.connect() as db: db.execute("UPDATE jobs SET status='succeeded' WHERE id=?", (job,))
                self.assertIsNone(listed())

    def test_fixed_power_and_snapshot_job_inputs(self):
        self.assertEqual(command('guac-box-control', 3,
                                 {'resource_key': 'container:web', 'action': 'restart'}),
                         ['guac-box-control', '3', 'container:web', 'restart'])
        self.assertEqual(command('guac-snapshot', 3,
                                 {'resource_key': 'vm:web', 'action': 'create',
                                  'snapshot': 'before-update'}),
                         ['guac-snapshot', '3', 'vm:web', 'create', '--snapshot', 'before-update'])
        for options in ({'resource_key': 'vm:../../etc', 'action': 'start'},
                        {'resource_key': 'vm:web', 'action': 'destroy'}):
            with self.assertRaises(ValueError):
                command('guac-box-control', 3, options)
        for options in ({'resource_key': 'vm:web', 'action': 'restore', 'snapshot': ''},
                        {'resource_key': 'vm:web', 'action': 'create', 'snapshot': 'ok;id'}):
            with self.assertRaises(ValueError):
                command('guac-snapshot', 3, options)

    def test_job_result_is_bound_to_actor_box_and_current_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = JobStore(root / 'jobs.sqlite3')
            job_id = jobs.submit('alice', 3, 'guac-snapshot',
                                 {'resource_key': 'vm:web', 'action': 'list', 'snapshot': ''})
            with jobs.connect() as db:
                db.execute("UPDATE jobs SET status='succeeded',output=? WHERE id=?",
                           (json.dumps({'snapshots': [{'name': 'before-update'}]}), job_id))
            request = {'operation': 'job', 'project': 3, 'resource_key': 'vm:web',
                       'job_id': job_id}
            with patch('crange_guac_bridge._project_rows', return_value=[
                    {'resource_key': 'vm:web'}, {'resource_key': 'vm:other'}]):
                result = handle('alice', request, auth=_Auth(), jobs=jobs, root=root)[1]
                self.assertEqual(result['job']['snapshots'], [{'name': 'before-update'}])
                for username, key in [('bob', 'vm:web'), ('alice', 'vm:other')]:
                    with self.assertRaisesRegex(BridgeError, 'Job access denied'):
                        handle(username, {**request, 'resource_key': key},
                               auth=_Auth(), jobs=jobs, root=root)
            with patch('crange_guac_bridge._project_rows', return_value=[]):
                with self.assertRaisesRegex(BridgeError, 'Box access denied'):
                    handle('alice', request, auth=_Auth(), jobs=jobs, root=root)

    def test_worker_rechecks_guac_grant_before_power_action(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / 'jobs.sqlite3', auth_path='test-auth.sqlite3')
            store.submit('p003-alice', 3, 'guac-box-control',
                         {'resource_key': 'vm:web', 'action': 'stop'})
            with patch('crange_auth.AuthStore'), patch('crange_guac_bridge._project_rows',
                                                       return_value=[]), \
                 patch('crange_jobs.subprocess.run') as process:
                store.run_next()
            process.assert_not_called()
            self.assertEqual(store.list(['3'])[0]['status'], 'failed')
            self.assertIn('access was revoked', store.list(['3'])[0]['output'])


if __name__ == '__main__':
    unittest.main()
