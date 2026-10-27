import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_scheduling as scheduling
from crange_scheduling import tf_overrides
from crange_jobs import command


class ScenarioVmDiskTests(unittest.TestCase):
    def test_request_uses_image_minimum_and_grows_only(self):
        profile = {'interface': 'sata0', 'disk_gb': 64.0, 'source_config_sha256': 'a' * 64}
        row = {'kind': 'vm', 'resource_key': 'vm:t254-web', 'box': {'disk_gb': 64}}
        image = {'disk_gb': 64.00050354003906, 'system_disk': profile}
        self.assertEqual(scheduling.requested_disk_gb(row, image), 64)
        for value in (True, 7, 31, 64.5, 1025):
            row['box']['disk_gb'] = value
            with self.assertRaises(scheduling.PlacementError):
                scheduling.requested_disk_gb(row, image)
        row['box']['disk_gb'] = 32
        with self.assertRaisesRegex(scheduling.PlacementError, 'OS disk size'):
            scheduling.requested_disk_gb(row, image)

        # Captured images can include EFI allocation in aggregate size. Their
        # verified boot-selected OS disk is the precise custom-size minimum.
        for total, os_disk in ((31.00390625, 31.0), (30.00390625, 30.0)):
            row['box']['disk_gb'] = int(os_disk)
            captured = {'disk_gb': total, 'system_disk': {**profile, 'disk_gb': os_disk}}
            self.assertEqual(scheduling.requested_disk_gb(row, captured), int(os_disk))
        row['box']['disk_gb'] = 30
        with self.assertRaisesRegex(scheduling.PlacementError, 'OS disk size'):
            scheduling.requested_disk_gb(row, {'disk_gb': 31.00390625,
                'system_disk': {**profile, 'disk_gb': 31.0}})

        # A retained guest keeps its applied image lineage after an approved
        # physical shrink. An unchanged declaration may stay at that verified
        # live size; fresh clones and replacements retain the image minimum.
        row['box']['disk_gb'] = 31
        retained = {'disk_gb': 64.00050354003906, 'system_disk': profile}
        self.assertEqual(scheduling.requested_disk_gb(
            {**row, 'minimum_system_disk_gb': 31.0}, retained), 31)
        with self.assertRaisesRegex(scheduling.PlacementError, 'OS disk size'):
            scheduling.requested_disk_gb(row, retained)
        row['box']['disk_gb'] = 40
        self.assertEqual(scheduling.requested_disk_gb(
            {**row, 'minimum_system_disk_gb': 31.0}, retained), 40)

        row['box']['disk_gb'] = 80
        self.assertTrue(scheduling.disk_growth_requested(row, {'disk': [{'size': 64}]}))
        self.assertFalse(scheduling.disk_growth_requested(row, {'disk': [{'size': 80}]}))
        with self.assertRaisesRegex(scheduling.PlacementError, 'cannot shrink'):
            scheduling.disk_growth_requested(row, {'disk': [{'size': 96}]})

    def test_job_field_is_optional_for_old_queued_requests(self):
        base = {'name': 'web', 'team': 0, 'ip': 20, 'network': '', 'template': 'approved-base',
                'node': 'auto', 'cores': 2, 'memory': 4096, 'health': [], 'guac_protocols': ['ssh']}
        old = command('scenario-add', 3, base)
        self.assertNotIn('--disk', old)
        new = command('scenario-add', 3, {**base, 'disk': 64})
        self.assertEqual(new[-2:], ['--disk', '64'])
        edit = {k: v for k, v in base.items() if k != 'team'} | {'name': 't254-web'}
        self.assertNotIn('--disk', command('scenario-edit', 3, edit))
        with self.assertRaises(ValueError):
            command('scenario-edit', 3, {**edit, 'disk': True})

    def test_sata_system_disk_interface_reaches_terraform_overrides(self):
        prepared = {
            'placements': [{'resource_key': 'vm:t254-windows', 'image': 'windows-v1',
                'resolved': {'node_name': 'proxmox2', 'datastore_id': 'local-lvm',
                             'disk_interface': 'sata0'},
                'resources': {'disk_gb': 40}}],
            'requests': [{'resource_key': 'vm:t254-windows', 'kind': 'vm',
                          'disk_gb': 40, 'disk_interface': 'sata0'}],
        }
        override = tf_overrides(prepared)['resolved_placements']['vm:t254-windows']
        self.assertEqual(override['disk_gb'], 40)
        self.assertEqual(override['disk_interface'], 'sata0')
        module = (Path(__file__).resolve().parents[1] /
                  'terraform/modules/scenario-vm/main.tf').read_text()
        self.assertIn('interface    = var.box.disk_interface', module)
        self.assertNotIn('interface    = "scsi0"', module)

    def test_legacy_vm_without_verified_interface_does_not_emit_disk_override(self):
        prepared = {
            'placements': [{'resource_key': 'vm:t001-old', 'image': 'legacy-v1',
                'resolved': {'node_name': 'proxmox1', 'datastore_id': 'local-lvm'},
                'resources': {'disk_gb': 64}}],
            'requests': [{'resource_key': 'vm:t001-old', 'kind': 'vm', 'disk_gb': 64}],
        }
        override = tf_overrides(prepared)['resolved_placements']['vm:t001-old']
        self.assertNotIn('disk_gb', override)
        self.assertNotIn('disk_interface', override)


if __name__ == '__main__':
    unittest.main()
