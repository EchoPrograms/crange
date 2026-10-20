"""Access baseline uses only selected applied Linux containers and private secrets."""
import copy
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_linux_access import access_inventory


class LinuxAccessTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {'project_id':100, 'scenario_containers': {
            'app': {'team_id':'2', 'ip_last_octet':40, 'guac_services':['ssh']},
            'other': {'team_id':'3', 'ip_last_octet':40, 'guac_services':['ssh']}}}
        self.inventory = {'all': {'children': {'scenario_containers': {
            'vars': {'ansible_user':'root'}, 'hosts': {
                key: {'crange_box_key':key, 'crange_project_id':100, 'crange_guest_id':200+i,
                      'ansible_host':f'10.100.{2+i}.40'} for i,key in enumerate(('app','other'))}}}}}
        self.params = lambda row: {'username':'user', 'password':'test-fixture-password'}

    def test_team_selection_preserves_management_identity_and_hashes_secret(self):
        original = copy.deepcopy(self.inventory)
        value = access_inventory(self.cfg, self.inventory, team=2, parameters=self.params)
        self.assertEqual(set(value['all']['hosts']), {'app'})
        host = value['all']['hosts']['app']
        self.assertEqual(host['ansible_user'], 'root')
        self.assertTrue(host['crange_access_password_hash'].startswith('$6$'))
        self.assertNotIn('test-fixture-password', str(value))
        self.assertEqual(self.inventory, original)

    def test_no_ssh_declaration_does_not_provision_a_login(self):
        self.cfg['scenario_containers']['app']['guac_services'] = ['vnc']
        self.assertEqual(access_inventory(self.cfg, self.inventory, team=2, parameters=self.params), {'all':{'hosts':{}}})

    def test_custom_network_is_resolved_from_its_owning_team(self):
        self.cfg['scenario_containers']['app']['network_key'] = 'warehouse'
        self.cfg['custom_networks'] = {'warehouse': {'owner_team_id':'2', 'network_id':100}}
        self.inventory['all']['children']['scenario_containers']['hosts']['app']['ansible_host'] = '10.100.100.40'
        result = access_inventory(self.cfg,self.inventory,team=2,parameters=self.params)
        self.assertEqual(result['all']['hosts']['app']['ansible_host'], '10.100.100.40')
        self.cfg['custom_networks']['warehouse']['owner_team_id'] = '3'
        with self.assertRaises(ValueError):access_inventory(self.cfg,self.inventory,team=2,parameters=self.params)

    def test_wrong_guest_project_or_address_is_rejected(self):
        for key,value in [('crange_project_id',101),('crange_guest_id',False),('ansible_host','10.100.3.40')]:
            bad = copy.deepcopy(self.inventory)
            bad['all']['children']['scenario_containers']['hosts']['app'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                access_inventory(self.cfg,bad,team=2,parameters=self.params)

    def test_system_login_and_control_characters_are_rejected(self):
        for login,password in [('root','fixture'),('daemon','fixture'),('bad name','fixture'),('user','bad\npassword')]:
            with self.subTest(login=login), self.assertRaises(ValueError):
                access_inventory(self.cfg,self.inventory,team=2,parameters=lambda row:{'username':login,'password':password})


if __name__ == '__main__':unittest.main()
