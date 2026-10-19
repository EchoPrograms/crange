import base64
import copy
import pathlib
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, call
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from crange_distribution import (capture_source_identity, export_image, DistributionError,
                                 _source_config_identity, FileJournal, ProbeVerifier, _fixed_guest_read)
from crange_catalog import (validate_source_identity, validate_sealed_windows_capture,
                            validate_sealed_windows_proof_retirement, SEALED_WINDOWS_IMAGE_ID, CatalogError)

KEY='ssh-ed25519 '+base64.b64encode(struct.pack('>I',11)+b'ssh-ed25519'+struct.pack('>I',32)+b'a'*32).decode()


class CollectorFailureTests(unittest.TestCase):
    def test_failed_readiness_names_state_without_bypassing_failure(self):
        api = Mock()
        api.request.side_effect = [{'pid': 4}, {'exited': True, 'exitcode': 1,
            'out-data': 'degraded\n', 'err-data': 'private diagnostic'}]
        with self.assertRaisesRegex(DistributionError,
                'systemctl is-system-running failed .*exit 1; state: degraded') as caught:
            _fixed_guest_read(api, 'proxmox6', 'qemu', 175,
                ['/usr/bin/systemctl', 'is-system-running'], None, 10)
        self.assertNotIn('private diagnostic', str(caught.exception))

    def test_other_collector_output_is_not_copied_into_job_error(self):
        api = Mock()
        api.request.side_effect = [{'pid': 4}, {'exited': True, 'exitcode': 2,
            'out-data': 'private value', 'err-data': 'private error'}]
        with self.assertRaisesRegex(DistributionError, 'collector cat failed') as caught:
            _fixed_guest_read(api, 'proxmox6', 'qemu', 175,
                ['/bin/cat', '/etc/machine-id'], None, 10)
        self.assertNotIn('private', str(caught.exception))

WINDOWS_SOURCE={'os_family':'windows','machine_guid':'bdce686e-757d-492c-8a6a-0451c365d0ea',
    'machine_sid':'S-1-5-21-1429816746-3004255909-1098465845',
    'local_administrator_sid':'S-1-5-21-1429816746-3004255909-1098465845-500',
    'computer_name':'MHL-WINBASE','os_caption':'Microsoft Windows Server 2025 Standard Evaluation',
    'installation_type':'Server','qemu_guest_agent_automatic':True,
    'term_service_automatic':True,'winrm_service_automatic':True,
    'os_version':'10.0.26100','os_build':'26100','part_of_domain':False,
    'image_state':'IMAGE_STATE_COMPLETE',
    'oobe_in_progress':0,'system_setup_in_progress':0,'setup_type':0,
    'qemu_guest_agent':True,'term_service_running':True,'winrm_service_running':True,
    'rdp_allowed_sources':['10.60.0.20']}

class CaptureTests(unittest.TestCase):
    def test_sealed_windows_capture_uses_two_clone_identities_and_no_source_identity(self):
        first = dict(WINDOWS_SOURCE, machine_guid='bc7e40b7-630a-4bdd-aed6-6657e4f25cdc',
                     machine_sid='S-1-5-21-3977005292-1299864674-2150539454',
                     local_administrator_sid='S-1-5-21-3977005292-1299864674-2150539454-500',
                     os_build='26100')
        second = dict(WINDOWS_SOURCE, machine_guid='97165798-6664-4262-83a9-47087ba1658f',
                      machine_sid='S-1-5-21-1988556285-2669735155-2149193717',
                      local_administrator_sid='S-1-5-21-1988556285-2669735155-2149193717-500',
                      os_build='26100')
        probes = []
        for vmid, identity, config_sha in ((8913, first, 'a'*64), (8914, second, 'b'*64)):
            probes.append({'vmid': vmid, 'state': 'unattended-windows-ready',
                'source_config_sha256': '5e9fdb9337ec631b74100dc1c1f47ffdfb02f8873b32ee6119b49bc7cef27e9d',
                'clone_upid': f'UPID:proxmox4:ABC{vmid}:DEF{vmid}:123{vmid}:qmclone:8912:root@pam:', 'clone_task_ok': True,
                'isolation_before_boot_verified': True, 'source_config_unchanged': True,
                'manual_interventions': [], 'probe_config_sha256': config_sha,
                'identity': identity})
        record = {'state': 'sealed-source-captured', 'provenance_type': 'sealed-windows-source-v1',
            'project': 100, 'node': 'proxmox4', 'kind': 'vm', 'source_id': 8912,
            'source_config_sha256': '5e9fdb9337ec631b74100dc1c1f47ffdfb02f8873b32ee6119b49bc7cef27e9d',
            'source_identity': None, 'source_state': 'stopped-never-booted',
            'source_volumes': ['crange-library:8912/vm-8912-disk-0.raw',
                               'crange-library:8912/vm-8912-disk-1.raw'],
            'hive_sha256': {'SYSTEM': 'fb2f3526a7af150d153080eb608aef50650d2abd837efc3b76cec194334aef8e',
                'SOFTWARE': '08e9281118a0bad1d28560483648c0607da1656b3ef50bd703b950687a81f7c9',
                'SAM': 'b9f3b16431b4cfba63812c74967cb07a609eac695152fc56029bcab622ab6c62'},
            'answer_sha256': '11dc5811c48ec41ffc97c9980171f345abd750a90acd89e3cf0da17da808e2dc',
            'generalization_state': 4, 'cleanup_state': 2,
            'image_state': 'IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE',
            'fresh_source_freeze_verified': True, 'protected_root_evidence_verified': True,
            'protected_bundle_sha256': 'c'*64,
            'protected_file_sha256': {key: letter*64 for key, letter in
                (('inspection','1'),('preparation','2'),('probe8913','3'),('probe8914','4'),('qga8914','5'))},
            'probes': probes}
        self.assertIs(validate_sealed_windows_capture(record), record)
        duplicate = copy.deepcopy(record)
        duplicate['probes'][1]['identity'] = copy.deepcopy(duplicate['probes'][0]['identity'])
        with self.assertRaises(CatalogError):
            validate_sealed_windows_capture(duplicate)
        fabricated = copy.deepcopy(record)
        fabricated['source_identity'] = WINDOWS_SOURCE
        with self.assertRaises(CatalogError):
            validate_sealed_windows_capture(fabricated)

    def test_sealed_windows_retirement_receipt_is_bound_to_image_and_original_proof(self):
        import hashlib, json
        record = self._sealed_record()
        image = {'image_id': SEALED_WINDOWS_IMAGE_ID, 'verified': True,
                 'approved_projects': [100], 'artifact_sha256': 'd' * 64}
        capture_sha = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        rows = []
        for proof in record['probes']:
            vmid = proof['vmid']
            rows.append({'vmid': vmid, 'clone_upid': proof['clone_upid'],
                'config_sha256': proof['probe_config_sha256'],
                'final_config_sha256': proof['probe_config_sha256'], 'final_status': 'stopped',
                'qga_identity_match': True, 'qga_sha256': 'e' * 64,
                'identity_sha256': hashlib.sha256(json.dumps(proof['identity'], sort_keys=True,
                    separators=(',', ':')).encode()).hexdigest(),
                'stop_upid': f'UPID:proxmox4:ABC{vmid}:DEF{vmid}:123{vmid}:qmstop:{vmid}:root@pam:',
                'delete_upid': f'UPID:proxmox4:ABC{vmid}:DEF{vmid}:123{vmid}:qmdestroy:{vmid}:root@pam:'})
        receipt = {'schema_version': 1, 'state': 'proof-guests-retired',
            'image_id': image['image_id'], 'artifact_sha256': image['artifact_sha256'],
            'capture_sha256': capture_sha, 'source_id': 8912,
            'source_config_sha256': record['source_config_sha256'], 'retired_vmids': [8913, 8914],
            'source_preserved': True, 'delete_tasks_ok': True, 'guests_absent': True, 'proofs': rows}
        self.assertIs(validate_sealed_windows_proof_retirement(record, receipt, image), receipt)
        for field, value in (('artifact_sha256', 'f' * 64), ('capture_sha256', '0' * 64),
                             ('source_preserved', False), ('guests_absent', False)):
            broken = copy.deepcopy(receipt); broken[field] = value
            with self.subTest(field=field), self.assertRaises(CatalogError):
                validate_sealed_windows_proof_retirement(record, broken, image)
        broken = copy.deepcopy(receipt); broken['proofs'][0]['identity_sha256'] = '0' * 64
        with self.assertRaises(CatalogError):
            validate_sealed_windows_proof_retirement(record, broken, image)

    @staticmethod
    def _sealed_record():
        first = dict(WINDOWS_SOURCE, machine_guid='bc7e40b7-630a-4bdd-aed6-6657e4f25cdc',
                     machine_sid='S-1-5-21-3977005292-1299864674-2150539454',
                     local_administrator_sid='S-1-5-21-3977005292-1299864674-2150539454-500')
        second = dict(WINDOWS_SOURCE, machine_guid='97165798-6664-4262-83a9-47087ba1658f',
                      machine_sid='S-1-5-21-1988556285-2669735155-2149193717',
                      local_administrator_sid='S-1-5-21-1988556285-2669735155-2149193717-500')
        probes = []
        for vmid, identity, digest in ((8913, first, 'a' * 64), (8914, second, 'b' * 64)):
            probes.append({'vmid': vmid, 'state': 'unattended-windows-ready',
                'source_config_sha256': '5e9fdb9337ec631b74100dc1c1f47ffdfb02f8873b32ee6119b49bc7cef27e9d',
                'clone_upid': f'UPID:proxmox4:ABC{vmid}:DEF{vmid}:123{vmid}:qmclone:8912:root@pam:',
                'clone_task_ok': True, 'isolation_before_boot_verified': True,
                'source_config_unchanged': True, 'manual_interventions': [],
                'probe_config_sha256': digest, 'identity': identity})
        return {'state': 'sealed-source-captured', 'provenance_type': 'sealed-windows-source-v1',
            'project': 100, 'node': 'proxmox4', 'kind': 'vm', 'source_id': 8912,
            'source_config_sha256': '5e9fdb9337ec631b74100dc1c1f47ffdfb02f8873b32ee6119b49bc7cef27e9d',
            'source_identity': None, 'source_state': 'stopped-never-booted',
            'source_volumes': ['crange-library:8912/vm-8912-disk-0.raw', 'crange-library:8912/vm-8912-disk-1.raw'],
            'hive_sha256': {'SYSTEM': 'fb2f3526a7af150d153080eb608aef50650d2abd837efc3b76cec194334aef8e',
                'SOFTWARE': '08e9281118a0bad1d28560483648c0607da1656b3ef50bd703b950687a81f7c9',
                'SAM': 'b9f3b16431b4cfba63812c74967cb07a609eac695152fc56029bcab622ab6c62'},
            'answer_sha256': '11dc5811c48ec41ffc97c9980171f345abd750a90acd89e3cf0da17da808e2dc',
            'generalization_state': 4, 'cleanup_state': 2,
            'image_state': 'IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE',
            'fresh_source_freeze_verified': True, 'protected_root_evidence_verified': True,
            'protected_bundle_sha256': 'c' * 64,
            'protected_file_sha256': {key: letter * 64 for key, letter in
                (('inspection','1'),('preparation','2'),('probe8913','3'),('probe8914','4'),('qga8914','5'))},
            'probes': probes}

    def test_windows_source_identity_requires_realistic_server_and_machine_identifiers(self):
        self.assertIs(validate_source_identity(WINDOWS_SOURCE), WINDOWS_SOURCE)
        client = {**WINDOWS_SOURCE, 'os_caption': 'Microsoft Windows 11 Pro',
                  'installation_type': 'Client'}
        self.assertIs(validate_source_identity(client), client)
        for key, value in (('machine_guid','not-a-guid'),('machine_sid','S-1-5-21-0-1-2'),
                           ('local_administrator_sid','S-1-5-21-1-2-3-501'),
                           ('computer_name','invalid host'),('part_of_domain',True),
                           ('qemu_guest_agent',False),('rdp_allowed_sources',['0.0.0.0/0']),
                           ('image_state','IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE'),
                           ('oobe_in_progress',1),('system_setup_in_progress',1),
                           ('setup_type',2),('oobe_in_progress',None),
                           ('system_setup_in_progress',False),('installation_type','Server Core'),
                           ('qemu_guest_agent_automatic',False),('term_service_automatic',None),
                           ('winrm_service_automatic',False)):
            with self.subTest(key=key):
                identity={**WINDOWS_SOURCE,key:value}
                with self.assertRaises(CatalogError):validate_source_identity(identity)

    def test_windows_capture_reads_identity_through_fixed_qga_command(self):
        api,config,_=self.fixture('vm')
        from unittest.mock import patch
        import json
        expected={k:v for k,v in WINDOWS_SOURCE.items() if k!='os_family'}
        with patch('crange_distribution._fixed_guest_read',return_value=json.dumps(expected)) as read:
            journal=Mock()
            record=capture_source_identity(api,node='proxmox2',kind='vm',vmid=100,
                job_id='win-capture-1',journal=journal,init_system='windows')
        command=read.call_args.args[4]
        self.assertEqual(command[0],r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe')
        self.assertEqual(command[1:3],['-NoProfile','-NonInteractive'])
        self.assertIn('Get-CimInstance Win32_OperatingSystem',command[-1])
        self.assertIn('Setup\\State',command[-1])
        self.assertIn('HKLM:\\SYSTEM\\Setup',command[-1])
        self.assertIn('oobe_in_progress=$setup.OOBEInProgress',command[-1])
        self.assertEqual(record['readiness'],'windows-ready')
        self.assertEqual(record['os_family'],'windows')
        self.assertNotIn('init_system',record)
        self.assertEqual(record['source_identity'],WINDOWS_SOURCE)
        journal.assert_called_once_with(record)

    def test_windows_probe_requires_machine_guid_and_local_sid_to_change(self):
        from unittest.mock import patch
        import json
        source=dict(WINDOWS_SOURCE)
        clone={**source,'machine_guid':'fc9f4512-9372-4428-9364-221e5572ce8b',
               'machine_sid':'S-1-5-21-120001-220002-330003',
               'local_administrator_sid':'S-1-5-21-120001-220002-330003-500'}
        api=Mock()
        api.request.side_effect=[{'pid':1},{'exited':True,'exitcode':0,'out-data':json.dumps({k:v for k,v in clone.items() if k!='os_family'})}]
        verifier=ProbeVerifier('crprobe1450',['crprobe1450'],source,init_system='windows')
        result=verifier._check_guest(api,'proxmox2','qemu',100)
        self.assertTrue(result['passed'])
        self.assertTrue(result['identity_unique'])
        self.assertEqual(result['evidence']['identity']['machine_guid'],clone['machine_guid'])

        same=Mock()
        same.request.side_effect=[{'pid':1},{'exited':True,'exitcode':0,'out-data':json.dumps({k:v for k,v in source.items() if k!='os_family'})}]
        self.assertFalse(verifier._check_guest(same,'proxmox2','qemu',100)['identity_unique'])

    def test_template_conversion_only_normalizes_same_guest_disk_prefix(self):
        before = {'scsi0': 'local-lvm:vm-8830-disk-0,size=20G', 'memory': 512,
                  'net0': 'virtio=AA:BB,bridge=crprobe1450,link_down=1'}
        after = {**before, 'template': 1,
                 'scsi0': 'local-lvm:base-8830-disk-0,size=20G'}
        self.assertEqual(_source_config_identity(before, 8830),
                         _source_config_identity(after, 8830))
        changed = {**after, 'scsi0': 'local-lvm:base-8830-disk-0,size=21G'}
        self.assertNotEqual(_source_config_identity(before, 8830),
                            _source_config_identity(changed, 8830))
        other = {**after, 'unused0': 'local-lvm:base-8831-disk-0,size=20G'}
        self.assertNotEqual(_source_config_identity(before, 8830),
                            _source_config_identity(other, 8830))
        nfs_before = {'scsi0': 'crange-library:109/vm-109-disk-0.raw,iothread=1,size=32G',
                      'memory': 6000, 'net0': 'virtio=AA:BB,bridge=crprobe1450,link_down=1'}
        nfs_after = {**nfs_before, 'template': 1,
                     'scsi0': 'crange-library:109/base-109-disk-0.raw,iothread=1,size=32G'}
        self.assertEqual(_source_config_identity(nfs_before, 109),
                         _source_config_identity(nfs_after, 109))
        wrong_directory = {**nfs_after,
                           'scsi0': 'crange-library:108/base-109-disk-0.raw,iothread=1,size=32G'}
        self.assertNotEqual(_source_config_identity(nfs_before, 109),
                            _source_config_identity(wrong_directory, 109))

    def fixture(self,kind='container'):
        config={'tags':'crange-image','memory':512,'cores':1,'rootfs':'local-lvm:vm-100-disk-0,size=8G'}
        def request(method,path,fields=None):
            if path == '/pools/crange-probes':return {'members':[{'type':'qemu' if kind=='vm' else 'lxc','node':'proxmox2','vmid':100}]}
            if path.endswith('/config'):return copy.deepcopy(config)
            if path.endswith('/status/current'):return {'status':'running'}
            raise AssertionError((method,path))
        api=Mock();api.request.side_effect=request
        execute=Mock(side_effect=['running','a'*32,KEY])
        return api,config,execute

    def test_fixed_capture_reads_only_identity_and_binds_source(self):
        api,config,execute=self.fixture();journal=Mock()
        record=capture_source_identity(api,node='proxmox2',kind='container',vmid=100,job_id='capture-1',journal=journal,ct_exec=execute)
        self.assertEqual(record['source_identity']['machine_id'],'a'*32)
        self.assertEqual([call.args[2] for call in execute.call_args_list],[
            ['/usr/bin/systemctl','is-system-running'],['/bin/cat','/etc/machine-id'],['/bin/cat','/etc/ssh/ssh_host_ed25519_key.pub']])
        journal.assert_called_once_with(record)
        self.assertTrue(all(call.args[0]=='GET' for call in api.request.call_args_list))

    def test_openrc_capture_uses_bin_path_with_fixed_sbin_fallback(self):
        for first_path, expected_path in (('/bin/rc-status', '/bin/rc-status'),
                                          ('/sbin/rc-status', '/sbin/rc-status')):
            with self.subTest(path=expected_path):
                api,config,execute=self.fixture()
                def read(node,vmid,command):
                    if command[0] == '/bin/rc-status' and first_path == '/sbin/rc-status':
                        raise FileNotFoundError(command[0])
                    return 'default' if 'rc-status' in command[0] else ('a'*32 if command[-1]=='/etc/machine-id' else KEY)
                execute.side_effect=read
                record=capture_source_identity(api,node='proxmox2',kind='container',vmid=100,job_id='capture-1',
                    journal=Mock(),ct_exec=execute,init_system='openrc')
                self.assertEqual(execute.call_args_list[-3].args[2], [expected_path,'--runlevel'])
                commands=[call.args[2] for call in execute.call_args_list]
                self.assertEqual(commands[0],['/bin/rc-status','--runlevel'])
                if expected_path == '/sbin/rc-status':
                    self.assertEqual(commands[1],[expected_path,'--runlevel'])
                self.assertIn(['/bin/cat','/etc/machine-id'],commands)
                self.assertEqual(record['init_system'], 'openrc')

    def test_source_outside_probe_pool_has_no_execution(self):
        for members in ([],[{'type':'lxc','node':'proxmox3','vmid':100}],
                        [{'type':'qemu','node':'proxmox2','vmid':100}]):
            with self.subTest(members=members):
                api,config,execute=self.fixture()
                api.request.side_effect=lambda method,path,fields=None: {'members':members}
                with self.assertRaisesRegex(DistributionError,'pool membership'):
                    capture_source_identity(api,node='proxmox2',kind='container',vmid=100,job_id='capture-1',journal=Mock(),ct_exec=execute)
                execute.assert_not_called()
                self.assertEqual(api.request.call_count,1)

    def test_template_is_never_booted_or_executed(self):
        api,config,execute=self.fixture();config['template']=1
        with self.assertRaises(DistributionError):capture_source_identity(api,node='proxmox2',kind='container',vmid=100,job_id='capture-1',journal=Mock(),ct_exec=execute)
        execute.assert_not_called()

    def test_export_requires_matching_capture_before_backup(self):
        api,config,execute=self.fixture()
        record=capture_source_identity(api,node='proxmox2',kind='container',vmid=100,job_id='capture-1',journal=Mock(),ct_exec=execute)
        config['template']=1
        def request(method,path,fields=None):
            if path.endswith('/config'):return config
            if path.endswith('/status/current'):return {'status':'stopped'}
            raise AssertionError('backup must not run with mismatched evidence')
        api.request.side_effect=request
        for change in ({'node':'proxmox3'},{'source_id':101},{'source_config_sha256':'b'*64}):
            with self.subTest(change=change),self.assertRaisesRegex(DistributionError,'Matching worker-captured'):
                export_image(api,node='proxmox2',kind='container',vmid=100,storage='library',job_id='export-1',journal=Mock(),hash_volume=Mock(),approved_backup_storage=[('proxmox2','library')],identity_record={**record,**change})

    def test_vm_capture_uses_fixed_guest_agent_commands(self):
        api,config,_=self.fixture('vm')
        original=api.request.side_effect
        outputs=iter(['running','a'*32,KEY])
        def request(method,path,fields=None):
            if path.endswith('/agent/exec'):return {'pid':42}
            if path.endswith('/agent/exec-status'):return {'exited':True,'exitcode':0,'out-data':next(outputs)}
            return original(method,path,fields)
        api.request.side_effect=request
        record=capture_source_identity(api,node='proxmox2',kind='vm',vmid=100,job_id='capture-1',journal=Mock())
        self.assertEqual(record['source_identity']['ssh_host_key'],KEY)
        commands=[call.args[2]['command'] for call in api.request.call_args_list if call.args[0]=='POST']
        self.assertEqual(len(commands),3)
        self.assertEqual(commands[-1],['/bin/cat','/etc/ssh/ssh_host_ed25519_key.pub'])

    def test_successful_export_carries_identity_and_capture_provenance(self):
        api,config,execute=self.fixture()
        execute.side_effect=['default','a'*32,KEY]
        record=capture_source_identity(api,node='proxmox2',kind='container',vmid=100,job_id='capture-1',journal=Mock(),ct_exec=execute,init_system='openrc')
        config['template']=1
        listings=iter([[],[{'volid':'library:backup/image','content':'backup','vmid':100}]])
        def request(method,path,fields=None):
            if path.endswith('/config'):return config
            if path.endswith('/status/current'):return {'status':'stopped'}
            if path.endswith('/content'):return next(listings)
            if path.endswith('/vzdump'):return 'UPID:backup'
            raise AssertionError(path)
        api.request.side_effect=request
        exported=export_image(api,node='proxmox2',kind='container',vmid=100,storage='library',job_id='export-1',journal=Mock(),hash_volume=Mock(return_value='a'*64),approved_backup_storage=[('proxmox2','library')],identity_record=record)
        self.assertEqual(exported['source_identity'],record['source_identity'])
        self.assertEqual(exported['identity_capture'],record)
        self.assertEqual(exported['init_system'],'openrc')

    def test_terminal_permission_failure_retries_once_only_without_partial_archive(self):
        api,config,execute=self.fixture();config['unprivileged']='1'
        record=capture_source_identity(api,node='proxmox2',kind='container',vmid=100,job_id='capture-1',journal=Mock(),ct_exec=execute)
        config['template']=1
        with tempfile.TemporaryDirectory() as tmp:
            journal=FileJournal(pathlib.Path(tmp))
            prior={'job_id':'export-retry','node':'proxmox2','kind':'container','source_id':100,
                   'storage':'library','state':'exporting','disk_gb':8.0,'source_identity':record['source_identity'],
                   'identity_capture':record,'init_system':'systemd','memory_mb':512,'cpu_cores':1,
                   'upid':'UPID:proxmox2:1:1:1:vzdump:100:root:','content_before':[]}
            journal(prior);submitted=[]
            def request(method,path,fields=None):
                if path.endswith('/config'):return copy.deepcopy(config)
                if path.endswith('/status/current'):return {'status':'stopped'}
                if path.endswith('/status'):return {'status':'stopped','exitstatus':'403 Permission check failed (/vms/100, VM.Backup)'}
                if path.endswith('/content'):return [{'content':'backup','vmid':100,'volid':'library:backup/vzdump-lxc-100.tar.zst'}] if submitted else []
                if path.endswith('/vzdump'):submitted.append(fields);return 'UPID:proxmox2:2:2:2:vzdump:100:root:'
                raise AssertionError(path)
            api.request.side_effect=request;api.task.return_value=None
            result=export_image(api,node='proxmox2',kind='container',vmid=100,storage='library',job_id='export-retry',journal=journal,hash_volume=Mock(return_value='a'*64),approved_backup_storage=iter([('proxmox2','library')]),identity_record=record)
            self.assertTrue(result['verified']);self.assertEqual(len(submitted),1)
            self.assertEqual(len(list(pathlib.Path(tmp).glob('export-retry-failed-*.json'))),1)

    def test_resume_export_hashes_only_archive_named_by_confirmed_terminal_task(self):
        api,config,execute=self.fixture()
        config['unprivileged']='1'
        record=capture_source_identity(api,node='proxmox2',kind='container',vmid=100,job_id='capture-1',journal=Mock(),ct_exec=execute)
        config['template']=1
        upid='UPID:proxmox2:1:1:1:vzdump:100:root:'
        with tempfile.TemporaryDirectory() as tmp:
            journal=FileJournal(pathlib.Path(tmp))
            prior={'job_id':'export-resume','node':'proxmox2','kind':'container','source_id':100,
                'storage':'library','state':'exporting','disk_gb':8.0,'source_identity':record['source_identity'],
                'identity_capture':record,'init_system':'systemd','memory_mb':512,'cpu_cores':1,'upid':upid}
            journal(prior)
            def request(method,path,fields=None):
                if path.endswith('/config'): return copy.deepcopy(config)
                if path.endswith('/status/current'): return {'status':'stopped'}
                if path.endswith('/status'):
                    return {'status':'stopped','exitstatus':'OK'} if api.task.called else {'status':'running'}
                if path.endswith('/log'): return [
                    {'t':"INFO: creating vzdump archive '/mnt/pve/library/dump/vzdump-lxc-100-2026_10_07-21_07_48.tar.zst'"},
                    {'t':'TASK OK'}]
                raise AssertionError(f'unexpected request {method} {path}')
            api.request.side_effect=request
            hasher=Mock(return_value='b'*64)
            exported=export_image(api,node='proxmox2',kind='container',vmid=100,storage='library',
                job_id='export-resume',journal=journal,hash_volume=hasher,
                approved_backup_storage=[('proxmox2','library')],identity_record=record)
            self.assertEqual(exported['volume_id'],'library:backup/vzdump-lxc-100-2026_10_07-21_07_48.tar.zst')
            self.assertEqual(exported['artifact_sha256'],'b'*64)
            self.assertTrue(exported['recovered_from_terminal_task'])
            api.task.assert_called_once_with('proxmox2',upid,timeout=1800)
            hasher.assert_called_once_with('proxmox2',exported['volume_id'])
            self.assertFalse(any(call.args[1].endswith('/vzdump') for call in api.request.call_args_list))

    def test_interrupted_vm_export_reuses_template_and_keeps_failed_journal(self):
        from unittest.mock import patch
        api,config,execute=self.fixture()
        record=capture_source_identity(api,node='proxmox2',kind='container',vmid=100,job_id='capture-1',journal=Mock(),ct_exec=execute)
        record['kind']='vm';config['template']=1;config['lock']='backup'
        job='capture-p100-abcdef123456-export';submitted=[]
        prior={'job_id':job,'node':'proxmox2','kind':'vm','source_id':100,'storage':'library',
               'state':'exporting','disk_gb':8.0,'source_identity':record['source_identity'],
               'identity_capture':record,'init_system':'systemd','memory_mb':512,'cpu_cores':1,
               'upid':'UPID:proxmox2:1:1:1:vzdump:100:root:','content_before':[]}
        def request(method,path,fields=None):
            if path.endswith('/config'):return copy.deepcopy(config)
            if path.endswith('/status/current'):return {'status':'stopped'}
            if path.endswith('/status'):return {'status':'stopped','exitstatus':'unexpected status'}
            if path.endswith('/content'):return [{'content':'backup','vmid':100,'volid':'library:backup/vzdump-qemu-100-2026_10_14-20_30_00.vma.zst'}] if submitted else []
            if path.endswith('/vzdump'):submitted.append(fields);return 'UPID:proxmox2:2:2:2:vzdump:100:root:'
            raise AssertionError(path)
        def unlock(*args):config.pop('lock');return {'state':'unlocked','partial_files_preserved':True}
        api.request.side_effect=request
        with tempfile.TemporaryDirectory() as tmp,patch('crange_distribution.recover_interrupted_export_lock',side_effect=unlock) as recover:
            journal=FileJournal(pathlib.Path(tmp));journal(prior)
            result=export_image(api,node='proxmox2',kind='vm',vmid=100,storage='library',job_id=job,
                journal=journal,hash_volume=Mock(return_value='a'*64),approved_backup_storage=[('proxmox2','library')],identity_record=record)
            self.assertTrue(result['verified']);self.assertEqual(len(submitted),1);recover.assert_called_once()
            history=list(pathlib.Path(tmp).glob(job+'-failed-*.json'))
            self.assertEqual(len(history),1)
            self.assertTrue(__import__('json').loads(history[0].read_text())['recovery']['partial_files_preserved'])

    def test_source_public_key_and_machine_id_are_validated(self):
        validate_source_identity({'machine_id':'a'*32,'ssh_host_key':KEY})
        for identity in ({'machine_id':'0'*32,'ssh_host_key':KEY},{'machine_id':'a'*32,'ssh_host_key':'ssh-ed25519 fabricated'}):
            with self.assertRaises(CatalogError):validate_source_identity(identity)
