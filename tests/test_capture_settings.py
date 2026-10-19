import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange
import crange_scheduling as scheduling
import crange_capture


class CaptureSettingsTests(unittest.TestCase):
    def test_node_specific_ssd_approval_preserves_other_nodes(self):
        policy = {'enabled': True, 'allowed_nodes': ['proxmox1', 'proxmox5'],
            'nodes': {'proxmox1': {'snapshot_storages': ['local-lvm']},
                      'proxmox5': {'snapshot_storages': ['local-lvm']}},
            'isolated_bridges': ['isolated'], 'allowed_storages': ['local-lvm', 'ssd-lvm'],
            'approved_storage': [['proxmox1', 'local-lvm'], ['proxmox5', 'local-lvm']],
            'approved_backup_storage': [['proxmox1', 'crange-library'], ['proxmox5', 'crange-library']]}
        api = Mock()
        def request(method, path, fields=None):
            if path == '/cluster/resources':
                return [{'node': 'proxmox5', 'storage': 'ssd-lvm'}]
            if '/ssd-lvm/' in path:
                self.assertIn('/proxmox5/', path)
                return {'active': 1, 'type': getattr(self, 'storage_type', 'lvmthin'), 'content': 'images,rootdir'}
            return {'active': 1, 'shared': 1, 'content': 'backup'}
        api.request.side_effect = request
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(scheduling, 'CONFIG', Path(temp)), \
                patch.object(scheduling, 'read_settings', return_value=(policy, {})), \
                patch.object(scheduling, 'atomic_json') as save, \
                patch.object(crange, 'ProxmoxAPI', return_value=api):
            crange.cmd_capture_settings(argparse.Namespace(bridge='isolated', storage='ssd-lvm', backup='crange-library'))
        updated = save.call_args.args[1]
        self.assertIn(['proxmox1', 'local-lvm'], updated['approved_storage'])
        self.assertIn(['proxmox5', 'ssd-lvm'], updated['approved_storage'])
        self.assertNotIn(['proxmox1', 'ssd-lvm'], updated['approved_storage'])
        self.assertEqual(updated['capture_datastore'], 'ssd-lvm')
        self.assertEqual(crange_capture.capture_policy('proxmox1', {'agent_enabled': True}, updated)[2], 'local-lvm')
        self.assertEqual(crange_capture.capture_policy('proxmox5', {'agent_enabled': True}, updated)[2], 'ssd-lvm')
        self.assertEqual(crange_capture.capture_policy('proxmox5', {}, updated, kind='container')[2], 'local-lvm')

    def test_thick_lvm_supports_vm_copies_but_not_container_linked_capture(self):
        self.storage_type = 'lvm'
        self.test_node_specific_ssd_approval_preserves_other_nodes()

    def test_new_node_hdd_capture_requires_exact_storage_and_snapshot_approval(self):
        policy = {'enabled': True, 'allowed_nodes': ['proxmox6'],
            'capture_datastore': 'local-hdd', 'allowed_storages': ['local-hdd'],
            'capture_isolated_bridge': 'isolated', 'isolated_bridges': ['isolated'],
            'capture_backup_storage': 'crange-library',
            'approved_storage': [['proxmox6','local-hdd']],
            'approved_backup_storage': [['proxmox6','crange-library']],
            'nodes': {'proxmox6': {'snapshot_storages': ['local-hdd']}}}
        self.assertEqual(crange_capture.capture_policy('proxmox6', {}, policy, kind='container')[2], 'local-hdd')
        policy['nodes']['proxmox6']['snapshot_storages'] = []
        with self.assertRaises(ValueError):
            crange_capture.capture_policy('proxmox6', {}, policy, kind='container')

    def test_missing_capture_default_does_not_silently_enable_capture(self):
        policy = {'enabled': True, 'allowed_nodes': ['proxmox1'],
            'capture_isolated_bridge': 'isolated', 'isolated_bridges': ['isolated'],
            'approved_storage': [['proxmox1', 'local-lvm']], 'allowed_storages': ['local-lvm']}
        with self.assertRaisesRegex(ValueError, 'Configure approved capture_datastore'):
            crange_capture.capture_policy('proxmox1', {'agent_enabled': True}, policy)
