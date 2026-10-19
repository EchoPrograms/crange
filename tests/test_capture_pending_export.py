import contextlib,json,pathlib,sys,tempfile,unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import crange_capture as capture
class PendingExportTests(unittest.TestCase):
    def test_pending_export_attaches_to_exact_checkpoint_and_releases_after_publication(self):
        job='capture-p100-abcdef123456';identity={'state':'identity-captured','source_id':138,'node':'proxmox5',
            'source_config_sha256':'stable','source_identity':{'identity_mode':'windows-scenario-snapshot-v1','snapshot_project_id':100}}
        allocation={'operation':'source_probe','project_id':100,'image':'source:proxmox1:qemu:128',
            'artifact_sha256':'source-hash','resolved':{'node_name':'proxmox5','datastore_id':'ssd-lvm'}}
        receipt={'state':'allocated','job_id':job,'source_node':'proxmox1','source_id':128,
            'source_config_sha256':'source-hash','node':'proxmox5','template_id':138,'capture_name':'owned',
            'marker':'crange-capture-abcdef123456','reservation':allocation,
            'provider_tasks':[{'path':'/nodes/proxmox5/qemu/138/template','upid':'UPID:proxmox5:1:2:3:qmtemplate:138:user:','state':'confirmed-terminal'}]}
        pending={'state':'exporting','identity_capture':identity,'node':'proxmox5','source_id':138,'kind':'vm',
                 'storage':'crange-library','upid':'UPID:proxmox5:1:2:3:vzdump:138:user:'}
        final={**pending,'state':'exported','verified':True,'complete_capture':True,'artifact_sha256':'a'*64}
        api=Mock();api.vm.return_value={'template':1,'lock':'backup'};api.status.return_value='stopped'
        ledger=Mock();ledger.get_job.return_value={'state':'running','project_id':100,'owner':'release-capture','allocations':[allocation]}
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);(root/'source-journal').mkdir()
            for suffix,value in (('identity',identity),('export',pending)):
                (root/'source-journal'/(job+'-'+suffix+'.json')).write_text(json.dumps(value))
            path=root/'receipt.json';path.write_text(json.dumps(receipt))
            with patch.object(capture,'DATA',root),patch.object(capture,'node_locks',return_value=contextlib.nullcontext()), \
                 patch.object(capture,'_source_config_identity',return_value='stable'),patch.object(capture,'_owned'), \
                 patch.object(capture,'_require_probe_pool'),patch('crange_windows_snapshot.validate_captured_identity'), \
                 patch.object(capture,'read_settings',return_value=({'allowed_nodes':['proxmox5'],'approved_backup_storage':[['proxmox5','crange-library']],'capture_isolated_bridge':'crprobe1450'},{})), \
                 patch.object(capture,'ReservationLedger',return_value=ledger),patch.object(capture,'export_image',return_value=final) as export, \
                 patch.object(capture,'_verified_capture_probe'),patch.object(capture,'_retire_capture_transport'),patch.object(capture,'_wait_capture_probe_load'), \
                 patch('crange_distribution.main') as main:
                vmid,result=capture.resume_exported_capture(api,receipt_path=path,receipt=receipt,project=100,
                    job=job,name='owned',source_id=128,slug='abcdef123456')
                self.assertEqual(vmid,138);self.assertEqual(receipt['state'],'verified')
                export.assert_called_once();self.assertEqual(export.call_args.kwargs['job_id'],job+'-export')
                ledger.reconcile_job.assert_called_once();api.request.assert_not_called()
                self.assertIn('source-probe',main.call_args.args[0])
                self.assertIn('--auto-storage',main.call_args.args[0])
                receipt['state']='captured'
                (root/'source-journal'/(job+'-export.json')).write_text(json.dumps(final))
                (root/'source-journal'/(job+'-probe.json')).write_text(json.dumps({'state':'allocated'}))
                main.reset_mock()
                capture.resume_exported_capture(api,receipt_path=path,receipt=receipt,project=100,
                    job=job,name='owned',source_id=128,slug='abcdef123456')
                self.assertIn('source-probe-resume',main.call_args.args[0])
                self.assertNotIn('--datastore-id',main.call_args.args[0])
                receipt['state']='captured'
                (root/'source-journal'/(job+'-probe.json')).write_text(json.dumps({'state':'source-probed'}))
                main.reset_mock()
                capture.resume_exported_capture(api,receipt_path=path,receipt=receipt,project=100,
                    job=job,name='owned',source_id=128,slug='abcdef123456')
                main.assert_not_called()
                receipt['state']='allocated'
                api.status.return_value='running'
                export.reset_mock();ledger.reconcile_job.reset_mock()
                with self.assertRaisesRegex(ValueError, r'differs from the retained template: stopped\.'):
                    capture.resume_exported_capture(api,receipt_path=path,receipt=receipt,project=100,
                        job=job,name='owned',source_id=128,slug='abcdef123456')
                export.assert_not_called();ledger.reconcile_job.assert_not_called()
                api.status.return_value='stopped'
                (root/'source-journal'/(job+'-export.json')).write_text(json.dumps({**pending,'source_id':139}))
                export.reset_mock();ledger.reconcile_job.reset_mock()
                with self.assertRaisesRegex(ValueError, r'differs from the retained template: export_guest\.'):
                    capture.resume_exported_capture(api,receipt_path=path,receipt=receipt,project=100,
                        job=job,name='owned',source_id=128,slug='abcdef123456')
                export.assert_not_called();ledger.reconcile_job.assert_not_called()
