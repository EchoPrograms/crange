import copy
import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from crange_configure_scope import online_configuration_keys


class OnlineConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.cfg={'project_id':100,'scenario_containers':{'ready':{'team_id':'254'},'offline':{'team_id':'254'}}}
        self.applied={}
        self.resources=[]
        for i,(key,node) in enumerate((('router:project','proxmox2'),('router:staging','proxmox5'),('container:ready','proxmox2'),('container:offline','proxmox1'))):
            vmid=200+i;name='p100-'+key.replace(':','-')
            self.applied[key]={'vm_id':vmid,'id':str(vmid),'node_name':node,'initialization':[{'hostname':name}]}
            self.resources.append({'vmid':vmid,'name':name,'node':node,'type':'lxc','pool':'crange-iac','tags':'p100;terraform','status':'running'})
        self.nodes=[{'node':f'proxmox{i}','status':'offline' if i==1 else 'online'} for i in range(1,7)]

    def test_offline_guest_does_not_block_online_configuration(self):
        selected,skipped=online_configuration_keys(self.cfg,254,self.applied,self.nodes,self.resources)
        self.assertEqual(selected,['container:ready','router:project','router:staging'])
        self.assertEqual(skipped,[('container:offline','node offline')])

    def test_returning_guest_is_selected_again_without_changing_placement(self):
        original=copy.deepcopy(self.applied);self.nodes[0]['status']='online'
        selected,skipped=online_configuration_keys(self.cfg,254,self.applied,self.nodes,self.resources)
        self.assertIn('container:offline',selected);self.assertEqual(skipped,[]);self.assertEqual(self.applied,original)

    def test_missing_offline_name_does_not_block_online_guests(self):
        self.resources[-1].pop('name')
        selected,skipped=online_configuration_keys(self.cfg,254,self.applied,self.nodes,self.resources)
        self.assertIn('container:ready',selected)
        self.assertEqual(skipped,[('container:offline','node offline')])
        self.nodes[0]['status']='online'
        with self.assertRaisesRegex(ValueError,'ownership'):
            online_configuration_keys(self.cfg,254,self.applied,self.nodes,self.resources)

    def test_offline_router_cannot_be_bypassed(self):
        self.nodes[1]['status']='offline'
        with self.assertRaisesRegex(ValueError,'existing router or native HA'):
            online_configuration_keys(self.cfg,254,self.applied,self.nodes,self.resources)

    def test_foreign_or_relocated_guest_cannot_be_configured(self):
        for key,value in [('pool','foreign'),('node','proxmox3'),('name','another-guest'),('tags','p101')]:
            rows=copy.deepcopy(self.resources);rows[2][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):
                online_configuration_keys(self.cfg,254,self.applied,self.nodes,rows)

    def test_online_missing_guest_is_an_error_and_stopped_guest_is_skipped(self):
        with self.assertRaisesRegex(ValueError,'missing'):
            online_configuration_keys(self.cfg,254,self.applied,self.nodes,self.resources[:2]+self.resources[3:])
        self.resources[2]['status']='stopped'
        self.assertIn(('container:ready','not running'),online_configuration_keys(self.cfg,254,self.applied,self.nodes,self.resources)[1])
