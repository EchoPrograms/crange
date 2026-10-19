import copy
import pathlib
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from crange_network_workflow import NetworkAcceptanceBackend


class CustomTopologyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {'project_id': 100, 'team_ids': ['1'], 'custom_networks': {
            'test': {'owner_team_id': '254', 'network_id': 100, 'uplink_last_octet': 2}}}
        self.routers = {'project': {'id': 113, 'address': '10.60.2.2', 'placement': {'node_name': 'proxmox2'}},
            'staging': {'id': 116, 'address': '10.100.0.7', 'lan': '10.100.254.0/24', 'placement': {'node_name': 'proxmox5'}},
            'custom_networks': {'test': {'id': 140, 'address': '10.100.254.2', 'upstream_address': '10.100.254.2',
                'vnet': 'p100c100', 'network_id': 100, 'owner_team_id': '254', 'lan': '10.100.100.0/24',
                'placement': {'node_name': 'proxmox2'}}}}
        self.configs = {
            113: {'tags': 'p100', 'net0': 'bridge=crtrans,ip=10.60.2.2/24', 'net1': 'bridge=p100tr,ip=10.100.0.1/24'},
            116: {'tags': 'p100;p100t254', 'net0': 'bridge=p100tr,ip=10.100.0.7/24,gw=10.100.0.1',
                  'net1': 'bridge=p100t254,ip=10.100.254.1/24'},
            140: {'tags': 'p100;p100c100', 'net0': 'bridge=p100t254,ip=10.100.254.2/24,gw=10.100.254.1',
                  'net1': 'bridge=p100c100,ip=10.100.100.1/24'}}
        self.rows = [{'vmid': row['id'], 'node': row['placement']['node_name'], 'type': 'lxc', 'status': 'running'}
                     for row in [self.routers['project'], self.routers['staging'], self.routers['custom_networks']['test']]]
        outer = self
        class API:
            def request(self, method, path, params=None):
                return copy.deepcopy(outer.rows if path == '/cluster/resources' else outer.configs[int(path.split('/')[-2])])
        self.backend = NetworkAcceptanceBackend.__new__(NetworkAcceptanceBackend)
        self.backend.project = 100
        self.backend.project_path = pathlib.Path('/not-used')
        self.backend.target = {'vnet': 'p100c100', 'node': 'proxmox1'}
        self.backend.api = API()

    def resolve(self):
        with patch('crange.json_config', return_value=self.cfg):
            return self.backend._custom_routers(self.routers)

    def test_resolves_live_three_router_chain(self):
        self.assertEqual(self.resolve(), {'project_router': '10.60.2.2', 'parent_team_router': '10.100.0.7',
                                         'team_router': '10.100.254.2', 'node': 'proxmox2'})

    def test_rejects_foreign_interfaces_stopped_or_moved_routers(self):
        changes = [(140, 'net0', 'bridge=p100t001,ip=10.100.254.2/24,gw=10.100.254.1'),
                   (113, 'net1', 'bridge=p101tr,ip=10.100.0.1/24'), (116, 'tags', 'p101;p100t254')]
        for guest, field, value in changes:
            original = self.configs[guest][field]
            self.configs[guest][field] = value
            with self.subTest(guest=guest, field=field), self.assertRaises(ValueError):
                self.resolve()
            self.configs[guest][field] = original
        self.rows[-1]['status'] = 'stopped'
        with self.assertRaises(ValueError): self.resolve()
        self.rows[-1]['status'] = 'running'
        self.rows[-1]['node'] = 'proxmox3'
        with self.assertRaises(ValueError): self.resolve()

    def test_requires_cross_node_probe_and_deployed_router(self):
        self.backend.target['node'] = 'proxmox2'
        with self.assertRaisesRegex(ValueError, 'another candidate'): self.resolve()
        self.routers['custom_networks'].clear()
        with self.assertRaisesRegex(ValueError, 'not been deployed'): self.resolve()
