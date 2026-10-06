from pathlib import Path
import copy
import sys
import unittest
import hashlib
import tempfile
import argparse
import contextlib
import io
import json
import os
import stat
import base64
import struct
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_catalog import CatalogError, validate_catalog, publish_replica
from crange_distribution import (ReplicaWorker, DistributionError, publish_artifact,
    ProbeVerifier, SSHContainerExec, normalize_projects, publish_stock_archive,
    publish_exported_image, required_capacity, _add_project_approval_arguments,
    _project_approval_scope, shared_reservation_validator, SourceProbeWorker,
    _qemu_capacity, _source_config_identity, catalog_transaction,
    main as distribution_main)


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
        self.assertEqual(self.journal.call_args.args[0]['probe_pool'], 'crange-probes')

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
            {'newid': 201, 'full': 0, 'pool': 'crange-probes',
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
        self.assertFalse(any(call.args[:2] == ('POST', '/nodes/proxmox2/qemu/200/template')
                             for call in self.api.request.call_args_list))
        self.assertFalse(any('/nodes/proxmox1/' in c.args[1] for c in self.api.request.call_args_list))

    def test_restored_stopped_template_resumes_without_reconversion(self):
        self.ids = iter([201])
        record = {'job_id': 'test', 'image_id': 'base-v1', 'node': 'proxmox2',
            'allocation_node': 'proxmox2', 'datastore_id': 'local-lvm', 'kind': 'qemu',
            'state': 'converting-template', 'template_id': 200,
            'artifact_sha256': 'a'*64, 'provider_tasks': []}
        result = self.worker.resume_template(self.catalog, 'base-v1', record=record)
        self.assertEqual(result['images']['base-v1']['replicas'][-1]['template_id'], 200)
        self.assertFalse(any(call.args[:2] == ('POST', '/nodes/proxmox2/qemu/200/template')
                             for call in self.api.request.call_args_list))
        self.api.request.assert_any_call('DELETE', '/nodes/proxmox2/qemu/201', {'purge': 1})

    def test_resume_rejects_wrong_state_or_artifact_before_probe(self):
        record = {'job_id': 'test', 'image_id': 'base-v1', 'node': 'proxmox2',
            'allocation_node': 'proxmox2', 'datastore_id': 'local-lvm', 'kind': 'qemu',
            'state': 'allocated', 'template_id': 200,
            'artifact_sha256': 'b'*64, 'provider_tasks': []}
        with self.assertRaisesRegex(DistributionError, 'eligible restored-template'):
            self.worker.resume_template(self.catalog, 'base-v1', record=record)
        self.assertFalse(any(call.args[0] != 'GET' for call in self.api.request.call_args_list))

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


    def test_fixed_source_probe_clones_verifies_deletes_and_journals(self):
        api = Mock()
        source = {'tags': 'crange-image', 'template': 1, 'memory': 1024,
                  'cores': 2, 'sockets': 1,
                  'scsi0': 'local-lvm:base-8830-disk-0,size=12G'}
        probe = {'tags': 'crange-image', 'template': 0, 'memory': 1024,
                 'cores': 2, 'scsi0': 'local-lvm:vm-20000-disk-0,size=12G'}
        def request(method, path, fields=None):
            if path == '/cluster/resources': return []
            if path.endswith('/status/current'):
                return {'status': 'stopped'}
            if path == '/cluster/nextid':
                return fields['vmid']
            if path == '/pools/crange-probes':
                return {'members': [{'type': 'qemu', 'node': 'proxmox1', 'vmid': 8830},
                                    {'type': 'qemu', 'node': 'proxmox1', 'vmid': 20000}]}
            if method == 'PUT' and path.endswith('/20000/config'):
                probe['tags'] = fields['tags']
                return None
            if path.endswith('/config'):
                return source if '/8830/' in path else probe
            return 'UPID:proxmox1:00000001:00000001:00000001:qmclone:20000:root@pam:'
        api.request.side_effect = request
        api.task.return_value = None
        capture = {'job_id': 'capture-8830', 'source_config_sha256':
                   _source_config_identity(source, 8830), 'source_identity': {'machine_id': '1' * 32,
                   'ssh_host_key': 'ssh-ed25519 YWJj source'}}
        reservation = {'reservation_id': 'reservation-1'}
        verify = Mock(return_value={'passed': True, 'identity_unique': True,
            'evidence': {'readiness': 'default', 'machine_id': '2' * 32,
                         'ssh_host_key': 'ssh-ed25519 ZGVm probe',
                         'isolated_bridge': 'crprobe1450'}})
        journal = Mock()
        worker = SourceProbeWorker(api, journal, verify, lambda *args: True)
        result = worker.run(source_node='proxmox1', source_id=8830,
            datastore_id='local-lvm', job_id='source-probe-1', capture=capture,
            reservation=reservation)
        api.request.assert_any_call('POST', '/nodes/proxmox1/qemu/8830/clone', {
            'newid': 20000, 'full': 1, 'storage': 'local-lvm', 'pool': 'crange-probes',
            'name': 'crange-source-probe-20000',
            'description': 'CRANGE source verification source-probe-1'})
        api.request.assert_any_call('GET', '/cluster/nextid', {'vmid': 20000})
        api.request.assert_any_call('DELETE', '/nodes/proxmox1/qemu/20000', {'purge': 1})
        self.assertEqual(result['state'], 'source-probed')
        self.assertTrue(all(t['state'] == 'confirmed-terminal' for t in result['provider_tasks']))
        self.assertEqual(verify.call_args.args[1:], ('proxmox1', 'qemu', 20000))

    def test_probe_requires_exact_captured_source_before_mutation(self):
        api = Mock()
        api.request.side_effect = lambda method, path, fields=None: (
            source if path.endswith('/config') else
            {'status': 'stopped'} if path.endswith('/status/current') else
            {'members': [{'type': 'qemu', 'node': 'proxmox1', 'vmid': 8830}]}
            if path == '/pools/crange-probes' else 990)
        worker = SourceProbeWorker(api, Mock(), Mock(), lambda *args: True)
        source = {'tags': 'crange-image', 'template': 1, 'memory': 1024, 'cores': 2,
             'scsi0': 'local-lvm:base-8830-disk-0,size=12G'}
        capture = {'job_id': 'capture', 'source_config_sha256': 'f' * 64,
                   'source_identity': {'machine_id': '1' * 32,
                                       'ssh_host_key': 'ssh-ed25519 key'}}
        with self.assertRaisesRegex(DistributionError, 'changed'):
            worker.run(source_node='proxmox1', source_id=8830,
                datastore_id='local-lvm', job_id='source-probe-1', capture=capture,
                reservation={'reservation_id': 'x'})
        self.assertEqual([call.args[0] for call in api.request.call_args_list], ['GET', 'GET', 'GET'])

    def test_qemu_capacity_requires_disk_size_for_reservation(self):
        self.assertEqual(_qemu_capacity({'memory': 512, 'cores': 2,
            'scsi0': 'local-lvm:vm-8830-disk-0,size=10G'}),
            {'memory_mb': 512, 'cpu_cores': 2, 'disk_gb': 11.0})
        with self.assertRaisesRegex(DistributionError, 'disk size'):
            _qemu_capacity({'memory': 512, 'cores': 2, 'scsi0': 'local-lvm:vm-8830-disk-0'})

    def test_resume_reconciles_exact_terminal_clone_before_verifying_and_deleting(self):
        from crange_distribution import FileJournal
        with tempfile.TemporaryDirectory() as directory:
            api = Mock()
            source = {'tags': 'crange-image', 'template': 1, 'memory': 512,
                      'cores': 1, 'scsi0': 'local-lvm:base-8830-disk-0,size=20G'}
            probe = {'tags': 'crange-image;crange-job-capture', 'template': 0,
                     'name': 'crange-source-probe-20000',
                     'description': 'CRANGE source verification resume-1',
                     'net0': 'virtio=aa:bb:cc:dd:ee:ff,bridge=crprobe1450,link_down=1'}
            tasks = iter([
                'UPID:proxmox1:00000001:00000001:00000001:qmstart:20000:root@pam:',
                'UPID:proxmox1:00000002:00000002:00000002:qmstop:20000:root@pam:',
                'UPID:proxmox1:00000003:00000003:00000003:qmdelete:20000:root@pam:'])
            def request(method, path, fields=None):
                if path == '/pools/crange-probes':
                    return {'members': [{'type': 'qemu', 'node': 'proxmox1', 'vmid': 8830},
                                        {'type': 'qemu', 'node': 'proxmox1', 'vmid': 20000}]}
                if path.endswith('/status/current'):
                    return {'status': 'stopped'}
                if path.endswith('/config'):
                    if method == 'PUT':
                        probe['tags'] = fields['tags']
                        return None
                    return source if '/8830/' in path else probe
                return next(tasks)
            api.request.side_effect = request
            api.task.return_value = None
            capture = {'job_id': 'capture-8830', 'source_config_sha256':
                       _source_config_identity(source, 8830),
                       'source_identity': {'machine_id': 'a' * 32,
                                           'ssh_host_key': 'ssh-ed25519 key source'}}
            reservation = {'image': 'source:proxmox1:qemu:8830',
                           'artifact_sha256': capture['source_config_sha256']}
            job = 'resume-1'
            journal = FileJournal(Path(directory))
            journal({'job_id': job, 'image_id': 'source:proxmox1:qemu:8830',
                'node': 'proxmox1', 'source_node': 'proxmox1', 'source_id': 8830,
                'source_config_sha256': capture['source_config_sha256'],
                'source_capture_job_id': capture['job_id'], 'datastore_id': 'local-lvm',
                'state': 'allocated', 'kind': 'vm', 'probe_id': 20000,
                'probe_pool': 'crange-probes', 'probe_marker': f'crange-job-{job}',
                'reservation': reservation})
            result = {'passed': True, 'identity_unique': True,
                      'evidence': {'isolated_bridge': 'crprobe1450'}}
            verify = Mock(return_value=result)
            verify.bridge = 'crprobe1450'
            worker = SourceProbeWorker(api, journal, verify, lambda *args: True)
            completed = worker.resume(source_node='proxmox1', source_id=8830,
                datastore_id='local-lvm', job_id=job, capture=capture,
                reservation=reservation,
                task_upid='UPID:proxmox1:00000004:00000004:00000004:qmclone:20000:root@pam:')
            self.assertEqual(completed['state'], 'source-probed')
            self.assertEqual(completed['provider_tasks'][0]['state'], 'confirmed-terminal')
            api.request.assert_any_call('DELETE', '/nodes/proxmox1/qemu/20000', {'purge': 1})

    def test_clone_task_finder_requires_unique_terminal_source_target_log(self):
        from crange_distribution import find_source_clone_task
        api = Mock()
        upid = 'UPID:proxmox1:00000004:00000004:00000004:qmclone:20000:root@pam:'
        api.request.side_effect = [
            [{'type': 'qmclone', 'id': '8830', 'upid': upid, 'status': 'OK'}],
            [{'t': 'create full clone of drive scsi0 (local-lvm:base-8830-disk-0)'},
             {'t': 'Logical volume vm-20000-disk-0 created.'}]]
        self.assertEqual(find_source_clone_task(api, 'proxmox1', 8830, 20000), upid)
        api.task.assert_called_once_with('proxmox1', upid, timeout=120)

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

    def test_shared_reservation_validator_binds_proof_image_node_storage_and_capacity(self):
        allocation = {'image': 'base-v1', 'artifact_sha256': 'a'*64,
            'resolved': {'node_name': 'proxmox2', 'datastore_id': 'local-lvm'},
            'resources': {'memory_mb': 512, 'cpu_cores': 1, 'disk_gb': 16}}
        ledger = Mock()
        ledger.validate_job.return_value = [allocation]
        validate = shared_reservation_validator(ledger, 'job-1', allocation, 'base-v1', 'a'*64)
        required = {'memory_mb': 512, 'cpu_cores': 1, 'disk_gb': 16}
        self.assertTrue(validate(allocation, 'proxmox2', 'local-lvm', required))
        self.assertFalse(validate({}, 'proxmox2', 'local-lvm', required))
        self.assertFalse(validate(allocation, 'proxmox3', 'local-lvm', required))
        self.assertFalse(validate(allocation, 'proxmox2', 'ssd-lvm', required))
        self.assertFalse(validate(allocation, 'proxmox2', 'local-lvm', {**required, 'disk_gb': 17}))
        ledger.validate_job.assert_called()

    def test_replica_cli_wires_shared_ledger_reservation_into_worker(self):
        import crange_distribution as distribution
        import crange_scheduling as scheduling
        import crange_placement as placement
        import crange_cache_jobs as cache_jobs
        import crange
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = base64.b64encode(struct.pack('>I', 11) + b'ssh-ed25519' +
                                   struct.pack('>I', 32) + b'x' * 32).decode()
            identity = {'machine_id': 'a' * 32, 'ssh_host_key': 'ssh-ed25519 ' + key}
            capture = {'state': 'identity-captured', 'job_id': 'capture-1', 'node': 'proxmox1',
                'kind': 'vm', 'source_id': 100, 'source_config_sha256': 'c' * 64,
                'source_identity': identity, 'init_system': 'openrc', 'readiness': 'default'}
            export = {'state': 'exported', 'complete_capture': True, 'kind': 'vm', 'node': 'proxmox1',
                'source_id': 100, 'artifact_sha256': 'a' * 64,
                'volume_id': 'crange-library:backup/vzdump-qemu-100.vma.zst', 'disk_gb': 8,
                'memory_mb': 512, 'cpu_cores': 1, 'source_identity': identity,
                'identity_capture': capture, 'init_system': 'openrc'}
            catalog = publish_exported_image({'schema_version': 1, 'images': {}}, 'base-v1', export,
                architecture='amd64', source_revision='revision', approved_projects=[3],
                datastore_id='local-lvm', verification={'passed': True, 'evidence': 'unit fixture'})
            catalog_path = root / 'catalog.json'
            catalog_path.write_text(json.dumps(catalog))
            policy_path = root / 'policy.json'
            policy_path.write_text(json.dumps({'enabled': True, 'revision': 'test',
                'allowed_nodes': ['proxmox1'], 'allowed_storages': ['local-lvm'],
                'approved_storage': [['proxmox1', 'local-lvm']],
                'tested_paths': ['local-linked-vm', 'vm-archive-restore'],
                'isolated_bridges': ['crprobe1450']}))
            identity_path = root / 'identity.json'
            identity_path.write_text(json.dumps(identity))
            archive_path = root / 'archive.json'
            archive_path.write_text(json.dumps(export))
            allocation = {'image': 'base-v1', 'artifact_sha256': 'a' * 64,
                'resolved': {'node_name': 'proxmox1', 'datastore_id': 'local-lvm'},
                'resources': {'memory_mb': 512, 'cpu_cores': 1, 'disk_gb': 16.8}}
            ledger = Mock()
            ledger.reserve_preparation.return_value = allocation
            ledger.validate_job.return_value = [allocation]
            worker_cls = Mock()
            worker = worker_cls.return_value
            final_inventory = {'guests': {'200': {'template': True, 'node_name': 'proxmox1'}}}

            def prepare(catalog_value, image_id, **kwargs):
                validator = worker_cls.call_args.kwargs['reservation_validator']
                self.assertTrue(validator(allocation, 'proxmox1', 'local-lvm', required_capacity(catalog_value['images'][image_id])))
                self.assertEqual(kwargs['reservation'], allocation)
                journal = worker_cls.call_args.args[1]
                record = {'job_id': 'replica-1', 'state': 'allocated', 'image_id': image_id,
                    'node': 'proxmox1', 'datastore_id': 'local-lvm', 'kind': 'qemu',
                    'source_node': 'proxmox1', 'source_id': 100, 'artifact_sha256': 'a' * 64,
                    'reservation': allocation, 'template_id': 200, 'allocation_node': 'proxmox1',
                    'probe_id': 201, 'probe_pool': 'crange-probes', 'provider_tasks': []}
                journal(record)
                for state in ('converting-template', 'verifying', 'ready'):
                    journal({**record, 'state': state})
                return publish_replica(catalog_value, image_id, {'node': 'proxmox1',
                    'template_id': 200, 'datastore_id': 'local-lvm', 'state': 'ready',
                    'artifact_sha256': 'a' * 64, 'verified_at': 'now',
                    'verification': {'passed': True, 'identity_unique': True},
                    'job_id': 'replica-1', 'clone_modes': ['linked'],
                    'clone_modes_verified': ['linked'], 'cache_local': True})
            worker.prepare.side_effect = prepare
            common = ['--catalog', str(catalog_path), '--journal-dir', str(root / 'journal'),
                '--policy', str(policy_path), 'replica', '--image-id', 'base-v1',
                '--source-node', 'proxmox1', '--node', 'proxmox1', '--datastore-id', 'local-lvm',
                '--job-id', 'replica-1', '--isolated-bridge', 'crprobe1450', '--project', '3',
                '--source-id', '100', '--source-identity', str(identity_path), '--archive', str(archive_path)]
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(crange, 'ProxmoxAPI', return_value=Mock()))
                stack.enter_context(patch.object(distribution, 'ReplicaWorker', worker_cls))
                stack.enter_context(patch.object(distribution, 'SSHVolumeHasher', return_value=Mock()))
                stack.enter_context(patch.object(placement, 'ReservationLedger', return_value=ledger))
                stack.enter_context(patch.object(scheduling, 'discover', side_effect=[{}, final_inventory]))
                stack.enter_context(patch.object(scheduling, 'node_locks', side_effect=lambda *a, **k: contextlib.nullcontext()))
                stack.enter_context(patch.object(cache_jobs, 'artifact_pin', return_value=contextlib.nullcontext()))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                distribution.main(common)
            ledger.reserve_preparation.assert_called_once()
            ledger.reconcile_job.assert_called_once_with('replica-1', operation_terminal=True,
                                                         allocations_reconciled=True)
            self.assertTrue(worker_cls.call_args.kwargs['reservation_validator'](
                allocation, 'proxmox1', 'local-lvm', required_capacity(catalog['images']['base-v1'])))

class CacheRecoveryTests(unittest.TestCase):
    def test_cache_bridge_defaults_only_to_one_approved_bridge(self):
        from crange_cache_jobs import select_probe_bridge
        self.assertEqual(select_probe_bridge({'isolated_bridges': ['crprobe1450']}), 'crprobe1450')
        self.assertEqual(select_probe_bridge({'isolated_bridge': 'crprobe1450',
                                              'isolated_bridges': ['other', 'crprobe1450']}),
                         'crprobe1450')
        with self.assertRaisesRegex(DistributionError, 'not in isolated_bridges'):
            select_probe_bridge({'isolated_bridge': 'vmbr0', 'isolated_bridges': ['crprobe1450']})
        with self.assertRaisesRegex(DistributionError, 'multiple isolated_bridges'):
            select_probe_bridge({'isolated_bridges': ['crprobe1450', 'crprobe1500']})

    def test_absent_cache_reconciliation_releases_only_exact_verified_job(self):
        import time
        import crange_cache_jobs as cache_jobs
        from crange_distribution import FileJournal
        digest = 'd' * 64
        job_id = 'cache-' + 'a' * 32
        image_id = 'alpine-test'
        volume = 'crange-library:backup/vzdump-qemu-8830.vma.zst'
        reservation = {'image': image_id, 'artifact_sha256': digest,
            'resolved': {'node_name': 'proxmox2', 'datastore_id': 'local-lvm'},
            'resources': {'memory_mb': 512, 'cpu_cores': 1, 'disk_gb': 42}}
        record = {'job_id': job_id, 'image_id': image_id, 'node': 'proxmox2',
            'datastore_id': 'local-lvm', 'state': 'allocated', 'kind': 'qemu',
            'source_node': 'proxmox1', 'source_id': 8830, 'artifact_sha256': digest,
            'reservation': reservation, 'template_id': 147, 'allocation_node': 'proxmox2',
            'created_at': time.time()}

        class API:
            def __init__(self, resources=(), active=(), history=()):
                self.resources, self.active, self.history = resources, active, history
            def request(self, method, path, fields=None):
                if path == '/nodes':
                    return [{'node': f'proxmox{i}', 'status': 'online'} for i in range(1, 6)]
                if path == '/cluster/resources': return list(self.resources)
                if path.endswith('/tasks') and fields.get('source') == 'active': return list(self.active)
                if path.endswith('/tasks') and fields.get('source') == 'all': return list(self.history)
                raise AssertionError(path)

        catalog = {'images': {image_id: {'artifact_sha256': digest,
            'artifact': {'volume_id': volume, 'node': 'proxmox1', 'source_id': 8830}, 'replicas': []}}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = FileJournal(root / 'cache-journal')
            journal(record)
            pins = root / 'artifact-pins'; pins.mkdir()
            pin_path = pins / (job_id + '.json')
            pin_path.write_text(json.dumps({'volume_id': volume, 'artifact_sha256': digest,
                                            'job_id': job_id, 'state': 'active'}))
            ledger = Mock()
            ledger.get_job.return_value = {'state': 'running', 'project_id': 3,
                                           'allocations': [reservation]}
            with patch.object(cache_jobs, '_root', return_value=root):
                result = cache_jobs._reconcile_absent_cache_job(job_id, record.copy(),
                    catalog=catalog, api=API(), journal=journal, ledger=ledger)
            self.assertEqual(result['state'], 'released')
            ledger.reconcile_job.assert_called_once_with(job_id, operation_terminal=True,
                                                         allocations_reconciled=True)
            saved = json.loads((root / 'cache-journal' / (job_id + '.json')).read_text())
            self.assertEqual(saved['state'], 'reconciled-absent')
            self.assertTrue(saved['absence_verified'])
            self.assertFalse(pin_path.exists())

    def test_absent_cache_reconciliation_fails_closed_on_guest_or_task(self):
        import time
        import crange_cache_jobs as cache_jobs
        from crange_distribution import FileJournal
        digest = 'e' * 64; image_id = 'alpine-test'; job_id = 'cache-' + 'b' * 32
        volume = 'crange-library:backup/vzdump-qemu-8830.vma.zst'
        reservation = {'image': image_id, 'artifact_sha256': digest,
            'resolved': {'node_name': 'proxmox2', 'datastore_id': 'local-lvm'},
            'resources': {'memory_mb': 512, 'cpu_cores': 1, 'disk_gb': 42}}
        record = {'job_id': job_id, 'image_id': image_id, 'node': 'proxmox2',
            'datastore_id': 'local-lvm', 'state': 'allocated', 'kind': 'qemu',
            'source_node': 'proxmox1', 'source_id': 8830, 'artifact_sha256': digest,
            'reservation': reservation, 'template_id': 147, 'allocation_node': 'proxmox2',
            'created_at': time.time()}
        class API:
            def __init__(self, resources=(), active=(), history=()):
                self.resources, self.active, self.history = resources, active, history
            def request(self, method, path, fields=None):
                if path == '/nodes': return [{'node': f'proxmox{i}', 'status': 'online'} for i in range(1, 6)]
                if path == '/cluster/resources': return list(self.resources)
                if path.endswith('/tasks') and fields.get('source') == 'active': return list(self.active)
                if path.endswith('/tasks') and fields.get('source') == 'all': return list(self.history)
                raise AssertionError(path)
        catalog = {'images': {image_id: {'artifact_sha256': digest,
            'artifact': {'volume_id': volume, 'node': 'proxmox1', 'source_id': 8830}, 'replicas': []}}}
        for api in (API(resources=[{'vmid': 147}]), API(active=[{'id': '147'}]),
                    API(history=[{'id': '147', 'type': 'qmrestore', 'starttime': time.time()}])):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); journal = FileJournal(root / 'cache-journal'); journal(record)
                pins = root / 'artifact-pins'; pins.mkdir()
                (pins / (job_id + '.json')).write_text(json.dumps({'volume_id': volume,
                    'artifact_sha256': digest, 'job_id': job_id, 'state': 'active'}))
                ledger = Mock(); ledger.get_job.return_value = {'state': 'running', 'project_id': 3,
                                                               'allocations': [reservation]}
                with patch.object(cache_jobs, '_root', return_value=root), self.assertRaises(DistributionError):
                    cache_jobs._reconcile_absent_cache_job(job_id, record.copy(), catalog=catalog,
                        api=api, journal=journal, ledger=ledger)
                ledger.reconcile_job.assert_not_called()


class PublicationTests(unittest.TestCase):
    def test_catalog_transaction_preserves_existing_owner_group_and_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'catalog.json'
            path.write_text(json.dumps({'schema_version': 1, 'images': {}}))
            path.chmod(0o640)
            original = path.stat()
            real_chown = os.chown
            calls = []

            def record_chown(target, uid, gid):
                calls.append((uid, gid))
                real_chown(target, uid, gid)

            with patch('crange_distribution.os.chown', side_effect=record_chown):
                catalog_transaction(path, lambda catalog: catalog)

            replaced = path.stat()
            self.assertEqual(calls, [(original.st_uid, original.st_gid)])
            self.assertEqual((replaced.st_uid, replaced.st_gid),
                             (original.st_uid, original.st_gid))
            self.assertEqual(stat.S_IMODE(replaced.st_mode), 0o640)

    def test_projects_normalized_and_invalid_scope_rejected(self):
        self.assertEqual(normalize_projects(['3', 3]), [3])
        self.assertEqual(normalize_projects(['*']), ['*'])
        for bad_scope in (['3', '*'], ['*', 3], ['*', '*']):
            with self.subTest(scope=bad_scope), self.assertRaisesRegex(
                    DistributionError, 'cannot be mixed'):
                normalize_projects(bad_scope)
        for bad in ('60', '61', '0', '256', 'evil', True):
            with self.assertRaises(DistributionError): normalize_projects([bad])

    def test_catalog_accepts_universal_scope_and_rejects_mixed_or_malformed_scope(self):
        image = {'kind': 'vm', 'architecture': 'amd64', 'artifact_sha256': 'a'*64,
                 'source_revision': 'revision', 'verified': True, 'replicas': []}
        catalog = {'schema_version': 1, 'images': {'base-v1': {**image, 'approved_projects': ['*']}}}
        self.assertIs(validate_catalog(catalog), catalog)
        for scope in (['*', 3], [3, '*'], ['*', '*'], [True], [60], ['3']):
            invalid = {'schema_version': 1, 'images': {'base-v1': {**image, 'approved_projects': scope}}}
            with self.subTest(scope=scope), self.assertRaisesRegex(CatalogError, 'approval scope'):
                validate_catalog(invalid)

    def test_publication_cli_scope_flag_is_exclusive_and_maps_to_existing_wildcard(self):
        parser = argparse.ArgumentParser()
        _add_project_approval_arguments(parser)
        self.assertEqual(_project_approval_scope(parser.parse_args(['--all-projects'])), ['*'])
        self.assertEqual(_project_approval_scope(parser.parse_args(['--project', '3', '--project', '4'])), ['3', '4'])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            parser.parse_args(['--all-projects', '--project', '3'])
        self.assertEqual(error.exception.code, 2)

    def test_both_publication_commands_reject_mixed_cli_scopes(self):
        common = ['--catalog', '/tmp/catalog.json', '--journal-dir', '/tmp/journal',
                  '--policy', '/tmp/policy.json']
        commands = [
            ['publish', '--export-record', '/tmp/export.json', '--verification', '/tmp/verification.json',
             '--image-id', 'base-v1', '--source-revision', 'revision', '--datastore-id', 'local'],
            ['publish-stock', '--image-id', 'base-v1', '--node', 'proxmox1',
             '--template-file-id', 'local:vztmpl/base.tar.zst', '--source-revision', 'revision',
             '--sha256', 'a'*64, '--disk-gb', '8', '--verification', '/tmp/verification.json'],
        ]
        for command in commands:
            with self.subTest(command=command[0]), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as error:
                distribution_main(common + command + ['--project', '3', '--all-projects'])
            self.assertEqual(error.exception.code, 2)

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

    def test_stock_archive_can_be_published_for_all_projects(self):
        catalog = publish_stock_archive({'schema_version': 1, 'images': {}}, 'base-universal',
            node='proxmox1', template_file_id='local:vztmpl/base.tar.zst', architecture='amd64',
            source_revision='revision', approved_projects=['*'], disk_gb=8,
            expected_sha256='a'*64, hash_volume=Mock(return_value='a'*64),
            verification={'passed': True, 'evidence': 'reviewed clone test'})
        self.assertEqual(catalog['images']['base-universal']['approved_projects'], ['*'])

    def test_export_publication_keeps_virtual_size(self):
        record = dict(state='exported', complete_capture=True, kind='vm', node='proxmox1',
                      source_id=100, artifact_sha256='a'*64, volume_id='local:backup/export', disk_gb=32,
                      init_system='openrc')
        arguments = dict(architecture='amd64', source_revision='revision', approved_projects=['3'],
                         datastore_id='local-lvm', verification={'passed': True, 'evidence': 'clone test'})
        import base64, struct
        key = base64.b64encode(struct.pack('>I',11)+b'ssh-ed25519'+struct.pack('>I',32)+b'a'*32).decode()
        record['source_identity']={'machine_id':'a'*32,'ssh_host_key':'ssh-ed25519 '+key}
        record['identity_capture']={'state':'identity-captured','job_id':'capture-1','node':'proxmox1',
            'kind':'vm','source_id':100,'source_config_sha256':'c'*64,'source_identity':record['source_identity'],
            'init_system':'openrc','readiness':'default'}

        catalog = publish_exported_image({'schema_version': 1, 'images': {}}, 'vm-v1', record, **arguments)
        self.assertEqual(catalog['images']['vm-v1']['disk_gb'], 32)
        self.assertEqual(catalog['images']['vm-v1']['source_identity'],record['source_identity'])
        self.assertEqual(catalog['images']['vm-v1']['init_system'],'openrc')
        self.assertEqual(catalog['images']['vm-v1']['identity_capture']['init_system'],'openrc')
        invalid=copy.deepcopy(catalog);invalid['images']['vm-v1']['init_system']='openrc-ish'
        with self.assertRaisesRegex(CatalogError,'init_system'):
            validate_catalog(invalid)
        universal = publish_exported_image({'schema_version': 1, 'images': {}}, 'vm-universal', record,
            **{**arguments, 'approved_projects': ['*']})
        self.assertEqual(universal['images']['vm-universal']['approved_projects'], ['*'])
        del record['disk_gb']
        with self.assertRaisesRegex(DistributionError, 'disk size'):
            publish_exported_image({'schema_version': 1, 'images': {}}, 'vm-v2', record, **arguments)

class ProbeTests(unittest.TestCase):
    def test_probe_attempts_stop_even_when_start_task_times_out(self):
        api = Mock()
        api.request.side_effect = lambda method, path, fields=None: (
            {'members': [{'type': 'qemu', 'node': 'proxmox1', 'vmid': 990}]}
            if path == '/pools/crange-probes' else
            {'tags': 'crange-image', 'net0': 'virtio=aa:bb:cc:dd:ee:ff,bridge=old'}
            if method == 'GET' else 'UPID:probe')
        api.task.side_effect = [TimeoutError('start uncertain'), None]
        verifier = ProbeVerifier('crprobe1450', ['crprobe1450'],
            {'machine_id': 'a' * 32, 'ssh_host_key': 'ssh-ed25519 oldkey source'})
        with self.assertRaises(TimeoutError):
            verifier(api, 'proxmox1', 'qemu', 990)
        api.request.assert_any_call('POST', '/nodes/proxmox1/qemu/990/status/stop', {})

    def test_ct_probe_uses_disconnected_approved_network_and_real_identities(self):
        api = Mock()
        api.request.side_effect = lambda method, path, fields=None: (
            {'members':[{'type':'lxc','node':'proxmox2','vmid':201}]} if path == '/pools/crange-probes' else
            {'tags': 'crange-image', 'net0': 'name=eth0,bridge=p003t254'}
            if method == 'GET' else 'UPID:probe')
        execute = Mock(side_effect=['running', 'b'*32, 'ssh-ed25519 newkey guest'])
        verifier = ProbeVerifier('vmbrtest', ['vmbrtest'],
            {'machine_id': 'a'*32, 'ssh_host_key': 'ssh-ed25519 oldkey source'}, ct_exec=execute)
        result = verifier(api, 'proxmox2', 'lxc', 201)
        self.assertTrue(result['identity_unique'])
        update = api.request.call_args_list[2]
        self.assertIn('link_down=1', update.args[2]['net0'])
        self.assertIn('bridge=vmbrtest', update.args[2]['net0'])
        self.assertEqual(result['evidence']['machine_id'], 'b'*32)

    def test_probe_outside_dedicated_pool_is_refused_before_guest_operations(self):
        api=Mock();api.request.return_value={'members':[]}
        execute=Mock()
        verifier=ProbeVerifier('vmbrtest',['vmbrtest'],{'machine_id':'a'*32,'ssh_host_key':'ssh-ed25519 oldkey source'},ct_exec=execute)
        with self.assertRaisesRegex(DistributionError,'pool membership'):
            verifier(api,'proxmox2','lxc',201)
        execute.assert_not_called()
        api.request.assert_called_once_with('GET','/pools/crange-probes')

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
