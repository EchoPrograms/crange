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

    def test_stage_prepares_approved_caches_before_retaining_plan(self):
        module=ModuleType('crange_cache_jobs')
        from unittest.mock import Mock
        module.prepare_cache=Mock(return_value={"template_id":301})
        cfg={'project_id':3}
        fill={'image':'base-v1','node':'proxmox2','storage':'local-lvm','additional_capacity':{'disk_gb':22,'memory_mb':2048,'cpu_cores':2},'resource_keys':['vm:web']}
        with patch.dict(sys.modules,{'crange_cache_jobs':module}),patch.object(crange,'project_dir',return_value=Path('/tmp/project-003')),patch.object(crange,'json_config',return_value=cfg),patch.object(scheduling,'read_settings',return_value=({'enabled':True},{})),patch.object(scheduling,'required_preparations',return_value=[fill]),patch.object(crange,'cmd_plan') as plan:
            crange.cmd_stage_changes(SimpleNamespace(project='3'))
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
