import copy
import importlib.util
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from crange_jobs import command
import crange_ct_read_relay as relay
spec=importlib.util.spec_from_file_location('storage',pathlib.Path(__file__).resolve().parents[1]/'ops/proxmox/range_storage.py')
storage=importlib.util.module_from_spec(spec);spec.loader.exec_module(storage)

class StorageTests(unittest.TestCase):
    def test_admin_scope_and_fixed_payload(self):
        self.assertEqual(command('template-range-storage-review',0,{'size_gb':512}),['range-storage-review','--size-gb','512'])
        for project,options in [(100,{'size_gb':512}),(0,{'size_gb':True}),(0,{'size_gb':1024}),(0,{'size_gb':512,'vg':'pve'})]:
            with self.assertRaises(ValueError): command('template-range-storage-review',project,options)

    def test_relay_peer_and_exact_fields(self):
        config={'peer_uid':101000,'node_addresses':{'proxmox5':'10.0.0.105'}}
        good={'command':'range-storage-review','node':'proxmox5','size_gb':512}
        self.assertEqual(relay.validate_request(good,config,101000)[:3],('proxmox5',0,'range-storage-review'))
        for request,uid in [({**good,'node':'proxmox1'},101000),({**good,'size_gb':True},101000),({**good,'shell':'id'},101000),(good,0)]:
            with self.assertRaises(ValueError):relay.validate_request(request,config,uid)

    def test_review_has_no_mutations_and_apply_checks_freshness(self):
        initial={'vg':{'vg_size':str(932*storage.GIB),'vg_free':str(932*storage.GIB)},'lvs':[],'storage':[]}
        with tempfile.TemporaryDirectory() as d,patch.object(storage,'ROOT',pathlib.Path(d)),patch.object(storage,'inventory',return_value=initial),patch.object(storage,'run') as run:
            plan=storage.review(512);run.assert_not_called()
            changed=copy.deepcopy(initial);changed['lvs']=[{'lv_name':'unrelated'}]
            with patch.object(storage,'inventory',return_value=changed),self.assertRaises(ValueError):storage.apply(plan['plan_id'],plan['review_sha256'])
            run.assert_not_called()
            with self.assertRaises(ValueError):storage.apply(plan['plan_id'],'b'*64)

    def test_nonempty_or_reserve_exhausting_group_refused(self):
        for initial in [ {'vg':{'vg_size':str(600*storage.GIB),'vg_free':str(600*storage.GIB)},'lvs':[],'storage':[]},
                         {'vg':{},'lvs':[{'lv_name':'existing'}],'storage':[]} ]:
            with patch.object(storage,'inventory',return_value=initial),self.assertRaises(ValueError):storage.review(512)

    def test_uncertain_creation_never_resubmits(self):
        initial={'vg':{'vg_size':str(932*storage.GIB),'vg_free':str(932*storage.GIB)},'lvs':[],'storage':[]}
        with tempfile.TemporaryDirectory() as d,patch.object(storage,'ROOT',pathlib.Path(d)),patch.object(storage,'inventory',return_value=initial):
            review=storage.review(512);path=pathlib.Path(d)/(review['plan_id']+'.json')
            plan=__import__('json').loads(path.read_text());plan['state']='creating';storage.save(path,plan)
            with patch.object(storage,'run') as run,self.assertRaises(ValueError):storage.apply(review['plan_id'],review['review_sha256'])
            run.assert_not_called()

class ApprovalTests(unittest.TestCase):
    def test_unverified_proof_cannot_change_policy(self):
        import crange_range_storage
        for proof in ({},{'node':'proxmox5','storage':'crange-thin','state':'ready','snapshots_verified':False,'proof_volumes_deleted':True}):
            with patch('crange.ProxmoxAPI') as api,self.assertRaises(ValueError):crange_range_storage.approve_storage(proof)
            api.assert_not_called()

    def test_approval_preserves_other_policy_and_adds_only_proved_storage(self):
        import json
        import crange_range_storage
        proof={'node':'proxmox5','storage':'crange-thin','state':'ready','snapshots_verified':True,'proof_volumes_deleted':True}
        policy={'revision':'before','allowed_nodes':['proxmox5'],'allowed_storages':['local-lvm'],
                'nodes':{'proxmox5':{'snapshot_storages':['local-lvm'],'cpu_reserve_cores':2}},
                'network_attestations':{'proxmox5':{'vnets':['p100c100']}},'memory_overcommit_ratio':2}
        def get(method,path):
            return [{'storage':'crange-thin','active':1,'shared':0}]
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d);path=root/'distribution-policy.json';path.write_text(json.dumps(policy))
            with patch('crange_scheduling.CONFIG',root),patch('crange_scheduling.read_settings',return_value=(copy.deepcopy(policy),{})),patch('crange.ProxmoxAPI') as api:
                api.return_value.request.side_effect=get
                crange_range_storage.approve_storage(proof)
            saved=json.loads(path.read_text())
        expected=copy.deepcopy(policy);expected['revision']=saved['revision'];expected['allowed_storages'].append('crange-thin')
        expected['nodes']['proxmox5']['snapshot_storages'].append('crange-thin');expected['nodes']['proxmox5']['storage_proofs']={'crange-thin':proof}
        self.assertEqual(saved,expected)

class NativeProofTests(unittest.TestCase):
    def test_retained_inactive_snapshot_is_activated_and_cleaned_after_cow(self):
        from types import SimpleNamespace
        import stat
        identity='a'*32;parent='crange_proof_'+identity[:12];child='crange_snap_'+identity[:12]
        tag='crange-storage-'+identity
        rows=[{'lv_name':name,'pool_lv':'crange_data','lv_attr':'Vwi---tz-k','lv_size':str(64*2**20),'lv_tags':tag} for name in (parent,child)]
        blocks={100:b'A'*4096,101:b'A'*4096};calls=[]
        def run(args):
            calls.append(args)
            if args[0]=='lvremove':rows[:]=[r for r in rows if not args[-1].endswith('/'+r['lv_name'])]
        def pwrite(fd,data,offset):blocks[fd]=data;return len(data)
        with patch.object(storage,'inventory',side_effect=lambda:{'lvs':copy.deepcopy(rows)}),patch.object(storage,'run',side_effect=run), \
             patch.object(storage.os,'open',side_effect=lambda path,flags:100 if path.endswith('/'+parent) else 101), \
             patch.object(storage.os,'fstat',return_value=SimpleNamespace(st_mode=stat.S_IFBLK)), \
             patch.object(storage.os,'pread',side_effect=lambda fd,*args:blocks[fd]),patch.object(storage.os,'pwrite',side_effect=pwrite), \
             patch.object(storage.os,'fsync'),patch.object(storage.os,'close'):
            self.assertTrue(storage.snapshot_proof(identity))
        self.assertFalse(any(args[0]=='lvcreate' for args in calls))
        self.assertIn(['lvchange','--activate','y','--ignoreactivationskip','hdd-lvm/'+child],calls)
        self.assertEqual(blocks[100],b'A'*4096);self.assertEqual(blocks[101],b'B'*4096)
        self.assertEqual(rows,[])

    def test_foreign_retained_proof_cannot_be_written_or_deleted(self):
        identity='a'*32
        with patch.object(storage,'inventory',return_value={'lvs':[{'lv_name':'crange_proof_'+identity[:12],'pool_lv':'crange_data','lv_tags':'foreign'}]}), \
             patch.object(storage,'run') as run,self.assertRaises(ValueError):storage.snapshot_proof(identity)
        run.assert_not_called()
