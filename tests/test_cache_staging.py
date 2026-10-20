import copy
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace, ModuleType
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange
import crange_scheduling as scheduling
from crange_jobs import command
from test_placement import fixtures

class CacheStagingTests(unittest.TestCase):
    def test_archive_cache_preview_does_not_depend_on_offline_original_template(self):
        from crange_distribution import DistributionError
        from unittest.mock import Mock
        for published_sizing in (True, False):
            with self.subTest(published_sizing=published_sizing):
                _,inv,cat,pol=fixtures();pol['enabled']=True;inv['captured_at']=time.time()
                inv['nodes']['n1']['online']=False
                image=cat['images']['base-v1'];image['disk_gb']=20
                image['replicas']=[image['replicas'][0]]
                if published_sizing:image.update(memory_mb=2048,cpu_cores=2)
                row={'resource_key':'vm:web','image':'base-v1','kind':'vm',
                     'box':{'template':'base','node_name':'auto','memory_mb':4096,'cores':2},
                     'reference':{'profile':{'cpu_type':'x86-64-v2-AES'}},'networks':['p003t254']}
                api=Mock();api.request.side_effect=AssertionError('Offline template must not be contacted')
                with tempfile.TemporaryDirectory() as directory:
                    path=Path(directory)/'project-003';path.mkdir()
                    with patch.object(scheduling,'DATA',Path(directory)/'distribution'), \
                         patch.object(scheduling,'read_settings',return_value=(pol,cat)), \
                         patch.object(scheduling,'declarations',return_value=[row]), \
                         patch.object(scheduling,'applied_resources',return_value={}), \
                         patch.object(scheduling,'discover',return_value=inv):
                        if published_sizing:
                            fills=scheduling.required_preparations(path,{'project_id':3},api=api)
                            self.assertEqual([fill['node'] for fill in fills],['n2'])
                        else:
                            with self.assertRaisesRegex(DistributionError,'defensible'):
                                scheduling.required_preparations(path,{'project_id':3},api=api)
                api.request.assert_not_called()

    def test_acceptance_policy_requires_root_owned_exact_node_and_storage(self):
        from crange_cache_jobs import acceptance_policy
        from crange_distribution import DistributionError
        from unittest.mock import Mock
        policy = {'enabled':True,'allowed_nodes':['proxmox2'],
            'approved_storage':[['proxmox2','local-lvm']],
            'nodes':{'proxmox2':{'container_cache_preparation_allowed':True}}}
        path = Mock(); path.stat.return_value = SimpleNamespace(st_uid=0, st_mode=0o640)
        path.is_symlink.return_value = False; path.read_text.return_value = json.dumps(policy)
        with patch('crange_cache_jobs.Path',return_value=path):
            self.assertEqual(acceptance_policy('/policy','proxmox2','local-lvm'),policy)
            for values in [(1000,0o640),(0,0o666)]:
                path.stat.return_value = SimpleNamespace(st_uid=values[0],st_mode=values[1])
                with self.assertRaises(DistributionError): acceptance_policy('/policy','proxmox2','local-lvm')
            path.stat.return_value = SimpleNamespace(st_uid=0,st_mode=0o640)
            with self.assertRaises(DistributionError): acceptance_policy('/policy','proxmox1','local-lvm')
            with self.assertRaises(DistributionError): acceptance_policy('/policy','proxmox2','hdd-lvm')
            path.is_symlink.return_value = True
            with self.assertRaises(DistributionError): acceptance_policy('/policy','proxmox2','local-lvm')

    def test_gui_cache_job_cannot_supply_acceptance_policy_or_identity(self):
        for field in ['acceptance_policy','acceptance_job_id']:
            with self.assertRaises(ValueError):
                command('template-distribution-prepare',3,{'image':'base-v1','node':'proxmox2',
                    'storage':'local-lvm',field:'/policy'})

    def test_terraform_override_follows_protected_environment_load(self):
        with patch('crange_proxmox_compat.reviewed_identity', return_value={'enabled':True,'endpoint':'http://127.0.0.1:18766/'}), patch.object(crange,'run') as run:
            crange.tf_run(Path('/project'), ['plan'])
        shell = run.call_args.args[0][2]
        self.assertLess(shell.index('. /home/crange/.config/crange/proxmox.env'), shell.index('export PROXMOX_VE_ENDPOINT='))
        self.assertIn('exec terraform plan -no-color', shell)

    def test_disabled_adapter_preserves_direct_environment_endpoint(self):
        with patch('crange_proxmox_compat.reviewed_identity', return_value={'enabled':False}), patch.object(crange,'run') as run:
            crange.tf_run(Path('/project'), ['plan'])
        self.assertNotIn('PROXMOX_VE_ENDPOINT=', run.call_args.args[0][2])

    def test_fixed_stage_job_has_no_user_command_inputs(self):
        self.assertEqual(command('stage-changes',3,{}),['stage-changes','3'])
        with self.assertRaises(ValueError):command('stage-changes',3,{'shell':'id'})

    def test_alias_change_does_not_restage_existing_guest(self):
        r,inv,cat,pol=fixtures();pol['enabled']=True
        row={'resource_key':'vm:web','image':'new-alias-version','kind':'vm','box':{'template':'base','node_name':'auto'},'reference':{'template_id':999},'networks':[]}
        live={'node_name':'n1','memory':[{'dedicated':2048}],'cpu':[{'cores':2}],'clone':[{'vm_id':301,'node_name':'n1','full':False}],'disk':[{'datastore_id':'local'}]}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'project-003';path.mkdir()
            (path/'crange-applied-placements.json').write_text(json.dumps({'vm:web':{'image_id':'old-version','declared_template':'base','node_name':'n1','datastore_id':'local','template_id':301,'template_node':'n1','clone_mode':'linked'}}))
            with patch.object(scheduling,'DATA',Path(directory)/'distribution'),patch.object(scheduling,'read_settings',return_value=(pol,cat)),patch.object(scheduling,'declarations',return_value=[row]),patch.object(scheduling,'applied_resources',return_value={'vm:web':live}),patch.object(scheduling,'discover',return_value=inv),patch.object(crange,'validate_project_quota'):
                result=scheduling.prepare(path,{'project_id':3},'plan','worker',api=object())
            self.assertEqual(result['placements'][0]['resolved']['template_id'],301)
            self.assertEqual(result['placements'][0]['image'],'old-version')
            self.assertEqual(result['requests'],[])

    def test_cache_target_preview_budgets_base_and_guest_without_catalog_mutation(self):
        r,inv,cat,pol=fixtures();pol['enabled']=True
        cat['images']['base-v1']['disk_gb']=20
        inv['captured_at']=time.time()
        for replica in cat['images']['base-v1']['replicas']:replica['clone_modes_verified']=[]
        row={'resource_key':'vm:web','image':'base-v1','kind':'vm','box':{'template':'base','node_name':'auto','memory_mb':4096,'cores':2},'reference':{'profile':{'cpu_type':'x86-64-v2-AES'}},'networks':['p003t254']}
        api=SimpleNamespace(request=lambda *a,**k:{'memory':2048,'cores':2})
        original=copy.deepcopy(cat)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'project-003';path.mkdir()
            with patch.object(scheduling,'DATA',Path(directory)/'distribution'),patch.object(scheduling,'read_settings',return_value=(pol,cat)),patch.object(scheduling,'declarations',return_value=[row]),patch.object(scheduling,'applied_resources',return_value={}),patch.object(scheduling,'discover',return_value=inv):
                fills=scheduling.required_preparations(path,{'project_id':3},api=api)
        self.assertEqual(len(fills),1)
        self.assertEqual(fills[0]['additional_capacity']['disk_gb'],22)
        self.assertEqual(fills[0]['image'],'base-v1')
        self.assertEqual(cat,original)
        self.assertEqual(inv['nodes']['n1']['committed_memory_mb'],4096)

    def cache_preview_with_existing_pool(self, free_gb, with_large_node=False):
        _,inv,cat,pol=fixtures();pol.update(enabled=True,allowed_nodes=['n1'],allowed_storages=['local','local-hdd'])
        inv['captured_at']=time.time();node=inv['nodes']['n1']
        node['storages']['local']['free_gb']=free_gb
        node['storages']['local-hdd']={**copy.deepcopy(node['storages']['local']),'total_gb':4000,'free_gb':3000,'committed_gb':0}
        cat['images']['base-v1']['disk_gb']=20
        if with_large_node:
            pol['allowed_nodes']=['n1','n2']
            cat['images']['base-v1']['replicas']=[r for r in cat['images']['base-v1']['replicas'] if r['node']=='n1']
            node['committed_memory_mb']=8192;node['committed_cpu_cores']=4
            other=inv['nodes']['n2'];other.update(memory_mb=262144,cpu_cores=64)
            other['storages']['local'].update(total_gb=4000,free_gb=3000,committed_gb=0)
        row={'resource_key':'vm:web','image':'base-v1','kind':'vm','box':{'template':'base','node_name':'auto','memory_mb':4096,'cores':2},'reference':{'profile':{'cpu_type':'x86-64-v2-AES'}},'networks':['p003t254']}
        api=SimpleNamespace(request=lambda *a,**k:{'memory':2048,'cores':2})
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'project-003';path.mkdir()
            with patch.object(scheduling,'DATA',Path(directory)/'distribution'), \
                 patch.object(scheduling,'read_settings',return_value=(pol,cat)), \
                 patch.object(scheduling,'declarations',return_value=[row]), \
                 patch.object(scheduling,'applied_resources',return_value={}), \
                 patch.object(scheduling,'discover',return_value=inv):
                return scheduling.required_preparations(path,{'project_id':3},api=api)

    def test_reuse_verified_cache_before_filling_another_pool_on_same_node(self):
        self.assertEqual(self.cache_preview_with_existing_pool(80),[])

    def test_cache_reuse_does_not_prevent_balancing_to_a_larger_node(self):
        fills=self.cache_preview_with_existing_pool(80,with_large_node=True)
        self.assertEqual(len(fills),1)
        self.assertEqual(fills[0]['node'],'n2')

    def test_prepare_another_pool_when_verified_pool_cannot_fit_guest(self):
        fills=self.cache_preview_with_existing_pool(0)
        self.assertEqual(len(fills),1)
        self.assertEqual(fills[0]['storage'],'local-hdd')

    def test_serial_cache_preview_releases_probe_resources_before_the_next_image(self):
        _,inv,cat,pol=fixtures();pol.update(enabled=True,allowed_nodes=['n1'],cpu_reserve_fraction=0)
        inv['captured_at']=time.time();inv['nodes']['n1']['cpu_cores']=4
        image=cat['images']['base-v1'];image['disk_gb']=20
        for replica in image['replicas']:replica['clone_modes_verified']=[]
        cat['images']['base-v2']=copy.deepcopy(image)
        for replica in cat['images']['base-v2']['replicas']:replica['template_id']+=100
        rows=[{'resource_key':'vm:'+name,'image':image_id,'kind':'vm',
               'box':{'template':'base','node_name':'auto','memory_mb':1024,'cores':1},
               'reference':{'profile':{'cpu_type':'x86-64-v2-AES'}},'networks':['p003t254']}
              for name,image_id in [('one','base-v1'),('two','base-v2')]]
        api=SimpleNamespace(request=lambda *a,**k:{'memory':2048,'cores':2})
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'project-003';path.mkdir()
            with patch.object(scheduling,'DATA',Path(directory)/'distribution'), \
                 patch.object(scheduling,'read_settings',return_value=(pol,cat)), \
                 patch.object(scheduling,'declarations',return_value=rows), \
                 patch.object(scheduling,'applied_resources',return_value={}), \
                 patch.object(scheduling,'discover',return_value=inv):
                fills=scheduling.required_preparations(path,{'project_id':3},api=api)
        self.assertEqual(len(fills),2)
        self.assertTrue(all(fill['node']=='n1' for fill in fills))
        self.assertEqual(inv['nodes']['n1']['committed_cpu_cores'],1)

    def test_preview_matches_ledger_ram_budget_and_rejects_excess_batch(self):
        from crange_placement import PlacementError, ReservationLedger, capacity_window
        _,inv,cat,pol=fixtures()
        pol.update(enabled=True,allowed_nodes=['n1'],cpu_reserve_fraction=0,
                   memory_reserve_mb=2048,memory_reserve_fraction=.125,memory_overcommit_ratio=2)
        inv['captured_at']=time.time();node=inv['nodes']['n1']
        node.update(memory_mb=8192,observed_memory_mb=2048,committed_memory_mb=2048)
        image=cat['images']['base-v1'];image['disk_gb']=8
        image['replicas']=[r for r in image['replicas'] if r['node']=='n1']
        self.assertEqual(capacity_window(node,pol,'n1')['memory_mb'],8192)
        rows=[{'resource_key':'vm:'+name,'image':'base-v1','kind':'vm',
               'box':{'template':'base','node_name':'auto','memory_mb':4096,'cores':1},
               'reference':{'profile':{'cpu_type':'x86-64-v2-AES'}},'networks':['p003t254']}
              for name in ('one','two','three')]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'project-003';path.mkdir()
            ledger=ReservationLedger(Path(directory)/'ledger.sqlite3')
            for i in range(2):
                ledger.reserve_preparation(project_id=3,job_id='job'+str(i),owner='test',
                    image_id='base-v1',catalog=cat,node_name='n1',datastore_id='local',
                    capacity={'memory_mb':4096,'cpu_cores':1,'disk_gb':8,'clone_mode':'linked'},inventory=inv,policy=pol)
            with self.assertRaises(PlacementError):
                ledger.reserve_preparation(project_id=3,job_id='job3',owner='test',
                    image_id='base-v1',catalog=cat,node_name='n1',datastore_id='local',
                    capacity={'memory_mb':4096,'cpu_cores':1,'disk_gb':8,'clone_mode':'linked'},inventory=inv,policy=pol)
            ledger.close()
            with patch.object(scheduling,'DATA',Path(directory)/'distribution'), \
                 patch.object(scheduling,'read_settings',return_value=(pol,cat)), \
                 patch.object(scheduling,'declarations',return_value=rows[:2]), \
                 patch.object(scheduling,'applied_resources',return_value={}), \
                 patch.object(scheduling,'discover',return_value=inv):
                self.assertEqual(scheduling.required_preparations(path,{'project_id':3},api=SimpleNamespace()),[])
                with patch.object(scheduling,'declarations',return_value=rows),self.assertRaises(PlacementError):
                    scheduling.required_preparations(path,{'project_id':3},api=SimpleNamespace())
        self.assertEqual(node['observed_memory_mb'],2048)

    def test_ready_container_archive_ram_failure_identifies_guest_only_budget(self):
        from crange_placement import PlacementError
        _,inv,cat,pol=fixtures();pol['enabled']=True;pol['allowed_nodes']=['n1']
        inv['captured_at']=time.time()
        node=inv['nodes']['n1'];node['guest_types']=['container'];node['observed_memory_mb']=15000
        node['storages']['local']['guest_types']=['container']
        image=cat['images']['base-v1'];image.update(kind='container',disk_gb=8,source_type='stock_archive')
        image['replicas']=[{'node':'n1','datastore_id':'local','state':'ready','template_file_id':'local:vztmpl/base.tar.zst',
                            'artifact_sha256':'a'*64,'verified_at':1,'clone_modes_verified':['full']}]
        row={'resource_key':'container:t254-db','image':'base-v1','kind':'container',
             'box':{'template':'base','node_name':'auto','memory_mb':1024,'cores':1},
             'reference':{},'networks':['p003t254']}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'project-003';path.mkdir()
            with patch.object(scheduling,'DATA',Path(directory)/'distribution'), \
                 patch.object(scheduling,'read_settings',return_value=(pol,cat)), \
                 patch.object(scheduling,'declarations',return_value=[row]), \
                 patch.object(scheduling,'applied_resources',return_value={}), \
                 patch.object(scheduling,'discover',return_value=inv), \
                 patch('crange_distribution.required_capacity',side_effect=AssertionError('Ready archive must not need a cache probe')):
                with self.assertRaises(PlacementError) as caught:
                    scheduling.required_preparations(path,{'project_id':3},api=SimpleNamespace(request=lambda *a,**k:None))
        message=str(caught.exception)
        self.assertIn('guest preparation needs 1024 MiB RAM; available 0.0',message)
        self.assertIn('cache=0',message)
        self.assertIn('observed=15000.0 MiB',message)
        self.assertNotIn('guest + cache preparation',message)

    def test_stage_prepares_approved_caches_before_retaining_plan(self):
        module=ModuleType('crange_cache_jobs')
        from unittest.mock import Mock
        module.prepare_cache=Mock(return_value={"template_id":301})
        module.reconcile_unstarted_caches=Mock(return_value=[])
        module.resume_project_snapshot_caches=Mock()
        cfg={'project_id':3}
        fill={'image':'base-v1','node':'proxmox2','storage':'local-lvm','additional_capacity':{'disk_gb':22,'memory_mb':2048,'cpu_cores':2},'resource_keys':['vm:web']}
        with patch.dict(sys.modules,{'crange_cache_jobs':module}),patch.object(crange,'project_dir',return_value=Path('/tmp/project-003')),patch.object(crange,'json_config',return_value=cfg),patch.object(scheduling,'read_settings',return_value=({'enabled':True},{})),patch.object(scheduling,'required_preparations',return_value=[fill]),patch.object(scheduling,'purge_expired_guest_reviews',return_value=[]) as purge,patch('crange_placement.ReservationLedger'),patch.object(crange,'ProxmoxAPI'),patch.object(scheduling,'declarations',return_value=[{'image':'base-v1'}]),patch.object(crange,'cmd_plan') as plan:
            crange.cmd_stage_changes(SimpleNamespace(project='3'))
        purge.assert_called_once()
        self.assertEqual(module.prepare_cache.call_args.kwargs['image_id'],'base-v1')
        self.assertEqual(module.prepare_cache.call_args.kwargs['additional_capacity']['disk_gb'],22)
        self.assertTrue(plan.call_args.args[0].retain)

    def test_disabled_stage_retains_pure_legacy_plan(self):
        with patch.object(crange,'project_dir',return_value=Path('/tmp/project-003')),patch.object(crange,'json_config',return_value={'project_id':3}),patch.object(scheduling,'read_settings',return_value=({'enabled':False},{})),patch.object(crange,'cmd_plan') as plan:
            crange.cmd_stage_changes(SimpleNamespace(project='3'))
        self.assertTrue(plan.call_args.args[0].retain)


class ImmutableDeclarationTests(unittest.TestCase):
    def test_new_vm_pins_current_alias_and_later_alias_does_not_change_it(self):
        import test_crange as fixture
        case=fixture.CrangeConfigTests();case.setUp()
        try:
            refs={'approved-base':{'template_id':112,'template_node':'proxmox4','image_id':'base-v2'}}
            (case.project/'approved-scenario-templates.json').write_text(json.dumps(refs))
            fixture.crange.cmd_scenario_add(SimpleNamespace(project='3',name='pinned',team=0,ip=25,template='approved-base',node='auto',cores=2,memory=2048,health='[]',full_clone=None,network=''))
            stored=fixture.crange.json_config(case.project)['scenario_boxes']['t254-pinned']
            self.assertEqual(stored['image'],'base-v2')
            self.assertFalse(stored['full_clone'])
            refs['approved-base'].update(image_id='base-v3',template_id=113)
            (case.project/'approved-scenario-templates.json').write_text(json.dumps(refs))
            fixture.crange.cmd_scenario_edit(SimpleNamespace(project='3',name='t254-pinned',template='approved-base',node='auto',cores=2,memory=2048,health='[]',ip=25,network=''))
            self.assertEqual(fixture.crange.json_config(case.project)['scenario_boxes']['t254-pinned']['image'],'base-v2')
        finally:
            case.tearDown();case.doCleanups()
