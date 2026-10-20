import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_ssh_identity import refresh_created_guests


class GuestSSHIdentityTests(unittest.TestCase):
    def setup_case(self, directory):
        home = Path(directory)
        path = home / 'project-100'
        path.mkdir()
        plans = home / '.local/share/crange/plans/project-100'
        plans.mkdir(parents=True)
        manifest = plans / ('a' * 32 + '.json')
        manifest.write_text(json.dumps({'project': 100, 'applied_at': '2026-10-09T12:00:00Z',
            'changes': [{'address': 'module.scenario_container["stage"].ct', 'actions': ['delete', 'create']},
                        {'address': 'module.scenario_container["team"].ct', 'actions': ['create']}]}))
        (path / 'crange-applied-placements.json').write_text(json.dumps({
            'container:stage': {'node_name': 'proxmox1', 'guest_id': 124},
            'container:team': {'node_name': 'proxmox2', 'guest_id': 125}}))
        (home / '.ssh').mkdir()
        (home / '.ssh/known_hosts').write_text('old key\n')
        guests = {'scenario_containers': {'value': {
            'stage': {'team_id': 254, 'id': 124, 'address': '10.100.254.40', 'network_vnet': 'p100t254'},
            'team': {'team_id': 1, 'id': 125, 'address': '10.100.1.40', 'network_vnet': 'p100t001'}}}}
        api = Mock()
        api.request.return_value = {'tags': 'p100;p100t254;scenario',
                                    'net0': 'bridge=p100t254,ip=10.100.254.40/24'}
        return home, path, manifest, guests, api

    def test_refresh_is_one_time_and_scoped_to_owned_created_staging_guest(self):
        with tempfile.TemporaryDirectory() as directory:
            home, path, manifest, guests, api = self.setup_case(directory)
            runner = Mock()
            self.assertEqual(refresh_created_guests(path, 254, home=home, output=guests, api=api, runner=runner), ['10.100.254.40'])
            runner.assert_called_once()
            self.assertEqual(runner.call_args.args[0][-2:], ['-R', '10.100.254.40'])
            self.assertEqual(refresh_created_guests(path, 254, home=home, output=guests, api=api, runner=runner), [])
            self.assertEqual(runner.call_count, 1)

    def test_mismatched_live_owner_or_address_never_removes_keys(self):
        for config in ({'tags': 'p200;p100t254', 'net0': 'bridge=p100t254,ip=10.100.254.40/24'},
                       {'tags': 'p100;p100t254', 'net0': 'bridge=p100t254,ip=10.100.254.41/24'}):
            with tempfile.TemporaryDirectory() as directory:
                home, path, manifest, guests, api = self.setup_case(directory)
                api.request.return_value = config
                runner = Mock()
                with self.assertRaises(ValueError):
                    refresh_created_guests(path, 254, home=home, output=guests, api=api, runner=runner)
                runner.assert_not_called()

    def test_unapplied_plan_does_not_refresh_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            home, path, manifest, guests, api = self.setup_case(directory)
            record = json.loads(manifest.read_text())
            del record['applied_at']
            manifest.write_text(json.dumps(record))
            self.assertEqual(refresh_created_guests(path, 254, home=home, output=guests, api=api), [])
            api.request.assert_not_called()

    def test_post_apply_scope_does_not_contact_another_created_staging_guest(self):
        with tempfile.TemporaryDirectory() as directory:
            home, path, manifest, guests, api = self.setup_case(directory)
            guests['scenario_containers']['value']['team'].update(team_id=254, address='10.100.254.41')
            api.request.side_effect = lambda method, endpoint: ({'tags': 'p100;p100t254;scenario',
                'net0': 'bridge=p100t254,ip=10.100.254.40/24'} if endpoint.endswith('/124/config')
                else self.fail('Unrelated offline guest contacted'))
            refreshed = refresh_created_guests(path, 254, home=home, output=guests, api=api,
                runner=Mock(), resource_keys=['container:stage'])
            self.assertEqual(refreshed, ['10.100.254.40'])
            api.request.assert_called_once_with('GET', '/nodes/proxmox1/lxc/124/config')

    def custom_router_case(self, directory):
        home, path, manifest, guests, api = self.setup_case(directory)
        resource = 'module.custom_router["test-primary"].router'
        record = json.loads(manifest.read_text())
        record['changes'] = [{'address':resource, 'actions':['create']}]
        manifest.write_text(json.dumps(record))
        (path / 'crange-applied-placements.json').write_text(json.dumps({
            'router:custom:test-primary': {'node_name':'proxmox6', 'guest_id':160}}))
        guests = {'routers':{'value':{'custom_networks':{'test-primary':{
            'id':160,'owner_team_id':'254','network_id':100,'address':'10.100.254.2',
            'upstream_address':'10.100.254.2','vnet':'p100c100'}}}}}
        api.request.return_value = {'tags':'p100;p100c100','net0':'bridge=p100t254,ip=10.100.254.2/24',
                                    'net1':'bridge=p100c100,ip=10.100.100.1/24'}
        return home, path, manifest, guests, api

    def test_recreated_custom_router_refreshes_only_its_verified_uplink_once(self):
        with tempfile.TemporaryDirectory() as directory:
            home,path,manifest,guests,api = self.custom_router_case(directory)
            runner = Mock()
            self.assertEqual(refresh_created_guests(path,254,home=home,output=guests,api=api,runner=runner),
                             ['10.100.254.2'])
            self.assertEqual(runner.call_args.args[0][-2:], ['-R','10.100.254.2'])
            self.assertEqual(refresh_created_guests(path,254,home=home,output=guests,api=api,runner=runner), [])
            self.assertEqual(runner.call_count, 1)

    def test_foreign_router_or_changed_interfaces_do_not_remove_known_keys(self):
        for mutation in ({'tags':'p200;p100c100'}, {'net1':'bridge=p100c101,ip=10.100.100.1/24'},
                         {'net0':'bridge=p100t254,ip=10.100.254.3/24'}, {'template':1}):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                home,path,manifest,guests,api = self.custom_router_case(directory)
                api.request.return_value.update(mutation)
                runner = Mock()
                with self.assertRaises(ValueError):
                    refresh_created_guests(path,254,home=home,output=guests,api=api,runner=runner)
                runner.assert_not_called()

    def test_other_team_router_and_unchanged_plan_do_not_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            home,path,manifest,guests,api = self.custom_router_case(directory)
            runner = Mock()
            self.assertEqual(refresh_created_guests(path,1,home=home,output=guests,api=api,runner=runner), [])
            record = json.loads(manifest.read_text());record['changes'][0]['actions']=['no-op']
            manifest.write_text(json.dumps(record))
            self.assertEqual(refresh_created_guests(path,254,home=home,output=guests,api=api,runner=runner), [])
            runner.assert_not_called()
