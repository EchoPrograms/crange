"""Scoped staging must tolerate unrelated outages without hiding state drift."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_apply_scope import guest_key, reviewed_distribution, verify_untouched
from crange_placement import PlacementError
import crange_scheduling as scheduling


class ApplyScopeTests(unittest.TestCase):
    def fixture(self):
        team = {'node_name': 'proxmox1', 'vm_id': 120, 'memory': [{'dedicated': 512}]}
        stage = {'node_name': 'proxmox2', 'vm_id': 124}
        actual = {'router:team:1': team, 'container:stage': stage}
        prepared = {'requests': [], 'placements': [
            {'resource_key': 'router:team:1', 'existing': True,
             'resolved': {'node_name': 'proxmox1', 'datastore_id': 'local-lvm'}},
            {'resource_key': 'container:stage', 'existing': True, 'image': 'immutable-v1',
             'resolved': {'node_name': 'proxmox2', 'datastore_id': 'local-lvm'}}]}
        detail = {'resource_changes': [{
            'address': 'module.scenario_container["stage"].proxmox_virtual_environment_container.scenario',
            'type': 'proxmox_virtual_environment_container',
            'change': {'actions': ['update'], 'before': stage, 'after': stage}}]}
        return prepared, detail, actual

    def test_recognizes_fixed_guest_addresses_and_rejects_wrong_resource_kind(self):
        self.assertEqual(guest_key('module.project_router.proxmox_virtual_environment_container.router'), 'router:project')
        self.assertEqual(guest_key('module.scenario_box["test0.1.0-vm"].proxmox_virtual_environment_vm.scenario'), 'vm:test0.1.0-vm')
        self.assertIsNone(guest_key('module.team_router["1"].proxmox_virtual_environment_vm.router'))

    def test_plan_binding_excludes_unrelated_router_but_detects_state_drift(self):
        prepared, detail, actual = self.fixture()
        scoped = reviewed_distribution(prepared, detail, actual)
        self.assertEqual([p['resource_key'] for p in scoped['placements']], ['container:stage'])
        self.assertEqual(len(prepared['placements']), 2)
        self.assertEqual(scoped, reviewed_distribution(scoped, detail, actual))
        verify_untouched(scoped, actual)
        changed = copy.deepcopy(actual)
        changed['router:team:1']['memory'][0]['dedicated'] = 1024
        with self.assertRaisesRegex(PlacementError, 'state changed'):
            reviewed_distribution(scoped, detail, changed)
        with self.assertRaisesRegex(PlacementError, 'state changed'):
            verify_untouched(scoped, changed)
        changed = {**actual, 'vm:unexpected': {'node_name': 'proxmox3', 'vm_id': 199}}
        with self.assertRaises(PlacementError):
            verify_untouched(scoped, changed)

    def test_unchanged_dependency_is_still_checked_if_terraform_includes_it(self):
        prepared, detail, actual = self.fixture()
        detail['resource_changes'].append({
            'address': 'module.team_router["1"].proxmox_virtual_environment_container.router',
            'change': {'actions': ['no-op']}})
        scoped = reviewed_distribution(prepared, detail, actual)
        self.assertEqual(len(scoped['placements']), 2)

    def test_never_discards_an_unreviewed_reservation_or_unknown_guest(self):
        prepared, detail, actual = self.fixture()
        prepared['requests'] = [{'resource_key': 'vm:unrelated'}]
        with self.assertRaisesRegex(PlacementError, 'reservations include'):
            reviewed_distribution(prepared, detail, actual)
        prepared['requests'] = []
        detail['resource_changes'][0]['address'] = 'proxmox_virtual_environment_container.unowned'
        with self.assertRaisesRegex(PlacementError, 'unrecognized guest'):
            reviewed_distribution(prepared, detail, actual)

    def test_begin_and_finish_check_only_reviewed_guests_on_online_node(self):
        prepared, detail, actual = self.fixture()
        policy, catalog = {'enabled': True}, {'schema_version': 1, 'images': {}}
        prepared.update(provider_compat={'enabled': False},
                        policy_sha256=hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest(),
                        catalog_sha256=scheduling.catalog_identity(catalog))
        scoped = reviewed_distribution(prepared, detail, actual)
        inventory = {'nodes': {'proxmox1': {'online': False}, 'proxmox2': {
            'online': True, 'storages': {'local-lvm': {'available': True}}}}}
        api = Mock()
        api.request.return_value = {'tags': 'p100;scenario'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'project-100'
            path.mkdir()
            previous = {'router:team:1': {'guest_id': 120, 'node_name': 'proxmox1', 'image_id': 'router-v1'}}
            (path / 'crange-applied-placements.json').write_text(json.dumps(previous))
            with patch.object(scheduling, 'applied_resources', return_value=actual), \
                 patch.object(scheduling, 'read_settings', return_value=(policy, catalog)), \
                 patch('crange_proxmox_compat.reviewed_identity', return_value={'enabled': False}), \
                 patch.object(scheduling, 'discover', return_value=inventory):
                scheduling.begin_apply(path, scoped, api=api)
                scheduling.finish_apply(path, scoped, succeeded=True, api=api)
                inventory['nodes']['proxmox2']['online'] = False
                with self.assertRaisesRegex(PlacementError, 'offline'):
                    scheduling.begin_apply(path, scoped, api=api)
            result = json.loads((path / 'crange-applied-placements.json').read_text())
            self.assertEqual(result['router:team:1'], previous['router:team:1'])
            self.assertEqual(result['container:stage']['guest_id'], 124)
        api.request.assert_called_once_with('GET', '/nodes/proxmox2/lxc/124/config')

    def test_removed_guests_are_scoped_but_must_be_gone_after_apply(self):
        prepared, detail, actual = self.fixture()
        prepared['placements'] = []
        detail['resource_changes'][0]['change'].update(actions=['delete'], after=None)
        scoped = reviewed_distribution(prepared, detail, actual)
        self.assertEqual(scoped['apply_scope']['removed_guests'], {
            'container:stage': {'node_name': 'proxmox2', 'guest_id': 124}})
        with patch.object(scheduling, 'applied_resources', return_value=actual):
            with self.assertRaisesRegex(PlacementError, 'deletion is incomplete'):
                scheduling.finish_apply(Path('/tmp/project-100'), scoped, succeeded=True, api=Mock())
        verify_untouched(scoped, {'router:team:1': actual['router:team:1']})


if __name__ == '__main__':
    unittest.main()
