import base64
import copy
import pathlib
import struct
import sys
import unittest
from unittest.mock import Mock, call
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from crange_distribution import capture_source_identity, export_image, DistributionError
from crange_catalog import validate_source_identity, CatalogError

KEY='ssh-ed25519 '+base64.b64encode(struct.pack('>I',11)+b'ssh-ed25519'+struct.pack('>I',32)+b'a'*32).decode()

class CaptureTests(unittest.TestCase):
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

    def test_source_public_key_and_machine_id_are_validated(self):
        validate_source_identity({'machine_id':'a'*32,'ssh_host_key':KEY})
        for identity in ({'machine_id':'0'*32,'ssh_host_key':KEY},{'machine_id':'a'*32,'ssh_host_key':'ssh-ed25519 fabricated'}):
            with self.assertRaises(CatalogError):validate_source_identity(identity)
