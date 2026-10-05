import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_user_management import validate_boxes, plan_boxes, DEFAULT_BOXES
import crange_rollouts as rollouts
import test_rollouts


class DistributionWorkflowTests(unittest.TestCase):
    def test_default_user_specs_preserve_auto_preferences_and_legacy_pins(self):
        cfg = {'project_id': 3, 'custom_networks': {}}
        self.assertEqual(DEFAULT_BOXES[0]['node'], 'auto')
        auto = validate_boxes(cfg, [{'network': '', 'preferred_node': 'proxmox2'}])[0]
        self.assertEqual((auto['node'], auto['preferred_node']), ('auto', 'proxmox2'))
        legacy = validate_boxes(cfg, [{'node': 'proxmox4'}])[0]
        self.assertEqual((legacy['node'], legacy['preferred_node']), ('proxmox4', ''))
        for invalid in [{'node': 'proxmox4', 'preferred_node': 'proxmox2'},
                        {'preferred_node': 'outside'}, {'shell': 'id'}]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_boxes(cfg, [invalid])

    def test_default_user_plan_passes_preference_without_mutating_existing_boxes(self):
        cfg = {'project_id': 3, 'team_ids': ['1'], 'jump_boxes': {}, 'custom_networks': {}}
        spec = validate_boxes(cfg, [{'preferred_node': 'proxmox2'}])[0]
        users = [{'home_team': 1, 'user_number': 1, 'connections': []}]
        with patch('crange.mutate_box') as mutate, patch('crange.project_dir', return_value=Path('/tmp')), patch('crange.validate_project_quota'):
            plan_boxes(cfg, users, [spec])
        args = mutate.call_args.args[0]
        self.assertEqual((args.node, args.preferred_node), ('auto', 'proxmox2'))
        self.assertEqual(cfg['jump_boxes'], {})

    def outputs(self):
        return {'scenario_boxes': {'value': {'old': {'id': 125, 'address': '10.3.100.20',
                    'placement': {'node_name': 'proxmox2', 'datastore_id': 'local-lvm',
                                  'template_id': 3012, 'template_node': 'proxmox2', 'clone_mode': 'full'}}}},
                'routers': {'value': {'custom_networks': {'release-net': {
                    'id': 126, 'address': '10.3.1.100', 'owner_team_id': '1',
                    'lan': '10.3.100.0/24', 'upstream_address': '10.3.1.100',
                    'placement': {'node_name': 'proxmox3', 'datastore_id': 'local-lvm'}}}}}}

    def test_rollout_records_actual_guest_nodes_without_changing_desired(self):
        cfg, _ = test_rollouts.RolloutTests().fixture()
        cfg['scenario_boxes']['old'].update(node_name='auto', placement={'mode': 'auto', 'preferred_node': 'proxmox4'})
        rollouts.declare(cfg, '1', {'release_id': 'p003-1.0', 'version': '1.0'}, 'deploy')
        desired = copy.deepcopy(cfg['release_rollouts']['1']['desired'])
        self.assertTrue(rollouts.applied(cfg, self.outputs()))
        record = cfg['release_rollouts']['1']
        self.assertEqual(record['desired'], desired)
        self.assertEqual(record['applied'], desired)
        self.assertEqual(record['applied_endpoints']['vm:old']['node_name'], 'proxmox2')
        self.assertEqual(record['applied_endpoints']['network:release-net']['node_name'], 'proxmox3')
        self.assertEqual(record['history'][-1]['endpoints'], record['applied_endpoints'])
        self.assertFalse(rollouts.applied(cfg, self.outputs()))
        rollouts.verify_owned(cfg, '1', desired)

    def test_auto_rollout_rejects_outputs_without_node_identity(self):
        cfg, _ = test_rollouts.RolloutTests().fixture()
        cfg['scenario_boxes']['old']['node_name'] = 'auto'
        rollouts.declare(cfg, '1', {'release_id': 'p003-1.0', 'version': '1.0'}, 'deploy')
        outputs = self.outputs()
        outputs['scenario_boxes']['value']['old'].pop('placement')
        with self.assertRaisesRegex(ValueError, 'applied node identity'):
            rollouts.applied(cfg, outputs)
        self.assertNotIn('applied', cfg['release_rollouts']['1'])
