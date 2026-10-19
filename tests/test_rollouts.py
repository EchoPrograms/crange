import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import crange_rollouts as rollouts
from crange_jobs import command
import test_release_networks as topology
crange = topology.crange

class RolloutTests(unittest.TestCase):
    def test_release_node_resolution_uses_live_cluster_not_auto_or_stale_hint(self):
        api=Mock();api.request.return_value=[{'vmid':152,'type':'qemu','node':'proxmox5'}]
        for hint in ('auto','proxmox1',None):
            self.assertEqual(crange.applied_guest_node(api,'scenario_boxes',{'id':152,'node_name':hint}), 'proxmox5')
        api.request.return_value=[{'vmid':152,'type':'lxc','node':'proxmox5'}]
        self.assertEqual(crange.applied_guest_node(api,'scenario_containers',{'id':152}), 'proxmox5')
        for rows in ([], [{'vmid':152,'type':'qemu','node':'proxmox5'}],
                     [{'vmid':152,'type':'lxc','node':'auto'}],
                     [{'vmid':152,'type':'lxc','node':'proxmox5'}]*2):
            api.request.return_value=rows
            with self.assertRaises(crange.CrangeError):
                crange.applied_guest_node(api,'scenario_containers',{'id':152})

    def fixture(self):
        box = {'team_id':'1','release_id':'p003-1.0','release_scope':'team','name':'r-old',
               'ip_last_octet':20,'network_id':100,'network_key':'release-net'}
        network = {'owner_team_id':'1','release_id':'p003-1.0','release_scope':'team',
                   'network_id':100,'uplink_last_octet':100}
        cfg = {'project_id':3,'scenario_boxes':{'old':box,'unowned':{'team_id':'1','name':'manual'}},
               'scenario_containers':{},'custom_networks':{'release-net':network},
               'jump_boxes':{'team-jump':{'team_id':'1','jump_number':1}}}
        release = {'release_id':'p003-1.0','version':'1.0'}
        rollouts.declare(cfg,'1',release,'deploy')
        return cfg, release

    def apply(self,cfg):
        outputs = {'scenario_boxes':{'value':{'old':{'id':125,'address':'10.3.100.20'}}},
            'routers':{'value':{'custom_networks':{'release-net':{
            'owner_team_id':'1','lan':'10.3.100.0/24','upstream_address':'10.3.1.100'}}}}}
        return rollouts.applied(cfg,outputs)

    def test_declaration_apply_and_health_are_separate_events(self):
        cfg, _ = self.fixture();r=cfg['release_rollouts']['1']
        self.assertNotIn('applied',r);self.assertEqual(r['health']['status'],'not-checked')
        with self.assertRaisesRegex(ValueError,'not applied'):
            rollouts.applied(cfg,{})
        self.assertNotIn('applied',r)
        self.assertTrue(self.apply(cfg));self.assertFalse(self.apply(cfg))
        self.assertEqual([e['event'] for e in r['history']],['declared','infrastructure-applied'])
        rollouts.health(r,False,[{'resource':'old','error':'timeout'}])
        self.assertEqual(r['health']['status'],'failed')
        rollouts.health(r,True,[]);self.assertEqual(r['health']['status'],'passed')

    def test_upgrade_removes_only_recorded_release_resources(self):
        cfg, _ = self.fixture();self.apply(cfg)
        manual=copy.deepcopy(cfg['scenario_boxes']['unowned']);jump=copy.deepcopy(cfg['jump_boxes'])
        rollouts.begin(cfg,'1','upgrade','2.0','1.0')
        self.assertEqual(cfg['scenario_boxes'],{'unowned':manual})
        self.assertEqual(cfg['jump_boxes'],jump);self.assertFalse(cfg['custom_networks'])

    def test_pending_expected_version_and_borrowed_network_are_rejected(self):
        cfg,_=self.fixture()
        with self.assertRaisesRegex(ValueError,'pending'):
            rollouts.begin(cfg,'1','upgrade','2.0','1.0')
        self.apply(cfg)
        with self.assertRaisesRegex(ValueError,'expected release'):
            rollouts.begin(cfg,'1','upgrade','2.0','0.9')
        cfg['jump_boxes']['borrowed']={'team_id':'1','network_key':'release-net','jump_number':2}
        before=copy.deepcopy(cfg)
        with self.assertRaisesRegex(ValueError,'other guests'):
            rollouts.begin(cfg,'1','upgrade','2.0','1.0')
        self.assertEqual(cfg,before)

    def test_drift_and_unregistered_resources_are_rejected(self):
        cfg,_=self.fixture();self.apply(cfg)
        cfg['scenario_boxes']['old']['name']='foreign'
        with self.assertRaisesRegex(ValueError,'drifted'):
            rollouts.begin(cfg,'1','upgrade','2.0','1.0')
        cfg.pop('release_rollouts')
        with self.assertRaisesRegex(ValueError,'unrecorded'):
            rollouts.begin(cfg,'1','deploy','2.0')

    def test_rollback_requires_previously_applied_target(self):
        cfg,_=self.fixture();self.apply(cfg)
        with self.assertRaisesRegex(ValueError,'never applied'):
            rollouts.begin(cfg,'1','rollback','0.9','1.0')
        r=cfg['release_rollouts']['1'];r['history'].append({'event':'infrastructure-applied','version':'0.9'})
        rollouts.begin(cfg,'1','rollback','0.9','1.0')
        self.assertNotIn('old',cfg['scenario_boxes'])

    def test_fixed_jobs_validate_transition_and_scope(self):
        for action in ('upgrade','rollback'):
            self.assertEqual(command('release-'+action,3,{'version':'2.0','from_version':'1.0','teams':[1]}),
                ['release-'+action,'3','2.0','--teams','1','--from-version','1.0'])
            with self.assertRaises(ValueError):
                command('release-'+action,3,{'version':'2.0','from_version':'$(id)','teams':[1]})
        self.assertEqual(command('release-check',3,{'teams':[2]}),['release-check','3','--teams','2'])

class TransitionIntegrationTests(unittest.TestCase):
    setUp = topology.ReleaseTopologyTests.setUp
    tearDown = topology.ReleaseTopologyTests.tearDown
    write_release = topology.ReleaseTopologyTests.write_release
    release = topology.ReleaseTopologyTests.release
    def deploy(self,release,action='deploy',from_version=None,teams=(1,)):
        with patch.object(crange,'load_release',return_value=release),patch.object(crange,'verify_release_images'):
            crange.cmd_release_deploy(SimpleNamespace(project='3',version=release['version'],teams=list(teams),
                release_action=action,from_version=from_version,team_override=[]))

    def test_upgrade_and_rollback_keep_staging_and_other_team(self):
        release=self.release();cfg=crange.json_config(self.project);cfg['team_ids']=['1','2']
        crange.save_config(self.project,cfg)
        results=crange.ROOT/'releases/project-003/test-results';results.mkdir(parents=True)
        for version in ('1.0.0','2.0.0'):
            (results/(version+'-pass.json')).write_text(json.dumps({'release_id':'p003-'+version,'result':'passed'}))
        self.deploy(release,teams=(1,2));cfg=crange.json_config(self.project)
        def mark_applied():
            cfg=crange.json_config(self.project)
            outputs={field:{'value':{key:{'id':i+200,'address':f"10.3.{box['network_id']}.{box['ip_last_octet']}"}
                for i,(key,box) in enumerate(cfg.get(field,{}).items())}}
                for field in ('scenario_boxes','scenario_containers')}
            outputs['routers']={'value':{'custom_networks':{key:{'owner_team_id':n['owner_team_id'],
                'lan':f"10.3.{n['network_id']}.0/24",'upstream_address':f"10.3.{n['owner_team_id']}.{n['uplink_last_octet']}"}
                for key,n in cfg['custom_networks'].items()}}}
            with patch.object(crange,'output_json',return_value=outputs):crange.record_release_apply(self.project)
        mark_applied();before=crange.json_config(self.project)
        newer=copy.deepcopy(release);newer.update(version='2.0.0',release_id='p003-2.0.0')
        self.deploy(newer,'upgrade','1.0.0')
        pending = crange.json_config(self.project)
        old_id = next(iter(before['release_rollouts']['1']['applied']['resources']['custom_networks'].values()))['network_id']
        new_id = next(iter(pending['release_rollouts']['1']['desired']['resources']['custom_networks'].values()))['network_id']
        self.assertNotEqual(old_id, new_id) # Avoid create/delete of the same VNet under different resource keys.
        mark_applied()
        self.deploy(release,'rollback','2.0.0');mark_applied()
        after=crange.json_config(self.project)
        self.assertEqual(after['scenario_boxes']['staging'],before['scenario_boxes']['staging'])
        self.assertEqual(rollouts.owned(after,'2'),rollouts.owned(before,'2'))
        self.assertEqual(rollouts.owned(after,'1'),rollouts.owned(before,'1'))
        r=after['release_rollouts']['1']
        self.assertEqual(r['applied']['version'],'1.0.0')
        self.assertEqual([e['action'] for e in r['history'] if e['event']=='declared'],['deploy','upgrade','rollback'])
        before_bytes=(self.project/'crange.auto.tfvars.json').read_bytes()
        with self.assertRaisesRegex(crange.CrangeError,'expected release'):
            self.deploy(newer,'upgrade','0.9.0')
        self.assertEqual((self.project/'crange.auto.tfvars.json').read_bytes(),before_bytes)

    def test_deployed_health_records_vm_ct_failures_then_success(self):
        release=self.release()
        results=crange.ROOT/'releases/project-003/test-results';results.mkdir(parents=True)
        (results/'1.0.0-pass.json').write_text(json.dumps({'release_id':release['release_id'],'result':'passed'}))
        self.deploy(release)
        cfg=crange.json_config(self.project)
        record=cfg['release_rollouts']['1'];names={};outputs={}
        for offset,field in ((200,'scenario_boxes'),(300,'scenario_containers')):
            values={}
            for i,(key,box) in enumerate(record['desired']['resources'][field].items()):
                names[offset+i]=f"p003c{box['network_id']:03d}-{box['name']}"
                values[key]={'id':offset+i,'address':f"10.3.{box['network_id']}.{box['ip_last_octet']}"}
            outputs[field]={'value':values}
        outputs['routers']={'value':{'custom_networks':{key:{'owner_team_id':'1',
            'lan':f"10.3.{net['network_id']}.0/24",'upstream_address':f"10.3.1.{net['uplink_last_octet']}"}
            for key,net in record['desired']['resources']['custom_networks'].items()}}}
        with patch.object(crange,'output_json',return_value=outputs):crange.record_release_apply(self.project)
        api=Mock();api.vm.side_effect=lambda node,vmid:{'name':names[vmid],'template':0}
        api.container.side_effect=lambda node,vmid:{'hostname':names[vmid],'template':0}
        api.request.return_value=[{'vmid':vmid,'type':'qemu' if vmid<300 else 'lxc','node':'proxmox4'} for vmid in names]
        api.status.return_value=api.container_status.return_value='running'
        args=SimpleNamespace(project='3',teams=[1])
        with patch.object(crange,'output_json',return_value=outputs),patch.object(crange,'ProxmoxAPI',return_value=api), \
             patch.object(crange,'check_endpoint',side_effect=[None,OSError('service timeout')]):
            with self.assertRaisesRegex(crange.CrangeError,'results recorded'):
                crange.cmd_release_check(args)
        r=crange.json_config(self.project)['release_rollouts']['1']
        self.assertEqual(r['health']['status'],'failed');self.assertEqual(len(r['health']['failures']),1)
        with patch.object(crange,'output_json',return_value=outputs),patch.object(crange,'ProxmoxAPI',return_value=api), \
             patch.object(crange,'check_endpoint'):
            crange.cmd_release_check(args)
        r=crange.json_config(self.project)['release_rollouts']['1']
        self.assertEqual(r['health']['status'],'passed')
        api.start.assert_not_called();api.container_start.assert_not_called()
        self.assertTrue(all(call.args[0]=='GET' for call in api.request.call_args_list))

class ReleasePlanScopeTests(unittest.TestCase):
    def setUp(self):
        self.cfg, _ = RolloutTests().fixture()
        self.cfg['team_ids']=['1','2']
        RolloutTests().apply(self.cfg)
        record = copy.deepcopy(self.cfg['release_rollouts']['1'])
        self.cfg['release_rollouts']['2'] = record
        for snapshot in ('desired','applied'):
            for field, values in record[snapshot]['resources'].items():
                for value in values.values():
                    value['owner_team_id' if field=='custom_networks' else 'team_id']='2'
        self.record=self.cfg['release_rollouts']['1']
    def check(self,resources):
        return rollouts.plan_blockers({'resource_changes':resources},self.cfg,['1'])
    def test_only_recorded_selected_resources_are_allowed(self):
        from test_staging import resource,guest,router
        resources=[resource('module.scenario_box["old"].guest',guest('p003c100'),guest('p003c100')),
            resource('module.custom_router["release-net"].router',router('p003t001','p003c100'),router('p003t001','p003c100')),
            resource('proxmox_sdn_vnet.custom["release-net"]',{'id':'p003c100'},{'id':'p003c100'})]
        self.assertEqual(self.check(resources),[])
        for bridge in ('p003t254','p003t002','unknown'):
            bad=resource('module.scenario_box["old"].guest',guest(bridge),guest('p003c100'))
            self.assertEqual(self.check([bad]),[bad['address']])
        unowned=resource('module.scenario_box["unowned"].guest',guest('p003t001'),guest('p003t001'))
        self.assertEqual(self.check([unowned]),[unowned['address']])
    def test_staging_and_shared_router_changes_are_rejected(self):
        from test_staging import resource
        resources=[resource('module.staging_router.router'),resource('module.team_router["2"].router'),
            resource('module.project_router.router'),resource('proxmox_sdn_vnet.transit')]
        self.assertEqual(self.check(resources),sorted(r['address'] for r in resources))
    def test_saved_release_plan_uses_actual_json_before_apply(self):
        import tempfile,time,hashlib
        from test_staging import resource
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);plans=root/'.local/share/crange/plans/project-003';plans.mkdir(parents=True)
            plan_id='a'*32;plan=plans/(plan_id+'.tfplan');plan.write_bytes(b'binary plan')
            cfgfile=root/'crange.auto.tfvars.json';cfgfile.write_text(json.dumps(self.cfg))
            manifest={'project':3,'plan_id':plan_id,'created':time.time(),
                'sha256':hashlib.sha256(plan.read_bytes()).hexdigest(),
                'inputs_sha256':hashlib.sha256(cfgfile.read_bytes()).hexdigest(),'automation_inputs_sha256':crange.automation_inputs_digest(root),'changes':[]}
            (plans/(plan_id+'.json')).write_text(json.dumps(manifest))
            args=SimpleNamespace(project='3',plan_id=plan_id,release_teams=['1'],allow_destroy=True,allow_replace=True)
            detail={'resource_changes':[resource('module.staging_router.router')]}
            with patch.object(Path,'home',return_value=root),patch.object(crange,'tf_run',return_value=SimpleNamespace(stdout=json.dumps(detail))) as tf:
                with self.assertRaisesRegex(crange.CrangeError,'unselected teams'):
                    crange.cmd_apply_saved(args,root)
            self.assertEqual(tf.call_count,1);self.assertTrue(plan.exists())
    def test_release_apply_configures_only_selected_teams_and_checks_health(self):
        args=SimpleNamespace(project='3',teams=[1],plan_id='a'*32)
        with patch.object(crange,'project_dir',return_value=Path('/tmp/project-003')),patch.object(crange,'json_config',return_value=self.cfg), \
             patch.object(crange,'load_release',return_value={'project_id':3,'scenario':{}}), \
             patch.object(crange,'cmd_apply') as apply,patch.object(crange,'cmd_configure_team') as configure,patch.object(crange,'cmd_release_check') as check:
            crange.cmd_release_apply(args)
        self.assertEqual(apply.call_args.args[0].release_teams,['1'])
        self.assertEqual(configure.call_count,1);self.assertEqual(configure.call_args.args[0].team,1)
        self.assertEqual(check.call_args.args[0].teams,[1])

    def test_release_services_run_for_selected_team_before_health(self):
        import crange_mhl_services as services
        args = SimpleNamespace(project='3', teams=[1], plan_id='a'*32)
        events = []
        with patch.object(crange, 'project_dir', return_value=Path('/tmp/project-003')), \
             patch.object(crange, 'json_config', return_value=self.cfg), \
             patch.object(crange, 'load_release', return_value={'project_id':3,'scenario':{'service_profiles':{'business':{'source_sha256':'a'*64}}}}), \
             patch.object(services, 'verify_release_profiles', return_value=['business']), \
             patch.object(crange, 'cmd_apply', side_effect=lambda a:events.append('apply')), \
             patch.object(crange, 'cmd_configure_team', side_effect=lambda a:events.append(('configure',a.team))), \
             patch.object(crange, 'cmd_mhl_business_configure', side_effect=lambda a:events.append(('business',a.network_id,a.recipe_sha256))), \
             patch.object(crange, 'cmd_release_check', side_effect=lambda a:events.append('health')):
            crange.cmd_release_apply(args)
        self.assertEqual(events, ['apply', ('configure',1), ('business',1,'a'*64), 'health'])

    def test_invalid_service_recipe_blocks_before_infrastructure_apply(self):
        import crange_mhl_services as services
        args = SimpleNamespace(project='3', teams=[1], plan_id='a'*32)
        with patch.object(crange, 'project_dir', return_value=Path('/tmp/project-003')), \
             patch.object(crange, 'json_config', return_value=self.cfg), \
             patch.object(crange, 'load_release', return_value={'project_id':3}), \
             patch.object(services, 'verify_release_profiles', side_effect=ValueError('recipe changed')), \
             patch.object(crange, 'cmd_apply') as apply:
            with self.assertRaisesRegex(crange.CrangeError, 'recipe changed'):
                crange.cmd_release_apply(args)
        apply.assert_not_called()
