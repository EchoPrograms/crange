import copy
import json
from pathlib import Path
import sys
import unittest
import tempfile
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from crange_live_status import observe, box_states, overlay_nodes, project_box_states
from crange_guac_broker import project_power_status
from crange_guac_ticket import TicketError

class LiveStatusTests(unittest.TestCase):
 def setUp(self):
  self.resource={'vmid':145,'node':'proxmox4','type':'qemu','name':'p100t254-web','pool':'crange-iac','tags':'p100;crange-scenario','status':'running'}
  self.row={'project':100,'kind':'vm','resource_key':'vm:web','guest_id':145,'guest_name':'p100t254-web'}
  self.api=Mock();self.api.request.side_effect=lambda method,path,*args: {'/nodes':[{'node':'proxmox1','status':'online'},{'node':'proxmox4','status':'online'}],'/cluster/status':[{'name':'proxmox1','type':'node','online':0},{'name':'proxmox4','type':'node','online':1}],'/cluster/resources':[self.resource]}[path]

 def test_cluster_membership_overrules_stale_online_flag(self):
  value=observe(self.api);self.assertFalse(next(n for n in value['nodes'] if n['node']=='proxmox1')['online'])
  self.assertEqual(len(self.api.request.call_args_list),3)
  self.assertEqual(box_states([self.row]*36,observation=value)[-1]['state'],'running')
  self.assertEqual(len(self.api.request.call_args_list),3,'batch reads never fetch per-guest configs or run Terraform')

 def test_guest_on_offline_host_never_appears_running(self):
  self.resource['node']='proxmox1';result=box_states([self.row],observation=observe(self.api))[0]
  self.assertEqual(result['state'],'offline');self.assertIn('offline',result['power_error'])

 def test_renamed_reused_or_foreign_guest_identity_is_unknown(self):
  for key,value in [('vmid',146),('name','unrelated-server'),('pool','other-services'),('tags','p101'),('template',1)]:
   original=copy.deepcopy(self.resource);self.resource[key]=value
   with self.subTest(key=key):self.assertEqual(box_states([self.row],observation=observe(self.api))[0]['state'],'unknown')
   self.resource=original

 def test_failed_observation_does_not_reuse_online_or_running(self):
  self.api.request.side_effect=TimeoutError('offline API')
  value=observe(self.api);self.assertTrue(all(n['online'] is None for n in value['nodes']))
  self.assertEqual(box_states([self.row],observation=value)[0]['state'],'unknown')

 def test_live_node_overlay_preserves_capacity_staleness(self):
  cached={'stale':True,'captured_at':1,'nodes':[{'node':'proxmox1','online':True,'capacity':{'cpu':48},'eligibility':{'eligible':True,'blockers':[]}}]}
  value=overlay_nodes(cached,observe(self.api));self.assertFalse(value['nodes'][0]['online']);self.assertFalse(value['nodes'][0]['eligibility']['eligible'])
  self.assertTrue(value['stale']);self.assertEqual(value['captured_at'],1);self.assertTrue(cached['nodes'][0]['online'])

 def project_fixture(self, root):
  self.resource.update(name='p100t001-web')
  cfg={'project_id':100,'scenario_boxes':{'web':{'team_id':1,'name':'web','started':True},
       'pending':{'team_id':1,'name':'pending','started':True}}}
  snapshot={'version':4,'resources':[{'module':'module.scenario_box["web"]',
       'mode':'managed','type':'proxmox_virtual_environment_vm','instances':[
        {'attributes':{'id':'145','vm_id':145,'node_name':'proxmox4'}}]}]}
  (root/'terraform.tfstate').write_text(json.dumps(snapshot))
  return cfg,snapshot

 def test_project_status_uses_actual_power_and_labels_missing_declarations_undeployed(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);cfg,_=self.project_fixture(root)
   self.resource['status']='stopped'
   value=project_box_states(root,cfg,observation=observe(self.api))
   self.assertEqual(value['boxes']['vm:web']['state'],'stopped','saved started=True is not observed power')
   self.assertEqual(value['boxes']['vm:web']['node'],'proxmox4')
   self.assertEqual(value['boxes']['vm:pending']['state'],'undeployed')
   self.assertIsNone(value['boxes']['vm:pending']['node'])
   self.assertEqual(self.api.request.call_count,3,'no per-guest calls or Terraform subprocesses')

 def test_project_without_state_is_undeployed_but_unreadable_state_is_unknown(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);cfg,_=self.project_fixture(root)
   (root/'terraform.tfstate').unlink()
   with patch('crange_live_status.observe') as live:
    self.assertTrue(all(row['state']=='undeployed' for row in project_box_states(root,cfg)['boxes'].values()))
    live.assert_not_called()
    (root/'terraform.tfstate').write_text('{broken')
    result=project_box_states(root,cfg)
    self.assertTrue(all(row['state']=='unknown' for row in result['boxes'].values()))
    self.assertIn('unavailable',result['error'])
    live.assert_not_called()

 def test_offline_or_failed_live_reads_never_use_desired_power_as_status(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);cfg,_=self.project_fixture(root)
   self.resource['node']='proxmox1'
   self.assertEqual(project_box_states(root,cfg,observation=observe(self.api))['boxes']['vm:web']['state'],'offline')
   self.api.request.side_effect=TimeoutError('unreachable')
   value=project_box_states(root,cfg,observation=observe(self.api))
   self.assertEqual(value['boxes']['vm:web']['state'],'unknown')
   self.assertEqual(value['boxes']['vm:pending']['state'],'undeployed')

 def test_container_custom_network_jumpbox_and_partial_apply_without_outputs(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);cfg,snapshot=self.project_fixture(root)
   cfg.update(custom_networks={'dmz':{'network_id':101}},scenario_containers={
      'shell':{'name':'shell','team_id':1,'network_key':'dmz','is_jumpbox':True}})
   cfg['jump_boxes']={'jump':{'team_id':1,'jump_number':2}}
   for module,kind,vmid in [('scenario_container','container',146),('jumpbox','vm',147)]:
    name='shell' if kind=='container' else 'jump'
    snapshot['resources'].append({'module':f'module.{module}["{name}"]','mode':'managed',
       'type':'proxmox_virtual_environment_container' if kind=='container' else 'proxmox_virtual_environment_vm',
       'instances':[{'attributes':{'id':str(vmid),'node_name':'proxmox4'}}]})
   (root/'terraform.tfstate').write_text(json.dumps(snapshot))
   observed=observe(self.api)
   for name,vmid,kind in [('p100c101-shell',146,'lxc'),('p100t001-jump-02',147,'qemu')]:
    observed['resources'].append({**self.resource,'name':name,'vmid':vmid,'type':kind})
   boxes=project_box_states(root,cfg,observation=observed)['boxes']
   self.assertEqual(boxes['container:shell']['state'],'running')
   self.assertEqual(boxes['jumpbox:jump']['state'],'running')

 def test_project_broker_denies_other_projects_before_reading_state(self):
  auth=Mock();auth.session.return_value={'admin':False,'managed_projects':['3']}
  with patch('crange.project_dir') as directory:
   with self.assertRaisesRegex(TicketError,'manager access'):
    project_power_status({'project':100,'session':'test-session'},auth=auth)
   directory.assert_not_called()
  auth.session.return_value=None
  with self.assertRaises(TicketError):project_power_status({'project':100,'session':'expired'},auth=auth)

 def test_project_broker_authorizes_each_read(self):
  auth=Mock();auth.session.return_value={'admin':False,'managed_projects':['100']}
  with patch('crange.project_dir',return_value=Path('/project-100')) as directory, \
       patch('crange.json_config',return_value={'project_id':100}), \
       patch('crange_project_lifecycle.require_active') as active, \
       patch('crange_live_status.project_box_states',return_value={'boxes':{}}) as states:
   self.assertEqual(project_power_status({'project':100,'session':'current'},auth=auth),{'boxes':{}})
   directory.assert_called_once_with('100');active.assert_called_once();states.assert_called_once()

if __name__=='__main__':unittest.main()
