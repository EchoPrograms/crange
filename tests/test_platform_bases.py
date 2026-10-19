import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_platform_bases as b
from crange_capture import _approved_platform_source
from crange_distribution import DistributionError
from crange_jobs import command


class MaintainedBaseTests(unittest.TestCase):
    def test_source_build_policy_is_private_and_never_fabricates_provider_acceptance(self):
        original = {'allowed_nodes': ['proxmox1'], 'nodes': {'proxmox4': {'cpu_reserve_cores': 0}},
                    'approved_storage': [['proxmox1', 'local-lvm']]}
        previous = copy.deepcopy(original)
        policy = b.source_policy(original)
        self.assertEqual(policy['allowed_nodes'], ['proxmox1', 'proxmox4'])
        self.assertTrue(policy['nodes']['proxmox4']['draining'])
        self.assertEqual(policy['approved_storage'], original['approved_storage'])
        self.assertNotIn('provider_container_acceptance', policy['nodes']['proxmox4'])
        self.assertNotIn('snapshot_storages', policy['nodes']['proxmox4'])
        self.assertEqual(original, previous)

    def test_only_fixed_admin_sources_can_be_queued(self):
        for key in b.BASES:
            self.assertEqual(command('template-base-prepare', 0, {'base': key})[-2:],
                             ['template-base-prepare', key])
        for project, options in [(100, {'base': 'debian-13-vm'}),
                                 (0, {'base': 'arbitrary-vm'}),
                                 (0, {'base': 'debian-13-vm', 'vmid': 128})]:
            with self.assertRaises(ValueError):
                command('template-base-prepare', project, options)

    def test_native_container_builder_requests_full_probe_without_faking_snapshot_proof(self):
        import crange_capture
        policy={'enabled':True,'allowed_nodes':['proxmox4'],'allowed_storages':['local-lvm'],
                'approved_storage':[['proxmox4','local-lvm']],
                'approved_backup_storage':[['proxmox4','crange-library']],
                'capture_datastore':'local-lvm','capture_backup_storage':'crange-library',
                'capture_isolated_bridge':'crprobe1450','isolated_bridges':['crprobe1450'],'nodes':{}}
        with self.assertRaises(DistributionError):
            crange_capture.capture_policy('proxmox4', {'init_system':'systemd'}, policy, kind='container')
        result=crange_capture.capture_policy('proxmox4', {'init_system':'systemd','_full_clone_probe':True}, policy, kind='container')
        self.assertEqual(result[2],'local-lvm')
        self.assertEqual(policy['nodes'],{})

    def test_fixed_source_guard_rejects_wrong_name_kind_tags_or_mutable_source(self):
        source = {'name': 'crange-base-debian-13', 'tags': 'range-lan;crange-base', 'template': 1}
        _approved_platform_source(source, 'proxmox4', 3015, 'vm')
        for changes in [{'name': 'live-vm'}, {'tags': 'range-lan'}, {'template': 0}]:
            with self.assertRaises(DistributionError):
                _approved_platform_source({**source, **changes}, 'proxmox4', 3015, 'vm')
        with self.assertRaises(DistributionError):
            _approved_platform_source(source, 'proxmox4', 3015, 'container')

    def test_incomplete_probe_never_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'source-journal').mkdir()
            (root / 'source-journal/export.json').write_text('{}')
            (root / 'source-journal/probe.json').write_text(json.dumps({'state': 'allocated'}))
            api = Mock()
            api.vm.return_value = {'name': 'crange-base-debian-13', 'tags': 'range-lan;crange-base', 'template': 1}
            with patch.object(b, 'DATA', root), patch.object(b, 'read_settings', return_value=({'enabled': True}, {'images': {}})), \
                    patch.object(b, 'capture_vm', return_value=(200, {'export_job': 'export', 'probe_job': 'probe'})) as capture, \
                    patch.object(b, 'catalog_transaction') as publish:
                with self.assertRaisesRegex(DistributionError, 'Fresh clone acceptance'):
                    b.prepare('debian-13-vm', version='1', api=api)
                publish.assert_not_called()
                profile = capture.call_args.kwargs['profile']
                self.assertEqual(profile['_capture_datastores']['proxmox6'], 'local-lvm')
                self.assertEqual(profile['_capture_preferred_nodes'][0], 'proxmox5')

    def test_verified_version_is_reused_without_guest_mutation(self):
        api = Mock()
        api.vm.return_value = {'name': 'crange-base-debian-13', 'tags': 'range-lan;crange-base', 'template': 1}
        image = {'kind': 'vm', 'verified': True, 'source_revision': 'platform-debian-13-vm-' + 'a' * 64}
        with patch.object(b, 'read_settings', return_value=({}, {'images': {'debian-13-vm-base-v1': image}})), \
                patch.object(b, 'distribute', return_value={'errors': {}}), patch.object(b, 'link_global_base'), \
                patch.object(b, 'capture_vm') as capture:
            self.assertTrue(b.prepare('debian-13-vm', version='1', api=api)['reused'])
            capture.assert_not_called()

    def test_wrong_existing_provenance_cannot_be_adopted(self):
        api = Mock()
        api.vm.return_value = {'name': 'crange-base-debian-13', 'tags': 'range-lan;crange-base', 'template': 1}
        with patch.object(b, 'read_settings', return_value=({}, {'images': {'debian-13-vm-base-v1': {'kind': 'vm', 'verified': True, 'source_revision': 'other'}}})):
            with self.assertRaisesRegex(DistributionError, 'different provenance'):
                b.prepare('debian-13-vm', version='1', api=api)

    def test_success_publishes_actual_destination_and_verified_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'source-journal').mkdir()
            (root / 'capture-journal').mkdir()
            export = {'identity_capture': {'source_config_sha256': 'a' * 64}}
            verification = {'passed': True, 'evidence': {'real': True}}
            (root / 'source-journal/export.json').write_text(json.dumps(export))
            (root / 'source-journal/probe.json').write_text(json.dumps({'state': 'source-probed', 'verification': verification,
                                                                      'provider_tasks': [{'state': 'confirmed-terminal'}]}))
            slug = b.hashlib.sha256(b.image_id_for('debian-13-vm', '1').encode()).hexdigest()[:12]
            (root / 'capture-journal' / ('capture-p001-' + slug + '.json')).write_text(json.dumps({
                'node': 'proxmox6', 'reservation': {'resolved': {
                    'node_name': 'proxmox6', 'datastore_id': 'local-hdd'}}, 'state': 'verified'}))
            api = Mock()
            api.vm.return_value = {'name': 'crange-base-debian-13', 'tags': 'range-lan;crange-base', 'template': 1}
            def transaction(path, update):
                return update({'images': {}})
            image_id = b.image_id_for('debian-13-vm', '1')
            with patch.object(b, 'DATA', root), patch.object(b, 'read_settings', return_value=({'enabled': True,
                    'approved_storage': [['proxmox6', 'local-hdd']]}, {'images': {}})), \
                    patch.object(b, 'capture_vm', return_value=(200, {'node': 'proxmox6', 'export_job': 'export', 'probe_job': 'probe'})) as capture, \
                    patch.object(b, 'distribute', return_value={'errors': {}}), patch.object(b, 'link_global_base'), \
                    patch.object(b, 'catalog_transaction', side_effect=transaction), \
                    patch.object(b, 'publish_exported_image', return_value={'images': {image_id: {}}}) as publish:
                self.assertEqual(b.prepare('debian-13-vm', version='1', api=api)['node'], 'proxmox6')
                self.assertEqual(capture.call_args.kwargs['profile']['_capture_datastores']['proxmox6'], 'local-hdd')
                self.assertEqual(publish.call_args.kwargs['datastore_id'], 'local-hdd')
                self.assertEqual(publish.call_args.kwargs['verification'], verification)
                self.assertEqual(publish.call_args.kwargs['approved_projects'], ['*'])

    def test_global_binding_preserves_legacy_reference_and_updates_future_defaults(self):
        import crange_templates
        from crange_template_selection import creation_templates
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'terraform/environments/project-100'
            default = root / 'terraform/environments/project-template'
            for path in [project, default]:
                path.mkdir(parents=True)
                (path / 'approved-container-templates.json').write_text(json.dumps({
                    'approved-ct-base': {'template_id': 3000, 'template_node': 'proxmox4'},
                    'p100-image-example': {'image_id': 'p100-image-example'}}))
            (project / 'crange.auto.tfvars.json').write_text('{}')
            def approve(image, projects, api):
                self.assertEqual(projects, [100])
                file = project / 'approved-container-templates.json'
                refs = json.loads(file.read_text())
                refs['debian-13-container-base-v1'] = {'image_id': image, 'template_id': 200, 'template_node': 'proxmox2'}
                file.write_text(json.dumps(refs))
                return {'alias': 'debian-13-container-base-v1'}
            with patch.object(crange_templates, 'ROOT', root), patch.object(crange_templates, 'approve_catalog_image', side_effect=approve):
                b.link_global_base('debian-13-container-base-v1', b.BASES['debian-13-container'], api=Mock())
            for path in [project, default]:
                refs = json.loads((path / 'approved-container-templates.json').read_text())
                self.assertEqual(refs['approved-ct-base']['template_id'], 3000)
                self.assertIn('p100-image-example', refs)
                self.assertEqual(set(creation_templates(refs)), {'debian-13-container-base-v1'})

    def test_distribution_reports_excluded_nodes_without_fabricating_readiness(self):
        import crange_cache_jobs
        with tempfile.TemporaryDirectory() as directory:
            policy = {'allowed_nodes': ['proxmox1'], 'approved_storage': [['proxmox1', 'local-lvm']]}
            image = {'kind': 'vm', 'verified': True, 'approved_projects': ['*']}
            with patch.object(b, 'DATA', Path(directory)), \
                    patch.object(b, 'read_settings', return_value=(policy, {'images': {'base-v1': image}})), \
                    patch.object(crange_cache_jobs, 'prepare_cache') as prepare:
                report = b.distribute('base-v1')
            self.assertEqual(report['ready'], ['proxmox1'])
            self.assertEqual(len(report['errors']), 5)
            self.assertIn('proxmox4', report['errors'])
            prepare.assert_called_once()
