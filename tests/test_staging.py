import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange
from crange_staging import staging_blockers, staging_projection, staging_targets


def resource(address, before=None, after=None, actions=None):
    return {'address': address, 'mode': 'managed', 'change': {
        'actions': actions or ['update'], 'before': before, 'after': after}}


def guest(bridge):
    return {'network_device': [{'bridge': bridge}]}


def router(upstream, downstream):
    return {'network_interface': [{'name': 'eth0', 'bridge': upstream},
                                  {'name': 'eth1', 'bridge': downstream}]}


class StagingScopeTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {'project_id': 3, 'scenario_boxes': {
            'stage': {'team_id': '254'}, 'live': {'team_id': '1'}},
            'scenario_containers': {'ct': {'team_id': '254'}},
            'jump_boxes': {'jump': {'team_id': '254'}},
            'custom_networks': {'lab': {'owner_team_id': '254', 'network_id': 101}}}

    def check(self, resources):
        return staging_blockers({'resource_changes': resources}, self.cfg)

    def test_projection_keeps_inputs_and_excludes_team_guests(self):
        original = json.dumps(self.cfg, sort_keys=True)
        scoped = staging_projection(self.cfg)
        self.assertEqual(set(scoped['scenario_boxes']), {'stage'})
        self.assertEqual(json.dumps(self.cfg, sort_keys=True), original)

    def test_targets_find_removed_subnets_before_guests_regardless_of_order(self):
        state = {'values': {'root_module': {'child_modules': [
            {'resources': [{'address': 'module.scenario_container["gone"].ct',
                            'values': {'network_interface': [{'bridge': 'p003c102'}]}}]},
            {'resources': [{'address': 'module.custom_router["old"].router',
                            'values': router('p003t254', 'p003c102')}]},
            {'resources': [{'address': 'module.scenario_box["live"].vm',
                            'values': guest('p003t001')}]}
        ], 'resources': [{'address': 'proxmox_sdn_vnet.custom["old"]',
                          'values': {'id': 'p003c102'}}]}}}
        targets = staging_targets(self.cfg, state)
        self.assertIn('module.scenario_container["gone"]', targets)
        self.assertIn('module.custom_router["old"]', targets)
        self.assertIn('proxmox_sdn_vnet.custom["old"]', targets)
        self.assertNotIn('module.scenario_box["live"]', targets)

    def test_all_staging_guest_kinds_and_networks_are_allowed(self):
        resources = [resource('terraform_data.sdn_revision'), resource('proxmox_sdn_applier.apply'), resource('proxmox_sdn_vnet.staging'),
                     resource('module.staging_router.router')]
        for kind, key in [('scenario_box', 'stage'), ('scenario_container', 'ct'), ('jumpbox', 'jump')]:
            resources.append(resource(f'module.{kind}["{key}"].guest', guest('p003t254'), guest('p003t254')))
        resources += [resource('module.custom_router["lab"].router', None, router('p003t254', 'p003c101'), ['create']),
                      resource('proxmox_sdn_vnet.custom["lab"]', None, {'id': 'p003c101'}, ['create'])]
        self.assertEqual(self.check(resources), [])

    def test_ordinary_team_shared_and_unknown_changes_are_blocked(self):
        resources = [resource('module.scenario_box["live"].guest', guest('p003t001'), guest('p003t001')),
                     resource('module.team_router["1"].router'), resource('module.project_router.router'),
                     resource('proxmox_sdn_vnet.team["1"]'), resource('proxmox_sdn_vnet.transit'),
                     resource('unrecognized.resource')]
        self.assertEqual(self.check(resources), sorted(item['address'] for item in resources))
        self.assertEqual(self.check([resource('module.team_router["1"].router', actions=['no-op'])]), [])

    def test_moving_team_guest_to_staging_is_still_a_team_change(self):
        item = resource('module.scenario_box["stage"].guest', guest('p003t001'), guest('p003t254'))
        self.assertEqual(self.check([item]), [item['address']])

    def test_plan_guest_network_must_match_staging_even_if_inputs_claim_staging(self):
        item = resource('module.scenario_box["stage"].guest', None, guest('p003t001'), ['create'])
        self.assertEqual(self.check([item]), [item['address']])

    def test_deleted_staging_guests_and_custom_networks_use_prior_state(self):
        self.cfg['scenario_boxes'] = {}
        self.cfg['custom_networks'] = {}
        resources = [resource('module.custom_router["lab"].router', router('p003t254', 'p003c101'), None, ['delete']),
                     resource('proxmox_sdn_vnet.custom["lab"]', {'id': 'p003c101'}, None, ['delete']),
                     resource('module.scenario_container["removed"].guest',
                              {'network_interface': [{'bridge': 'p003c101'}]}, None, ['delete'])]
        self.assertEqual(self.check(resources), [])
        resources[0]['change']['before'] = router('p003t001', 'p003c101')
        self.assertEqual(len(self.check(resources)), 3)

    def test_unknown_prior_guest_network_fails_closed(self):
        item = resource('module.scenario_box["stage"].guest', {}, guest('p003t254'))
        self.assertEqual(self.check([item]), [item['address']])

    def test_new_or_moved_custom_network_ownership_is_checked(self):
        item = resource('proxmox_sdn_vnet.custom["lab"]', {'id': 'p003c101'}, {'id': 'p003c101'})
        self.assertEqual(self.check([item]), [item['address']])
        prior = resource('module.custom_router["lab"].router', router('p003t254', 'p003c101'),
                         router('p003t254', 'p003c101'), ['no-op'])
        self.assertEqual(self.check([item, prior]), [])
        self.cfg['custom_networks']['lab']['owner_team_id'] = '1'
        self.assertEqual(self.check([item, prior]), [item['address']])

    def test_saved_plan_checks_actual_terraform_json_before_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_dir = root / '.local/share/crange/plans/project-003'
            plan_dir.mkdir(parents=True)
            plan_id = 'a' * 32
            plan = plan_dir / (plan_id + '.tfplan')
            plan.write_bytes(b'protected saved binary plan')
            cfgfile = root / 'crange.auto.tfvars.json'
            cfgfile.write_text(json.dumps(self.cfg))
            record = {'project': 3, 'plan_id': plan_id, 'created': time.time(),
                      'sha256': hashlib.sha256(plan.read_bytes()).hexdigest(),
                      'inputs_sha256': hashlib.sha256(cfgfile.read_bytes()).hexdigest(),
                      'automation_inputs_sha256': crange.automation_inputs_digest(root),
                      'changes': [], 'staging_blockers': []}
            (plan_dir / (plan_id + '.json')).write_text(json.dumps(record))
            detail = {'resource_changes': [resource('module.team_router["1"].router')]}
            args = SimpleNamespace(project='3', plan_id=plan_id, staging_only=True,
                                   allow_destroy=True, allow_replace=True)
            with patch.object(Path, 'home', return_value=root), patch.object(crange, 'tf_run',
                    return_value=SimpleNamespace(stdout=json.dumps(detail))) as terraform:
                with self.assertRaisesRegex(crange.CrangeError, 'cannot change team'):
                    crange.cmd_apply_saved(args, root)
            self.assertEqual(terraform.call_count, 1)
            self.assertEqual(terraform.call_args.args[1][:2], ['show', '-json'])
            self.assertTrue(plan.exists())

    def test_new_cli_plan_is_checked_before_apply(self):
        detail = {'resource_changes': [resource('module.team_router["1"].router')]}
        args = SimpleNamespace(project='3', yes=True, staging_only=True, plan_id=None,
                               allow_destroy=True, allow_replace=True)
        with patch('crange_scheduling.read_settings', return_value=({}, {})), \
             patch('crange_scheduling.prepare'), \
             patch('crange_templates.media_tf_overrides', return_value={}), \
             patch.object(crange, 'project_dir', return_value=Path('/tmp/project-003')), \
             patch.object(crange, 'json_config', return_value=self.cfg), \
             patch.object(crange, 'tf_run', return_value=SimpleNamespace(stdout=json.dumps(detail))) as terraform:
            with self.assertRaisesRegex(crange.CrangeError, 'cannot change team'):
                crange.cmd_apply(args)
        self.assertEqual([call.args[1][0] for call in terraform.call_args_list], ['init', 'plan', 'show'])

    def test_scoped_apply_preserves_live_team_image_records_and_prunes_deleted(self):
        import crange_scheduling as scheduling
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'project-100'
            path.mkdir()
            prior = {'vm:team': {'guest_id': 123, 'node_name': 'proxmox1', 'image_id': 'immutable-v1'},
                     'vm:gone': {'guest_id': 999, 'node_name': 'proxmox1'}}
            (path / 'crange-applied-placements.json').write_text(json.dumps(prior))
            actual = {'vm:team': {'vm_id': 123, 'node_name': 'proxmox1'},
                      'container:stage': {'vm_id': 124, 'node_name': 'proxmox2'}}
            prepared = {'requests': [], 'placements': [{'resource_key': 'container:stage',
                'resolved': {'node_name': 'proxmox2'}, 'image': 'stock-v1'}]}
            api = __import__('unittest.mock', fromlist=['Mock']).Mock()
            api.request.return_value = {'tags': 'p100;scenario'}
            with patch.object(scheduling, 'applied_resources', return_value=actual):
                scheduling.finish_apply(path, prepared, succeeded=True, api=api)
            result = json.loads((path / 'crange-applied-placements.json').read_text())
            self.assertEqual(result['vm:team'], prior['vm:team'])
            self.assertEqual(result['container:stage']['guest_id'], 124)
            self.assertNotIn('vm:gone', result)

    def test_no_cache_discovery_for_unchanged_existing_guests(self):
        import crange_scheduling as scheduling
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            row = {'resource_key': 'container:stage', 'box': {}, 'networks': ['p100t254']}
            with patch.object(scheduling, 'read_settings', return_value=({'enabled': True}, {})), \
                 patch.object(scheduling, 'declarations', return_value=[row]), \
                 patch.object(scheduling, 'applied_resources', return_value={'container:stage': {}}), \
                 patch.object(scheduling, 'discover') as discover:
                self.assertEqual(scheduling.required_preparations(path, {}, api=Mock()), [])
            discover.assert_not_called()

    def test_stage_deploy_always_enables_scope_gate(self):
        args = SimpleNamespace(project='3')
        with patch.object(crange, 'cmd_apply') as apply, patch.object(crange, 'cmd_configure_team'):
            crange.cmd_stage_deploy(args)
        self.assertTrue(apply.call_args.args[0].staging_only)
