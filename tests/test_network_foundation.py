import copy
import pathlib
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from crange_network_foundation import pending, targets, validate_plan
import crange_scheduling as scheduling


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {'project_id': 100, 'team_ids': ['1'], 'custom_networks': {
            'test': {'owner_team_id': '254', 'network_id': 100, 'uplink_last_octet': 2},
            'team': {'owner_team_id': '1', 'network_id': 101, 'uplink_last_octet': 2}}}
        self.prepared = {'placements': [{'resource_key': 'router:custom:test', 'resolved': {
            'node_name': 'proxmox2', 'datastore_id': 'local-lvm', 'template_file_id': 'crange-library:vztmpl/debian.tar.zst'}}],
            'requests': [{'resource_key': 'router:custom:test', 'disk_gb': 4}]}
        router = {'node_name': 'proxmox2', 'pool_id': 'crange-iac', 'tags': ['p100', 'p100c100'],
            'disk': [{'datastore_id': 'local-lvm', 'size': 4}],
            'operating_system': [{'template_file_id': 'crange-library:vztmpl/debian.tar.zst'}],
            'network_interface': [{'name': 'eth0', 'bridge': 'p100t254'}, {'name': 'eth1', 'bridge': 'p100c100'}],
            'initialization': [{'ip_config': [{'ipv4': [{'address': '10.100.254.2/24', 'gateway': '10.100.254.1'}]},
                                             {'ipv4': [{'address': '10.100.100.1/24'}]}]}]}
        self.plan = {'resource_changes': [
            {'address': 'module.custom_router["test"].proxmox_virtual_environment_container.router',
             'change': {'actions': ['create'], 'before': None, 'after': router}},
            {'address': 'proxmox_sdn_vnet.custom["test"]', 'change': {'actions': ['create'], 'before': None,
             'after': {'id': 'p100c100', 'zone': 'crange', 'tag': 125700}}},
            {'address': 'terraform_data.sdn_revision', 'change': {'actions': ['update'], 'after': {}}},
            {'address': 'proxmox_sdn_applier.apply', 'change': {'actions': ['delete', 'create'],
             'before': {'on_destroy': False}, 'after': {'on_create': True, 'on_destroy': False}}}]}

    def test_only_missing_staging_routers_are_selected(self):
        self.assertEqual(pending(self.cfg, {}), ['test'])
        self.assertEqual(pending(self.cfg, {'router:custom:test': {}}), [])
        self.assertIn('module.custom_router["test"]', targets(['test']))
        self.assertNotIn('module.staging_router', targets(['test']))

    def test_valid_plan_accepts_reload_but_not_live_guest_mutation(self):
        self.assertTrue(validate_plan(self.plan, self.cfg, ['test'], self.prepared))
        for address, actions in [('module.staging_router.proxmox_virtual_environment_container.router', ['update']),
                                 ('module.scenario_box["dc"].proxmox_virtual_environment_vm.vm', ['create']),
                                 ('proxmox_sdn_applier.finalizer', ['delete', 'create'])]:
            altered = copy.deepcopy(self.plan)
            altered['resource_changes'].append({'address': address, 'change': {'actions': actions}})
            with self.subTest(address=address), self.assertRaises(ValueError):
                validate_plan(altered, self.cfg, ['test'], self.prepared)

    def test_rejects_wrong_placement_storage_network_and_destruction(self):
        for field, value in [('node_name', 'proxmox5'), ('pool_id', 'other'),
                             ('disk', [{'datastore_id': 'ssd-lvm', 'size': 4}]),
                             ('network_interface', [{'name': 'eth0', 'bridge': 'p100t001'}])]:
            altered = copy.deepcopy(self.plan)
            altered['resource_changes'][0]['change']['after'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_plan(altered, self.cfg, ['test'], self.prepared)
        for index in (0, 1):
            altered = copy.deepcopy(self.plan)
            altered['resource_changes'][index]['change']['actions'] = ['delete', 'create']
            with self.assertRaises(ValueError): validate_plan(altered, self.cfg, ['test'], self.prepared)
        altered = copy.deepcopy(self.plan)
        altered['resource_changes'][-1]['change']['after']['on_destroy'] = True
        with self.assertRaises(ValueError): validate_plan(altered, self.cfg, ['test'], self.prepared)
        with self.assertRaises(ValueError): validate_plan({'resource_changes': []}, self.cfg, ['test'], self.prepared)

    def test_eligibility_is_temporary_router_only_and_requires_parent_acceptance(self):
        requests = [{'resource_key': 'router:custom:test', 'project_id': 100, 'kind': 'container',
                     'networks': ['p100t254', 'p100c100']}]
        inventory = {'nodes': {'proxmox1': {'online': True, 'management_verified': True,
                                          'networks': ['p100t254'], 'verified_networks': ['p100t254']},
                               'proxmox2': {'online': True, 'management_verified': True,
                                          'networks': ['p100t254'], 'verified_networks': []}}}
        with patch.object(scheduling, 'bootstrap_node_candidates', return_value=['proxmox1', 'proxmox2']):
            result = scheduling._custom_foundation_networks(inventory, {}, requests, self.cfg, ['test'])
            self.assertEqual(result['nodes']['proxmox1']['verified_networks'], ['p100t254'])
            self.assertEqual(result['nodes']['proxmox1']['bootstrap_planned_networks'], ['p100t254', 'p100c100'])
            self.assertNotIn('bootstrap_planned_networks', result['nodes']['proxmox2'])
            requests[0]['kind'] = 'vm'
            with self.assertRaises(ValueError):
                scheduling._custom_foundation_networks(inventory, {}, requests, self.cfg, ['test'])
