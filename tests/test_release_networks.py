import copy
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import test_crange as fixtures
from crange_release_networks import allocate_networks, attachment

crange = fixtures.crange

class AllocationTests(unittest.TestCase):
    def fixture(self):
        cfg = {'team_ids': ['1', '2'], 'custom_networks': {'lab': {
            'owner_team_id': '254', 'network_id': 100, 'uplink_last_octet': 100,
            'node_name': 'proxmox4'}}, 'jump_boxes': {'jump': {'team_id': '1', 'jump_number': 1}},
            'scenario_boxes': {'svc': {'team_id': '1', 'ip_last_octet': 2}}}
        release = {'release_id': 'p003-1.0', 'scenario': {'networks': {'lab': copy.deepcopy(cfg['custom_networks']['lab'])}}}
        return cfg, release

    def test_each_team_and_test_gets_separate_subnet_with_stable_reuse(self):
        cfg, release = self.fixture()
        original = copy.deepcopy(cfg['custom_networks']['lab'])
        a, owned = allocate_networks(cfg, release, '1', 'team', [3])
        b, _ = allocate_networks(cfg, release, '2', 'team')
        c, _ = allocate_networks(cfg, release, '254', 'test', range(90, 101))
        self.assertEqual(len({100, a['lab']['network_id'], b['lab']['network_id'], c['lab']['network_id']}), 4)
        self.assertEqual(next(iter(owned.values()))['uplink_last_octet'], 4)
        self.assertEqual(allocate_networks(cfg, release, '1', 'team', [3]), (a, owned))
        self.assertEqual(cfg['custom_networks']['lab'], original)
        self.assertEqual(attachment({'network_key': 'lab'}, '1', a)['network_id'], a['lab']['network_id'])
        with self.assertRaisesRegex(ValueError, 'missing'):
            attachment({'network_key': 'missing'}, '1', a)

    def test_refuse_unmanaged_collision_or_drift(self):
        cfg, release = self.fixture()
        mapping, _ = allocate_networks(cfg, release, '1', 'team')
        key = mapping['lab']['key']
        cfg['custom_networks'][key]['cores'] = 4
        with self.assertRaisesRegex(ValueError, 'differs'):
            allocate_networks(cfg, release, '1', 'team')
        cfg['custom_networks'][key].pop('release_id')
        with self.assertRaisesRegex(ValueError, 'unmanaged'):
            allocate_networks(cfg, release, '1', 'team')

class ReleaseTopologyTests(unittest.TestCase):
    setUp = fixtures.CrangeConfigTests.setUp
    tearDown = fixtures.CrangeConfigTests.tearDown
    write_release = fixtures.CrangeConfigTests.write_release

    def test_maximum_source_name_fits_release_guest_name_and_is_unique(self):
        self.write_release()
        release = crange.load_release(3, '1.0.0')
        first = copy.deepcopy(release['scenario']['boxes'][0])
        first['name'] = 'abcdefghijklmnopqrstuvwx'
        second = copy.deepcopy(first)
        second['name'] = 'abcdefghijklmnopqrstuvwy'
        second['ip_last_octet'] += 1
        release['scenario']['boxes'] = [first, second]
        for prefix in ('test1.2.0', 'r1.2.0'):
            boxes = crange.release_scenario_boxes(self.project, release, '1', prefix)
            names = [b['name'] for b in boxes.values()]
            self.assertEqual(len(set(names)), 2)
            for name in names:
                self.assertRegex(name, r'^[a-z][a-z0-9-]{0,23}$')

    def release(self):
        self.write_release()
        release = crange.load_release(3, '1.0.0')
        source = release['scenario']['boxes'][0]
        source.update(network_key='lab', network_id=100, guac_protocol='rdp')
        release['schema_version'] = 4
        release['scenario']['networks'] = {'lab': {'owner_team_id': '254', 'network_id': 100,
            'uplink_last_octet': 100, 'node_name': 'proxmox4'}}
        release['scenario']['containers'] = [{**source, 'name': 'ct', 'ip_last_octet': 21, 'template': 'approved-ct-base'}]
        release['scenario']['templates']['approved-ct-base'] = {'template_id': 120, 'template_node': 'proxmox4'}
        cfg = crange.json_config(self.project)
        cfg['custom_networks'] = copy.deepcopy(release['scenario']['networks'])
        cfg['scenario_boxes'] = {'staging': {**source, 'name': 'original'}}
        crange.save_config(self.project, cfg)
        return release

    def prepare(self, release):
        with patch.object(crange, 'load_release', return_value=release), patch.object(crange, 'verify_release_images'):
            crange.cmd_release_test_prepare(SimpleNamespace(project='3', version='1.0.0'))

    def test_vm_ct_topology_and_cleanup_preserve_staging(self):
        release = self.release()
        before = crange.json_config(self.project)
        self.prepare(release)
        cfg = crange.json_config(self.project)
        record = json.loads((crange.ROOT/'releases/project-003/tests/1.0.0.json').read_text())
        network = next(iter(record['networks'].values()))
        self.assertNotEqual(network['network_id'], 100)
        self.assertEqual(network['owner_team_id'], '254')
        for field, group, host in [('scenario_boxes', 'boxes', 20), ('scenario_containers', 'containers', 21)]:
            key = next(iter(record[group]))
            self.assertEqual(cfg[field][key]['network_id'], network['network_id'])
            self.assertEqual(cfg[field][key]['ip_last_octet'], host)
            self.assertEqual(record[group][key]['address'], f"10.3.{network['network_id']}.{host}")
            self.assertEqual(cfg[field][key]['guac_protocol'], 'rdp')
        crange.cmd_release_test_cleanup(SimpleNamespace(project='3', version='1.0.0'))
        after = crange.json_config(self.project)
        for field in ('custom_networks', 'scenario_boxes', 'jump_boxes'):
            self.assertEqual(after[field], before[field])
        self.assertFalse(after['scenario_containers'])

    def test_cleanup_refuses_borrowed_network_and_preserves_desired_state(self):
        release = self.release(); self.prepare(release)
        cfg = crange.json_config(self.project)
        key = next(k for k in cfg['custom_networks'] if k != 'lab')
        cfg['scenario_boxes']['borrowed'] = {**cfg['scenario_boxes']['staging'], 'name': 'borrowed',
            'ip_last_octet': 30, 'network_key': key, 'network_id': cfg['custom_networks'][key]['network_id']}
        crange.save_config(self.project, cfg)
        before = (self.project/'crange.auto.tfvars.json').read_bytes()
        with self.assertRaisesRegex(crange.CrangeError, 'other guests'):
            crange.cmd_release_test_cleanup(SimpleNamespace(project='3', version='1.0.0'))
        self.assertEqual((self.project/'crange.auto.tfvars.json').read_bytes(), before)

    def test_missing_topology_fails_without_persisting_allocations(self):
        release = self.release();release['scenario']['networks'] = {}
        before = (self.project/'crange.auto.tfvars.json').read_bytes()
        with self.assertRaisesRegex(crange.CrangeError, 'missing'):
            self.prepare(release)
        self.assertEqual((self.project/'crange.auto.tfvars.json').read_bytes(), before)
        self.assertFalse((crange.ROOT/'releases/project-003/tests/1.0.0.json').exists())

    def test_capture_records_custom_topology_and_uses_actual_subnet(self):
        self.release()
        cfg = crange.json_config(self.project)
        outputs = {'scenario_boxes': {'value': {'staging': {'id': 115, 'address': '10.3.100.20'}}},
            'routers': {'value': {'custom_networks': {'lab': {'lan': '10.3.100.0/24',
                'owner_team_id': '254', 'upstream_address': '10.3.254.100'}}}}}
        class API:
            state = 'running'
            def vm(self, node, vmid): return {'name': 'p003c100-original', 'template': 0, 'agent': '1'}
            def status(self, node, vmid): return self.state
            def shutdown(self, node, vmid, address):
                assert address == '10.3.100.20'
                self.state = 'stopped'
            def start(self, node, vmid): self.state = 'running'
            def capture_image(self, *args): return 201
        with patch.object(crange, 'output_json', return_value=outputs), patch.object(crange, 'ProxmoxAPI', return_value=API()), \
             patch.object(crange, 'check_endpoint'), patch.object(crange, 'run', return_value=SimpleNamespace(stdout='revision')), \
             patch('crange_capture.capture_policy'), \
             patch('crange_capture.capture_vm', return_value=(201, {'export_job': 'export', 'probe_job': 'probe'})), \
             patch.object(crange, 'cmd_release_publish'):
            crange.cmd_release_capture(SimpleNamespace(project='3', version='2.0'))
        release = crange.load_release(3, '2.0')
        self.assertEqual(release['schema_version'], 4)
        self.assertEqual(release['scenario']['networks']['lab']['network_id'], 100)
        self.assertEqual(release['scenario']['boxes'][0]['network_key'], 'lab')
        reference=next(iter(release['scenario']['templates'].values()))
        self.assertEqual(reference['kind'], 'vm')
        self.assertEqual(reference['profile']['bios'], 'seabios')
        self.assertFalse(reference['profile']['upgrade_packages'])
        approvals=json.loads((self.project/'approved-scenario-templates.json').read_text())
        self.assertEqual(approvals[release['scenario']['boxes'][0]['template']]['profile'],reference['profile'])
        # Exercise the captured manifest directly rather than a legacy fixture
        # missing the kind metadata written by real capture.
        with patch.object(crange, 'verify_release_images'):
            crange.cmd_release_test_prepare(SimpleNamespace(project='3', version='2.0'))
        prepared = crange.json_config(self.project)['scenario_boxes']
        self.assertTrue(any(box.get('release_id') == 'p003-2.0' for box in prepared.values()))

    def test_test_check_refuses_changed_guest_before_power_actions(self):
        release = self.release(); self.prepare(release)
        record = json.loads((crange.ROOT/'releases/project-003/tests/1.0.0.json').read_text())
        outputs = {field: {'value': {key: {'id': 200 + i, 'address': info['address']}
                   for i, (key, info) in enumerate(record[group].items())}}
                   for field, group in [('scenario_boxes', 'boxes'), ('scenario_containers', 'containers')]}
        from unittest.mock import Mock
        api = Mock();api.vm.return_value = {'name': 'unrelated-production-guest'}
        with patch.object(crange, 'load_release', return_value=release), patch.object(crange, 'output_json', return_value=outputs), \
             patch.object(crange, 'ProxmoxAPI', return_value=api):
            with self.assertRaisesRegex(crange.CrangeError, 'identity has changed'):
                crange.cmd_release_test_check(SimpleNamespace(project='3', version='1.0.0'))
        api.start.assert_not_called();api.request.assert_not_called();api.container_stop.assert_not_called()

    def test_rollout_declares_selected_team_topology_without_changing_staging_or_other_team(self):
        release = self.release()
        cfg = crange.json_config(self.project)
        cfg['team_ids'] = ['1', '2']
        cfg['scenario_boxes']['other-team'] = {**cfg['scenario_boxes']['staging'],
            'team_id': '2', 'name': 'other', 'network_key': '', 'network_id': 2}
        crange.save_config(self.project, cfg)
        before = crange.json_config(self.project)
        results = crange.ROOT/'releases/project-003/test-results'
        results.mkdir(parents=True)
        (results/'1.0.0-pass.json').write_text(json.dumps({'release_id': release['release_id'], 'result': 'passed'}))
        with patch.object(crange, 'load_release', return_value=release), patch.object(crange, 'verify_release_images'):
            crange.cmd_release_deploy(SimpleNamespace(project='3', version='1.0.0', teams=[1], team_override=[]))
        after = crange.json_config(self.project)
        self.assertEqual(after['scenario_boxes']['staging'], before['scenario_boxes']['staging'])
        self.assertEqual(after['scenario_boxes']['other-team'], before['scenario_boxes']['other-team'])
        self.assertEqual(after['custom_networks']['lab'], before['custom_networks']['lab'])
        networks = [net for key, net in after['custom_networks'].items() if key != 'lab']
        self.assertEqual(len(networks), 1)
        self.assertEqual(networks[0]['owner_team_id'], '1')
        for field in ('scenario_boxes', 'scenario_containers'):
            clones = [b for b in after[field].values() if b.get('release_scope') == 'team']
            self.assertEqual(len(clones), 1)
            self.assertEqual((clones[0]['team_id'], clones[0]['network_id']), ('1', networks[0]['network_id']))
