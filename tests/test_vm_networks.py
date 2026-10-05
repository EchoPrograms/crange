from types import SimpleNamespace
import unittest
import test_crange as fixtures
crange = fixtures.crange

class VMNetworkTests(unittest.TestCase):
    setUp = fixtures.CrangeConfigTests.setUp
    tearDown = fixtures.CrangeConfigTests.tearDown
    def add_vm(self, name='svc', team=0, ip=22, network='lab'):
        crange.cmd_scenario_add(SimpleNamespace(project='3', name=name, team=team, ip=ip,
            network=network, template='approved-base', node='proxmox4', cores=2,
            memory=1024, full_clone=None, health='[]'))

    def setup_network(self):
        crange.mutate_custom_network(SimpleNamespace(project='3',name='lab',team=0,
            network_id=100,node='proxmox4'),True)

    def test_vm_on_custom_network_keeps_owner_and_collision_domain(self):
        self.setup_network()
        self.add_vm()
        cfg=crange.json_config(self.project)
        box=cfg['scenario_boxes']['t254-svc']
        self.assertEqual((box['team_id'],box['network_id'],box['network_key']),('254',100,'lab'))
        self.add_vm(name='standard',network='') # Same octet, different subnet.
        with self.assertRaisesRegex(crange.CrangeError,'already assigned'):
            self.add_vm(name='duplicate')
        with self.assertRaisesRegex(crange.CrangeError,'guest owner'):
            self.add_vm(name='foreign',team=1)
        with self.assertRaisesRegex(crange.CrangeError,'still has scenario VMs'):
            crange.mutate_custom_network(SimpleNamespace(project='3',name='lab'),False)

    def test_vm_edit_and_network_renumber_update_attachment_without_other_team_edits(self):
        self.setup_network()
        self.add_vm(network='')
        crange.cmd_scenario_edit(SimpleNamespace(project='3',name='t254-svc',template='approved-base',
            node='proxmox4',cores=2,memory=1024,health='[]',ip=30,network='lab'))
        crange.cmd_network_edit(SimpleNamespace(project='3',name='lab',id=101,node='proxmox4',
            uplink=100,cores=1,memory=512))
        cfg=crange.json_config(self.project)
        self.assertEqual(cfg['scenario_boxes']['t254-svc']['network_id'],101)
        self.assertEqual(cfg['scenario_boxes']['t254-svc']['ip_last_octet'],30)
        self.assertEqual(cfg['jump_boxes']['team1_jump01']['team_id'],'1')

    def test_vm_and_ct_cannot_use_same_custom_address(self):
        self.setup_network()
        self.add_vm()
        args=SimpleNamespace(project='3',name='ct',team=0,network='lab',ip=22,
            template='approved-ct-base',node='proxmox4',cores=1,memory=1024,health='[]')
        with self.assertRaisesRegex(crange.CrangeError,'scenario VM'):
            crange.cmd_container_add(args)

    def test_custom_vm_jump_collision_is_rejected_in_either_creation_order(self):
        self.setup_network()
        self.add_vm(ip=11)
        args=SimpleNamespace(project='3',name='lab-j01',team=0,network='lab',node='proxmox4',
                             cores=2,memory=1024,full_clone=None)
        with self.assertRaisesRegex(crange.CrangeError,'scenario VM'):
            crange.mutate_box(args,True)
        crange.cmd_scenario_remove(SimpleNamespace(project='3',name='t254-svc'))
        crange.mutate_box(args,True)
        with self.assertRaisesRegex(crange.CrangeError,'already assigned'):
            self.add_vm(ip=11)

    def test_vm_edit_duplicate_address_does_not_persist(self):
        self.setup_network();self.add_vm();self.add_vm(name='other',ip=23)
        before=(self.project/'crange.auto.tfvars.json').read_bytes()
        with self.assertRaisesRegex(crange.CrangeError,'Duplicate static address'):
            crange.cmd_scenario_edit(SimpleNamespace(project='3',name='t254-other',template='approved-base',
                node='proxmox4',cores=2,memory=1024,health='[]',ip=22,network='lab'))
        self.assertEqual((self.project/'crange.auto.tfvars.json').read_bytes(),before)
