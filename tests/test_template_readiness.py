import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_templates


class TemplateReadinessTests(unittest.TestCase):
    def test_readiness_requires_a_catalog_id_scope_kind_and_ready_replica(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'approved-scenario-templates.json').write_text(json.dumps({
                'legacy': {'display_name': 'Parrot base', 'template_id': 112},
                'ready': {'display_name': 'Portable Alpine', 'image_id': 'alpine-v1'},
                'wrong-scope': {'image_id': 'private-v1'},
                'wrong-kind': {'image_id': 'ct-v1'},
                'no-replica': {'image_id': 'missing-replica-v1'},
            }))
            (root / 'approved-container-templates.json').write_text('{}')
            catalog = {'images': {
                'alpine-v1': {'kind': 'vm', 'approved_projects': ['*'],
                    'system_disk': {'interface': 'sata0', 'disk_gb': 31.0, 'source_config_sha256': 'a' * 64}, 'replicas': [
                    {'node': 'proxmox2', 'state': 'ready'}]},
                'private-v1': {'kind': 'vm', 'approved_projects': [3], 'replicas': [
                    {'node': 'proxmox4', 'state': 'ready'}]},
                'ct-v1': {'kind': 'container', 'approved_projects': ['*'], 'replicas': [
                    {'node': 'proxmox2', 'state': 'ready'}]},
                'missing-replica-v1': {'kind': 'vm', 'approved_projects': ['*'], 'replicas': [
                    {'node': 'proxmox2', 'state': 'missing'}]},
            }}
            with patch('crange.project_dir', return_value=root), \
                 patch('crange_scheduling.read_settings', return_value=({}, catalog)):
                result = crange_templates.project_image_readiness(1)['vm_templates']
            self.assertEqual(result['legacy']['status'], 'missing-image-id')
            self.assertEqual(result['ready']['status'], 'ready')
            self.assertEqual(result['ready']['ready_nodes'], ['proxmox2'])
            self.assertEqual(result['ready']['system_disk_gb'], 31.0)
            self.assertNotIn('system_disk_gb', result['legacy'])
            self.assertEqual(result['wrong-scope']['status'], 'not-approved')
            self.assertEqual(result['wrong-kind']['status'], 'kind-mismatch')
            self.assertEqual(result['no-replica']['status'], 'no-ready-replica')

    def test_admin_links_only_verified_catalog_image_to_a_separate_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'project-001'
            root.mkdir()
            approved = {'approved-base': {'template_id': 112, 'display_name': 'Parrot base'}}
            (root / 'approved-scenario-templates.json').write_text(json.dumps(approved))
            replica = {'state': 'ready', 'artifact_sha256': 'a' * 64, 'cache_local': True,
                       'clone_modes_verified': ['linked'], 'template_id': 147,
                       'node': 'proxmox2', 'datastore_id': 'local-lvm',
                       'verification': {'passed': True, 'evidence': {
                           'machine_id': 'replica-id', 'ssh_host_key': 'ssh-ed25519 replica-key'}}}
            image = {'kind': 'vm', 'verified': True, 'approved_projects': ['*'],
                     'artifact_sha256': 'a' * 64,
                     'identity_capture': {'state': 'identity-captured', 'kind': 'vm',
                                          'source_config_sha256': 'b' * 64,
                                          'source_identity': {'machine_id': 'source-id',
                                                              'ssh_host_key': 'ssh-ed25519 source-key'}},
                     'replicas': [replica]}
            api = Mock()
            api.request.return_value = {'template': 1, 'name': 'portable-alpine',
                                        'tags': 'crange-cache-147;crange-image'}
            with patch('crange.project_dir', return_value=root), \
                 patch('crange_scheduling.read_settings', return_value=({}, {'images': {'alpine-v1': image}})):
                linked = crange_templates.approve_catalog_image('alpine-v1', [1], api)
            result = json.loads((root / 'approved-scenario-templates.json').read_text())
            self.assertEqual(linked['template_id'], 147)
            self.assertEqual(linked['template_node'], 'proxmox2')
            self.assertEqual(result['approved-base'], approved['approved-base'])
            self.assertEqual(result['alpine-v1']['image_id'], 'alpine-v1')
            self.assertEqual(result['alpine-v1']['template_id'], 147)
            api.request.assert_called_once_with('GET', '/nodes/proxmox2/qemu/147/config')
            # Another ready cache must not rewrite an already approved alias.
            image['replicas']=[{**replica,'node':'proxmox3','template_id':148}]
            api.request.return_value={'template':1,'tags':'crange-cache-148;crange-image'}
            previous=result['alpine-v1']
            with patch('crange.project_dir',return_value=root), \
                    patch('crange_scheduling.read_settings',return_value=({}, {'images':{'alpine-v1':image}})):
                again=crange_templates.approve_catalog_image('alpine-v1',[1],api)
            self.assertEqual(json.loads((root/'approved-scenario-templates.json').read_text())['alpine-v1'],previous)
            self.assertEqual(again['template_id'],147)

    def test_admin_link_rejects_unapproved_or_unverified_images(self):
        base = {'kind': 'vm', 'verified': True, 'approved_projects': [1],
                'artifact_sha256': 'a' * 64,
                'identity_capture': {'state': 'identity-captured', 'kind': 'vm',
                                     'source_config_sha256': 'b' * 64,
                                     'source_identity': {'machine_id': 'source-id',
                                                         'ssh_host_key': 'ssh-ed25519 source-key'}},
                'replicas': [{'state': 'ready', 'artifact_sha256': 'a' * 64,
                              'cache_local': True, 'clone_modes_verified': ['linked'],
                              'template_id': 147, 'node': 'proxmox2',
                              'verification': {'passed': True, 'evidence': {
                                  'machine_id': 'replica-id', 'ssh_host_key': 'ssh-ed25519 replica-key'}}}]}
        with patch('crange_scheduling.read_settings', return_value=({}, {'images': {'alpine-v1': base}})):
            with self.assertRaisesRegex(ValueError, 'not approved for every selected project'):
                crange_templates.approve_catalog_image('alpine-v1', [2], Mock())
        base['approved_projects'] = ['*']
        base['verified'] = False
        with patch('crange_scheduling.read_settings', return_value=({}, {'images': {'alpine-v1': base}})):
            with self.assertRaisesRegex(ValueError, 'verified VM or container image'):
                crange_templates.approve_catalog_image('alpine-v1', [1], Mock())

    def test_container_approval_uses_lxc_and_preserves_vm_approvals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'approved-scenario-templates.json').write_text('{"existing-vm": {}}')
            (root / 'approved-container-templates.json').write_text('{}')
            image = {'kind': 'container', 'verified': True, 'approved_projects': ['*'],
                     'init_system': 'openrc', 'display_name': 'Alpine Container', 'artifact_sha256': 'a' * 64,
                     'identity_capture': {'state': 'identity-captured', 'kind': 'container',
                        'source_config_sha256': 'b' * 64,
                        'source_identity': {'machine_id': 'original', 'ssh_host_key': 'original-key'}},
                     'replicas': [{'state': 'ready', 'artifact_sha256': 'a' * 64, 'cache_local': True,
                        'clone_modes_verified': ['linked'], 'template_id': 147, 'node': 'proxmox2',
                        'datastore_id': 'local-lvm', 'verification': {'passed': True,
                            'evidence': {'machine_id': 'clone', 'ssh_host_key': 'clone-key'}}}]}
            api = Mock()
            api.request.return_value = {'template': 1, 'tags': 'crange-image;crange-cache-147'}
            with patch('crange.project_dir', return_value=root), \
                    patch('crange_scheduling.read_settings', return_value=({}, {'images': {'alpine-ct-v1': image}})):
                crange_templates.approve_catalog_image('alpine-ct-v1', [1], api)
            api.request.assert_called_once_with('GET', '/nodes/proxmox2/lxc/147/config')
            ref = json.loads((root / 'approved-container-templates.json').read_text())['alpine-ct-v1']
            self.assertEqual(ref['profile']['init_system'], 'openrc')
            self.assertEqual(json.loads((root / 'approved-scenario-templates.json').read_text()), {'existing-vm': {}})


if __name__ == '__main__':
    unittest.main()
