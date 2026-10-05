from pathlib import Path
import sys
import unittest
import hashlib
import tempfile
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_distribution import ReplicaWorker, DistributionError, publish_artifact, ProbeVerifier, SSHContainerExec, normalize_projects, publish_stock_archive, publish_exported_image, required_capacity


class ReplicaTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {'schema_version': 1, 'images': {'base-v1': {
            'kind': 'vm', 'architecture': 'amd64', 'artifact_sha256': 'a'*64,
            'disk_gb': 8, 'memory_mb': 512, 'cpu_cores': 1,
            'source_revision': 'revision', 'verified': True, 'approved_projects': ['*'],
            'replicas': [{'node': 'proxmox2', 'template_id': 100,
                'datastore_id': 'local-lvm', 'state': 'ready',
                'artifact_sha256': 'a'*64, 'verified_at': '2026-01-01T00:00:00Z'}]}}}
        self.api = Mock()
        self.ids = iter([200, 201])
        def request(method, path, fields=None):
            if path == '/cluster/nextid': return next(self.ids)
            if path.endswith('/status/current'): return {'status': 'stopped'}
            if method == 'GET': return {'tags': 'crange-image;crange-job-test;crange-job-archive', 'template': 1}
            return 'UPID:test'
        self.api.request.side_effect = request
        self.journal = Mock()
        self.verify = Mock(return_value={'passed': True, 'identity_unique': True})
        self.worker = ReplicaWorker(self.api, self.journal, self.verify,
            approved_storage=[('proxmox2', 'local-lvm')],
            tested_paths=['local-linked-vm'], reservation_validator=lambda *args: True)

    def prepare(self):
        return self.worker.prepare(self.catalog, 'base-v1', source_node='proxmox2',
            source_id=100, node='proxmox2', datastore_id='local-lvm', job_id='test')

    def test_publishes_only_after_probe_deleted_and_preserves_catalog(self):
        result = self.prepare()
        self.assertEqual(len(self.catalog['images']['base-v1']['replicas']), 1)
        replica = result['images']['base-v1']['replicas'][-1]
        self.assertEqual(replica['template_id'], 200)
        self.assertEqual(replica['state'], 'ready')
        self.api.request.assert_any_call('DELETE', '/nodes/proxmox2/qemu/201', {'purge': 1})
        self.assertEqual(self.journal.call_args.args[0]['state'], 'ready')

    def test_missing_cache_reservation_has_no_provider_mutations(self):
        self.worker.reservation_validator = None
        with self.assertRaisesRegex(DistributionError, 'reservation'): self.prepare()
        self.assertTrue(all(c.args[0] == 'GET' for c in self.api.request.call_args_list))
        self.journal.assert_not_called()

    def test_verified_probe_profile_reserves_and_applies_exact_resources(self):
        image = self.catalog['images']['base-v1']
        image.update(cpu_cores=4, memory_mb=6144, probe_resources={
            'cpu_cores': 1, 'memory_mb': 512, 'verified': True,
            'evidence': 'owned fresh-clone boot and identity acceptance'})
        self.assertEqual(required_capacity(image)['cpu_cores'], 1)
        self.assertEqual(required_capacity(image)['memory_mb'], 512)
        self.worker.reservation_validator = Mock(return_value=True)
        self.prepare()
        admitted = self.worker.reservation_validator.call_args.args[3]
        self.assertEqual((admitted['cpu_cores'], admitted['memory_mb']), (1, 512))
        updates = [call.args[2] for call in self.api.request.call_args_list
                   if call.args[:2] == ('PUT', '/nodes/proxmox2/qemu/201/config')]
        self.assertEqual(len(updates), 1)
        self.assertEqual({key: updates[0][key] for key in ('memory', 'cores', 'sockets')},
                         {'memory': 512, 'cores': 1, 'sockets': 1})
        self.assertIn('crange-job-test', updates[0]['tags'].split(';'))

    def test_unverified_reduced_probe_profile_fails_before_allocation(self):
        self.catalog['images']['base-v1']['probe_resources'] = {
            'cpu_cores': 1, 'memory_mb': 512, 'verified': False, 'evidence': ''}
        with self.assertRaisesRegex(ValueError, 'live boot evidence'):
            self.prepare()
        self.assertTrue(all(call.args[0] == 'GET' for call in self.api.request.call_args_list))
        self.journal.assert_not_called()

    def test_linked_probe_and_upid_are_recorded_before_wait(self):
        self.prepare()
        self.api.request.assert_any_call('POST', '/nodes/proxmox2/qemu/200/clone',
            {'newid': 201, 'full': 0, 'pool': 'crange-iac',
             'description': 'CRANGE verification test', 'name': 'crange-probe-201'})
        self.assertEqual(self.journal.call_args.args[0]['provider_tasks'][-1]['state'], 'confirmed-terminal')

    def test_archive_cache_restore_does_not_create_source_local_temporary_copy(self):
        volume = 'crange-library:backup/vzdump-qemu-100.vma.zst'
        self.catalog['images']['base-v1']['artifact'] = {
            'source_id': 100, 'node': 'proxmox1', 'volume_id': volume}
        self.worker.tested_paths.add('vm-archive-restore')
        self.worker.hash_volume = Mock(return_value='a'*64)
        archive = dict(source_id=100, node='proxmox1', volume_id=volume,
                       artifact_sha256='a'*64, state='exported', complete_capture=True)
        self.worker.prepare(self.catalog, 'base-v1', source_node='proxmox1', source_id=100,
            node='proxmox2', datastore_id='local-lvm', job_id='archive', archive=archive)
        self.api.request.assert_any_call('POST', '/nodes/proxmox2/qemu',
            {'vmid': 200, 'storage': 'local-lvm', 'pool': 'crange-iac', 'unique': 1,
             'tags': 'crange-image;crange-job-archive', 'archive': volume, 'name': 'crange-replica-200'})
        self.assertFalse(any('/nodes/proxmox1/' in c.args[1] for c in self.api.request.call_args_list))

    def test_unverified_cross_node_path_has_no_mutations(self):
        self.worker.tested_paths.clear()
        with self.assertRaisesRegex(DistributionError, 'acceptance'): self.prepare()
        self.assertTrue(all(c.args[0] == 'GET' for c in self.api.request.call_args_list))
        self.journal.assert_not_called()

    def test_failed_identity_verification_never_publishes(self):
        self.verify.return_value = {'passed': True, 'identity_unique': False}
        with self.assertRaisesRegex(DistributionError, 'verification failed'): self.prepare()
        self.assertEqual(self.journal.call_args.args[0]['state'], 'failed-verification')
        self.assertFalse(any(c.args[0] == 'DELETE' for c in self.api.request.call_args_list))

    def test_unowned_source_rejected(self):
        self.api.request.side_effect = None
        self.api.request.return_value = {'template': 1, 'tags': 'homelab'}
        with self.assertRaisesRegex(DistributionError, 'owned'): self.prepare()
        self.journal.assert_not_called()

    def test_ct_cross_node_requires_complete_capture_archive(self):
        self.catalog['images']['base-v1']['kind'] = 'container'
        self.worker.tested_paths = {'local-linked-container'}
        self.catalog['images']['base-v1']['replicas'][0]['node'] = 'proxmox1'
        with self.assertRaisesRegex(DistributionError, 'complete backup'):
            self.worker.prepare(self.catalog, 'base-v1', source_node='proxmox1',
                source_id=100, node='proxmox2', datastore_id='local-lvm', job_id='test')
        self.journal.assert_not_called()

    def test_timeout_retains_journal_and_does_not_retry_or_publish(self):
        self.api.task.side_effect = TimeoutError('task')
        with self.assertRaises(TimeoutError): self.prepare()
        self.assertEqual(self.journal.call_count, 2)
        self.assertEqual(self.journal.call_args.args[0]['template_id'], 200)
        self.verify.assert_not_called()

class PublicationTests(unittest.TestCase):
    def test_projects_normalized_and_invalid_scope_rejected(self):
        self.assertEqual(normalize_projects(['3', '*', 3]), [3, '*'])
        for bad in ('60', '61', '0', '256', 'evil', True):
            with self.assertRaises(DistributionError): normalize_projects([bad])

    def test_stock_archive_checksum_and_multinode_replicas(self):
        arguments = dict(template_file_id='local:vztmpl/base.tar.zst', architecture='amd64',
            source_revision='revision', approved_projects=['3'], disk_gb=8,
            expected_sha256='a'*64, hash_volume=Mock(return_value='a'*64),
            verification={'passed': True, 'evidence': 'reviewed clone test'})
        catalog = publish_stock_archive({'schema_version': 1, 'images': {}}, 'base-v1',
                                        node='proxmox1', **arguments)
        catalog = publish_stock_archive(catalog, 'base-v1', node='proxmox2', **arguments)
        image = catalog['images']['base-v1']
        self.assertEqual(image['approved_projects'], [3])
        self.assertEqual(image['disk_gb'], 8)
        self.assertEqual(len(image['replicas']), 2)
        arguments['hash_volume'].return_value = 'b'*64
        with self.assertRaisesRegex(DistributionError, 'checksum'):
            publish_stock_archive(catalog, 'base-v2', node='proxmox3', **arguments)

    def test_export_publication_keeps_virtual_size(self):
        record = dict(state='exported', complete_capture=True, kind='vm', node='proxmox1',
                      source_id=100, artifact_sha256='a'*64, volume_id='local:backup/export', disk_gb=32)
        arguments = dict(architecture='amd64', source_revision='revision', approved_projects=['3'],
                         datastore_id='local-lvm', verification={'passed': True, 'evidence': 'clone test'})
        catalog = publish_exported_image({'schema_version': 1, 'images': {}}, 'vm-v1', record, **arguments)
        self.assertEqual(catalog['images']['vm-v1']['disk_gb'], 32)
        del record['disk_gb']
        with self.assertRaisesRegex(DistributionError, 'disk size'):
            publish_exported_image({'schema_version': 1, 'images': {}}, 'vm-v2', record, **arguments)

class ProbeTests(unittest.TestCase):
    def test_ct_probe_uses_disconnected_approved_network_and_real_identities(self):
        api = Mock()
        api.request.side_effect = lambda method, path, fields=None: (
            {'tags': 'crange-image', 'net0': 'name=eth0,bridge=p003t254'}
            if method == 'GET' else 'UPID:probe')
        execute = Mock(side_effect=['running', 'b'*32, 'ssh-ed25519 newkey guest'])
        verifier = ProbeVerifier('vmbrtest', ['vmbrtest'],
            {'machine_id': 'a'*32, 'ssh_host_key': 'ssh-ed25519 oldkey source'}, ct_exec=execute)
        result = verifier(api, 'proxmox2', 'lxc', 201)
        self.assertTrue(result['identity_unique'])
        update = api.request.call_args_list[1]
        self.assertIn('link_down=1', update.args[2]['net0'])
        self.assertIn('bridge=vmbrtest', update.args[2]['net0'])
        self.assertEqual(result['evidence']['machine_id'], 'b'*32)

    def test_command_adapter_rejects_arbitrary_command(self):
        run = Mock()
        execute = SSHContainerExec(run)
        with self.assertRaises(DistributionError):
            execute('proxmox2', 201, ['/bin/sh', '-c', 'evil'])
        run.assert_not_called()

class ArtifactTests(unittest.TestCase):
    def test_publication_rejects_bad_hash_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            source = library / 'source'
            source.write_bytes(b'captured export')
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            with self.assertRaises(DistributionError):
                publish_artifact(source, library, 'a'*64)
            self.assertFalse((library / ('a'*64)).exists())
            artifact = publish_artifact(source, library, digest)
            self.assertEqual(artifact.read_bytes(), b'captured export')
            self.assertEqual(publish_artifact(source, library, digest), artifact)
            artifact.chmod(0o600)
            artifact.write_bytes(b'corrupted')
            with self.assertRaisesRegex(DistributionError, 'inconsistent'):
                publish_artifact(source, library, digest)
            self.assertEqual(artifact.read_bytes(), b'corrupted')

if __name__ == '__main__': unittest.main()
