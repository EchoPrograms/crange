import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / 'ops/proxmox' / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


guard = module('management_guard', 'management_guard.py')
rejoin = module('cluster_rejoin_guard', 'cluster_rejoin_guard.py')
CFG = {'hostname': 'proxmox1', 'bridge': 'vmbr0', 'nic': 'eno1', 'address': '10.0.0.101/24',
       'gateway': '10.0.0.1', 'peers': ['10.0.0.103', '10.0.0.104']}


class ManagementGuardTests(unittest.TestCase):
    def addresses(self, present=True, other=False):
        return json.dumps([{'flags': ['UP'], 'addr_info': ([{'family': 'inet', 'local': '10.0.0.199' if other else '10.0.0.101', 'prefixlen': 24}] if present else [])}])

    def exercise(self, *, present=True, reachable=True, hang=False, peer=False, routes=True, other=False):
        calls = []
        def command(*args, **kwargs):
            calls.append(args)
            if args[:4] == ('ip', '-j', 'address', 'show'):
                return self.addresses(present if len(calls) == 1 else True, other=other)
            if args[:4] == ('ip', '-j', 'link', 'show'):
                return '[{"flags":["UP"]}]'
            if args[:4] == ('ip', '-j', 'route', 'show'):
                return '[{"gateway":"10.0.0.1"}]' if routes else '[]'
            return ''
        with patch.object(guard, 'run', side_effect=command), patch.object(guard, 'evidence'), \
             patch.object(guard, 'ping', side_effect=lambda cfg, target: reachable if target == cfg['gateway'] else peer), \
             patch.object(guard, 'recent_hang', return_value=hang):
            guard.repair(CFG)
        return calls

    def test_missing_address_and_route_restored_without_link_bounce(self):
        calls = self.exercise(present=False, routes=False)
        self.assertIn(('ip', 'address', 'add', '10.0.0.101/24', 'dev', 'vmbr0'), calls)
        self.assertIn(('ip', 'route', 'add', 'default', 'via', '10.0.0.1', 'dev', 'vmbr0'), calls)
        self.assertFalse(any('down' in call or 'ifreload' in call for call in calls))

    def test_healthy_check_is_read_only(self):
        calls = self.exercise()
        self.assertTrue(all(call[:2] == ('ip', '-j') for call in calls))

    def test_gateway_outage_alone_does_not_escalate(self):
        self.exercise(reachable=False)
        self.exercise(reachable=False, hang=True, peer=True)
        with self.assertRaises(guard.RecoveryFailure):
            self.exercise(reachable=False, hang=True, peer=False)

    def test_different_address_is_not_overwritten(self):
        with self.assertRaisesRegex(ValueError, 'refusing overwrite'):
            self.exercise(other=True)

    def test_hang_must_name_the_physical_interface(self):
        for line, expected in [('e1000e eno1: Detected Hardware Unit Hang', True),
                               ('e1000e eno10: Detected Hardware Unit Hang', False),
                               ('r8169 enp2s0: NETDEV WATCHDOG', False)]:
            with patch.object(guard, 'run', return_value=line):
                self.assertEqual(guard.recent_hang(CFG), expected)

    def test_reboot_threshold_boot_grace_and_cross_boot_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            original = Path.read_text
            def read(path, *args, **kwargs):
                if str(path) == '/proc/uptime':
                    return '900 0'
                if str(path) == '/proc/sys/kernel/random/boot_id':
                    return 'test-boot'
                return original(path, *args, **kwargs)
            with patch.object(guard, 'STATE', state), patch.object(Path, 'read_text', read), \
                 patch.object(guard.time, 'time', return_value=10000), patch.object(guard, 'evidence'), patch.object(guard, 'run') as command:
                for _ in range(5):
                    guard.escalate(CFG, 'local fault')
                command.assert_not_called()
                guard.escalate(CFG, 'local fault')
                command.assert_called_once_with('systemctl', 'reboot', timeout=15)
                guard.escalate(CFG, 'local fault')
                command.assert_called_once()
                data=json.loads(state.read_text());data.update(boot='old-boot',failures=80);state.write_text(json.dumps(data))
                guard.escalate(CFG, 'local fault')
                self.assertEqual(json.loads(state.read_text())['failures'], 1)
                command.assert_called_once()

    def test_configuration_or_tool_errors_do_not_escalate(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(guard, 'load_config', return_value=CFG), \
             patch('builtins.open', return_value=open(Path(directory) / 'lock', 'w')), \
             patch.object(guard, 'mitigate'), patch.object(guard, 'repair', side_effect=ValueError('configuration changed')), \
             patch.object(guard, 'escalate') as escalation:
            with self.assertRaises(ValueError):
                guard.main()
            escalation.assert_not_called()

    def test_boot_grace_prevents_reboot_even_after_six_failures(self):
        original = Path.read_text
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            def read(path, *args, **kwargs):
                if str(path) == '/proc/uptime':
                    return '120 0'
                if str(path) == '/proc/sys/kernel/random/boot_id':
                    return 'new-boot'
                return original(path, *args, **kwargs)
            with patch.object(guard, 'STATE', state), patch.object(Path, 'read_text', read), \
                 patch.object(guard, 'evidence'), patch.object(guard, 'run') as command:
                for _ in range(8):
                    guard.escalate(CFG, 'local fault')
                command.assert_not_called()


class ClusterRejoinGuardTests(unittest.TestCase):
    OLD = 'nodelist {\n node {\n name: proxmox1\n ring0_addr: 10.0.0.101\n quorum_votes: 1\n }\n}\ntotem {\n cluster_name: proxmoxcluster\n config_version: 22\n}\n'
    NEW = OLD.replace('version: 22', 'version: 23').replace('votes: 1', 'votes: 2')

    def snapshot(self, host, configuration=None, quorate=True):
        return {'host': host, 'configuration': configuration or self.NEW, 'quorate': quorate}

    def test_only_older_matching_membership_configuration_qualifies(self):
        self.assertTrue(rejoin.compatible(self.OLD, self.NEW))
        self.assertFalse(rejoin.compatible(self.NEW, self.OLD))
        self.assertFalse(rejoin.compatible(self.OLD, self.NEW.replace('10.0.0.101', '10.0.0.199')))
        self.assertFalse(rejoin.compatible(self.OLD, self.NEW.replace('proxmoxcluster', 'different')))

    def test_two_distinct_quorate_peers_must_agree(self):
        for snapshots in ([self.snapshot('proxmox4')], [self.snapshot('proxmox4')]*2,
                          [self.snapshot('proxmox4'), self.snapshot('proxmox3', quorate=False)],
                          [self.snapshot('proxmox4'), self.snapshot('proxmox3', self.OLD)]):
            with self.assertRaises(ValueError):
                rejoin.agreed_configuration(snapshots, 'proxmox1')
        self.assertEqual(rejoin.agreed_configuration([self.snapshot('proxmox4'), self.snapshot('proxmox3')], 'proxmox1'), self.NEW)

    def test_active_corosync_is_never_interfered_with(self):
        with patch.object(rejoin.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'active\n')), \
             patch.object(rejoin, 'run') as command:
            rejoin.recover('proxmox1')
            command.assert_not_called()

    def test_verified_repair_updates_only_local_copy_and_starts_corosync(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / 'corosync.conf'
            local.write_text(self.OLD)
            backups = Path(directory) / 'backups'
            calls = []
            def command(*args, **kwargs):
                calls.append(args)
                if args[0] == 'ssh':
                    peer = next(value.removeprefix('root@') for value in args if value.startswith('root@'))
                    return json.dumps(self.snapshot(peer))
                return 'active\n' if args[:2] == ('systemctl', 'is-active') else ''
            with patch.object(rejoin, 'LOCAL', local), patch.object(rejoin, 'BACKUPS', backups), \
                 patch.object(rejoin, 'run', side_effect=command), \
                 patch.object(rejoin.subprocess, 'run', return_value=subprocess.CompletedProcess([], 3, 'failed\n')):
                rejoin.recover('proxmox1')
            self.assertEqual(local.read_text(), self.NEW)
            self.assertEqual(next(backups.glob('*.before')).read_text(), self.OLD)
            self.assertIn(('systemctl', 'start', 'corosync'), calls)
            self.assertFalse(any('expected' in call or 'reboot' in call for call in calls))

    def test_no_quorate_peer_agreement_keeps_original_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / 'corosync.conf'
            local.write_text(self.OLD)
            def command(*args, **kwargs):
                if args[0] == 'ssh':
                    peer = next(value.removeprefix('root@') for value in args if value.startswith('root@'))
                    return json.dumps(self.snapshot(peer, quorate=False))
                return 'active\n'
            with patch.object(rejoin, 'LOCAL', local), patch.object(rejoin, 'run', side_effect=command), \
                 patch.object(rejoin.subprocess, 'run', return_value=subprocess.CompletedProcess([], 3, 'failed\n')):
                with self.assertRaisesRegex(ValueError, 'two quorate peers'):
                    rejoin.recover('proxmox1')
            self.assertEqual(local.read_text(), self.OLD)
