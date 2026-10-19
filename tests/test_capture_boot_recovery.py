import pathlib,sys,unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import crange_capture as capture
class CaptureBootRecoveryTests(unittest.TestCase):
    def fixture(self):
        receipt={'node':'proxmox5','template_id':138,'marker':'crange-capture-abcdef123456','capture_name':'owned',
         'inspection_evidence':{'domain':'mhl.test','role':5,'directory':{'ok':True},
          'boot':{'LastShutdownGood':'true','LastBootGood':'true'},
          'services':[{'Name':name,'state':'Running'} for name in ('QEMU-GA','WinRM','TermService','NTDS')]},
         'diagnostic_tasks':[{'operation':op,'upid':f'UPID:proxmox5:1:2:3:{kind}:138:user:'} for op,kind in [('start','qmstart'),('shutdown','qmshutdown')]]}
        config={'name':'owned','tags':'crange-image;crange-capture-abcdef123456;p100','net0':'virtio=AA:BB:CC:DD:EE:FF,bridge=crprobe1450,link_down=0'}
        api=Mock();api.status.return_value='stopped'
        return api,receipt,config
    def test_clean_owned_diagnostic_allows_only_fresh_collection_not_publication(self):
        api,receipt,config=self.fixture()
        with patch.object(capture,'_require_probe_pool'),patch.object(capture,'private_bridge_request'):
            capture._recover_inspected_boot_readiness(api,receipt,config,100,'crprobe1450')
        self.assertEqual(api.task.call_count,2);api.request.assert_not_called();api.start.assert_not_called()
    def test_foreign_running_unclean_or_unverified_capture_is_rejected(self):
        for mutation in ('running','foreign','unclean','missing_directory','wrong_task','connected','template'):
            api,receipt,config=self.fixture()
            if mutation=='running':api.status.return_value='running'
            if mutation=='foreign':config['tags']=config['tags'].replace('p100','p101')
            if mutation=='unclean':receipt['inspection_evidence']['boot']['LastBootGood']='false'
            if mutation=='missing_directory':receipt['inspection_evidence']['directory']['ok']=False
            if mutation=='wrong_task':receipt['diagnostic_tasks'][1]['upid']='UPID:proxmox5:1:2:3:qmshutdown:129:user:'
            if mutation=='connected':config['net0']='bridge=p100t254,link_down=0'
            if mutation=='template':config['template']=1
            with self.subTest(mutation=mutation),patch.object(capture,'_require_probe_pool'),patch.object(capture,'private_bridge_request'),self.assertRaises(ValueError):
                capture._recover_inspected_boot_readiness(api,receipt,config,100,'crprobe1450')
