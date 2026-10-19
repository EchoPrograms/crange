import copy
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_cluster


class CapacityPolicyTests(unittest.TestCase):
    def test_fixed_policy_update_changes_only_ratios_and_preserves_physical_headroom(self):
        policy = {'revision': 'mhl-cluster-cpu-reserve-20261008', 'enabled': True,
            'allowed_nodes': ['proxmox1'], 'allowed_storages': ['local-lvm'],
            'approved_storage': [['proxmox1', 'local-lvm']], 'clone_mode': 'linked',
            'cpu_overcommit_ratio': 3.5, 'memory_overcommit_ratio': 2.0,
            'local_linked_disk_overcommit_ratio': 1.5,
            'nodes': {'proxmox1': {'cpu_reserve_cores': 2}},
            'memory_reserve_fraction': .25, 'memory_reserve_mb': 4096,
            'disk_reserve_fraction': .2, 'disk_reserve_gb': 20}
        original = copy.deepcopy(policy)
        inventory = {'nodes': {'proxmox1': {'online': True, 'cpu_cores': 4, 'memory_mb': 16384,
            'observed_memory_mb': 2048, 'committed_cpu_cores': 6, 'committed_memory_mb': 8192,
            'storages': {'local-lvm': {'total_gb': 100, 'free_gb': 50,
                'committed_gb': 20, 'shared': False, 'snapshots_verified': True}}}}}
        writes = []
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, 'distribution-policy.json').write_text('{"fixture":true}')
            with patch('crange_scheduling.CONFIG', Path(temp)), \
             patch('crange_scheduling.read_settings', return_value=(policy, {'schema_version': 1, 'images': {}})), \
             patch('crange_cluster.hashlib.sha256', return_value=unittest.mock.Mock(
                 hexdigest=lambda: 'bb1328bf74b86d5971333cd19590c011129f820b67b55fb444e5ffb649cce81b')), \
             patch('crange_scheduling.discover', return_value=inventory), \
             patch('crange.ProxmoxAPI'), \
             patch.object(crange_cluster.scheduling, 'atomic_json', side_effect=lambda _p, value, **_kw: writes.append(copy.deepcopy(value))):
                result = crange_cluster.expand_capacity_policy(5, 2, 2)
        self.assertEqual(result['before_headroom']['proxmox1']['cpu_cores'], 1)
        self.assertEqual(result['after_headroom']['proxmox1']['cpu_cores'], 4)
        self.assertEqual(result['before_headroom']['proxmox1']['local_lvm_disk_gb'], 30)
        self.assertEqual(result['after_headroom']['proxmox1']['local_lvm_disk_gb'], 30)
        self.assertEqual(result['after_headroom']['proxmox1']['memory_mb'],
                         result['before_headroom']['proxmox1']['memory_mb'])
        self.assertEqual(writes[0]['allowed_nodes'], original['allowed_nodes'])
        self.assertEqual(writes[0]['allowed_storages'], original['allowed_storages'])
        self.assertEqual(writes[0]['approved_storage'], original['approved_storage'])
        self.assertEqual(writes[0]['nodes'], original['nodes'])
        self.assertEqual(writes[0]['memory_reserve_mb'], original['memory_reserve_mb'])
        self.assertEqual(writes[0]['disk_reserve_gb'], original['disk_reserve_gb'])
        self.assertEqual(writes[0]['clone_mode'], original['clone_mode'])

    def test_policy_update_rejects_invalid_ratios_and_malformed_existing_limits(self):
        with self.assertRaisesRegex(ValueError, 'reviewed CPU'):
            crange_cluster.expand_capacity_policy(6.25, 2, 2)
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, 'distribution-policy.json').write_text('{"fixture":true}')
            with patch('crange_scheduling.CONFIG', Path(temp)), patch('crange_scheduling.read_settings', return_value=(
                {'revision': 'changed-by-other-admin', 'nodes': {}}, {'schema_version': 1, 'images': {}})), \
             patch('crange_cluster.hashlib.sha256', return_value=unittest.mock.Mock(hexdigest=lambda: '0' * 64)):
                with self.assertRaisesRegex(ValueError, 'Existing capacity limits'):
                    crange_cluster.expand_capacity_policy(5, 2, 2)

    def test_valid_network_revision_can_update_ratios_without_losing_acceptance(self):
        policy={'revision':'capture-settings-current+network-accepted','nodes':{},
                'allowed_nodes':['proxmox1'],'cpu_overcommit_ratio':6,
                'memory_overcommit_ratio':2,'local_linked_disk_overcommit_ratio':2,
                'network_attestations':{'proxmox1':{'vnets':['p100c100']}}}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'distribution-policy.json';path.write_text(__import__('json').dumps(policy))
            with patch('crange_scheduling.CONFIG',root), \
                 patch('crange_scheduling.read_settings',return_value=(policy,{'schema_version':1,'images':{}})), \
                 patch('crange_scheduling.discover',return_value={'nodes':{}}),patch('crange.ProxmoxAPI'):
                result=crange_cluster.expand_capacity_policy(5,2,2)
            saved=__import__('json').loads(path.read_text())
        self.assertEqual(result['state'],'updated')
        self.assertEqual(saved['network_attestations'],policy['network_attestations'])
