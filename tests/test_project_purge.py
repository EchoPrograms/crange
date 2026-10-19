import copy
from contextlib import ExitStack, nullcontext
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from crange_project_purge import purge_images, forget_project
from crange_auth import AuthStore
from crange_jobs import JobStore

class PurgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.archive=self.root/'terraform/environments/project-100/.crange-lifecycle';self.archive.mkdir(parents=True)
        releases=self.root/'releases/project-100';releases.mkdir(parents=True)
        (releases/'1.0.0.json').write_text(json.dumps({'project_id':100,'release_id':'release-owned'}))
        self.volume='crange-library:backup/vzdump-qemu-145.vma.zst'
        self.replica={'node':'proxmox4','template_id':145}
        self.image={'kind':'vm','approved_projects':[100],'release':{'release_id':'release-owned'},'retained_release_references':['release-owned'],
                    'identity_capture':{'capture_name':'p100-image-web','template_id':145,'node':'proxmox4'},
                    'replicas':[self.replica], 'artifact':{'node':'proxmox4','volume_id':self.volume,'complete_capture':True}}
        self.catalog={'images':{'private':self.image,'generic':{'kind':'container','approved_projects':[100],'replicas':[]}}}
        self.config={'name':'p100-image-web','template':1,'tags':'p100;crange-image'};self.present=True;self.backup=True
        self.api=Mock();self.api.request.side_effect=self.request
        self.evidence={'cluster_complete':True,'snapshots_complete':True,'backing_complete':True,'reservations_complete':True,'operations_complete':True,'pins_complete':True,
            'revision':'tested','observed_at':time.time(),'candidate':{'node':'proxmox4','template_id':145,'volumes':['local-lvm:base-145-disk-0']},'guests':[]}
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        for name,value in [('read_settings',lambda: ({},copy.deepcopy(self.catalog))),('node_locks',lambda *a,**k:nullcontext()),('DATA',self.root/'data'),('CONFIG',self.root/'config')]:
            self.stack.enter_context(patch('crange_scheduling.'+name,value))
        self.stack.enter_context(patch('crange_distribution.FileJournal'))
        self.stack.enter_context(patch('crange_placement.ReservationLedger'))
        self.collect=self.stack.enter_context(patch('crange_cache_jobs.collect_cache_evidence',return_value=self.evidence))
        def transaction(path,update):self.catalog=update(copy.deepcopy(self.catalog));return self.catalog
        self.stack.enter_context(patch('crange_distribution.catalog_transaction',side_effect=transaction))

    def request(self,method,path,params=None):
        if method=='DELETE':
            if path.endswith('/qemu/145'):self.present=False
            elif '/storage/crange-library/content/' in path:self.backup=False
            else:raise AssertionError(path)
            return 'UPID:owned'
        if path=='/cluster/resources':return [{'vmid':145,'node':'proxmox4','type':'qemu','template':1,'tags':'p100;crange-image'}] if self.present else []
        if path.endswith('/config'):return self.config
        if path.endswith('/content'):return [{'volid':self.volume}] if self.backup else []
        raise AssertionError(path)

    def deletes(self):return [c for c in self.api.request.call_args_list if c.args[0]=='DELETE']

    def test_removes_only_owned_replicas_and_archives_and_preserves_base(self):
        self.evidence['snapshots']=[{'node':'proxmox4','vmid':145,'name':'owned-release-snapshot'}]
        purge_images(self.api,self.root,100,self.archive)
        self.assertFalse(self.present);self.assertFalse(self.backup);self.assertEqual(set(self.catalog['images']),{'generic'})
        self.assertEqual(len(self.deletes()),2)
        purge_images(self.api,self.root,100,self.archive)
        self.assertEqual(len(self.deletes()),2,'completed deletion is never replayed')

    def test_dependency_or_unreachable_node_blocks_all_deletions(self):
        self.evidence['guests']=[{'vmid':999,'node':'proxmox5','origin_template_id':145}]
        with self.assertRaisesRegex(ValueError,'required'):purge_images(self.api,self.root,100,self.archive)
        self.assertFalse(self.deletes())
        self.evidence['guests']=[];self.collect.side_effect=ValueError('Every cluster node must be reachable')
        with self.assertRaisesRegex(ValueError,'reachable'):purge_images(self.api,self.root,100,self.archive)
        self.assertFalse(self.deletes())

    def test_reused_template_id_or_shared_image_is_not_deleted(self):
        self.config['name']='unrelated-production-vm'
        with self.assertRaisesRegex(ValueError,'ownership'):purge_images(self.api,self.root,100,self.archive)
        self.assertFalse(self.deletes());self.config['name']='p100-image-web';self.image['approved_projects']=[100,101]
        with self.assertRaisesRegex(ValueError,'shared'):purge_images(self.api,self.root,100,self.archive)
        self.assertFalse(self.deletes())

    def test_permanent_metadata_cleanup_keeps_users_other_projects_and_admin_audit(self):
        auth=AuthStore(self.root/'auth.sqlite3');auth.set_password('manager','test long password',projects=['100','101'])
        auth.set_project_role('manager',100,'manager');auth.set_project_role('manager',101,'manager')
        job_path=self.root/'jobs.sqlite3';jobs=JobStore(job_path)
        with patch('crange.ENVS',self.root/'terraform/environments'):
            job=jobs.submit('manager',100,'plan',{})
        path=self.archive.parent;(path/'.crange-lifecycle.json').write_text('{"state":"purging"}')
        with patch.dict(os.environ,{'CRANGE_AUTH_DB':str(auth.path),'CRANGE_JOB_DB':str(job_path),'CRANGE_GUEST_CREDENTIALS_DIR':str(self.root/'credentials')}):
            forget_project(self.root,100,path,'admin')
        self.assertFalse(path.exists());self.assertFalse((self.root/'releases/project-100').exists())
        record=auth.list_users()[0];self.assertEqual(record['projects'],['101']);self.assertEqual(record['roles'],{'101':'manager'})
        self.assertIsNone(jobs.get(job,['100']));self.assertIsNotNone(jobs.get(job,[],admin=True))
        self.assertTrue(list((self.root/'data/project-retirements').glob('*.json')))

if __name__=='__main__':unittest.main()
