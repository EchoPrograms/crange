"""Release publication must preserve worker evidence and captured image identity."""
import base64
import copy
import struct
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_catalog import CatalogError, publish_release_image, retire_image
import crange


class ReleaseCatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {'schema_version': 1, 'images': {}}
        self.alias = 'p003-image-captured'
        self.release = {'project_id': 3, 'release_id': 'p003-1.0', 'source_revision': 'revision',
            'integrity_sha256': 'd' * 64, 'scenario': {'templates': {self.alias: {
                'kind': 'vm', 'template_node': 'proxmox4', 'template_id': 200,
                'image_id': self.alias}}}}
        raw = struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32) + b'a' * 32
        identity = {'machine_id': 'a' * 32, 'ssh_host_key': 'ssh-ed25519 ' + base64.b64encode(raw).decode()}
        capture = {'state': 'identity-captured', 'job_id': 'capture', 'node': 'proxmox4',
            'kind': 'vm', 'source_id': 200, 'source_config_sha256': 'b' * 64,
            'source_identity': identity, 'init_system': 'systemd', 'readiness': 'running'}
        self.export = {'state': 'exported', 'job_id': 'export', 'node': 'proxmox4',
            'verified': True, 'upid': 'UPID:export',
            'kind': 'vm', 'source_id': 200, 'disk_gb': 8, 'complete_capture': True,
            'identity_capture': capture, 'source_identity': identity, 'init_system': 'systemd',
            'volume_id': 'crange-library:backup/vzdump-qemu-200.zst', 'artifact_sha256': 'c' * 64}
        self.probe = {'state': 'source-probed', 'job_id': 'probe', 'kind': 'vm',
            'source_node': 'proxmox4', 'source_id': 200, 'probe_id': 201,
            'source_config_sha256': 'b' * 64, 'source_capture_job_id': 'capture',
            'datastore_id': 'local-lvm', 'reservation': {'operation': 'source_probe',
                'project_id': 3, 'artifact_sha256': 'b' * 64},
            'provider_tasks': [{'state': 'confirmed-terminal',
                'path': '/nodes/proxmox4/qemu/201', 'upid': 'UPID:delete'}],
            'verification': {'passed': True, 'identity_unique': True, 'evidence': {'readiness': 'running'}}}

    def publish(self):
        return publish_release_image(self.catalog, self.release, self.alias,
                                     self.export, self.probe, source_datastore='crange-library')

    def test_scoped_publication_uses_actual_source_storage_and_retains_release(self):
        result = self.publish()
        image = result['images'][self.alias]
        self.assertEqual(image['approved_projects'], [3])
        self.assertEqual(image['replicas'][0]['datastore_id'], 'crange-library')
        self.assertEqual(image['artifact_sha256'], 'c' * 64)
        self.assertEqual(image['release']['manifest_sha256'], 'd' * 64)
        self.assertEqual(self.catalog['images'], {})
        with self.assertRaisesRegex(CatalogError, 'retained release'):
            retire_image(result, self.alias)
        self.catalog = result
        with self.assertRaisesRegex(CatalogError, 'immutable'):
            self.publish()

    def test_legacy_manifest_alias_can_be_published_without_manifest_rewrite(self):
        del self.release['scenario']['templates'][self.alias]['image_id']
        self.assertIn(self.alias, self.publish()['images'])

    def test_unrelated_export_or_probe_is_rejected(self):
        for record, key, value in ((self.export, 'source_id', 199),
                (self.probe, 'source_id', 199), (self.probe, 'source_capture_job_id', 'other'),
                (self.probe, 'source_config_sha256', 'e' * 64)):
            with self.subTest(key=key):
                before = record[key]; record[key] = value
                with self.assertRaises(CatalogError): self.publish()
                record[key] = before

    def test_incomplete_probe_cleanup_and_unverified_identity_are_rejected(self):
        original = copy.deepcopy(self.probe)
        for mutate in (lambda p: p.update(state='verifying'),
                       lambda p: p.update(provider_tasks=[]),
                       lambda p: p['provider_tasks'][0].update(state='running'),
                       lambda p: p['provider_tasks'][0].update(path='/wrong'),
                       lambda p: p['verification'].update(identity_unique=False),
                       lambda p: p['reservation'].update(project_id=4)):
            self.probe = copy.deepcopy(original); mutate(self.probe)
            with self.assertRaises(CatalogError): self.publish()

    def test_noncentral_archive_is_rejected(self):
        self.export['volume_id'] = 'local:backup/vzdump-qemu-200.zst'
        with self.assertRaisesRegex(CatalogError, 'central library'): self.publish()

    def test_unverified_export_is_rejected(self):
        self.export['verified'] = False
        with self.assertRaisesRegex(CatalogError, 'verified export'): self.publish()

    def test_declarations_pin_captured_image_and_do_not_inherit_base_identity(self):
        ref = self.release['scenario']['templates'][self.alias]
        self.release['scenario']['boxes'] = [{'name': 'web', 'ip_last_octet': 20,
            'template': self.alias, 'image': 'base-v1', 'node_name': 'proxmox4',
            'cores': 1, 'memory_mb': 512, 'full_clone': True, 'started': True, 'health_checks': []}]
        with patch.object(crange, 'json_config', return_value={}), \
                patch.object(crange, 'templates_for', return_value={self.alias: ref}):
            first = next(iter(crange.release_scenario_boxes(Path('/unused'), self.release, '1', 'r1').values()))
            second = next(iter(crange.release_scenario_boxes(Path('/unused'), self.release, '2', 'r1').values()))
        self.assertEqual(first['image'], self.alias)
        self.assertEqual(second['image'], self.alias)
        self.assertEqual(first['team_id'], '1'); self.assertEqual(second['team_id'], '2')

    def test_container_declaration_pins_captured_image(self):
        ref = self.release['scenario']['templates'][self.alias]
        ref['kind'] = 'container'
        self.release['scenario']['containers'] = [{'name': 'svc', 'ip_last_octet': 20,
            'template': self.alias, 'image': 'base-ct-v1', 'node_name': 'proxmox4',
            'cores': 1, 'memory_mb': 512, 'started': True}]
        with patch.object(crange, 'container_templates_for', return_value={self.alias: ref}):
            box = next(iter(crange.release_scenario_containers(Path('/unused'), self.release, '2', 'r1').values()))
        self.assertEqual(box['image'], self.alias)
        self.assertEqual(box['team_id'], '2')


if __name__ == '__main__': unittest.main()
