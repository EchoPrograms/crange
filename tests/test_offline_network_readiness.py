import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_distribution as d


class OfflineReadinessTests(unittest.TestCase):
    def fixture(self):
        config = {'tags': 'crange-image;crange-capture-123456abcdef',
                  'net0': 'virtio=AA:BB,bridge=isolated,link_down=1'}
        api = Mock()
        def request(method, path, fields=None):
            if path.endswith('/config'): return config
            if path.endswith('/status/current'): return {'status': 'running'}
            if method == 'POST': return {'pid': 7}
            if path.endswith('/agent/exec-status'): return {'exited': True, 'exitcode': 0}
            raise AssertionError((method, path))
        api.request.side_effect = request
        return api, config

    def test_resets_only_known_failed_wait_after_owned_disconnected_guard(self):
        api, _ = self.fixture()
        with patch.object(d, '_require_probe_pool'), patch.object(d, '_fixed_guest_read',
                return_value='● systemd-networkd-wait-online.service loaded failed failed Wait for Network'):
            result = d.normalize_isolated_network_wait(api, 'proxmox6', 'qemu', 175, 'isolated')
        self.assertFalse(result['services_disabled'])
        self.assertEqual([c.args[2] for c in api.request.call_args_list if c.args[0] == 'POST'],
            [{'command': ['/usr/bin/systemctl', 'reset-failed', 'systemd-networkd-wait-online.service']}])

    def test_other_failures_or_additional_failures_are_never_reset(self):
        for output in ('ssh.service loaded failed failed SSH', '',
                'systemd-networkd-wait-online.service loaded failed failed Wait\nssh.service loaded failed failed SSH',
                'systemd-networkd-wait-online.service not-found failed failed Wait'):
            api, _ = self.fixture()
            with self.subTest(output=output), patch.object(d, '_require_probe_pool'), \
                    patch.object(d, '_fixed_guest_read', return_value=output):
                self.assertIsNone(d.normalize_isolated_network_wait(api, 'proxmox6', 'qemu', 175, 'isolated'))
            self.assertFalse(any(c.args[0] == 'POST' for c in api.request.call_args_list))

    def test_connected_foreign_or_template_guests_cannot_be_mutated(self):
        for change in ({'net0': 'virtio=AA,bridge=isolated,link_down=0'},
                       {'net0': 'virtio=AA,bridge=production,link_down=1'},
                       {'template': 1}, {'tags': 'foreign'}):
            api, config = self.fixture(); config.update(change)
            with self.subTest(change=change), patch.object(d, '_require_probe_pool'), \
                    patch.object(d, '_fixed_guest_read') as read, self.assertRaises(d.DistributionError):
                d.normalize_isolated_network_wait(api, 'proxmox6', 'qemu', 175, 'isolated')
            read.assert_not_called()
            self.assertFalse(any(c.args[0] == 'POST' for c in api.request.call_args_list))

    def test_containers_do_not_gain_new_exec_capabilities(self):
        api = Mock()
        self.assertIsNone(d.normalize_isolated_network_wait(api, 'proxmox6', 'lxc', 175, 'isolated'))
        api.request.assert_not_called()

    def test_normalization_does_not_bypass_independent_identity(self):
        source = {'machine_id': 'a' * 32, 'ssh_host_key': 'ssh-ed25519 source'}
        verifier = d.ProbeVerifier('isolated', ['isolated'], source)
        api = Mock(); api.request.side_effect = [
            {'pid': 1}, {'exited': True, 'exitcode': 1, 'out-data': 'degraded'},
            {'pid': 2}, {'exited': True, 'exitcode': 0, 'out-data': 'running'},
            {'pid': 3}, {'exited': True, 'exitcode': 0, 'out-data': 'a' * 32},
            {'pid': 4}, {'exited': True, 'exitcode': 0, 'out-data': 'ssh-ed25519 clone'}]
        with patch.object(d, 'normalize_isolated_network_wait', return_value={'unit': 'network-wait'}), \
                patch('time.sleep'):
            result = verifier._check_guest(api, 'proxmox6', 'qemu', 175)
        self.assertFalse(result['passed'])
        self.assertEqual(result['evidence']['readiness'], 'running')
        self.assertIn('offline_network_readiness', result['evidence'])
