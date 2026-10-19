import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from crange_distribution import (_readonly_template_backup_repair,export_image,
    _source_config_identity,FileJournal,DistributionError)
from test_source_identity_capture import KEY

class ReadonlyTemplateRecoveryTests(unittest.TestCase):
    def fixture(self):
        self.node='proxmox6';self.vmid=160;self.job='capture-p100-44891a7a6389-export'
        self.upid='UPID:proxmox6:0007F10A:007A0736:6AD2778A:vzdump:160:crange-iac@pve!terraform:'
        self.config={'tags':'crange-image;crange-capture-44891a7a6389;p100','template':1,
            'sata0':'local-lvm:base-160-disk-1,size=8G','memory':512,'cores':1}
        self.status={'node':self.node,'upid':self.upid,'type':'vzdump','id':'160','status':'stopped','exitstatus':'job errors'}
        self.log=[{'t':v} for v in [f'CRANGE export {self.job}',"include disk 'sata0' 'local-lvm:base-160-disk-1' 8G",
            'starting kvm to execute backup task','The device is not writable: Permission denied','Backup of VM 160 failed']]
        self.package={'Package':'qemu-server','CurrentState':'Installed','OldVersion':'9.0.18','Version':'9.2.11'}
        self.submitted=[];self.partial=False
        def request(method,path,fields=None):
            if path.endswith('/config'):return copy.deepcopy(self.config)
            if path.endswith('/status/current'):return {'status':'stopped'}
            if path.endswith('/status'):return copy.deepcopy(self.status)
            if path.endswith('/log'):return self.log
            if path.endswith('/apt/versions'):return [self.package]
            if path.endswith('/content'):
                return [{'vmid':160,'content':'backup','volid':'library:backup/vzdump-qemu-160-2026_10_16-10_00_00.vma.zst'}] if self.submitted or self.partial else []
            if path.endswith('/vzdump'):
                self.submitted.append(fields);return 'UPID:proxmox6:1:2:3:vzdump:160:root@pam:'
            raise AssertionError(path)
        self.api=Mock();self.api.request.side_effect=request
        self.identity={'state':'identity-captured','job_id':'capture-p100-44891a7a6389-identity',
            'node':self.node,'kind':'vm','source_id':160,'source_config_sha256':_source_config_identity(self.config,160),
            'init_system':'systemd','readiness':'running','source_identity':{'machine_id':'a'*32,'ssh_host_key':KEY}}
    def proof(self):
        return _readonly_template_backup_repair(self.api,node=self.node,vmid=160,job_id=self.job,
            upid=self.upid,status=self.status,config=self.config)
    def run_export(self,journal):
        return export_image(self.api,node=self.node,kind='vm',vmid=160,storage='library',job_id=self.job,
            journal=journal,hash_volume=Mock(return_value='a'*64),approved_backup_storage=[(self.node,'library')],identity_record=self.identity)
    def journal(self,directory):
        journal=FileJournal(Path(directory));journal({'job_id':self.job,'node':self.node,'kind':'vm','source_id':160,
            'storage':'library','state':'exporting','disk_gb':8.0,'identity_capture':self.identity,
            'source_identity':self.identity['source_identity'],'init_system':'systemd','memory_mb':512,'cpu_cores':1,
            'upid':self.upid,'content_before':[]});return journal
    def test_confirmed_repair_retries_same_template_once_and_preserves_failed_history(self):
        self.fixture()
        with tempfile.TemporaryDirectory() as tmp:
            journal=self.journal(tmp);result=self.run_export(journal)
            self.assertTrue(result['verified']);self.assertEqual(len(self.submitted),1)
            history=list(Path(tmp).glob('*-failed-*.json'));self.assertEqual(len(history),1)
            proof=json.loads(history[0].read_text())['recovery']
            self.assertEqual(proof['qemu_server_version'],'9.0.18');self.assertEqual(proof['failed_upid'],self.upid)
    def test_available_candidate_does_not_prove_installed_fix(self):
        self.fixture();self.package['OldVersion']='9.0.16'
        with self.assertRaisesRegex(DistributionError,'Prepare native'):self.proof()
        self.assertEqual(self.submitted,[])
    def test_unpacked_package_is_not_ready(self):
        self.fixture();self.package['CurrentState']='UnPacked'
        with self.assertRaisesRegex(DistributionError,'fully installed'):self.proof()
    def test_running_foreign_locked_or_non_regression_failure_never_retries(self):
        for case in ['running','foreign-task','different-node','locked','log','disk','job']:
            self.fixture()
            if case=='running':self.status['status']='running'
            if case=='foreign-task':self.status['id']='103'
            if case=='different-node':self.status['node']='proxmox1'
            if case=='locked':self.config['lock']='backup'
            if case=='log':self.log=[{'t':'unrelated backup error'}]
            if case=='disk':self.config['sata0']='local-lvm:vm-160-disk-1,size=8G'
            if case=='job':self.job='unowned-export'
            with self.subTest(case=case),self.assertRaises(DistributionError):self.proof()
            self.assertEqual(self.submitted,[])
    def test_changed_identity_or_new_archive_retains_original_journal(self):
        for case in ['identity','archive']:
            self.fixture()
            with tempfile.TemporaryDirectory() as tmp:
                journal=self.journal(tmp)
                if case=='identity':self.config['memory']=1024
                else:self.partial=True
                with self.subTest(case=case),self.assertRaises(DistributionError):self.run_export(journal)
                self.assertTrue((Path(tmp)/(self.job+'.json')).exists())
                self.assertEqual(len(list(Path(tmp).glob('*-failed-*.json'))),0)
                self.assertEqual(self.submitted,[])
