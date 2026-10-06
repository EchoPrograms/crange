from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_cluster as cluster
import crange_scheduling as scheduling


class ClusterTests(unittest.TestCase):
    def test_cluster_reader_is_admin_only_and_never_discovers(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(scheduling, 'DATA', Path(directory)), \
                patch.object(scheduling, 'discover') as discover:
            with self.assertRaises(PermissionError): cluster.inventory_snapshot()
            self.assertTrue(cluster.inventory_snapshot(admin=True)['stale'])
            discover.assert_not_called()

    def test_refresh_publishes_sanitized_totals_with_reservations_and_headroom(self):
        raw = {'captured_at': 100, 'nodes': {'proxmox4': {
            'online': True, 'memory_mb': 16384, 'cpu_cores': 8, 'committed_memory_mb': 4096,
            'observed_memory_mb': 3000, 'committed_cpu_cores': 1, 'management_verified': True,
            'verified_networks': ['crtrans'], 'storages': {'local-lvm': {
                'available': True, 'total_gb': 200, 'free_gb': 150, 'committed_gb': 50,
                'snapshots_verified': True, 'clone_modes': ['linked'], 'shared': False}}}},
            'guests': {'200': {'node_name': 'proxmox4', 'kind': 'qemu', 'name': 'base', 'status': 'stopped',
                'template': True, 'memory_mb': 512, 'cpu_cores': 1, 'disks': {'local-lvm': 8},
                'token': 'never-publish-this', 'tags': 'private'}}}
        policy = {'revision': 'v1', 'enabled': True, 'allowed_storages': ['local-lvm']}
        catalog = {'images': {'base-v1': {'kind': 'vm', 'disk_gb': 8,
            'approved_projects': [3], 'artifact': {'complete_capture': True},
            'replicas': [{'node': 'proxmox4', 'datastore_id': 'local-lvm', 'template_id': 200,
                          'state': 'ready', 'cache_local': True,
                          'clone_modes_verified': ['linked']}]}}}
        reservation = {'node_name': 'proxmox4', 'datastore_id': 'local-lvm', 'memory_mb': 1024,
                       'cpu_cores': 1, 'disk_gb': 10}
        ledger = Mock(active_reservations=Mock(return_value=[reservation]))
        with tempfile.TemporaryDirectory() as directory, patch.object(scheduling, 'DATA', Path(directory)), \
             patch.object(scheduling, 'read_settings', return_value=(policy, catalog)), \
             patch.object(scheduling, 'discover', return_value=raw), \
             patch.object(cluster, 'ReservationLedger', return_value=ledger):
            result = cluster.refresh_inventory_snapshot(api=Mock())
            node = result['nodes'][0]
            self.assertEqual(node['availability']['memory_mb'], 7168)
            self.assertEqual(node['capacity']['storages'][0]['available_gb'], 100)
            self.assertEqual(node['guests'][0]['status'], 'stopped')
            self.assertTrue(node['guests'][0]['cache'])
            self.assertEqual(result['images'][0]['locations'][0]['replicas'][0]['template_id'], 200)
            self.assertTrue(result['images'][0]['locations'][0]['replicas'][0]['observed'])
            self.assertEqual(result['images'][0]['approved_projects'], [3])
            self.assertTrue(result['images'][0]['archived'])
            self.assertEqual(result['images'][0]['locations'][0]['node'], 'proxmox4')
            self.assertNotIn('never-publish-this', json.dumps(result))
            path = Path(directory) / 'cluster-inventory.json'
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            with patch.object(cluster.time, 'time', return_value=500):
                self.assertTrue(cluster.inventory_snapshot(admin=True)['stale'])

    def test_failed_refresh_retains_snapshot_and_exposes_sanitized_failure(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(scheduling, 'DATA', Path(directory)):
            snapshot = {'captured_at': 100, 'max_inventory_age_seconds': 120, 'nodes': [], 'guests': []}
            (Path(directory) / 'cluster-inventory.json').write_text(json.dumps(snapshot))
            with patch.object(cluster, '_refresh_inventory_snapshot', side_effect=RuntimeError('private-token-value')):
                with self.assertRaises(RuntimeError): cluster.refresh_inventory_snapshot(api=Mock())
            with patch.object(cluster.time, 'time', return_value=101):
                result = cluster.inventory_snapshot(admin=True)
            self.assertTrue(result['stale'])
            self.assertEqual(result['captured_at'], 100)
            self.assertNotIn('private-token-value', json.dumps(result))
            self.assertEqual(result['refresh_error']['error_type'], 'RuntimeError')

    def test_new_jumpbox_default_is_soft_and_explicit_empty_is_respected(self):
        self.assertEqual(scheduling.intent_for({'placement': {'mode': 'auto'}}, 'proxmox4')['preferred_node'], 'proxmox4')
        self.assertEqual(scheduling.intent_for({'placement': {'mode': 'auto', 'preferred_node': ''}}, 'proxmox4')['preferred_node'], '')
        self.assertEqual(scheduling.intent_for({'placement': {'mode': 'pin', 'pinned_node': 'proxmox2'}}, 'proxmox4')['pinned_node'], 'proxmox2')
