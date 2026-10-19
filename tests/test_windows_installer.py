import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_windows_installer as installer
from crange_jobs import command


class InstallerBootTests(unittest.TestCase):
    def setup_scope(self, writes=0, starts=1):
        record = {'id': 'a'*32, 'status': 'installing',
                  'created': datetime.fromtimestamp(time.time()-200, timezone.utc).isoformat()}
        identity = {'node': 'proxmox5', 'vmid': 123, 'status': 'running', 'config_sha256': 'fixed-config'}
        api = Mock()
        def request(method, path, fields=None):
            if method == 'GET' and path == '/cluster/status':
                return [{'name': 'proxmox5', 'type': 'node', 'online': 1, 'ip': '10.0.0.105'}]
            if method == 'GET' and path.endswith('/status/current'):
                return {'diskwrite': writes, 'uptime': 100, 'mem': 110000000, 'cpu': 0}
            if method == 'GET' and path.endswith('/tasks'):
                return [{'type': 'qmstart', 'status': 'OK', 'starttime': time.time()-100} for _ in range(starts)]
            if method == 'POST' and path.endswith('/status/reset'):
                return 'UPID:proxmox5:reset'
            if method == 'PUT' and path.endswith('/sendkey'):
                self.assertEqual(fields, {'key': 'ret'})
                return None
            self.fail('Unexpected API call')
        api.request.side_effect = request
        return record, identity, api

    def test_fixed_admin_operation(self):
        self.assertEqual(command('template-start-installer', 0, {'build_id': 'a'*32})[-2:],
                         ['template-start-installer', 'a'*32])
        with self.assertRaises(ValueError):
            command('template-start-installer', 100, {'build_id': 'a'*32})

    def test_only_original_untouched_session_can_start(self):
        for writes, starts in [(1, 1), (0, 2)]:
            record, identity, api = self.setup_scope(writes, starts)
            with patch.object(installer.base, '_identity', return_value=(identity, 'hash')):
                self.assertFalse(installer.eligibility(record, api)['can_start'])
            self.assertTrue(all(c.args[0] == 'GET' for c in api.request.call_args_list))

    def test_single_reset_then_fixed_enter_keys(self):
        record, identity, api = self.setup_scope()
        with patch.object(installer.templates, 'load', return_value=record), \
             patch.object(installer.templates, 'save') as save, \
             patch.object(installer.base, '_identity', return_value=(identity, 'hash')), \
             patch.object(installer.time, 'sleep'):
            result = installer.start(record['id'], api)
            self.assertEqual(result['state'], 'boot-requested')
            api.task.assert_called_once_with('proxmox5', 'UPID:proxmox5:reset', timeout=120)
            self.assertEqual(sum(c.args[0] == 'POST' for c in api.request.call_args_list), 1)
            self.assertEqual(sum(c.args[0] == 'PUT' for c in api.request.call_args_list), 8)
            self.assertGreater(save.call_count, 8)
            api.request.side_effect = lambda method, path, fields=None: {'diskwrite': 1024, 'uptime': 100}
            with self.assertRaises(ValueError):
                installer.start(record['id'], api)

    def test_interrupted_key_sequence_resumes_without_another_reset(self):
        record, identity, api = self.setup_scope()
        normal = api.request.side_effect
        puts = 0
        def interrupted(method, path, fields=None):
            nonlocal puts
            if method == 'PUT':
                puts += 1
                if puts == 3:
                    raise RuntimeError('Transport unavailable')
            return normal(method, path, fields)
        api.request.side_effect = interrupted
        with patch.object(installer.templates, 'load', return_value=record), \
             patch.object(installer.templates, 'save'), \
             patch.object(installer.base, '_identity', return_value=(identity, 'hash')), \
             patch.object(installer.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                installer.start(record['id'], api)
            self.assertEqual(record['installer_boot']['keys_sent'], 2)
            api.request.side_effect = normal
            installer.start(record['id'], api)
        self.assertEqual(sum(c.args[0] == 'POST' for c in api.request.call_args_list), 1)
