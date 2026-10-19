import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_capture as capture
from crange_distribution import DistributionError


class CaptureTransportTests(unittest.TestCase):
    def test_probe_waits_for_backup_load_without_changing_limit(self):
        api=Mock();api.request.side_effect=[{'cpuinfo':{'cpus':4},'loadavg':[8]}, {'cpuinfo':{'cpus':4},'loadavg':[2]}]
        policy={'allowed_nodes':['proxmox5'],'max_load':1}
        with patch.object(capture.time,'sleep') as sleep,patch.object(capture.time,'monotonic',return_value=0):
            capture._wait_capture_probe_load(api,'proxmox5',policy)
        sleep.assert_called_once_with(15)
        self.assertEqual(policy['max_load'],1)
        self.assertEqual(api.request.call_count,2)

    def test_probe_wait_is_bounded_and_excluded_nodes_are_not_polled(self):
        api=Mock();api.request.return_value={'cpuinfo':{'cpus':4},'loadavg':[8]}
        policy={'allowed_nodes':['proxmox5'],'max_load':1}
        with patch.object(capture.time,'monotonic',side_effect=[0,6]):
            with self.assertRaisesRegex(DistributionError,'retained for retry'):
                capture._wait_capture_probe_load(api,'proxmox5',policy,timeout=5)
        api.request.reset_mock()
        with self.assertRaisesRegex(DistributionError,'excluded'):
            capture._wait_capture_probe_load(api,'proxmox4',policy)
        api.request.assert_not_called()

    def test_restored_nic_allows_unique_mac_but_no_topology_change(self):
        source={'net0':'e1000=AA:BB:CC:DD:EE:01,bridge=p100t254,firewall=1'}
        copied={'net0':'e1000=AA:BB:CC:DD:EE:02,bridge=p100t254,firewall=1'}
        self.assertTrue(capture._restored_network_matches(copied,source))
        for changed in ({'net0':copied['net0'].replace('p100t254','vmbr0')}, {},
                        {'net0':copied['net0'].replace('e1000=','virtio=')},
                        {'net0':copied['net0'].replace('firewall=1','firewall=0')}):
            self.assertFalse(capture._restored_network_matches(changed,source))

    def test_capture_ownership_and_disconnect_precede_carrier_proof(self):
        events=[];api=Mock()
        fields={'tags':'crange-image;marker;p100','net0':'e1000=AA,bridge=isolated,link_down=1'}
        api.request.side_effect=lambda *args:events.append(('put',args[2]))
        api.vm.return_value=fields
        with patch.object(capture,'_owned',side_effect=lambda *a:events.append(('owned',None))), \
             patch.object(capture,'_require_probe_pool',side_effect=lambda *a:events.append(('pool',None))), \
             patch.object(capture,'private_bridge_request',side_effect=lambda *a,**k:events.append(('proof',None))):
            capture._prepare_capture_network(api,'proxmox5',132,fields,'marker','isolated',carrier=True)
        self.assertEqual([e[0] for e in events],['put','owned','pool','proof','put'])
        self.assertIn('link_down=1',events[0][1]['net0'])
        self.assertIn('link_down=0',events[-1][1]['net0'])

    def test_failed_proof_never_enables_carrier(self):
        api=Mock();fields={'net0':'e1000=AA,bridge=isolated,link_down=1'};api.vm.return_value=fields
        with patch.object(capture,'_owned'),patch.object(capture,'_require_probe_pool'), \
             patch.object(capture,'private_bridge_request',side_effect=DistributionError('proof rejected')):
            with self.assertRaisesRegex(DistributionError,'proof rejected'):
                capture._prepare_capture_network(api,'proxmox5',132,fields,'marker','isolated',carrier=True)
        api.request.assert_called_once_with('PUT','/nodes/proxmox5/qemu/132/config',fields)

    def test_portable_machine_keeps_family_and_requires_live_capability(self):
        api=Mock();api.request.return_value=[{'id':'pc-q35-9.2+pve1','type':'q35'}]
        self.assertEqual(capture._portable_capture_machine(api,'proxmox5',{'machine':'pc-q35-10.1+pve0'}),
                         'pc-q35-9.2+pve1')
        with self.assertRaisesRegex(DistributionError,'unknown machine family'):
            capture._portable_capture_machine(api,'proxmox5',{'machine':'virt'})
        api.request.return_value=[]
        with self.assertRaisesRegex(DistributionError,'portable machine baseline'):
            capture._portable_capture_machine(api,'proxmox5',{'machine':'q35'})

    def test_destination_uses_remote_headroom_when_source_node_is_full(self):
        policy = {'allowed_nodes': ['proxmox1', 'proxmox5']}
        store = {'available': True, 'guest_types': ['vm']}
        inventory = {'nodes': {node: {'online': True, 'features': [],
            'storages': {'local-lvm': store}} for node in policy['allowed_nodes']}}
        ledger = Mock(); ledger.active_reservations.return_value = []
        with patch.object(capture, 'discover', return_value=inventory), \
             patch.object(capture, '_source_capacity', return_value={'cpu_cores': 2, 'memory_mb': 4096, 'disk_gb': 70}), \
             patch.object(capture, 'capture_policy', return_value=(policy, 'isolated', 'local-lvm', 'crange-library')), \
             patch.object(capture, 'ReservationLedger', return_value=ledger), \
             patch('crange_placement.capacity_window', side_effect=[
                 {'cpu_cores': 1, 'memory_mb': 6000, 'disk_gb': -10},
                 {'cpu_cores': 8, 'memory_mb': 12000, 'disk_gb': 400}]):
            result = capture._capture_destination(Mock(), 'proxmox1', {'cpu': 'kvm64'}, {}, policy)
        self.assertEqual(result, 'proxmox5')

    def test_builder_preference_never_overrides_headroom_or_local_capture(self):
        policy = {'allowed_nodes': ['proxmox5', 'proxmox6']}
        store = {'available': True, 'guest_types': ['vm']}
        inventory = {'nodes': {node: {'online': True, 'load': 0.5 if node == 'proxmox5' else 0,
            'storages': {'local-lvm': store}} for node in policy['allowed_nodes']}}
        ledger = Mock(); ledger.active_reservations.return_value = []
        enough = {'cpu_cores': 8, 'memory_mb': 12000, 'disk_gb': 400}
        small = {**enough, 'memory_mb': 1000}
        for source, windows, expected in [('proxmox4', [enough, enough], 'proxmox5'),
                                          ('proxmox4', [small, enough], 'proxmox6'),
                                          ('proxmox6', [enough, enough], 'proxmox6')]:
            with self.subTest(source=source, expected=expected), \
                 patch.object(capture, 'discover', return_value=inventory), \
                 patch.object(capture, '_source_capacity', return_value={'cpu_cores': 2, 'memory_mb': 2048, 'disk_gb': 22}), \
                 patch.object(capture, 'capture_policy', return_value=(policy, 'isolated', 'local-lvm', 'crange-library')), \
                 patch.object(capture, 'ReservationLedger', return_value=ledger), \
                 patch('crange_placement.capacity_window', side_effect=windows):
                result = capture._capture_destination(Mock(), source, {'cpu': 'kvm64'},
                    {'_capture_preferred_nodes': ['proxmox5', 'proxmox6']}, policy)
                self.assertEqual(result, expected)

    def transport(self, *, owner='p100', archives=1, failed_backup=False, resume=False):
        api = Mock()
        api.vm.return_value = {'tags': owner}
        api.status.return_value = 'stopped'
        archive_rows = [
            {'volid': f'crange-library:backup/vzdump-qemu-128-2026_10_12-12_00_0{i}.vma.zst',
             'vmid': 128, 'content': 'backup'} for i in range(archives)]
        api.request.side_effect = [archive_rows] if resume else [[], archive_rows]
        task = Mock(side_effect=RuntimeError('backup failed') if failed_backup else None)
        receipt = {'job_id': 'capture-p100-example', 'capture_name': 'captured-dc'}
        if resume:
            receipt['transport'] = {'content_before': []}
        with patch.object(capture, 'SSHVolumeHasher', return_value=Mock(return_value='a' * 64)):
            capture._capture_transport(api, source_node='proxmox1', source_id=128,
                target_node='proxmox5', target_id=131, storage='local-lvm', backup='crange-library',
                project=100, receipt=receipt, save=Mock(), task=task)
        return task, receipt

    def test_completed_frozen_backup_is_reused_without_duplicate_submission(self):
        task, receipt = self.transport(resume=True)
        self.assertEqual(task.call_count, 1)
        self.assertEqual(task.call_args.args[1], '/nodes/proxmox5/qemu')
        self.assertEqual(receipt['transport']['state'], 'verified')

    def test_restore_is_stopped_and_in_probe_pool_after_verified_backup(self):
        task, receipt = self.transport()
        self.assertEqual(task.call_args_list[0].kwargs, {'task_node': 'proxmox1'})
        fields = task.call_args_list[1].args[2]
        self.assertEqual(fields['start'], 0)
        self.assertEqual(fields['unique'], 1)
        self.assertEqual(fields['pool'], 'crange-probes')
        self.assertEqual(fields['vmid'], 131)
        self.assertEqual(receipt['transport']['artifact_sha256'], 'a' * 64)

    def test_wrong_project_source_is_rejected(self):
        with self.assertRaisesRegex(DistributionError, 'owned stopped'):
            self.transport(owner='p101')

    def test_ambiguous_archive_is_rejected(self):
        with self.assertRaisesRegex(DistributionError, 'ambiguous'):
            self.transport(archives=2)

    def test_failed_backup_prevents_restore(self):
        with self.assertRaisesRegex(RuntimeError, 'backup failed'):
            self.transport(failed_backup=True)
