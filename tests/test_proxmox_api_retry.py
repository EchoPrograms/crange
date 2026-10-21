import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange


class ProxmoxReadRetryTests(unittest.TestCase):
    def setUp(self):
        self.api = object.__new__(crange.ProxmoxAPI)
        self.api.endpoint = 'https://pve.example/api2/json'
        self.api.token = 'not-a-real-token'
        self.api.ssl_context = None

    def test_interactive_reads_can_bound_timeout_and_disable_retries(self):
        self.api.request_timeout = 2
        self.api.read_attempts = 1
        error = HTTPError('https://pve.example/api2/json/nodes/pve/status', 595, 'offline', {}, None)
        with patch('crange.urllib.request.urlopen', side_effect=error) as urlopen, patch('crange.time.sleep') as sleep:
            with self.assertRaises(crange.CrangeError):
                self.api.request('GET', '/nodes/pve/status')
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(urlopen.call_args.kwargs['timeout'], 2)
        sleep.assert_not_called()
        error.close()

    def test_backup_progress_prints_numeric_evidence_without_raw_logs(self):
        import io
        self.api.request = Mock(side_effect=[{'status': 'running'},
            [{'t': 'INFO: private configuration must not be printed'},
             {'t': 'INFO:  21% (13.5 GiB of 64.0 GiB) in 4m 37s, read: 40.7 MiB/s'}],
            {'status': 'stopped', 'exitstatus': 'OK'}])
        output = io.StringIO()
        with patch('crange.time.sleep'), patch('sys.stdout', output):
            self.api.task('proxmox1', 'UPID:proxmox1:001:002:003:vzdump:128:user:')
        self.assertIn('Backup progress: 21% (13.5 GiB of 64.0 GiB)', output.getvalue())
        self.assertNotIn('private configuration', output.getvalue())

    def test_clone_progress_reports_only_bounded_numeric_transfer_evidence(self):
        import io
        self.api.request = Mock(side_effect=[{'status':'running'},
            [{'t':'private clone configuration'},
             {'t':'transferred 65.0 GiB of 64.0 GiB (101.00%)'},
             {'t':'transferred 16.7 GiB of 64.0 GiB (26.03%)'}],
            {'status':'stopped','exitstatus':'OK'}])
        output=io.StringIO()
        with patch('crange.time.sleep'),patch('sys.stdout',output):
            self.api.task('proxmox5','UPID:proxmox5:1:2:3:qmclone:138:user:')
        self.assertIn('Clone progress: 26.03% (16.7 GiB of 64.0 GiB)',output.getvalue())
        self.assertNotIn('private',output.getvalue());self.assertNotIn('101.00',output.getvalue())
        self.assertTrue(all(c.args[0]=='GET' for c in self.api.request.call_args_list))

    def test_optional_backup_log_failure_does_not_replay_backup(self):
        self.api.request = Mock(side_effect=[{'status': 'running'},
            crange.CrangeError('log unavailable'), {'status': 'stopped', 'exitstatus': 'OK'}])
        with patch('crange.time.sleep'):
            self.api.task('proxmox1', 'UPID:proxmox1:001:002:003:vzdump:128:user:')
        self.assertTrue(all(call.args[0] == 'GET' for call in self.api.request.call_args_list))

    def test_restore_progress_is_read_only_and_uses_gib(self):
        import io
        self.api.request = Mock(side_effect=[{'status': 'running'},
            [{'t': 'progress 18% (read 12369657856 bytes, duration 130 sec)'}],
            {'status': 'stopped', 'exitstatus': 'OK'}])
        output = io.StringIO()
        with patch('crange.time.sleep'), patch('sys.stdout', output):
            self.api.task('proxmox5', 'UPID:proxmox5:001:002:003:qmrestore:131:user:')
        self.assertIn('Restore progress: 18% (11.52 GiB read)', output.getvalue())
        self.assertTrue(all(call.args[0] == 'GET' for call in self.api.request.call_args_list))

    def test_start_accepts_only_verified_non_virtio_mtu_warning(self):
        self.api.request = Mock(side_effect=[
            {'status': 'stopped', 'exitstatus': 'WARNINGS: 1'},
            [{'t': "WARN: netdev net0: ignoring MTU '1450', not using VirtIO"},
             {'t': 'TASK WARNINGS: 1'}]])
        self.api.task('proxmox1', 'UPID:proxmox1:001:002:003:qmstart:128:user:')

    def test_unknown_warning_remains_failure(self):
        self.api.request = Mock(side_effect=[
            {'status': 'stopped', 'exitstatus': 'WARNINGS: 1'},
            [{'t': 'WARN: unrelated storage warning'}, {'t': 'TASK WARNINGS: 1'}]])
        with self.assertRaisesRegex(crange.CrangeError, 'WARNINGS: 1'):
            self.api.task('proxmox1', 'UPID:proxmox1:001:002:003:qmstart:128:user:')

    def test_warning_on_clone_is_not_accepted(self):
        self.api.request = Mock(return_value={'status': 'stopped', 'exitstatus': 'WARNINGS: 1'})
        with self.assertRaisesRegex(crange.CrangeError, 'WARNINGS: 1'):
            self.api.task('proxmox1', 'UPID:proxmox1:001:002:003:qmclone:128:user:')
        self.api.request.assert_called_once()

    def test_backup_mtu_warning_requires_successful_completion_log(self):
        logs = [{'t': "WARN: netdev net0: ignoring MTU '1450', not using VirtIO"},
                {'t': 'INFO: Backup job finished successfully'}, {'t': 'TASK WARNINGS: 1'}]
        self.api.request = Mock(side_effect=[{'status': 'stopped', 'exitstatus': 'WARNINGS: 1'}, logs])
        self.api.task('proxmox1', 'UPID:proxmox1:001:002:003:vzdump:128:user:')
        self.api.request = Mock(side_effect=[{'status': 'stopped', 'exitstatus': 'WARNINGS: 1'}, [logs[0], logs[-1]]])
        with self.assertRaisesRegex(crange.CrangeError, 'WARNINGS: 1'):
            self.api.task('proxmox1', 'UPID:proxmox1:001:002:003:vzdump:128:user:')

    def test_get_retries_transient_595_then_returns_data(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read = Mock(return_value=json.dumps({'data': {'status': 'online'}}).encode())
        error = HTTPError('https://pve.example/api2/json/nodes/pve/status',
                          595, 'gateway', {}, None)
        with patch('crange.urllib.request.urlopen', side_effect=[
                error, response]) as urlopen, patch('crange.time.sleep') as sleep:
            result = self.api.request('GET', '/nodes/pve/status')
        self.assertEqual(result, {'status': 'online'})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)
        error.close()

    def test_gateway_596_retries_only_reads_and_task_observation(self):
        response=Mock();response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        error=HTTPError('https://pve.example/api2/json/nodes/pve/status',596,'gateway',{},None)
        with patch('crange.urllib.request.urlopen',side_effect=[error,response]) as urlopen, \
             patch('crange.json.load',return_value={'data':{'status':'running'}}),patch('crange.time.sleep'):
            self.assertEqual(self.api.request('GET','/nodes/pve/status'),{'status':'running'})
            self.assertEqual(urlopen.call_count,2)
        wrapped=crange.CrangeError('gateway');wrapped.__cause__=error
        self.api.request=Mock(side_effect=[wrapped,{'status':'running'},[],{'status':'stopped','exitstatus':'OK'}])
        with patch('crange.time.sleep'):
            self.api.task('proxmox5','UPID:proxmox5:1:2:3:vzdump:138:user:')
        self.assertTrue(all(call.args[0]=='GET' for call in self.api.request.call_args_list))
        error.close()

    def test_mutating_post_is_never_retried(self):
        error = HTTPError('https://pve.example/api2/json/nodes/pve/qemu/100/status/start',
                          596, 'gateway', {}, None)
        with patch('crange.urllib.request.urlopen', side_effect=error) as urlopen:
            with self.assertRaisesRegex(crange.CrangeError, 'POST .* HTTP 596'):
                self.api.request('POST', '/nodes/pve/qemu/100/status/start', {})
        urlopen.assert_called_once()
        error.close()

    def test_busy_adapter_retries_reads_but_never_writes(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        error = HTTPError('https://pve.example/api2/json/nodes/pve/status', 503, 'busy', {}, None)
        with patch('crange.urllib.request.urlopen', side_effect=[error, response]) as urlopen, \
             patch('crange.json.load', return_value={'data': {'status': 'running'}}), patch('crange.time.sleep'):
            self.assertEqual(self.api.request('GET', '/nodes/pve/status'), {'status': 'running'})
            self.assertEqual(urlopen.call_count, 2)
        for method in ('POST', 'PUT', 'DELETE'):
            with self.subTest(method=method), patch('crange.urllib.request.urlopen', side_effect=error) as urlopen:
                with self.assertRaisesRegex(crange.CrangeError, 'HTTP 503'):
                    self.api.request(method, '/nodes/pve/qemu/100/status/start', {})
                urlopen.assert_called_once()
        wrapped = crange.CrangeError('busy'); wrapped.__cause__ = error
        self.api.request = Mock(side_effect=[wrapped, {'status': 'stopped', 'exitstatus': 'OK'}])
        with patch('crange.time.sleep'):
            self.api.task('proxmox6', 'UPID:proxmox6:1:2:3:qmclone:175:user:')
        self.assertTrue(all(call.args == ('GET', '/nodes/proxmox6/tasks/UPID%3Aproxmox6%3A1%3A2%3A3%3Aqmclone%3A175%3Auser%3A/status')
                            for call in self.api.request.call_args_list))
        error.close()

    def test_get_does_not_retry_other_http_failures(self):
        error = HTTPError('https://pve.example/api2/json/nodes/pve/status',
                          500, 'server error', {}, None)
        with patch('crange.urllib.request.urlopen', side_effect=error) as urlopen:
            with self.assertRaisesRegex(crange.CrangeError, 'GET .* HTTP 500'):
                self.api.request('GET', '/nodes/pve/status')
        urlopen.assert_called_once()
        error.close()


if __name__ == '__main__':
    unittest.main()
