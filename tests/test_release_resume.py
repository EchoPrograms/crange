import copy
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange
from crange_jobs import command
from crange_rollouts import declare, endpoint
from crange_release_resume import validate_applied

class ReleaseResumeTests(unittest.TestCase):
    def fixture(self):
        box={'team_id':'1','release_id':'p003-1.0','release_scope':'team','name':'release-web','network_id':1,'ip_last_octet':20}
        cfg={'project_id':3,'team_ids':['1','2'],'scenario_boxes':{'web':box,'pending':{'team_id':'1'}},'scenario_containers':{},'custom_networks':{},'jump_boxes':{}}
        declare(cfg,'1',{'release_id':'p003-1.0','version':'1.0'},'deploy')
        live={'id':125,'address':'10.3.1.20','placement':{'node_name':'proxmox1','template_id':301,'template_node':'proxmox1','clone_mode':'linked'}}
        record=cfg['release_rollouts']['1'];record['applied']=copy.deepcopy(record['desired']);record['applied_endpoints']={'vm:web':endpoint(live)}
        outputs={'scenario_boxes':{'value':{'web':live,'pending':{'id':None,'address':'10.3.1.21'}}}}
        rows=[{'vmid':125,'type':'qemu','node':'proxmox1','pool':'crange-iac','status':'running'}]
        api=Mock();api.request.return_value=rows;api.vm.return_value={'name':'p003t001-release-web','net0':'e1000,bridge=p003t001','template':0}
        return cfg,outputs,api

    def test_typed_resume_command_has_no_plan_or_shell_inputs(self):
        self.assertEqual(command('release-configure',3,{'teams':[1,2]}),['release-configure','3','--teams','1','2','--yes'])
        for teams in ([],[0],[254],[True],['1;id']):
            with self.assertRaises(ValueError):command('release-configure',3,{'teams':teams})
        with self.assertRaises(ValueError):command('release-configure',3,{'teams':[1],'plan_id':'a'*32})

    def test_preflight_checks_only_recorded_release_guests(self):
        cfg,out,api=self.fixture()
        with patch.object(crange,'output_json',return_value=out):validate_applied(Path('/project'),cfg,['1'],api=api)
        api.vm.assert_called_once_with('proxmox1',125)
        self.assertTrue(all(call.args[0]=='GET' for call in api.request.call_args_list))

    def test_preflight_checks_owned_custom_router_generation_and_topology(self):
        cfg,out,api=self.fixture()
        cfg['custom_networks']['lab']={'owner_team_id':'1','network_id':101,'uplink_last_octet':100,'release_scope':'team','release_id':'p003-1.0'}
        declare(cfg,'1',{'release_id':'p003-1.0','version':'1.0'},'deploy')
        record=cfg['release_rollouts']['1'];record['applied']=copy.deepcopy(record['desired'])
        router={'id':126,'owner_team_id':'1','lan':'10.3.101.0/24','upstream_address':'10.3.1.100','placement':{'node_name':'proxmox1'}}
        out['routers']={'value':{'custom_networks':{'lab':router}}};record['applied_endpoints']['network:lab']=endpoint(router)
        api.request.return_value.append({'vmid':126,'type':'lxc','node':'proxmox1','pool':'crange-iac','status':'running'})
        api.container.return_value={'hostname':'p003-custom-lab-router','net0':'name=eth0,bridge=p003t001','net1':'name=eth1,bridge=p003c101'}
        with patch.object(crange,'output_json',return_value=out):validate_applied(Path('/project'),cfg,['1'],api=api)
        api.container.assert_called_once_with('proxmox1',126)
        router['owner_team_id']='2'
        with patch.object(crange,'output_json',return_value=out),self.assertRaisesRegex(crange.CrangeError,'topology'):
            validate_applied(Path('/project'),cfg,['1'],api=api)

    def test_preflight_refuses_changed_generation_owner_name_network_or_power(self):
        for change in ('id','pending-release','pool','name','network','power','missing-record'):
            with self.subTest(change=change):
                cfg,out,api=self.fixture()
                if change=='id':out['scenario_boxes']['value']['web']['id']=126
                if change=='pending-release':cfg['release_rollouts']['1']['applied']['version']='older'
                if change=='pool':api.request.return_value[0]['pool']='unrelated'
                if change=='name':api.vm.return_value['name']='unrelated'
                if change=='network':api.vm.return_value['net0']='e1000,bridge=p003t002'
                if change=='power':api.request.return_value[0]['status']='stopped'
                if change=='missing-record':cfg['release_rollouts']['1']['applied_endpoints']={}
                with patch.object(crange,'output_json',return_value=out),self.assertRaises(crange.CrangeError):validate_applied(Path('/project'),cfg,['1'],api=api)

    def test_resume_replays_archived_recipes_without_terraform_apply(self):
        cfg,out,api=self.fixture();digest='a'*64
        release={'project_id':3,'scenario':{'service_profiles':{'windows':{'source_sha256':digest}}}}
        with patch.object(crange,'project_dir',return_value=Path('/project')), \
             patch.object(crange,'json_config',return_value=cfg), \
             patch.object(crange,'output_json',return_value=out), \
             patch.object(crange,'ProxmoxAPI',return_value=api), \
             patch.object(crange,'load_release',return_value=release), \
             patch('crange_mhl_services.verify_release_profiles',return_value=['windows']), \
             patch.object(crange,'cmd_apply') as apply, \
             patch.object(crange,'cmd_configure_team') as configure, \
             patch.object(crange,'cmd_mhl_windows_configure') as windows, \
             patch.object(crange,'cmd_release_check') as check:
            crange.cmd_release_configure(SimpleNamespace(project='3',teams=[1],yes=True))
        apply.assert_not_called();self.assertEqual(configure.call_args.args[0].team,1)
        self.assertEqual(windows.call_args.args[0].recipe_sha256,digest)
        self.assertEqual(check.call_args.args[0].teams,[1])

    def test_health_checkpoints_each_team_before_configuring_the_next(self):
        cfg,out,api=self.fixture();cfg['scenario_boxes']['other']={**cfg['scenario_boxes']['web'],'team_id':'2','name':'other'}
        declare(cfg,'2',{'release_id':'p003-1.0','version':'1.0'},'deploy');events=[]
        with patch.object(crange,'project_dir',return_value=Path('/project')), \
             patch.object(crange,'json_config',return_value=cfg), \
             patch.object(crange,'load_release',return_value={'project_id':3,'scenario':{}}), \
             patch.object(crange,'cmd_apply'), \
             patch.object(crange,'cmd_configure_team',side_effect=lambda a:events.append(('configure',a.team))), \
             patch.object(crange,'cmd_release_check',side_effect=lambda a:events.append(('health',a.teams))):
            crange.cmd_release_apply(SimpleNamespace(project='3',teams=[1,2],plan_id='a'*32))
        self.assertEqual(events,[('configure',1),('health',[1]),('configure',2),('health',[2])])

    def test_changed_archive_blocks_configuration_before_any_guest_changes(self):
        cfg,out,api=self.fixture()
        with patch.object(crange,'project_dir',return_value=Path('/project')), \
             patch.object(crange,'json_config',return_value=cfg), \
             patch.object(crange,'load_release',return_value={'project_id':3}), \
             patch('crange_mhl_services.verify_release_profiles',side_effect=ValueError('changed archive')), \
             patch.object(crange,'cmd_configure_team') as configure, \
             self.assertRaisesRegex(crange.CrangeError,'changed archive'):
            crange.cmd_release_configure(SimpleNamespace(project='3',teams=[1],yes=True))
        configure.assert_not_called()

if __name__=='__main__':unittest.main()
