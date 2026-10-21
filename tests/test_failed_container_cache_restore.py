import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_cache_jobs as jobs
from crange_distribution import DistributionError, FileJournal


class FailedContainerRestoreTests(unittest.TestCase):
    def run_case(self, *, reused=False, remains=None, active=False, failed=True, other_task=False):
        job='cache-'+'a'*32;digest='b'*64;node='proxmox2';vmid=247
        upid='UPID:proxmox2:001:002:003:vzrestore:247:user:'
        image={'kind':'container','artifact_sha256':digest,
               'artifact':{'node':'proxmox6','source_id':174,'volume_id':'crange-library:backup/image.tar.zst'}}
        allocation={'operation':'cache_prepare','image':'ubuntu-base','artifact_sha256':digest,
                    'resolved':{'node_name':node,'datastore_id':'local-lvm'}}
        record={'state':'allocated','job_id':job,'kind':'lxc','node':node,'allocation_node':node,
                'template_id':vmid,'datastore_id':'local-lvm','source_node':'proxmox6','source_id':174,
                'image_id':'ubuntu-base','artifact_sha256':digest,'reservation':allocation,
                'provider_tasks':[{'node':node,'upid':upid,'state':'running'}]}
        ledger=Mock();ledger.get_job.return_value={'state':'running','allocations':[allocation]}
        api=Mock()
        def request(method,path,fields=None):
            self.assertEqual(method,'GET')
            if '/tasks/' in path:
                return {'status':'stopped' if failed else 'running','exitstatus':
                    "unable to restore CT 247 - unable to activate storage 'crange-library' - directory '/mnt/pve/crange-library' does not exist or is unreachable"}
            if path=='/cluster/resources':
                return ([{'vmid':247,'node':'proxmox3','type':'lxc'}] if reused else [])+(
                       [{'vmid':247,'node':node,'type':'lxc'}] if remains=='guest' else [])
            if path.endswith('/tasks') and fields['source']=='all':return [{'id':'247','type':'vzrestore','starttime':5,'upid':'different'}] if other_task else []
            if path.endswith('/tasks'):return [{'id':'247','upid':upid}] if active else []
            if path.endswith('/content'):return [{'volid':'local-lvm:vm-247-disk-0'}] if remains=='volume' else []
            raise AssertionError(path)
        api.request.side_effect=request
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);pins=root/'artifact-pins';pins.mkdir()
            pin=pins/(job+'.json');pin.write_text(json.dumps({'job_id':job,'state':'active',
                'artifact_sha256':digest,'volume_id':image['artifact']['volume_id']}))
            journal=FileJournal(root/'journal');journal(copy.deepcopy(record))
            with patch.object(jobs,'_root',return_value=root):
                if remains or active or not failed or other_task:
                    with self.assertRaises(DistributionError):
                        jobs.reconcile_failed_container_restore(api,ledger,journal,record,image)
                    self.assertEqual(json.loads((journal.directory/(job+'.json')).read_text())['state'],'allocated')
                    self.assertTrue(pin.exists());ledger.reconcile_job.assert_not_called()
                else:
                    jobs.reconcile_failed_container_restore(api,ledger,journal,record,image)
                    final=json.loads((journal.directory/(job+'.json')).read_text())
                    self.assertEqual(final['state'],'reconciled-absent')
                    self.assertEqual(final['absence_evidence']['vmid_present_elsewhere'],reused)
                    self.assertFalse(pin.exists());ledger.reconcile_job.assert_called_once()
        self.assertTrue(all(call.args[0]=='GET' for call in api.request.call_args_list))

    def test_terminal_failed_restore_without_any_allocation_releases_reservation(self):
        self.run_case()

    def test_reused_id_on_another_node_is_preserved(self):
        self.run_case(reused=True)

    def test_guest_volume_active_or_uncertain_task_blocks_reconciliation(self):
        for case in ({'remains':'guest'},{'remains':'volume'},{'active':True},{'failed':False},{'other_task':True}):
            with self.subTest(case=case):self.run_case(**case)


if __name__=='__main__':unittest.main()
