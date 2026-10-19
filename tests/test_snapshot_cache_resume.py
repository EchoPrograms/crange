import json,pathlib,sys,tempfile,unittest
from contextlib import nullcontext
from unittest.mock import Mock,patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import crange_cache_jobs as jobs
from crange_placement import ReservationLedger

class ResumeTests(unittest.TestCase):
    def run_case(self,foreign=False,active=False,replace=False,missing_evidence=False,already_replaced=False):
        job='cache-'+'a'*32;sha='b'*64;node='proxmox5';store='crange-thin'
        item={'operation':'cache_prepare','image':'image','artifact_sha256':sha,
              'resolved':{'node_name':node,'datastore_id':store},'resources':{},'reservation_id':'r','resource_key':'cache:image'}
        upid='UPID:proxmox5:00000001:00000001:00000001:qmclone:139:crange-iac@pve!terraform:'
        record={'job_id':job,'node':node,'datastore_id':store,'image_id':'image','artifact_sha256':sha,
                'state':'verifying','kind':'qemu','template_id':139,'probe_id':140,
                'provider_tasks':[{'node':node,'upid':upid,'state':'confirmed-terminal'}]}
        proof={'node':node,'storage':store,'state':'ready','snapshots_verified':True,'proof_volumes_deleted':True}
        policy={'enabled':True,'allowed_nodes':[node],'allowed_storages':[store],
                'nodes':{node:{'storage_proofs':{store:proof},'snapshot_storages':[store]}}}
        catalog={'images':{'image':{'artifact_sha256':sha,'artifact':{'volume_id':'crange-library:backup/test'}}}}
        if replace and not missing_evidence:record['verification_evidence']=[{'type':'qga-timeout'}]
        if already_replaced:record['probe_replacement']={'probe_id':140,'reason':'guest-agent-verification-failed','requested_at':'2026-10-17T17:30:00Z'}
        api=Mock();published=[]
        def request(method,path,fields=None):
            if path.endswith('/config'):
                vmid=139 if '/139/' in path else 140
                return {'template':int(vmid==139),'tags':f'crange-image;crange-cache-{vmid}',
                        'description':'foreign' if foreign else ('CRANGE distribution '+job+' image image' if vmid==139 else 'CRANGE verification '+job)}
            if path.endswith('/status/current'):return {'status':'stopped'}
            if '/tasks/' in path:return {'status':'running' if active else 'stopped','exitstatus':'OK'}
            if path=='/cluster/resources':return []
            if path=='/cluster/nextid':return 140
            return upid.replace('qmclone:139','qmdestroy:140')
        api.request.side_effect=request
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);journal=root/'cache-journal';journal.mkdir();(journal/(job+'.json')).write_text(json.dumps(record))
            pins=root/'artifact-pins';pins.mkdir();(pins/(job+'.json')).write_text(json.dumps({'job_id':job,'artifact_sha256':sha,'volume_id':'crange-library:backup/test'}))
            ledger=ReservationLedger(root/'reservations.sqlite3');ledger.db.execute('INSERT INTO jobs (job_id,project_id,owner,state,expires,heartbeat,allocations) VALUES (?,?,?,?,?,?,?)',(job,100,'admin','running',9999999999,0,json.dumps([item])));ledger.close()
            with patch.object(jobs,'_root',return_value=root),patch.object(jobs,'_settings',return_value=(policy,catalog)),patch.object(jobs,'ProxmoxAPI',return_value=api), \
                 patch.object(jobs,'_cache_probe_identity',return_value=({'identity_mode':'windows-scenario-snapshot-v1'},'windows',False)), \
                 patch.object(jobs.scheduling,'node_locks',return_value=nullcontext()),patch.object(jobs,'select_probe_bridge',return_value='crprobe1450'), \
                 patch('crange_distribution._require_probe_pool'),patch.object(jobs,'ProbeVerifier') as verify, \
                 patch.object(jobs,'catalog_transaction',side_effect=lambda path,callback:callback(catalog)), \
                 patch('crange_catalog.publish_replica',side_effect=lambda c,i,r:published.append(r) or c), \
                 patch.object(jobs.scheduling,'discover',return_value={'guests':{'139':{'node_name':node,'template':True}}}):
                verify.return_value.return_value={'passed':True,'identity_unique':True}
                if foreign or active or missing_evidence or already_replaced:
                    with self.assertRaises(jobs.DistributionError):jobs.resume_snapshot_cache(job,replace_probe=replace)
                    if missing_evidence or already_replaced:verify.return_value.assert_not_called()
                    else:verify.assert_not_called()
                else:self.assertEqual(jobs.resume_snapshot_cache(job,replace_probe=replace)['state'],'ready')
            ledger=ReservationLedger(root/'reservations.sqlite3');state=ledger.get_job(job)['state'];ledger.close()
        return api,published,state

    def test_resume_verifies_existing_probe_without_restore_or_clone(self):
        api,published,state=self.run_case()
        mutations=[c.args[:2] for c in api.request.call_args_list if c.args[0]!='GET']
        self.assertEqual(mutations,[('DELETE','/nodes/proxmox5/qemu/140')]);self.assertEqual(state,'released')
        self.assertEqual(published[0]['template_id'],139);self.assertTrue(published[0]['storage_verification']['local'])

    def test_foreign_or_active_task_preserves_both_guests_and_reservation(self):
        for kwargs in ({'foreign':True},{'active':True},{'foreign':True,'replace':True},{'active':True,'replace':True}):
            api,published,state=self.run_case(**kwargs)
            self.assertEqual(state,'running');self.assertEqual(published,[])
            self.assertTrue(all(c.args[0]=='GET' for c in api.request.call_args_list))

    def test_replacement_reuses_parent_and_deletes_only_disposable_probe(self):
        api,published,state=self.run_case(replace=True)
        mutations=[c for c in api.request.call_args_list if c.args[0]!='GET']
        deletes=[c.args[1] for c in mutations if c.args[0]=='DELETE']
        self.assertEqual(deletes,['/nodes/proxmox5/qemu/140']*2)
        clones=[c for c in mutations if c.args[1].endswith('/clone')]
        self.assertEqual(len(clones),1);self.assertEqual(clones[0].args[1],'/nodes/proxmox5/qemu/139/clone')
        self.assertEqual(clones[0].args[2]['full'],0)
        self.assertEqual(state,'released');self.assertEqual(published[0]['template_id'],139)

    def test_second_probe_replacement_is_refused_without_mutation(self):
        api,published,state=self.run_case(replace=True,already_replaced=True)
        self.assertEqual(state,'running');self.assertFalse(published)
        self.assertTrue(all(c.args[0]=='GET' for c in api.request.call_args_list))

    def test_replacement_without_failed_verification_is_read_only(self):
        api,published,state=self.run_case(replace=True,missing_evidence=True)
        self.assertEqual(state,'running');self.assertFalse(published)
        self.assertTrue(all(c.args[0]=='GET' for c in api.request.call_args_list))

class ProbeBootBudgetTests(unittest.TestCase):
    def test_windows_restore_has_a_bounded_longer_boot_budget(self):
        from crange_distribution import ProbeVerifier
        with patch('crange_windows_snapshot.validate_captured_identity'):
            source={'os_family':'windows','identity_mode':'windows-scenario-snapshot-v1'}
            self.assertEqual(ProbeVerifier('probe0',['probe0'],source,init_system='windows').timeout,600)
            self.assertEqual(ProbeVerifier('probe0',['probe0'],source,init_system='windows',timeout=25).timeout,25)
        linux={'machine_id':'a','ssh_host_key':'key'}
        self.assertEqual(ProbeVerifier('probe0',['probe0'],linux).timeout,180)
