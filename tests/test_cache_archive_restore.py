import json
from pathlib import Path
from urllib.parse import unquote
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_cache_restore as recovery
import crange_jobs


class ArchiveRestoreRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'artifact-pins').mkdir()
        self.job = 'cache-' + 'a' * 32
        self.image = {'kind': 'vm', 'artifact_sha256': 'b' * 64, 'approved_projects': [100],
                      'artifact': {'complete_capture': True, 'node': 'proxmox1', 'source_id': 151,
                                   'volume_id': 'crange-library:backup/source.vma.zst'}, 'replicas': []}
        self.policy = {'allowed_nodes': ['proxmox5', 'proxmox6'],
                       'approved_storage': [['proxmox5', 'crange-thin'], ['proxmox6', 'local-hdd']],
                       'cache': {'tested_paths': ['local-linked-vm', 'vm-archive-restore'],
                                 'isolated_bridges': ['crprobe1450']}}
        self.allocation = {'operation': 'cache_prepare', 'image': 'image', 'artifact_sha256': 'b' * 64,
                           'resolved': {'node_name': 'proxmox5', 'datastore_id': 'crange-thin'}}
        self.record = {'job_id': self.job, 'image_id': 'image', 'node': 'proxmox5',
                       'allocation_node': 'proxmox5', 'datastore_id': 'crange-thin', 'kind': 'qemu',
                       'template_id': 162, 'state': 'allocated', 'artifact_sha256': 'b' * 64,
                       'source_node': 'proxmox1', 'source_id': 151, 'reservation': self.allocation,
                       'provider_tasks': [{'node': 'proxmox5', 'state': 'running',
                           'upid': 'UPID:proxmox5:001411FF:00D7CCD1:6AD228CD:qmrestore:162:crange-iac@pve!terraform:'}]}
        self.pin = {'job_id': self.job, 'state': 'active', 'artifact_sha256': 'b' * 64,
                    'volume_id': self.image['artifact']['volume_id']}
        self.config = {'name': 'crange-replica-162', 'tags': 'crange-image;crange-cache-162',
                       'template': 1, 'net0': 'e1000=AA:BB:CC:DD:EE:FF,bridge=crprobe1450,link_down=0',
                       'efidisk0': 'crange-thin:base-162-disk-0,size=4M',
                       'scsi0': 'crange-thin:base-162-disk-1,size=64G', 'ide2': 'none,media=cdrom'}
        self.resource = {'vmid': 162, 'node': 'proxmox5', 'type': 'qemu',
                         'pool': 'crange-iac', 'status': 'stopped'}
        self.task_status = {'status': 'stopped', 'exitstatus': 'OK'}
        self.active = []
        self.ledger = Mock()
        self.ledger.get_job.side_effect = lambda _: {'project_id': 100, 'state': 'running',
                                                   'allocations': [self.allocation]}
        self.api = Mock()
        def request(method, path, fields=None):
            if method == 'PUT': return {}
            if '/tasks/' in path: return self.task_status
            if path.endswith('/tasks'): return self.active
            if path == '/cluster/resources': return [self.resource]
            if path.endswith('/config'): return self.config
            raise AssertionError((method, path))
        self.api.request.side_effect = request
        self.journal = Mock()
        self.patches = [patch.object(recovery.jobs, '_root', return_value=self.root),
                        patch.object(recovery.jobs, 'shared_reservation_validator', return_value=lambda *a: True),
                        patch.object(recovery.jobs, 'required_capacity', return_value={}),
                        patch.object(recovery.jobs, 'select_probe_bridge', return_value='crprobe1450'),
                        patch.object(recovery.jobs, '_cache_probe_identity', return_value=({'os_family': 'windows'}, 'windows', False)),
                        patch.object(recovery.jobs, 'proved_linked_storage', return_value=[]),
                        patch.object(recovery.jobs, 'ProbeVerifier'), patch.object(recovery.jobs, 'ReplicaWorker')]
        mocks = [p.start() for p in self.patches]
        for p in self.patches: self.addCleanup(p.stop)
        self.worker = mocks[-1]
        self.worker.return_value.resume_template.return_value = {'verified': True}

    def run_recovery(self):
        (self.root / 'artifact-pins' / (self.job + '.json')).write_text(json.dumps(self.pin))
        return recovery._resume(self.api, self.ledger, self.journal, self.policy,
                                {'images': {'image': self.image}}, self.record)

    def assert_refused_before_mutation(self):
        with self.assertRaises(recovery.jobs.DistributionError): self.run_recovery()
        self.assertTrue(all(c.args[0] == 'GET' for c in self.api.request.call_args_list))
        self.worker.assert_not_called()
        self.journal.assert_not_called()

    def test_completed_windows_restore_adopted_once_then_delegates_fresh_probe(self):
        self.assertEqual(self.run_recovery(), {'verified': True})
        writes = [c for c in self.api.request.call_args_list if c.args[0] != 'GET']
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].args[:2], ('PUT', '/nodes/proxmox5/qemu/162/config'))
        self.assertIn(self.job, writes[0].args[2]['description'])
        self.assertEqual(self.record['state'], 'converting-template')
        self.assertEqual(self.record['provider_tasks'][0]['state'], 'confirmed-terminal')
        self.worker.return_value.resume_template.assert_called_once()
        self.ledger.reconcile_job.assert_not_called()

    def test_readonly_shared_iso_is_not_a_writable_disk_but_cloudinit_stays_owned(self):
        self.config['ide0']='crange-thin:vm-162-cloudinit,media=cdrom,size=4M'
        self.config['ide2']='crange-library:iso/Parrot-security-7.3_amd64.iso,media=cdrom,size=8G'
        self.assertEqual(self.run_recovery(),{'verified':True})

    def test_foreign_cloudinit_or_writable_iso_never_bypasses_volume_ownership(self):
        for key,value in [('ide0','crange-thin:vm-999-cloudinit,media=cdrom,size=4M'),
                          ('ide2','crange-library:iso/Parrot-security-7.3_amd64.iso,size=8G'),
                          ('ide2','foreign-store:iso/image.iso,media=cdrom,size=8G')]:
            with self.subTest(value=value):
                previous=dict(self.config);self.config[key]=value
                self.assert_refused_before_mutation()
                self.config.clear();self.config.update(previous)

    def prepare_pending_probe(self):
        self.image.update(memory_mb=6000,cpu_cores=4,init_system='systemd')
        self.record.update(state='verifying',probe_id=163,probe_pool='crange-probes',
                           probe_candidate={'memory_mb':2048,'cpu_cores':2})
        self.record['provider_tasks'].append({'node':'proxmox5','state':'confirmed-terminal',
            'upid':'UPID:proxmox5:001411FF:00D7CCD1:6AD228CD:qmclone:162:crange-iac@pve!terraform:'})
        self.config['description']=recovery.cache_description(self.job,'image')
        self.probe={'name':'crange-probe-163','tags':'crange-image;crange-cache-162',
                    'description':f'CRANGE verification {self.job}','template':0,
                    'scsi0':'crange-thin:vm-163-disk-0,size=64G',
                    'ide0':'crange-thin:vm-163-cloudinit,media=cdrom,size=4M'}
        self.probe_status={'status':'stopped'}
        original=self.api.request.side_effect
        def request(method,path,fields=None):
            if path.endswith('/qemu/163/config') and method=='GET':return self.probe
            if path.endswith('/qemu/163/status/current'):return self.probe_status
            if path=='/cluster/resources':
                return [self.resource,{'vmid':163,'node':'proxmox5','type':'qemu','pool':'crange-probes','status':'stopped'}]
            return original(method,path,fields)
        self.api.request.side_effect=request

    def test_prestart_probe_metadata_failure_resumes_exact_clone_without_second_clone(self):
        self.prepare_pending_probe()
        self.assertEqual(self.run_recovery(),{'verified':True})
        self.assertEqual(self.record['state'],'verifying')
        changes=[c for c in self.api.request.call_args_list if c.args[0]!='GET']
        self.assertEqual(len(changes),2)
        self.assertTrue(all(c.args[0]=='PUT' for c in changes))
        self.assertEqual(changes[0].args[1],'/nodes/proxmox5/qemu/163/config')
        self.assertEqual(changes[0].args[2]['tags'],'crange-cache-163;crange-image')

    def test_prestart_resume_rejects_wrong_probe_identity_before_any_mutation(self):
        self.prepare_pending_probe();self.probe['name']='unrelated-server'
        self.assert_refused_before_mutation()

    def test_prestart_resume_rejects_any_start_task_or_foreign_disk(self):
        self.prepare_pending_probe()
        self.record['provider_tasks'].append({'node':'proxmox5','upid':'UPID:proxmox5:1:2:3:qmstart:163:user:'})
        self.assert_refused_before_mutation()
        self.record['provider_tasks'].pop();self.probe['scsi0']='crange-thin:vm-999-disk-0,size=64G'
        self.assert_refused_before_mutation()
        self.assertTrue((self.root / 'artifact-pins' / (self.job + '.json')).exists())

    def prepare_failed_machine_probe(self):
        self.prepare_pending_probe()
        self.probe['machine'] = 'pc-q35-10.1+pve0'
        for operation in ('qmstart', 'qmstop'):
            self.record['provider_tasks'].append({'node': 'proxmox5',
                'path': '/nodes/proxmox5/qemu/163/status/' + ('start' if operation == 'qmstart' else 'stop'),
                'upid': f'UPID:proxmox5:1:2:3:{operation}:163:user:'})
        original = self.api.request.side_effect
        self.machine_error = "Installed QEMU version '10.0.2' is too old to run machine type 'pc-q35-10.1+pve0', please upgrade node 'proxmox5'"
        def request(method, path, fields=None):
            if '/tasks/' in path and ':qmstart:' in unquote(path):
                return {'status': 'stopped', 'exitstatus': self.machine_error}
            if path.endswith('/capabilities/qemu/machines'):
                return [{'id': 'pc-q35-9.2+pve1', 'type': 'q35'}]
            return original(method, path, fields)
        self.api.request.side_effect = request

    def test_exact_failed_machine_start_reuses_owned_probe(self):
        self.prepare_failed_machine_probe()
        self.assertEqual(self.run_recovery(), {'verified': True})
        self.assertTrue(all(c.args[0] in ('GET', 'PUT') for c in self.api.request.call_args_list))
        self.assertEqual(self.record['probe_id'], 163)

    def test_real_provider_history_without_candidate_or_node_and_implicit_pve_version(self):
        self.prepare_failed_machine_probe()
        self.record.pop('probe_candidate')
        self.probe['machine'] = 'pc-q35-10.1'
        for task in self.record['provider_tasks'][2:]: task.pop('node')
        self.assertEqual(self.run_recovery(), {'verified': True})

    def test_generic_boot_failure_and_changed_machine_are_not_recoverable(self):
        self.prepare_failed_machine_probe()
        self.machine_error = 'boot failed'
        self.assert_refused_before_mutation()

    def test_changed_probe_machine_rejected_before_mutation(self):
        self.prepare_failed_machine_probe()
        self.probe['machine'] = 'pc-q35-9.2+pve1'
        self.assert_refused_before_mutation()

    def test_node_and_storage_are_policy_derived(self):
        self.record.update(node='proxmox6', allocation_node='proxmox6', datastore_id='local-hdd')
        self.allocation['resolved'] = {'node_name': 'proxmox6', 'datastore_id': 'local-hdd'}
        self.record['provider_tasks'][0]['node'] = 'proxmox6'
        self.record['provider_tasks'][0]['upid'] = self.record['provider_tasks'][0]['upid'].replace('proxmox5', 'proxmox6')
        self.resource['node'] = 'proxmox6'
        self.config = {k: v.replace('crange-thin:', 'local-hdd:') if isinstance(v, str) else v for k,v in self.config.items()}
        self.assertEqual(self.run_recovery(), {'verified': True})
        self.assertEqual(self.worker.call_args.kwargs['approved_storage'], [('proxmox6', 'local-hdd')])

    def test_active_or_failed_restore_never_adopted(self):
        self.task_status = {'status': 'running'}
        self.assert_refused_before_mutation()
        self.task_status = {'status': 'stopped', 'exitstatus': 'ERROR'}
        self.assert_refused_before_mutation()

    def test_changed_archive_pin_or_image_rejected(self):
        self.pin['artifact_sha256'] = 'c'*64
        self.assert_refused_before_mutation()

    def test_reused_vmid_or_foreign_pool_rejected(self):
        self.resource['pool'] = 'unrelated'
        self.assert_refused_before_mutation()

    def test_active_followup_task_rejected(self):
        self.active = [{'id': '162', 'type': 'qmclone'}]
        self.assert_refused_before_mutation()

    def test_foreign_disk_or_management_bridge_rejected(self):
        self.config['scsi0'] = 'crange-thin:base-999-disk-1,size=64G'
        self.assert_refused_before_mutation()
        self.config['scsi0'] = 'crange-thin:base-162-disk-1,size=64G'
        self.config['net0'] = 'bridge=vmbr0'
        self.assert_refused_before_mutation()

    def test_different_cache_tag_rejected(self):
        self.config['tags'] += ';crange-cache-999'
        self.assert_refused_before_mutation()

    def test_unknown_task_or_preexisting_probe_rejected(self):
        self.record['provider_tasks'][0]['upid'] = self.record['provider_tasks'][0]['upid'].replace('qmrestore:162', 'qmrestore:999')
        self.assert_refused_before_mutation()

    def test_fixed_dashboard_job_accepts_only_admin_platform_scope(self):
        argv = crange_jobs.command('template-cache-restore-resume', 0, {'source_job_id': self.job})
        self.assertIn('cache-restore-resume', argv)
        with self.assertRaises(ValueError):
            crange_jobs.command('template-cache-restore-resume', 100, {'source_job_id': self.job})
        with self.assertRaises(ValueError):
            crange_jobs.command('template-cache-restore-resume', 0, {'source_job_id': '../other'})
