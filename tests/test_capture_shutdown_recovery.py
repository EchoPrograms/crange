import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange_capture as capture
from crange_distribution import DistributionError

class ShutdownRecoveryTests(unittest.TestCase):
    def fixture(self):
        receipt={'node':'proxmox5','template_id':133,'capture_name':'retained-file',
                 'marker':'crange-capture-example',
                 'inspection_evidence':{'computer':'MHL-FS01','domain':'mhl.test','role':3,
                    'boot':{'LastShutdownGood':'true','LastBootGood':'true'}},
                 'diagnostic_tasks':[{'operation':'start','upid':'UPID:proxmox5:1:2:3:qmstart:133:user:'},
                                     {'operation':'shutdown','upid':'UPID:proxmox5:1:2:3:qmshutdown:133:user:'}]}
        identity={'state':'identity-captured','kind':'vm','node':'proxmox5','source_id':133,
                  'source_config_sha256':'hash','source_identity':{'identity_mode':'windows-scenario-snapshot-v1',
                     'snapshot_project_id':100,'computer_name':'MHL-FS01','domain_name':'mhl.test','domain_role':3}}
        config={'name':'retained-file','tags':'crange-image;crange-capture-example;p100',
                'net0':'e1000=AA,bridge=isolated,link_down=0'}
        api=Mock();api.status.return_value='stopped'
        return api,receipt,identity,config

    def recover(self,api,receipt,identity,config):
        with patch('crange_windows_snapshot.validate_captured_identity'), \
             patch.object(capture,'_source_config_identity',return_value='hash'), \
             patch.object(capture,'_require_probe_pool'), \
             patch.object(capture,'private_bridge_request'):
            capture._recover_inspected_shutdown(api,receipt,identity,config,100,'isolated')

    def test_only_confirmed_owned_inspection_tasks_are_reconciled_without_guest_mutation(self):
        api,receipt,identity,config=self.fixture();self.recover(api,receipt,identity,config)
        self.assertEqual(api.task.call_count,2)
        api.request.assert_not_called()
        api.start.assert_not_called()
        api.shutdown.assert_not_called()

    def test_unclean_boot_changed_identity_foreign_guest_or_unproved_shutdown_are_rejected(self):
        for change in ('unclean','missing_boot','running','foreign_guest','changed_domain','wrong_hash','wrong_task'):
            api,receipt,identity,config=self.fixture()
            if change=='unclean':receipt['inspection_evidence']['boot']['LastShutdownGood']='false'
            if change=='missing_boot':receipt['inspection_evidence'].pop('boot')
            if change=='running':api.status.return_value='running'
            if change=='foreign_guest':config['tags']=config['tags'].replace('p100','p101')
            if change=='changed_domain':receipt['inspection_evidence']['domain']='foreign.test'
            if change=='wrong_hash':identity['source_config_sha256']='changed'
            if change=='wrong_task':receipt['diagnostic_tasks'][-1]['upid']=receipt['diagnostic_tasks'][-1]['upid'].replace(':133:',':129:')
            with self.subTest(change=change),self.assertRaises(DistributionError):
                self.recover(api,receipt,identity,config)
            api.request.assert_not_called()

if __name__=='__main__':unittest.main()
