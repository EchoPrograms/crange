import contextlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import crange_cache_jobs as jobs


class ContainerResumeTests(unittest.TestCase):
    def test_owned_restore_resumes_and_uncertain_restore_does_not(self):
        job = 'cache-' + 'a' * 32
        image = {'artifact_sha256':'b'*64, 'approved_projects':[100],
                 'artifact':{'node':'proxmox2','source_id':134,'volume_id':'crange-library:backup/test.tar.zst'}}
        allocation = {'operation':'cache_prepare','image':'app','artifact_sha256':'b'*64,
                      'resolved':{'node_name':'proxmox5','datastore_id':'crange-thin'}}
        record = {'job_id':job,'image_id':'app','node':'proxmox5','allocation_node':'proxmox5',
                  'datastore_id':'crange-thin','template_id':140,'kind':'lxc',
                  'state':'converting-template','artifact_sha256':'b'*64,'reservation':allocation,
                  'source_node':'proxmox2','source_id':134,
                  'provider_tasks':[{'node':'proxmox5','upid':'UPID:proxmox5:1:2:3:vzrestore:140:user:',
                                     'state':'confirmed-terminal'}]}
        api = Mock(); uncertain = False
        def request(method, path, fields=None):
            if '/tasks/' in path: return {'status':'stopped','exitstatus':'ERROR' if uncertain else 'OK'}
            if path.endswith('/tasks'): return []
            if path.endswith('/config'):
                return {'template':1,'rootfs':'crange-thin:base-140-disk-0,size=8G',
                        'net0':'bridge=crprobe1450,link_down=1'}
            if path.endswith('/status/current'): return {'status':'stopped'}
            if path == '/pools/crange-iac': return {'members':[{'type':'lxc','node':'proxmox5','vmid':140}]}
            raise AssertionError(path)
        api.request.side_effect = request
        ledger = Mock(); ledger.get_job.return_value = {'state':'running','allocations':[allocation]}
        journal = Mock(); journal.allocation_lock.side_effect = contextlib.nullcontext
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory); (root/'artifact-pins').mkdir()
            pin = root/'artifact-pins'/(job+'.json')
            pin.write_text(json.dumps({'job_id':job,'state':'active','artifact_sha256':'b'*64,
                                      'volume_id':image['artifact']['volume_id']}))
            with patch.object(jobs,'_root',return_value=root), \
                 patch.object(jobs,'shared_reservation_validator',return_value=lambda *args:True), \
                 patch.object(jobs,'required_capacity',return_value={}), \
                 patch.object(jobs,'select_probe_bridge',return_value='crprobe1450'), \
                 patch.object(jobs,'cache_owned',return_value=True), \
                 patch.object(jobs,'_cache_probe_identity',return_value=({'machine_id':'original'},'systemd',False)), \
                 patch.object(jobs,'proved_linked_storage',return_value={('proxmox5','crange-thin')}), \
                 patch.object(jobs,'ProbeVerifier'), \
                 patch.object(jobs,'ReplicaWorker') as factory:
                factory.return_value.resume_template.return_value = {'verified':True}
                result = jobs._resume_accepted_container_restore(api,ledger,journal,{}, {},image,record,100)
                self.assertEqual(result, {'verified':True})
                factory.return_value.resume_template.assert_called_once()
                self.assertTrue(pin.exists())
                ledger.reconcile_job.assert_not_called()
                self.assertTrue(all(call.args[0]=='GET' for call in api.request.call_args_list))
                factory.reset_mock(); uncertain = True
                with self.assertRaisesRegex(jobs.DistributionError,'not confirmed successful'):
                    jobs._resume_accepted_container_restore(api,ledger,journal,{}, {},image,record,100)
                factory.assert_not_called()
