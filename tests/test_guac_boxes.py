import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from crange import CrangeError
from crange_guac_boxes import power, snapshots, resolve


class BoxActionsTests(unittest.TestCase):
    def test_resolve_automatic_or_migrated_guest_uses_live_node(self):
        api = Mock()
        api.request.return_value = [{'vmid': 123, 'type': 'qemu', 'node': 'proxmox5'}]
        api.vm.return_value = {'name': 'p003t001-web', 'net0': 'virtio=AA,bridge=p003t001'}
        with patch('crange.project_dir', return_value=Path('/project')), \
             patch('crange.json_config', return_value={'scenario_boxes': {'web': {'team_id': 1, 'name': 'web', 'node_name': 'auto'}}}), \
             patch('crange.output_json', return_value={'scenario_boxes': {'value': {'web': {'id': 123, 'address': '10.3.1.20', 'node_name': 'proxmox1'}}}}), \
             patch('crange.ProxmoxAPI', return_value=api):
            self.assertEqual(resolve(3, 'vm:web')[5], 'proxmox5')
            api.vm.assert_called_once_with('proxmox5', 123)
            api.vm.return_value['net0'] = 'virtio=AA,bridge=p004t001'
            with self.assertRaisesRegex(CrangeError, 'network does not match'):
                resolve(3, 'vm:web')
            api.request.return_value = []
            with self.assertRaisesRegex(CrangeError, 'missing or ambiguous'):
                resolve(3, 'vm:web')

    def fixture(self, kind='vm'):
        api = Mock()
        box = {'started': True}
        cfg = {'scenario_boxes': {'web': box}}
        return (Path('/project'), cfg, box, api, kind, 'proxmox1', 123, '10.3.1.20')

    def test_restart_checks_shutdown_and_quota_before_start(self):
        resolved = self.fixture()
        api = resolved[3]
        api.status.side_effect = ['running', 'stopped', 'running']
        with patch('crange_guac_boxes.resolve', return_value=resolved), \
                patch('crange.validate_project_quota') as quota, \
                patch('crange.save_config') as save, contextlib.redirect_stdout(io.StringIO()):
            power(3, 'vm:web', 'restart')
        api.request.assert_called_once_with('POST', '/nodes/proxmox1/qemu/123/status/shutdown', {'timeout': 90, 'forceStop': 0})
        quota.assert_called_once_with(resolved[0], resolved[1])
        api.start.assert_called_once_with('proxmox1', 123)
        save.assert_called_once_with(resolved[0], resolved[1])

    def test_failed_shutdown_never_starts_or_updates_desired_state(self):
        resolved = self.fixture()
        resolved[3].status.return_value = 'running'
        with patch('crange_guac_boxes.resolve', return_value=resolved), \
                patch('crange.save_config') as save:
            with self.assertRaisesRegex(CrangeError, 'did not stop cleanly'):
                power(3, 'vm:web', 'restart')
        resolved[3].start.assert_not_called()
        save.assert_not_called()

    def test_force_stop_overrides_shutdown_for_vms_and_containers(self):
        for kind in ('vm', 'container'):
            with self.subTest(kind=kind):
                resolved = self.fixture(kind)
                api = resolved[3]
                status = api.container_status if kind == 'container' else api.status
                status.side_effect = ['running', 'stopped']
                with patch('crange_guac_boxes.resolve', return_value=resolved), \
                     patch('crange.save_config'), contextlib.redirect_stdout(io.StringIO()):
                    power(3, kind + ':web', 'stop')
                path_kind = 'lxc' if kind == 'container' else 'qemu'
                api.request.assert_called_once_with('POST', f'/nodes/proxmox1/{path_kind}/123/status/stop',
                                                    {'overrule-shutdown': 1})
                api.shutdown.assert_not_called()
                self.assertFalse(resolved[2]['started'])

    def test_snapshot_listing_and_restore_use_only_resolved_guest(self):
        resolved = self.fixture('container')
        api = resolved[3]
        api.request.return_value = [{'name': 'current'}, {'name': 'before-update', 'snaptime': 42}]
        output = io.StringIO()
        with patch('crange_guac_boxes.resolve', return_value=resolved), contextlib.redirect_stdout(output):
            snapshots(3, 'container:web', 'list')
        self.assertEqual(json.loads(output.getvalue())['snapshots'],
                         [{'name': 'before-update', 'description': '', 'created': 42}])
        api.request.side_effect = [[{'name': 'before-update'}], 'task-id']
        api.container_status.return_value = 'stopped'
        with patch('crange_guac_boxes.resolve', return_value=resolved), \
                patch('crange.save_config') as save, contextlib.redirect_stdout(io.StringIO()):
            snapshots(3, 'container:web', 'restore', 'before-update')
        api.request.assert_called_with('POST', '/nodes/proxmox1/lxc/123/snapshot/before-update/rollback', {})
        api.task.assert_called_once_with('proxmox1', 'task-id', timeout=900)
        self.assertFalse(resolved[2]['started'])
        save.assert_called_once()


if __name__ == '__main__':
    unittest.main()
