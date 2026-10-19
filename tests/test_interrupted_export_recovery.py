import copy,json,pathlib,sys,tempfile,unittest
from unittest.mock import Mock
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from crange_ct_read_relay import recover_export_lock,validate_request,_sealed_config_identity

class ExportLockRecoveryTests(unittest.TestCase):
    def fixture(self):
        node='proxmox5';vmid=138
        config={'template':1,'lock':'backup','tags':'crange-image;crange-capture-abcdef123456;p100',
                'sata0':'ssd-lvm:vm-138-disk-1,size=64G','memory':4096,'cores':2}
        upid='UPID:proxmox5:1:2:3:vzdump:138:crange-iac@pve!terraform:'
        request={'command':'capture-export-unlock','node':node,'vmid':vmid,
                 'marker':'crange-capture-abcdef123456','upid':upid,'config_sha256':_sealed_config_identity(config,vmid)}
        task={'upid':upid,'type':'vzdump','id':'138','status':'stopped','exitstatus':'unexpected status','starttime':100}
        current={'status':'stopped'};active=[]
        def read(path,*args):
            if path.endswith('/config'):return copy.deepcopy(config)
            if path.endswith('/status/current'):return current
            if path.endswith('/status'):return task
            if path=='/pools/crange-probes':return {'members':[{'type':'qemu','node':node,'vmid':vmid}]}
            if path.endswith('/tasks'):return active
            raise AssertionError(path)
        def unlock(argv):
            self.assertEqual(argv,['qm','unlock','138']);config.pop('lock',None)
        return request,config,task,current,active,read,Mock(side_effect=unlock)

    def test_exact_failed_preboot_export_unlocks_without_removing_files(self):
        request,config,task,current,active,read,run=self.fixture()
        with tempfile.TemporaryDirectory() as tmp:
            result=recover_export_lock(request,read=read,execute=run,boot_time=200,evidence_dir=tmp)
            self.assertEqual(result['state'],'unlocked');self.assertTrue(result['partial_files_preserved'])
            self.assertEqual(len(list(pathlib.Path(tmp).glob('*.json'))),1)
            run.assert_called_once()

    def test_running_changed_foreign_successful_or_current_boot_exports_denied(self):
        for failure in ['running','config','active','success','current_boot','foreign_task']:
            request,config,task,current,active,read,run=self.fixture()
            if failure=='running':current['status']='running'
            elif failure=='config':config['memory']=8192
            elif failure=='active':active.append({'type':'vzdump','id':'138'})
            elif failure=='success':task['exitstatus']='OK'
            elif failure=='current_boot':task['starttime']=300
            elif failure=='foreign_task':task['id']='103'
            with tempfile.TemporaryDirectory() as tmp,self.subTest(failure=failure):
                with self.assertRaises(ValueError):recover_export_lock(request,read=read,execute=run,boot_time=200,evidence_dir=tmp)
                run.assert_not_called();self.assertFalse(list(pathlib.Path(tmp).iterdir()))

    def test_socket_scope_is_exact(self):
        request,*_=self.fixture();policy={'peer_uid':101000,'node_addresses':{'proxmox5':'10.0.0.105'}}
        validate_request(request,policy,101000)
        for value,uid in [(request,0),({**request,'vmid':103},101000),({**request,'node':'proxmox1'},101000),({**request,'shell':'qm unlock 103'},101000)]:
            with self.assertRaises(ValueError):validate_request(value,policy,uid)
