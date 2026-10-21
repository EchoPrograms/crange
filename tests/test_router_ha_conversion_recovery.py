"""A timed-out observer cannot replay a router move or release a live lock."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import ssl
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('ha_conversion', Path(__file__).resolve().parents[1] / 'scripts/setup-router-ha.py')
operator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operator)


class ConversionRecoveryTests(unittest.TestCase):
    def fixture(self):
        self.original = {'hostname': 'p100t002-router', 'unprivileged': 1,
            'tags': 'network-infrastructure;p100;range-lan;terraform',
            'rootfs': 'local-lvm:vm-118-disk-0,size=8G',
            'net0': 'bridge=p100tr,ip=10.100.0.3/24',
            'net1': 'bridge=p100t002,ip=10.100.2.1/24'}
        self.current = {**self.original, 'rootfs': 'crange-router-ha:118/vm-118-disk-0.raw,size=8G', 'lock': 'disk'}
        self.plan = {'node': 'proxmox2', 'vmid': 118, 'state_lineage': 'lineage',
            'address': 'router-address', 'config': self.original,
            'config_sha256': operator.sha(self.original), 'disk_gib': 8,
            'eligible_nodes': ['proxmox2'], 'bridges': ['p100tr', 'p100t002'],
            'phase': 'submitting-move-rootfs', 'submitted_at': 1000}
        self.native = {'type': 'move_volume', 'id': '118', 'user': 'root@pam',
            'upid': 'UPID:proxmox2:ABC:', 'starttime': 1001}
        self.task_result = {'status': 'stopped', 'exitstatus': 'broken pipe '}
        self.history = [self.native]
        self.logs = [{'t': 'total size is 520,131,135  speedup is 1.00'}, {'t': 'TASK ERROR: broken pipe '}]
        self.resource = {'vmid': 118, 'type': 'lxc', 'node': 'proxmox2', 'pool': 'crange-iac'}

    def api(self, path, *args, **kwargs):
        if path == '/cluster/resources': return [self.resource]
        if path == '/nodes': return [{'node': 'proxmox2', 'status': 'online'}]
        if path == '/cluster/ha/resources': return []
        if path.endswith('/config'): return copy.deepcopy(self.current)
        if path.endswith('/status/current'): return {'status': 'stopped'}
        if path.endswith('/tasks'): return self.history
        if path.endswith('/log'): return self.logs
        if '/tasks/' in path and path.endswith('/status'): return self.task_result
        raise AssertionError(path)

    def resume(self, file):
        return operator.resume_conversion(100, 'router:team:2', {'lineage': 'lineage'},
            'router-address', {'vm_id': 118, 'node_name': 'proxmox2'}, self.plan, file)

    def test_native_mutation_returns_upid_without_attached_cli_worker(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"data":"UPID:proxmox2:ABC:"}'
        tls = SimpleNamespace(verify_flags=ssl.VERIFY_X509_STRICT,
                              verify_mode=ssl.CERT_REQUIRED, check_hostname=True)
        with patch.object(operator, 'run', return_value=json.dumps({'ticket': 'test-ticket', 'csrf': 'test-csrf'})) as run, \
                patch.object(operator.ssl, 'create_default_context', return_value=tls) as context, \
                patch.object(operator, 'urlopen', return_value=response) as open_url:
            self.assertEqual(operator.api('/nodes/proxmox2/lxc/118/move_volume', 'create', volume='rootfs'), 'UPID:proxmox2:ABC:')
        self.assertEqual(run.call_args.args[0][0], 'perl')
        self.assertIn('assemble_ticket(q(root@pam))', run.call_args.args[0][-1])
        context.assert_called_once_with(cafile='/etc/pve/pve-root-ca.pem')
        self.assertEqual(tls.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(tls.check_hostname)
        self.assertFalse(tls.verify_flags & ssl.VERIFY_X509_STRICT)
        request = open_url.call_args.args[0]
        self.assertEqual(request.method, 'POST')
        self.assertEqual(request.full_url, 'https://proxmox4:8006/api2/json/nodes/proxmox2/lxc/118/move_volume')
        self.assertEqual(request.data, b'volume=rootfs')

    def test_submission_timeout_observes_one_original_task(self):
        self.fixture()
        with tempfile.TemporaryDirectory() as directory, patch.object(operator.time, 'time', return_value=1000), \
                patch.object(operator, 'api', side_effect=[TimeoutError(), self.history, {'status': 'stopped', 'exitstatus': 'OK'}]) as api:
            operator.task(self.plan, Path(directory)/'receipt.json', 'move-rootfs', '/move')
        self.assertEqual(sum(len(c.args)>1 and c.args[1]=='create' for c in api.call_args_list), 1)
        self.assertEqual(self.plan['phase'], 'completed-move-rootfs')

    def test_committed_copy_is_proved_before_unlock_and_only_start_is_submitted(self):
        self.fixture()
        events = []
        def files(*args): events.append('filesystem-proof'); return {'passed': True}
        def unlock(node, command, **kwargs):
            events.append(command)
            self.current.pop('lock')
        with tempfile.TemporaryDirectory() as directory, patch.object(operator, 'api', side_effect=self.api), \
                patch.object(operator, 'verify_clients'), patch.object(operator, 'verify_committed_files', side_effect=files), \
                patch.object(operator, 'source_command', side_effect=unlock), patch.object(operator, 'task') as task, \
                patch.object(operator, 'refresh_and_enroll', return_value={'phase': 'enrolled'}):
            self.assertEqual(self.resume(Path(directory)/'receipt.json')['phase'], 'enrolled')
        self.assertEqual(events, ['filesystem-proof', ['pct', 'unlock', '118']])
        self.assertEqual(task.call_args.args[2:], ('start', '/nodes/proxmox2/lxc/118/status/start'))
        self.assertTrue(self.plan['committed_move_recovery']['original_failure_preserved'])
        self.assertEqual(self.plan['committed_move_recovery']['task']['exitstatus'], 'broken pipe ')

    def test_uncertain_foreign_failed_or_changed_copy_never_unlocks_or_starts(self):
        for change in ('active', 'another-task', 'foreign-actor', 'other-failure', 'incomplete-log', 'foreign-pool', 'changed-host', 'extra-volume', 'filesystem-error'):
            self.fixture()
            if change == 'active': self.task_result = {'status': 'running'}
            if change == 'another-task': self.history.append({**self.native, 'type': 'vzstart'})
            if change == 'foreign-actor': self.native['user'] = 'other@pam'
            if change == 'other-failure': self.task_result['exitstatus'] = 'No space left'
            if change == 'incomplete-log': self.logs.pop(0)
            if change == 'foreign-pool': self.resource['pool'] = 'unrelated'
            if change == 'changed-host': self.current['hostname'] = 'unrelated'
            if change == 'extra-volume': self.current['unused0'] = 'local-lvm:vm-118-disk-1'
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory, \
                    patch.object(operator, 'api', side_effect=self.api), patch.object(operator, 'verify_clients'), \
                    patch.object(operator, 'verify_committed_files', side_effect=ValueError('filesystem') if change=='filesystem-error' else None), \
                    patch.object(operator, 'source_command') as unlock, patch.object(operator, 'task') as task, \
                    patch.object(operator, 'refresh_and_enroll') as finish:
                with self.assertRaises(ValueError): self.resume(Path(directory)/'receipt.json')
                unlock.assert_not_called(); task.assert_not_called(); finish.assert_not_called()

    def test_unverified_filesystem_response_or_volume_identity_is_rejected(self):
        self.fixture()
        with patch.object(operator, 'source_command', return_value='{}'):
            with self.assertRaisesRegex(ValueError, 'proof differs'):
                operator.verify_committed_files(self.plan, self.current)
        self.current['rootfs'] = 'crange-router-ha:114/vm-114-disk-0.raw,size=8G'
        with patch.object(operator, 'source_command') as run:
            with self.assertRaisesRegex(ValueError, 'exact managed'):
                operator.verify_committed_files(self.plan, self.current)
            run.assert_not_called()


if __name__ == '__main__': unittest.main()
