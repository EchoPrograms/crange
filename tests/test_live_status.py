import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from crange_live_status import observe, box_states, overlay_nodes

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

if __name__=='__main__':unittest.main()
