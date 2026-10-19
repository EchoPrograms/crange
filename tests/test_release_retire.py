from contextlib import ExitStack, nullcontext
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange
from crange import CrangeError
from crange_release_retire import retirements, quiesce


class ReleaseRetirementTests(unittest.TestCase):
    def fixture(self):
        box = {'name': 'old-web', 'team_id': '1', 'ip_last_octet': 40,
               'release_scope': 'team', 'release_id': 'p100-1.0'}
        endpoint = {'id': 125, 'node_name': 'proxmox1', 'address': '10.100.1.40'}
        cfg = {'project_id': 100, 'release_rollouts': {'1': {
            'applied': {'release_id': 'p100-1.0', 'resources': {'scenario_boxes': {'old': box}}},
            'applied_endpoints': {'vm:old': endpoint}}}}
        address = 'module.scenario_box["old"].proxmox_virtual_environment_vm.scenario'
        detail = {'resource_changes': [{'address': address, 'change': {
            'actions': ['delete'], 'before': {'vm_id': 125, 'node_name': 'proxmox1', 'name': 'p100t001-old-web'}}}]}
        return cfg, detail

    def api(self, targets):
        api = Mock(); states = {row['id']: 'running' for row in targets}
        configs = {row['id']: {'name' if row['kind'] == 'qemu' else 'hostname': row['name'], 'tags': 'p100;scenario',
                              'net0': 'e1000=AA:BB,bridge=' + row['bridge']} for row in targets}
        active = []
        def request(method, path, fields=None):
            if path == '/cluster/resources':
                return [{'vmid': row['id'], 'node': row['node'], 'type': row['kind'], 'pool': 'crange-iac'} for row in targets]
            if path.endswith('/tasks'): return active
            guest = int(path.split('/')[4])
            if path.endswith('/config'): return configs[guest]
            if path.endswith('/status/current'): return {'status': states[guest]}
            if method == 'POST' and path.endswith('/status/stop'):
                states[guest] = 'stopped'; return f'UPID:stop:{guest}'
            raise AssertionError((method, path))
        api.request.side_effect = request
        return api, states, configs, active

    def test_only_exact_selected_old_release_deletes_are_stopped(self):
        cfg, detail = self.fixture()
        targets = retirements(cfg, ['1'], detail)
        self.assertEqual([row['id'] for row in targets], [125])
        self.assertEqual(retirements(cfg, ['2'], detail), [])
        detail['resource_changes'][0]['change']['actions'] = ['update']
        self.assertEqual(retirements(cfg, ['1'], detail), [])

    def test_changed_applied_id_node_or_owner_refuses_before_mutation(self):
        for kind in ('id', 'node', 'owner'):
            cfg, detail = self.fixture()
            if kind == 'id': cfg['release_rollouts']['1']['applied_endpoints']['vm:old']['id'] = 999
            elif kind == 'node': detail['resource_changes'][0]['change']['before']['node_name'] = 'proxmox2'
            else: cfg['release_rollouts']['1']['applied']['resources']['scenario_boxes']['old']['team_id'] = '2'
            with self.subTest(kind=kind), self.assertRaises(CrangeError): retirements(cfg, ['1'], detail)

    def test_all_ownership_checked_before_first_stop_and_stopped_guests_not_replayed(self):
        cfg, detail = self.fixture(); targets = retirements(cfg, ['1'], detail)
        api, states, _, _ = self.api(targets)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'receipt.json'
            quiesce(api, targets, path, 'a'*32)
            quiesce(api, targets, path, 'a'*32)
            self.assertEqual(json.loads(path.read_text())['state'], 'stopped')
        self.assertEqual(states[125], 'stopped')
        self.assertEqual(sum(call.args[0] == 'POST' for call in api.request.call_args_list), 1)

    def test_foreign_later_guest_blocks_all_stops(self):
        cfg, detail = self.fixture(); targets = retirements(cfg, ['1'], detail)
        targets.append({**targets[0], 'id': 126, 'name': 'p100t001-second'})
        api, _, configs, _ = self.api(targets); configs[126]['name'] = 'foreign'
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(CrangeError):
            quiesce(api, targets, Path(temp)/'receipt.json', 'a'*32)
        self.assertFalse(any(call.args[0] == 'POST' for call in api.request.call_args_list))

    def test_active_foreign_task_and_ambiguous_submission_never_replayed(self):
        cfg, detail = self.fixture(); targets = retirements(cfg, ['1'], detail)
        api, _, _, active = self.api(targets); active.append({'id': '125', 'upid': 'foreign'})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'receipt.json'
            with self.assertRaises(CrangeError): quiesce(api, targets, path, 'a'*32)
            active.clear()
            path.write_text(json.dumps({'plan_id': 'a'*32, 'targets': targets,
                                       'steps': {'125': {'state': 'submitting'}}}))
            with self.assertRaisesRegex(CrangeError, 'ambiguous'): quiesce(api, targets, path, 'a'*32)
        self.assertFalse(any(call.args[0] == 'POST' for call in api.request.call_args_list))

    def test_container_stop_uses_exact_lxc_endpoint(self):
        cfg, detail = self.fixture()
        record = cfg['release_rollouts']['1']
        record['applied']['resources']['scenario_containers'] = record['applied']['resources'].pop('scenario_boxes')
        record['applied_endpoints']['container:old'] = record['applied_endpoints'].pop('vm:old')
        resource = detail['resource_changes'][0]
        resource['address'] = 'module.scenario_container["old"].proxmox_virtual_environment_container.scenario'
        resource['change']['before']['initialization'] = [{'hostname': resource['change']['before'].pop('name')}]
        targets = retirements(cfg, ['1'], detail); api, _, _, _ = self.api(targets)
        # API configuration uses hostname for CTs, as the real endpoint does.
        with tempfile.TemporaryDirectory() as temp: quiesce(api, targets, Path(temp)/'receipt.json', 'a'*32)
        api.request.assert_any_call('POST', '/nodes/proxmox1/lxc/125/status/stop', {})

    def saved_apply(self, fail_stop=False):
        cfg, detail = self.fixture(); targets = retirements(cfg, ['1'], detail)
        api, _, _, _ = self.api(targets); events = []
        original = api.request.side_effect
        def request(method, path, fields=None):
            if method == 'POST': events.append('stop')
            return original(method, path, fields)
        api.request.side_effect = request
        if fail_stop: api.task.side_effect = CrangeError('stop task uncertain')
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp); private = root/'.local/share/crange/plans/project-100'
            private.mkdir(parents=True); plan_id = 'a'*32
            binary = private/(plan_id+'.tfplan'); binary.write_bytes(b'reviewed binary')
            inputs = root/'crange.auto.tfvars.json'; inputs.write_text(json.dumps(cfg))
            record = {'project': 100, 'plan_id': plan_id, 'created': time.time(),
                      'sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
                      'inputs_sha256': hashlib.sha256(inputs.read_bytes()).hexdigest(),
                      'automation_inputs_sha256': 'trusted-source',
                      'changes': [{'address': targets[0]['resource'], 'actions': ['delete']}],
                      'distribution': {'placements': [{'resolved': {'node_name': 'proxmox2'}}]}}
            (private/(plan_id+'.json')).write_text(json.dumps(record))
            def terraform(path, command, **kwargs):
                if command[0] == 'apply': events.append('apply')
                return SimpleNamespace(stdout=json.dumps(detail))
            stack.enter_context(patch.object(Path, 'home', return_value=root))
            stack.enter_context(patch.object(crange, 'automation_inputs_digest', return_value='trusted-source'))
            stack.enter_context(patch.object(crange, 'ProxmoxAPI', return_value=api))
            stack.enter_context(patch.object(crange, 'tf_run', side_effect=terraform))
            stack.enter_context(patch.object(crange, 'record_release_apply'))
            stack.enter_context(patch('crange_rollouts.plan_blockers', return_value=[]))
            stack.enter_context(patch('crange_scheduling.begin_apply', side_effect=lambda *a: events.append('begin')))
            stack.enter_context(patch('crange_scheduling.finish_apply', side_effect=lambda *a, **k: events.append('finish')))
            locks = stack.enter_context(patch('crange_scheduling.node_locks', return_value=nullcontext()))
            args = SimpleNamespace(project='100', plan_id=plan_id, release_teams=['1'],
                                   allow_destroy=True, allow_replace=False)
            if fail_stop:
                with self.assertRaisesRegex(CrangeError, 'stop task uncertain'): crange.cmd_apply_saved(args, root)
                self.assertTrue(binary.exists())
            else:
                crange.cmd_apply_saved(args, root)
                self.assertFalse(binary.exists())
            locks.assert_called_once_with({'proxmox1', 'proxmox2'})
        return events

    def test_saved_apply_stops_old_guests_before_replacement_and_locks_old_nodes(self):
        self.assertEqual(self.saved_apply(), ['begin', 'stop', 'apply', 'finish'])

    def test_uncertain_stop_blocks_terraform_replacement_and_preserves_saved_plan(self):
        self.assertEqual(self.saved_apply(fail_stop=True), ['begin', 'stop'])
