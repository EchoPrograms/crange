import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_scheduling as scheduling
from crange_templates import media_tf_overrides


class ScopedVmDiskPreservationTests(unittest.TestCase):
    def test_staging_review_preserves_unselected_jumpbox_disk_and_source(self):
        key = 'jumpbox:t001-j01'
        row = {'resource_key': key, 'kind': 'vm', 'image': 'new-image',
               'box': {'template': 'approved', 'disk_gb': 64}, 'reference': {}}
        live = {'vm_id': 301, 'node_name': 'proxmox6',
                'boot_order': ['scsi0', 'ide2', 'net0'],
                'disk': [{'interface': 'scsi0', 'size': 48,
                          'datastore_id': 'local-lvm'}]}
        prior = {'guest_id': 301, 'node_name': 'proxmox6',
                 'image_id': 'old-image', 'declared_template': 'approved',
                 'template_id': 219, 'template_node': 'proxmox6'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'crange-applied-placements.json').write_text(json.dumps({key: prior}))
            with patch.object(scheduling, 'declarations', return_value=[row]), \
                 patch.object(scheduling, 'applied_resources', return_value={key: live}):
                result = media_tf_overrides(path, {}, {'placements': []},
                                           api=Mock(), scoped=True)['resolved_placements'][key]
        self.assertEqual(result['disk_gb'], 48)
        self.assertEqual(result['disk_interface'], 'scsi0')
        self.assertEqual(result['image_id'], 'old-image')
        self.assertEqual(result['template_id'], 219)
        self.assertEqual(row['box']['disk_gb'], 64)

    def test_unproven_disk_interface_remains_blocked(self):
        row = {'resource_key': 'vm:legacy', 'kind': 'vm', 'box': {'disk_gb': 48}}
        disk = {'interface': 'sata0', 'size': 48, 'datastore_id': 'local-lvm'}
        for disks, boot in (([disk], []), ([disk], ['scsi0']),
                            ([disk, {**disk, 'interface': 'sata1'}], ['sata0']),
                            ([{**disk, 'interface': 'invalid'}], ['invalid'])):
            with self.subTest(disks=disks, boot=boot), \
                 self.assertRaisesRegex(scheduling.PlacementError, 'verified system-disk interface'):
                scheduling.freeze(row, {'node_name': 'proxmox1', 'disk': disks,
                                        'boot_order': boot})


if __name__ == '__main__':
    unittest.main()
