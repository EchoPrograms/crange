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
_spec = importlib.util.spec_from_file_location('builder', Path(__file__).resolve().parents[1] / 'scripts/build-container-base.py')
builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builder)


class ArchiveFallbackTests(unittest.TestCase):
    def test_fresh_router_uses_resolved_default_archive(self):
        router = {'resource_key': 'router:project', 'kind': 'container',
                  'box': {'node_name': 'proxmox4'}, 'reference': {'image_id': 'debian-13-router-v1'}}
        api = Mock()
        with patch('crange_scheduling.declarations', return_value=[router]), \
             patch('crange_scheduling.applied_resources', return_value={}), \
             patch.object(templates, 'resolve_archive', return_value='media-mirror1:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst') as resolve:
            result = templates.media_tf_overrides(Path('/unused'), {'router_node': 'proxmox4'}, api=api)
        resolve.assert_called_once_with(api, 'proxmox4', 'crange-library:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst')
        self.assertEqual(result['resolved_placements']['router:project']['template_file_id'],
                         'media-mirror1:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst')

    def test_prepared_archive_cannot_silently_change_after_review(self):
        router = {'resource_key': 'router:project', 'kind': 'container',
                  'box': {'node_name': 'proxmox4'}, 'reference': {'image_id': 'debian-13-router-v1'}}
        prepared = {'placements': [{'resource_key': 'router:project', 'resolved': {'node_name': 'proxmox4',
                        'template_file_id': 'crange-library:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst'}}],
                    'resolved_placements': {'router:project': {'node_name': 'proxmox4',
                        'template_file_id': 'crange-library:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst'}}}
        with patch('crange_scheduling.declarations', return_value=[router]), \
             patch('crange_scheduling.applied_resources', return_value={}), \
             patch.object(templates, 'resolve_archive', return_value='media-mirror1:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst'), \
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


if __name__ == '__main__':
    unittest.main()
