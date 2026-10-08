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
                                 _source_config_identity, FileJournal, ProbeVerifier)
from crange_catalog import validate_source_identity, CatalogError

KEY='ssh-ed25519 '+base64.b64encode(struct.pack('>I',11)+b'ssh-ed25519'+struct.pack('>I',32)+b'a'*32).decode()
WINDOWS_SOURCE={'os_family':'windows','machine_guid':'bdce686e-757d-492c-8a6a-0451c365d0ea',
    'machine_sid':'S-1-5-21-1429816746-3004255909-1098465845',
    'local_administrator_sid':'S-1-5-21-1429816746-3004255909-1098465845-500',
    'computer_name':'MHL-WINBASE','os_caption':'Microsoft Windows Server 2025 Standard Evaluation',
    'os_version':'10.0.26100','os_build':'26100','part_of_domain':False,
    'image_state':'IMAGE_STATE_COMPLETE',
    'qemu_guest_agent':True,'term_service_running':True,'winrm_service_running':True,
    'rdp_allowed_sources':['10.60.0.20']}

class CaptureTests(unittest.TestCase):
    def test_windows_source_identity_requires_realistic_server_and_machine_identifiers(self):
        self.assertIs(validate_source_identity(WINDOWS_SOURCE), WINDOWS_SOURCE)
        for key, value in (('machine_guid','not-a-guid'),('machine_sid','S-1-5-21-0-1-2'),
                           ('local_administrator_sid','S-1-5-21-1-2-3-501'),
                           ('computer_name','invalid host'),('part_of_domain',True),
                           ('qemu_guest_agent',False),('rdp_allowed_sources',['0.0.0.0/0']),
                           ('image_state','IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE')):
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
                if path.endswith('/status'): return {'status':'stopped','exitstatus':'OK'}
                if path.endswith('/log'): return [
                    {'t':"INFO: creating vzdump archive '/mnt/pve/library/dump/vzdump-lxc-100-2026_10_07-21_07_48.vma.zst'"},
                    {'t':'TASK OK'}]
                raise AssertionError(f'unexpected request {method} {path}')
            api.request.side_effect=request
            hasher=Mock(return_value='b'*64)
            exported=export_image(api,node='proxmox2',kind='container',vmid=100,storage='library',
                job_id='export-resume',journal=journal,hash_volume=hasher,
                approved_backup_storage=[('proxmox2','library')],identity_record=record)
            self.assertEqual(exported['volume_id'],'library:backup/vzdump-lxc-100-2026_10_07-21_07_48.vma.zst')
            self.assertEqual(exported['artifact_sha256'],'b'*64)
            self.assertTrue(exported['recovered_from_terminal_task'])
            hasher.assert_called_once_with('proxmox2',exported['volume_id'])
            self.assertFalse(any(call.args[1].endswith('/vzdump') for call in api.request.call_args_list))

    def test_source_public_key_and_machine_id_are_validated(self):
        validate_source_identity({'machine_id':'a'*32,'ssh_host_key':KEY})
        for identity in ({'machine_id':'0'*32,'ssh_host_key':KEY},{'machine_id':'a'*32,'ssh_host_key':'ssh-ed25519 fabricated'}):
            with self.assertRaises(CatalogError):validate_source_identity(identity)
