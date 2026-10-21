import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_guest_outputs import complete_metadata


class AppliedOutputMetadataTests(unittest.TestCase):
    def router_fixture(self):
        raw={'project_id':{'value':100},'routers':{'value':{'custom_networks':{}}}}
        cfg={'project_id':100,'team_ids':['1'],'custom_networks':{'test':{
            'owner_team_id':'254','network_id':100,'uplink_last_octet':2}}}
        saved={'router:custom:test':{'guest_id':138,'node_name':'proxmox6',
            'datastore_id':'crange-router-ha','image_id':'debian-router','clone_mode':'archive'}}
        attrs={'id':'138','vm_id':138,'node_name':'proxmox6','pool_id':'crange-iac','unprivileged':True,
            'tags':['p100','p100c100','terraform','network-infrastructure','range-lan'],
            'disk':[{'datastore_id':'crange-router-ha','size':4}],
            'network_interface':[{'bridge':'p100t254'},{'bridge':'p100c100'}],
            'initialization':[{'hostname':'p100-custom-test-router','ip_config':[
                {'ipv4':[{'address':'10.100.254.2/24','gateway':'10.100.254.1'}]},
                {'ipv4':[{'address':'10.100.100.1/24'}]}]}]}
        state={'version':4,'lineage':'real-project','resources':[{
            'module':'module.custom_router["test"]','mode':'managed',
            'type':'proxmox_virtual_environment_container','name':'router',
            'instances':[{'attributes':attrs}]}]}
        return raw,cfg,saved,state,attrs

    def test_targeted_foundation_fills_only_its_allocated_router_generation(self):
        raw,cfg,saved,state,attrs=self.router_fixture();original=copy.deepcopy(raw)
        row=complete_metadata(raw,cfg,saved,state=state)['routers']['value']['custom_networks']['test']
        self.assertEqual(row['id'],138)
        self.assertEqual(row['address'],'10.100.254.2')
        self.assertEqual(row['lan'],'10.100.100.0/24')
        self.assertEqual(row['owner_team_id'],'254')
        self.assertEqual(row['placement']['datastore_id'],'crange-router-ha')
        self.assertEqual(raw,original)

    def test_declared_unallocated_foreign_or_reused_router_never_becomes_inventory(self):
        for change in ('undeployed','no-receipt','reused','node','bridge','gateway','pool','owner','privileged','tainted'):
            raw,cfg,saved,state,attrs=self.router_fixture()
            if change=='undeployed':attrs['vm_id']=None
            if change=='no-receipt':saved.clear()
            if change=='reused':saved['router:custom:test']['guest_id']=139
            if change=='node':attrs['node_name']='proxmox3'
            if change=='bridge':attrs['network_interface'][1]['bridge']='p101c100'
            if change=='gateway':attrs['initialization'][0]['ip_config'][0]['ipv4'][0]['gateway']='10.100.1.1'
            if change=='pool':attrs['pool_id']='unrelated'
            if change=='owner':cfg['custom_networks']['test']['owner_team_id']='1'
            if change=='privileged':attrs['unprivileged']=False
            if change=='tainted':state['resources'][0]['instances'][0]['status']='tainted'
            with self.subTest(change=change):
                self.assertEqual(complete_metadata(raw,cfg,saved,state=state)['routers']['value']['custom_networks'],{})

    def test_explicit_router_identity_conflict_is_not_silently_repaired(self):
        raw,cfg,saved,state,attrs=self.router_fixture()
        raw['routers']['value']['custom_networks']['test']={'id':139}
        with self.assertRaisesRegex(ValueError,'saved applied identity'):
            complete_metadata(raw,cfg,saved,state=state)

    def fixture(self):
        raw={'scenario_containers':{'value':{'db':{'id':127,'address':'10.100.254.30',
                                                  'placement':{'image_id':'','node_name':None}}}}}
        cfg={'scenario_containers':{'db':{'template':'debian-stock'}}}
        saved={'container:db':{'guest_id':127,'declared_template':'debian-stock',
            'image_id':'debian-image','node_name':'proxmox1','template_file_id':'library:vztmpl/debian.tar.zst'}}
        return raw,cfg,saved

    def test_unchanged_archive_guest_recovers_missing_identity_without_changing_input(self):
        fixture=self.fixture();before=copy.deepcopy(fixture)
        live=complete_metadata(*fixture)['scenario_containers']['value']['db']
        self.assertEqual(live['placement']['image_id'],'debian-image')
        self.assertEqual(live['placement']['node_name'],'proxmox1')
        self.assertIn('template_file_id',live['placement'])
        self.assertEqual(fixture,before)

    def test_explicit_conflicts_stay_visible(self):
        fixture=self.fixture();fixture[0]['scenario_containers']['value']['db']['placement']={'image_id':'foreign','node_name':'proxmox3'}
        live=complete_metadata(*fixture)['scenario_containers']['value']['db']
        self.assertEqual(live['placement']['image_id'],'foreign')
        self.assertEqual(live['placement']['node_name'],'proxmox3')

    def test_null_reallocated_or_changed_template_never_adopts_old_generation(self):
        for change in (lambda f:f[0]['scenario_containers']['value']['db'].update(id=None),
                       lambda f:f[0]['scenario_containers']['value']['db'].update(id=128),
                       lambda f:f[1]['scenario_containers']['db'].update(template='other'),
                       lambda f:f[1]['scenario_containers']['db'].update(image='other'),
                       lambda f:f[2]['container:db'].update(guest_id=True)):
            fixture=self.fixture();change(fixture)
            self.assertEqual(complete_metadata(*fixture),fixture[0])

    def test_vm_metadata_is_bound_the_same_way_and_non_guest_outputs_untouched(self):
        raw,cfg,saved=self.fixture()
        raw={'scenario_boxes':raw['scenario_containers'],'project_id':{'value':100}}
        cfg={'scenario_boxes':cfg['scenario_containers']};saved={'vm:db':saved['container:db']}
        completed=complete_metadata(raw,cfg,saved)
        self.assertEqual(completed['scenario_boxes']['value']['db']['placement']['image_id'],'debian-image')
        self.assertEqual(completed['project_id'],{'value':100})


if __name__=='__main__':unittest.main()
