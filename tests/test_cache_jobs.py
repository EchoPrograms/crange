import base64
import struct
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_cache_jobs as jobs
from crange_distribution import FileJournal, DistributionError
from crange_image_cache import CacheError


class CacheJobTests(unittest.TestCase):
    def test_snapshot_resume_retains_offline_or_router_only_cache_without_blocking_other_work(self):
        from crange_placement import ReservationLedger
        for node,status,restricted,resumed in [('proxmox1','offline',False,False),
                ('proxmox1','online',True,False),('proxmox2','online',False,True)]:
            with self.subTest(node=node,status=status,restricted=restricted),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);journal=root/'cache-journal';journal.mkdir()
                allocation={'operation':'cache_prepare','image':'image-v1'}
                ledger=ReservationLedger(root/'reservations.sqlite3')
                ledger.db.execute('INSERT INTO jobs(job_id,project_id,state,allocations) VALUES(?,?,?,?)',
                                  ('cache-123',100,'running',json.dumps([allocation])))
                ledger.close()
                (journal/'cache-123.json').write_text(json.dumps({'state':'verifying','kind':'qemu',
                    'node':node,'image_id':'image-v1'}))
                policy={'allowed_nodes':['proxmox1','proxmox2'],'router_only_nodes':['proxmox1'] if restricted else []}
                catalog={'images':{'image-v1':{}}}
                api=Mock();api.request.return_value=[{'node':node,'status':status}]
                with patch.object(jobs,'_root',return_value=root), \
                     patch.object(jobs,'_settings',return_value=(policy,catalog)), \
                     patch.object(jobs,'_cache_probe_identity',return_value=({'identity_mode':'windows-scenario-snapshot-v1'},None,False)), \
                     patch.object(jobs,'ProxmoxAPI',return_value=api), \
                     patch.object(jobs,'resume_snapshot_cache') as resume:
                    jobs.resume_project_snapshot_caches(100,{'image-v1'})
                self.assertEqual(resume.call_count,int(resumed))
                retained=ReservationLedger(root/'reservations.sqlite3')
                self.assertEqual(retained.get_job('cache-123')['state'],'running')
                retained.close()
                self.assertTrue((journal/'cache-123.json').exists())

    def test_sealed_windows_cache_uses_exact_null_sid_capture(self):
        capture = {'provenance_type': 'sealed-windows-source-v1', 'source_identity': None,
                   'probes': [{'identity': {'machine_guid': 'g', 'machine_sid': 's',
                                             'local_administrator_sid': 'a'}}]}
        identity, profile, sealed = jobs._cache_probe_identity({
            'identity_capture': capture, 'init_system': 'systemd'})
        self.assertIs(identity, capture)
        self.assertEqual(profile, 'windows')
        self.assertTrue(sealed)
        self.assertIsNone(identity['source_identity'])

    def test_ready_generation_reused_without_new_ledger_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = {'schema_version': 1, 'images': {'base-v1': {
                'kind': 'vm', 'architecture': 'amd64', 'artifact_sha256': 'a'*64,
                'source_revision': 'rev', 'verified': True, 'approved_projects': [3], 'disk_gb': 8,
                'replicas': [{'node': 'proxmox2', 'template_id': 200, 'datastore_id': 'local-lvm',
                    'state': 'ready', 'verified_at': 'now', 'artifact_sha256': 'a'*64,
                    'cache_local': True, 'clone_modes_verified': ['linked'], 'job_id': 'prepare'}]}}}
            (root / 'image-catalog.json').write_text(json.dumps(catalog))
            api = Mock()
            api.request.side_effect = lambda method, path, fields=None: (
                {'template': 1, 'tags': 'crange-image;crange-cache-200',
                 'description': 'CRANGE distribution prepare image base-v1'}
                if path.endswith('/config') else {'status': 'stopped'})
            with patch.object(jobs.scheduling, 'DATA', root), patch.object(jobs.scheduling, 'CONFIG', root), \
                 patch.object(jobs, '_settings', return_value=({'enabled': True}, catalog)), \
                 patch.object(jobs, 'ProxmoxAPI', return_value=api):
                result = jobs.prepare_cache(project_id=3, image_id='base-v1', node_name='proxmox2')
            self.assertTrue(result['reused'])
            self.assertEqual(result['template_id'], 200)
            self.assertEqual(api.request.call_count, 2)

    def test_ready_cache_with_drifted_image_metadata_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = {'schema_version': 1, 'images': {'base-v1': {
                'kind': 'vm', 'architecture': 'amd64', 'artifact_sha256': 'a'*64,
                'source_revision': 'rev', 'verified': True, 'approved_projects': [3], 'disk_gb': 8,
                'replicas': [{'node': 'proxmox2', 'template_id': 200, 'datastore_id': 'local-lvm',
                    'state': 'ready', 'verified_at': 'now', 'artifact_sha256': 'b'*64,
                    'cache_local': True, 'clone_modes_verified': ['linked'], 'job_id': 'prepare'}]}}}
            (root / 'image-catalog.json').write_text(json.dumps(catalog))
            with patch.object(jobs.scheduling, 'DATA', root), patch.object(jobs.scheduling, 'CONFIG', root), \
                 patch.object(jobs, '_settings', return_value=({'enabled': True}, catalog)), \
                 patch.object(jobs, 'ProxmoxAPI', return_value=Mock()) as api:
                with self.assertRaisesRegex(DistributionError, 'metadata'):
                    jobs.prepare_cache(project_id=3, image_id='base-v1', node_name='proxmox2')
            api.return_value.request.assert_not_called()

    def test_running_ready_cache_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = {'schema_version': 1, 'images': {'base-v1': {
                'kind': 'vm', 'architecture': 'amd64', 'artifact_sha256': 'a'*64,
                'source_revision': 'rev', 'verified': True, 'approved_projects': [3], 'disk_gb': 8,
                'replicas': [{'node': 'proxmox2', 'template_id': 200, 'datastore_id': 'local-lvm',
                    'state': 'ready', 'verified_at': 'now', 'artifact_sha256': 'a'*64,
                    'cache_local': True, 'clone_modes_verified': ['linked'], 'job_id': 'prepare'}]}}}
            (root / 'image-catalog.json').write_text(json.dumps(catalog))
            api = Mock()
            api.request.side_effect = lambda method, path, fields=None: (
                {'template': 1, 'tags': 'crange-image;crange-job-prepare'}
                if path.endswith('/config') else {'status': 'running'})
            with patch.object(jobs.scheduling, 'DATA', root), patch.object(jobs.scheduling, 'CONFIG', root), \
                 patch.object(jobs, '_settings', return_value=({'enabled': True}, catalog)), \
                 patch.object(jobs, 'ProxmoxAPI', return_value=api):
                with self.assertRaisesRegex(DistributionError, 'not stopped'):
                    jobs.prepare_cache(project_id=3, image_id='base-v1', node_name='proxmox2')
            self.assertEqual(api.request.call_count, 2)

    def test_cache_preparation_uses_shared_capacity_and_reconciles_only_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = {'kind': 'vm', 'architecture': 'amd64', 'artifact_sha256': 'a'*64,
                'source_revision': 'rev', 'verified': True, 'approved_projects': [3],
                'disk_gb': 8, 'memory_mb': 512, 'cpu_cores': 1, 'replicas': [],
                'source_identity': {'machine_id': 'a'*32, 'ssh_host_key': 'ssh-ed25519 '+base64.b64encode(struct.pack('>I',11)+b'ssh-ed25519'+struct.pack('>I',32)+b'a'*32).decode()},
                'artifact': {'node': 'proxmox1', 'source_id': 100, 'complete_capture': True,
                             'volume_id': 'crange-library:backup/vzdump-qemu-100.vma.zst'}}
            catalog = {'schema_version': 1, 'images': {'base-v1': image}}
            (root / 'image-catalog.json').write_text(json.dumps(catalog))
            policy = {'enabled': True, 'revision': 'v1', 'allowed_nodes': ['proxmox2'],
                'allowed_storages': ['local-lvm'], 'cache': {'isolated_bridge': 'vmbrtest',
                'isolated_bridges': ['vmbrtest'], 'tested_paths': ['local-linked-vm', 'vm-archive-restore']}}
            inventory = {'captured_at': jobs.time.time(), 'quota_usage': {}, 'guests': {},
                'nodes': {'proxmox2': {'online': True, 'memory_mb': 16384, 'cpu_cores': 16,
                'committed_memory_mb': 0, 'observed_memory_mb': 1024, 'committed_cpu_cores': 0,
                'architectures': ['amd64'], 'guest_types': ['vm'], 'storages': {'local-lvm': {'available': True,
                'guest_types': ['vm'], 'snapshots_verified': True, 'clone_modes': ['linked'],
                'free_gb': 500, 'total_gb': 500, 'committed_gb': 0}}}}}
            allocation_seen = []
            def factory(api, journal, verify, **kwargs):
                worker = Mock()
                def prepare(catalog, image_id, **inputs):
                    allocation_seen.append(inputs['reservation'])
                    self.assertTrue(kwargs['reservation_validator'](inputs['reservation'], 'proxmox2', 'local-lvm',
                                                                    jobs.required_capacity(image)))
                    replica = {'node': 'proxmox2', 'template_id': 200, 'datastore_id': 'local-lvm',
                        'state': 'ready', 'verified_at': 'now', 'artifact_sha256': 'a'*64,
                        'cache_local': True, 'clone_modes_verified': ['linked'], 'job_id': 'prepare'}
                    catalog['images']['base-v1']['replicas'].append(replica)
                    initial = {'job_id': 'prepare', 'state': 'allocated', 'template_id': 200, 'probe_id': 201,
                               'provider_tasks': [], 'image_id': image_id, 'node': 'proxmox2', 'datastore_id': 'local-lvm'}
                    journal(initial)
                    converting = {**initial, 'state': 'converting-template'}
                    journal(converting)
                    verifying = {**converting, 'state': 'verifying'}
                    journal(verifying)
                    journal({**verifying, 'state': 'ready'})
                    return catalog
                worker.prepare.side_effect = prepare
                return worker
            final_inventory = {**inventory, 'guests': {'200': {'template': True, 'node_name': 'proxmox2'}}}
            with patch.object(jobs.scheduling, 'DATA', root), patch.object(jobs.scheduling, 'CONFIG', root), \
                 patch.object(jobs, '_settings', return_value=(policy, catalog)), \
                 patch.object(jobs, 'ProxmoxAPI', return_value=Mock()), \
                 patch.object(jobs.scheduling, 'discover', side_effect=[inventory, final_inventory]), \
                 patch.object(jobs, 'ReplicaWorker', side_effect=factory):
                result = jobs.prepare_cache(project_id=3, image_id='base-v1', node_name='proxmox2', job_id='prepare',
                    additional_capacity={'memory_mb': 1024, 'cpu_cores': 2, 'disk_gb': 8})
            self.assertFalse(result['reused'])
            self.assertEqual(allocation_seen[0]['resources']['memory_mb'], 1024)
            self.assertEqual(allocation_seen[0]['resources']['cpu_cores'], 2)
            self.assertAlmostEqual(allocation_seen[0]['resources']['disk_gb'], 16.8)
            ledger = jobs.ReservationLedger(root / 'reservations.sqlite3')
            try: self.assertEqual(ledger.get_job('prepare')['state'], 'released')
            finally: ledger.close()

    def test_failed_artifact_pin_is_durable(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(jobs.scheduling, 'DATA', Path(directory)):
            with self.assertRaises(TimeoutError):
                with jobs.artifact_pin('crange-library:backup/example', 'a'*64, 'prepare'):
                    raise TimeoutError('uncertain provider task')
            self.assertTrue((Path(directory) / 'artifact-pins/prepare.json').exists())

    def test_collector_checks_snapshots_pending_backings_and_physical_orphans(self):
        api = Mock()
        def request(method, path, fields=None):
            if path == '/access/permissions': return {'/vms': {'VM.Audit': 1}}
            if path == '/nodes': return [{'node': 'proxmox2', 'status': 'online'}]
            if path == '/cluster/resources': return [{'node': 'proxmox2', 'type': 'qemu', 'vmid': 200, 'status': 'stopped'},
                                                       {'node': 'proxmox2', 'type': 'qemu', 'vmid': 201, 'status': 'stopped'}]
            if path.endswith('/tasks'): return []
            if path.endswith('/storage'): return [{'storage': 'local-lvm', 'active': 1, 'content': 'images,rootdir'}]
            if path.endswith('/content'): return [{'volid': 'local-lvm:vm-202-disk-0', 'content': 'images'}]
            if path.endswith('/snapshot'): return [{'name': 'current'}, {'name': 'saved'}] if '/201/' in path else [{'name': 'current'}]
            if path.endswith('/config'):
                if '/200/' in path: return {'scsi0': 'local-lvm:base-200-disk-0,size=8G'}
                if fields and fields.get('snapshot'): return {'scsi0': 'local-lvm:vm-201-disk-0,size=8G'}
                return {'scsi0': 'local-lvm:vm-201-disk-0,size=8G'}
            raise AssertionError(path)
        api.request.side_effect = request
        inspector = Mock(return_value={'complete': True, 'volumes': {
            'local-lvm:base-200-disk-0': ['local-lvm:base-200-disk-0'],
            'local-lvm:vm-201-disk-0': ['local-lvm:vm-201-disk-0', 'local-lvm:base-200-disk-0'],
            'local-lvm:vm-202-disk-0': ['local-lvm:vm-202-disk-0', 'local-lvm:base-200-disk-0']},
            'physical_dependencies': []})
        catalog = {'images': {'base-v1': {'artifact_sha256': 'a'*64,
            'artifact': {'volume_id': 'crange-library:backup/example'}}}}
        with tempfile.TemporaryDirectory() as directory:
            evidence = jobs.collect_cache_evidence(api, catalog, 'base-v1', 'proxmox2', 200,
                ledger=Mock(status=Mock(return_value=[])), journal=FileJournal(Path(directory)),
                policy={'expected_cluster_nodes': ['proxmox2']}, backing_inspector=inspector, hasher=Mock(return_value='a'*64))
        self.assertTrue(evidence['snapshots_complete'])
        self.assertIn('local-lvm:base-200-disk-0', evidence['snapshots'][0]['backing_volumes'])
        self.assertTrue(any(str(g['vmid']).startswith('physical:') for g in evidence['guests']))
        self.assertTrue(evidence['central_verification']['verified'])

    def test_offline_cluster_node_makes_evidence_incomplete(self):
        api = Mock()
        api.request.side_effect = lambda method, path, fields=None: ({'/vms': {'VM.Audit': 1}} if path == '/access/permissions' else [{'node': 'proxmox2', 'status': 'offline'}])
        with self.assertRaisesRegex(CacheError, 'Every cluster node'):
            jobs.collect_cache_evidence(api, {}, 'base-v1', 'proxmox2', 200,
                                        ledger=Mock(), journal=Mock(), policy={})

class PreallocationRecoveryTests(unittest.TestCase):
    def test_only_exact_journal_free_cache_reservation_is_released(self):
        from contextlib import nullcontext
        from crange_placement import ReservationLedger
        identity='cache-'+'a'*32
        item={'operation':'cache_prepare','image':'base','artifact_sha256':'b'*64,
              'resolved':{'node_name':'proxmox5','datastore_id':'crange-thin'},
              'resources':{'memory_mb':4096,'cpu_cores':2,'disk_gb':134},
              'resource_key':'cache:base:proxmox5:crange-thin','reservation_id':'reservation'}
        for has_journal,foreign_pin,project,expected in [(False,False,100,[identity]),(True,False,100,[]),(False,True,100,[]),(False,False,101,[])]:
            with self.subTest(journal=has_journal,foreign=foreign_pin,project=project),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);ledger=ReservationLedger(root/'reservations.sqlite3')
                ledger.db.execute('INSERT INTO jobs (job_id,project_id,owner,state,expires,heartbeat,allocations) VALUES (?,?,?,?,?,?,?)',(identity,100,'admin','running',9999999999,0,json.dumps([item])));ledger.close()
                journal=root/'cache-journal';journal.mkdir()
                if has_journal:(journal/(identity+'.json')).write_text('{}')
                pins=root/'artifact-pins';pins.mkdir();pin=pins/(identity+'.json')
                pin.write_text(json.dumps({'job_id':identity,'state':'active','volume_id':'crange-library:backup/test',
                    'artifact_sha256':'c'*64 if foreign_pin else 'b'*64}))
                policy={'enabled':True,'allowed_nodes':['proxmox5']}
                catalog={'images':{'base':{'artifact_sha256':'b'*64,'artifact':{'volume_id':'crange-library:backup/test'},'replicas':[]}}}
                with patch.object(jobs,'_root',return_value=root),patch.object(jobs,'_settings',return_value=(policy,catalog)),patch.object(jobs.scheduling,'node_locks',return_value=nullcontext()):
                    self.assertEqual(jobs.reconcile_unstarted_caches(project),expected)
                ledger=ReservationLedger(root/'reservations.sqlite3');state=ledger.db.execute('SELECT state FROM jobs').fetchone()[0];ledger.close()
                self.assertEqual(state,'released' if expected else 'running')
                self.assertEqual(pin.exists(),not bool(expected))

    def test_native_storage_proof_must_match_exact_destination(self):
        proof={'node':'proxmox5','storage':'crange-thin','state':'ready','snapshots_verified':True,'proof_volumes_deleted':True}
        policy={'nodes':{'proxmox5':{'storage_proofs':{'crange-thin':proof},'snapshot_storages':['crange-thin']}}}
        self.assertEqual(jobs.proved_linked_storage(policy,'proxmox5','crange-thin'),[('proxmox5','crange-thin')])
        self.assertEqual(jobs.proved_linked_storage(policy,'proxmox1','crange-thin'),[])
        proof['proof_volumes_deleted']=False
        self.assertEqual(jobs.proved_linked_storage(policy,'proxmox5','crange-thin'),[])
