import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_templates as templates
import crange_scheduling as scheduling
_spec = importlib.util.spec_from_file_location('builder', Path(__file__).resolve().parents[1] / 'scripts/build-container-base.py')
builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builder)


class ArchiveFallbackTests(unittest.TestCase):
    def test_new_vm_uses_capability_checked_portable_machine_in_reviewed_overrides(self):
        row = {'resource_key': 'vm:new', 'kind': 'vm', 'box': {},
               'reference': {'profile': {'machine': 'pc-q35-10.1'}}}
        prepared = {'placements': [{'resource_key': 'vm:new', 'resolved': {
            'node_name': 'proxmox6', 'datastore_id': 'local-hdd', 'template_id': 219}}]}
        api = Mock(); api.request.return_value = [{'id': 'pc-q35-9.2+pve1', 'type': 'q35'}]
        with patch.object(scheduling, 'declarations', return_value=[row]):
            result = templates.media_tf_overrides(Path('/unused'), {}, prepared, api)
        self.assertEqual(result['resolved_placements']['vm:new']['machine'], 'pc-q35-9.2+pve1')
        api.request.assert_called_once_with('GET', '/nodes/proxmox6/capabilities/qemu/machines')

    def test_existing_vm_machine_is_preserved_and_capabilities_are_not_requeried(self):
        row = {'resource_key': 'vm:live', 'kind': 'vm', 'box': {}, 'reference': {}}
        live = {'node_name': 'proxmox1', 'machine': 'pc-q35-10.1'}
        frozen = scheduling.freeze(row, live)
        prepared = {'placements': [frozen]}; api = Mock()
        with patch.object(scheduling, 'declarations', return_value=[row]):
            result = templates.media_tf_overrides(Path('/unused'), {}, prepared, api)
        self.assertEqual(result['resolved_placements']['vm:live']['machine'], 'pc-q35-10.1')
        api.request.assert_not_called()

    def test_live_vm_resize_never_normalizes_its_machine(self):
        row = {'resource_key': 'vm:live', 'kind': 'vm', 'box': {},
               'reference': {'profile': {'machine': 'pc-q35-10.1'}}}
        prepared = {'placements': [{'resource_key': 'vm:live', 'resolved': {
            'node_name': 'proxmox6', 'datastore_id': 'local-hdd', 'template_id': 219}}]}
        api = Mock()
        with patch.object(scheduling, 'declarations', return_value=[row]), \
             patch.object(scheduling, 'applied_resources', return_value={
                 'vm:live': {'machine': 'pc-q35-10.1', 'node_name': 'proxmox6'}}):
            result = templates.media_tf_overrides(Path('/unused'), {}, prepared, api)
        self.assertEqual(result['resolved_placements']['vm:live']['machine'], 'pc-q35-10.1')
        api.request.assert_not_called()

    def test_missing_portable_capability_refuses_new_vm_plan(self):
        row = {'resource_key': 'vm:new', 'kind': 'vm', 'box': {},
               'reference': {'profile': {'machine': 'pc-q35-10.1'}}}
        prepared = {'placements': [{'resource_key': 'vm:new', 'resolved': {
            'node_name': 'proxmox6', 'datastore_id': 'local-hdd', 'template_id': 219}}]}
        api = Mock(); api.request.return_value = []
        with patch.object(scheduling, 'declarations', return_value=[row]), self.assertRaises(ValueError):
            templates.media_tf_overrides(Path('/unused'), {}, prepared, api)
        self.assertTrue(all(c.args[0] == 'GET' for c in api.request.call_args_list))

    def test_fresh_router_uses_resolved_default_archive(self):
        router = {'resource_key': 'router:project', 'kind': 'container',
                  'box': {'node_name': 'proxmox4'}, 'reference': {'image_id': 'debian-12-router-v1'}}
        api = Mock()
        with patch('crange_scheduling.declarations', return_value=[router]), \
             patch('crange_scheduling.applied_resources', return_value={}), \
             patch.object(templates, 'resolve_archive', return_value='media-mirror1:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst') as resolve:
            result = templates.media_tf_overrides(Path('/unused'), {'router_node': 'proxmox4'}, api=api)
        resolve.assert_called_once_with(api, 'proxmox4', 'crange-library:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst')
        self.assertEqual(result['resolved_placements']['router:project']['template_file_id'],
                         'media-mirror1:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst')

    def test_prepared_archive_cannot_silently_change_after_review(self):
        router = {'resource_key': 'router:project', 'kind': 'container',
                  'box': {'node_name': 'proxmox4'}, 'reference': {'image_id': 'debian-12-router-v1'}}
        prepared = {'placements': [{'resource_key': 'router:project', 'resolved': {'node_name': 'proxmox4',
                        'template_file_id': 'crange-library:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst'}}],
                    'resolved_placements': {'router:project': {'node_name': 'proxmox4',
                        'template_file_id': 'crange-library:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst'}}}
        with patch('crange_scheduling.declarations', return_value=[router]), \
             patch('crange_scheduling.applied_resources', return_value={}), \
             patch.object(templates, 'resolve_archive', return_value='media-mirror1:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst'), \
             self.assertRaisesRegex(ValueError, 'restage'):
            templates.media_tf_overrides(Path('/unused'), {'router_node': 'proxmox4'}, prepared, Mock())

    def test_existing_guest_source_is_frozen_without_media_discovery(self):
        router = {'resource_key': 'router:project', 'kind': 'container', 'box': {}, 'reference': {}}
        live = {'node_name': 'proxmox4', 'operating_system': [{'template_file_id': 'local:vztmpl/old.tar.zst'}]}
        with patch('crange_scheduling.declarations', return_value=[router]), \
             patch('crange_scheduling.applied_resources', return_value={'router:project': live}), \
             patch('crange_scheduling.freeze', return_value={'resolved': {'node_name': 'proxmox4',
                 'template_file_id': 'local:vztmpl/old.tar.zst'}}) as freeze, \
             patch.object(templates, 'resolve_archive') as resolve:
            result = templates.media_tf_overrides(Path('/unused'), {}, api=Mock())
        freeze.assert_called_once_with(router, live)
        resolve.assert_not_called()
        self.assertEqual(result['resolved_placements']['router:project']['template_file_id'], 'local:vztmpl/old.tar.zst')

    def test_builder_uses_first_signed_matching_mirror_after_primary_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'archive.tar.zst'
            archive.write_bytes(b'signed archive fixture')
            digest = hashlib.sha512(archive.read_bytes()).hexdigest()
            args = SimpleNamespace(archive='debian-13-standard_test_amd64.tar.zst')

            def lookup(argv):
                if argv[-1].startswith('crange-library:'):
                    raise builder.BuildError('offline')
                return str(archive)

            with patch.object(builder, 'command', side_effect=lookup):
                path, volume = builder.find_signed_archive(args, digest)
            self.assertEqual(path, archive)
            self.assertEqual(volume, 'media-mirror1:vztmpl/debian-13-standard_test_amd64.tar.zst')
            with patch.object(builder, 'command', side_effect=lookup):
                self.assertEqual(builder.find_signed_archive(args, '0' * 128), (None, None))

    def test_scheduler_adds_only_manifest_hash_verified_stock_archive_aliases(self):
        digest = 'a' * 64
        catalog = {'schema_version': 1, 'images': {'debian-stock-v1': {
            'kind': 'container', 'source_type': 'stock_archive', 'architecture': 'amd64',
            'artifact_sha256': digest, 'source_revision': 'debian13', 'verified': True,
            'approved_projects': [3], 'disk_gb': 8,
            'replicas': [{'node': 'proxmox2', 'template_file_id': 'local:vztmpl/debian.tar.zst',
                'datastore_id': 'local', 'state': 'ready', 'artifact_sha256': digest,
                'verified_at': '2026-10-01T00:00:00Z'}]}}}
        status = {'updated_at': '2026-10-07T16:00:00Z', 'files': [{
            'content': 'vztmpl', 'name': 'debian.tar.zst', 'size': 100,
            'sha256': digest, 'copies': [
                {'storage': 'crange-library', 'sha256': digest},
                {'storage': 'media-mirror1', 'sha256': digest},
                {'storage': 'media-mirror4', 'sha256': 'b' * 64},
                {'storage': 'other-storage', 'sha256': digest}]}]}
        resolved = scheduling.verified_archive_copies(catalog, status)
        replicas = resolved['images']['debian-stock-v1']['replicas']
        self.assertEqual([r['template_file_id'] for r in replicas], [
            'local:vztmpl/debian.tar.zst', 'crange-library:vztmpl/debian.tar.zst',
            'media-mirror1:vztmpl/debian.tar.zst'])
        self.assertEqual(replicas[1]['copy_verification']['sha256'], digest)
        self.assertEqual(scheduling.catalog_identity(resolved),
                         scheduling.catalog_identity(scheduling.verified_archive_copies(catalog, status)))
        later_status = {**status, 'updated_at': '2026-10-07T16:05:00Z'}
        self.assertEqual(scheduling.catalog_identity(resolved),
                         scheduling.catalog_identity(scheduling.verified_archive_copies(catalog, later_status)))
        self.assertEqual(len(catalog['images']['debian-stock-v1']['replicas']), 1)

    def test_scheduler_ignores_archive_manifest_with_wrong_artifact_hash(self):
        catalog = {'schema_version': 1, 'images': {'base': {
            'kind': 'container', 'source_type': 'stock_archive', 'architecture': 'amd64',
            'artifact_sha256': 'a' * 64, 'source_revision': 'debian13', 'verified': True,
            'approved_projects': [3], 'disk_gb': 8,
            'replicas': [{'node': 'proxmox2', 'template_file_id': 'local:vztmpl/debian.tar.zst',
                'datastore_id': 'local', 'state': 'ready', 'artifact_sha256': 'a' * 64,
                'verified_at': '2026-10-01T00:00:00Z'}]}}}
        status = {'updated_at': '2026-10-07T16:00:00Z', 'files': [{
            'content': 'vztmpl', 'name': 'debian.tar.zst', 'size': 100,
            'sha256': 'b' * 64, 'copies': [{'storage': 'media-mirror1', 'sha256': 'b' * 64}]}]}
        result = scheduling.verified_archive_copies(catalog, status)
        self.assertEqual(len(result['images']['base']['replicas']), 1)


if __name__ == '__main__':
    unittest.main()
