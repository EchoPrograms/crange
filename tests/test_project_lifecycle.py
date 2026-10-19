import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange
import crange_project_lifecycle as lifecycle
from crange_jobs import JobStore, command

class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.envs = self.root / 'terraform/environments'
        self.path = self.envs / 'project-100'; self.path.mkdir(parents=True)
        self.cfg = {'project_id':100, 'team_ids':['1'], 'scenario_boxes':{'web':{'name':'web'}},
                    'release_rollouts':{'1':{'version':'1.0.0', 'applied':{}, 'applied_endpoints':{}}}}
        (self.path / 'crange.auto.tfvars.json').write_text(json.dumps(self.cfg))
        for name, value in [('ENVS', self.envs), ('ROOT', self.root)]:
            patcher=patch.object(crange,name,value);patcher.start();self.addCleanup(patcher.stop)
        home=self.root/'home';home.mkdir()
        home_patch=patch.object(Path,'home',return_value=home);home_patch.start();self.addCleanup(home_patch.stop)
        self.api=Mock();self.resources=[]
        self.api.request.side_effect=self.request

    def request(self, method, endpoint, params=None):
        if endpoint=='/cluster/resources':return self.resources
        if endpoint=='/nodes':return [{'node':'proxmox4','status':'online'}]
        if endpoint.endswith('/config'):return {'name':'p100t254-web','tags':'p100;crange-scenario','template':0}
        raise AssertionError(endpoint)

    def plan(self):
        self.resources=[{'vmid':145,'node':'proxmox4','type':'qemu','pool':'crange-iac','tags':'p100'}]
        return {'resource_changes':[{'type':'proxmox_virtual_environment_vm','address':'module.scenario_box["web"].proxmox_virtual_environment_vm.scenario',
                  'change':{'actions':['delete'],'before':{'vm_id':145,'node_name':'proxmox4','tags':['p100'],'name':'p100t254-web'}}}]}

    def test_exact_owned_guest_accepted_and_foreign_template_move_blocked(self):
        plan=self.plan();self.assertEqual(lifecycle.validate_destroy_plan(plan,100,self.api)[0]['id'],145)
        for field,value in [('name','different'),('template',True),('node_name','proxmox1'),('tags',['p101'])]:
            altered=copy.deepcopy(plan);altered['resource_changes'][0]['change']['before'][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):lifecycle.validate_destroy_plan(altered,100,self.api)
        self.resources[0]['pool']='homelab'
        with self.assertRaisesRegex(ValueError,'pool'):lifecycle.validate_destroy_plan(plan,100,self.api)

    def test_unowned_project_guest_blocks_while_images_are_retained(self):
        self.resources=[{'vmid':999,'tags':'p100;crange-image','template':1}]
        self.assertEqual(lifecycle.validate_destroy_plan({},100,self.api),[])
        self.resources.append({'vmid':888,'tags':'p100'})
        with self.assertRaisesRegex(ValueError,'outside the teardown'):lifecycle.validate_destroy_plan({},100,self.api)

    def test_foreign_shared_network_and_bookkeeping_blocked(self):
        for kind,before,address in [('proxmox_sdn_vnet',{'id':'crtrans','zone':'crange'},'proxmox_sdn_vnet.transit'),
                                     ('terraform_data',{},'terraform_data.unrecognized')]:
            with self.assertRaises(ValueError):lifecycle.validate_destroy_plan({'resource_changes':[{'type':kind,'address':address,'change':{'actions':['delete'],'before':before}}]},100,self.api)
        plan={'resource_changes':[{'type':'terraform_data','address':'terraform_data.sdn_revision','change':{'actions':['delete'],'before':{}}}]}
        self.assertEqual(lifecycle.validate_destroy_plan(plan,100,self.api),[])

    def test_delete_retains_definitions_and_releases_and_archives_state(self):
        release=self.root/'releases/project-100/1.0.0.json';release.parent.mkdir(parents=True);release.write_text('{"release_id":"retained"}')
        (self.path/'terraform.tfstate').write_text('{}');(self.path/'crange-applied-placements.json').write_text('{}')
        execute=Mock(return_value=SimpleNamespace(stdout='{}'))
        with patch('crange_project_lifecycle._retire_observations', wraps=lifecycle._retire_observations),patch('crange_scheduling.DATA',self.root/'distribution'):
            lifecycle.delete_project(100,api=self.api,terraform=execute,revoke=Mock(),route_remove=Mock())
        self.assertEqual(json.loads((self.path/'crange.auto.tfvars.json').read_text()),self.cfg)
        self.assertTrue(release.exists());self.assertFalse((self.path/'terraform.tfstate').exists())
        self.assertEqual(lifecycle.state(self.path)['state'],'deleted')
        with self.assertRaises(ValueError):crange.project_dir('100')
        lifecycle.restore_project(100)
        self.assertTrue(lifecycle.is_active(self.path));self.assertTrue(lifecycle.state(self.path)['needs_redeploy'])
        cfg=crange.json_config(self.path);self.assertEqual(cfg['scenario_boxes'],self.cfg['scenario_boxes']);self.assertNotIn('applied',cfg['release_rollouts']['1'])

    def test_failed_preflight_never_applies_or_revokes(self):
        plan=self.plan();plan['resource_changes'][0]['change']['before']['tags']=['p101']
        (self.path/'terraform.tfstate').write_text('{}');execute=Mock(return_value=SimpleNamespace(stdout=json.dumps(plan)));revoke=Mock()
        with self.assertRaises(ValueError):lifecycle.delete_project(100,api=self.api,terraform=execute,revoke=revoke,route_remove=Mock())
        self.assertEqual(lifecycle.state(self.path)['state'],'delete_failed');revoke.assert_not_called()
        self.assertFalse(any(call.args[1][0]=='apply' for call in execute.call_args_list))

    def test_offline_guest_defers_before_terraform_or_revocation(self):
        (self.path/'terraform.tfstate').write_text(json.dumps({'resources':[{'type':'proxmox_virtual_environment_vm','instances':[{'attributes':{'node_name':'proxmox1'}}]}]}))
        execute,revoke=Mock(),Mock()
        with self.assertRaises(lifecycle.WaitingForNodes):lifecycle.delete_project(100,api=self.api,terraform=execute,revoke=revoke,route_remove=Mock())
        execute.assert_not_called();revoke.assert_not_called()
        self.assertEqual(lifecycle.state(self.path)['waiting_nodes'],['proxmox1'])
        self.assertEqual(lifecycle.state(self.path)['state'],'deleting')

    def test_queue_is_fenced_in_both_directions(self):
        store=JobStore(self.root/'jobs.sqlite3');store.submit('manager',100,'plan',{})
        with self.assertRaisesRegex(ValueError,'Wait'):store.submit('manager',100,'project-delete',{'confirmation':'p100'})
        with store.connect() as db:db.execute("UPDATE jobs SET status='succeeded'")
        job=store.submit('manager',100,'project-delete',{'confirmation':'p100'})
        with self.assertRaisesRegex(ValueError,'queued'):store.submit('manager',100,'plan',{})
        with self.assertRaises(ValueError):lifecycle.require_editable(store,100)
        self.assertTrue(store.get(job,['100']))

    def test_typed_confirmation_and_purge_archived_history(self):
        for value in ('100','p101','p100; reboot',None):
            with self.assertRaises(ValueError):command('project-delete',100,{'confirmation':value})
        self.assertEqual(command('project-restore',100,{'confirmation':'p100'}),['project-restore','100','--confirm','p100'])
        store=JobStore(self.root/'jobs.sqlite3');job=store.submit('manager',100,'plan',{})
        with store.connect() as db:db.execute("UPDATE jobs SET archived=1,status='succeeded'")
        self.assertFalse(store.list(['100']));self.assertIsNone(store.get(job,['100']));self.assertTrue(store.get(job,[],True))
        self.assertEqual(store.list_page(['100'])['total'],0)

    def test_purge_never_treats_generic_approval_as_image_ownership(self):
        base={'approved_projects':[100], 'replicas':[]}
        release={'project_id':100,'release_id':'owned'}
        private={**base,'release':{'release_id':'owned'},'retained_release_references':['owned']}
        self.assertEqual(set(lifecycle.private_images({'images':{'base':base,'release':private}},100,[release])),{'release'})
        with self.assertRaises(ValueError):lifecycle.private_images({'images':{'shared':{**private,'approved_projects':[100,101]}}},100,[release])

if __name__=='__main__':unittest.main()
