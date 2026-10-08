"""Placement intent remains distinct from reviewed physical placement."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_jobs import command
from crange_jumpboxes import creation, preset_settings
from crange_templates import validate
from test_admin_templates import DEFINITION


class DistributionJobTests(unittest.TestCase):
    def test_scenario_job_protocol_lists_are_unique_and_fixed(self):
        base = {'name': 'web', 'team': 0, 'ip': 2, 'network': '', 'template': 'base',
                'cores': 2, 'memory': 2048, 'health': []}
        for operation in ('scenario-add', 'container-add'):
            result = command(operation, 3, {**base, 'guac_protocols': ['ssh', 'rdp', 'vnc']})
            self.assertEqual(result[result.index('--guac-protocols') + 1], '["ssh","rdp","vnc"]')
        edit = {'name':'t254-web', 'ip':2, 'network':'', 'template':'base',
                'node':'auto', 'cores':2, 'memory':2048, 'health':[]}
        for operation in ('scenario-edit', 'container-edit'):
            result = command(operation, 3, {**edit, 'guac_protocols':['rdp','vnc']})
            self.assertEqual(result[result.index('--guac-protocols') + 1], '["rdp","vnc"]')
        # Old callers that send one protocol keep their existing behavior.
        result = command('scenario-add', 3, {**base, 'guac_protocol': 'rdp'})
        self.assertEqual(result[result.index('--guac-protocols') + 1], '["rdp"]')
        for protocols in ([], ['ssh', 'ssh'], ['telnet'], 'ssh'):
            with self.subTest(protocols=protocols), self.assertRaises(ValueError):
                command('scenario-add', 3, {**base, 'guac_protocols': protocols})

    def test_new_guests_default_to_auto_and_soft_preference_is_explicit(self):
        operations = {
            'network-add': {'name': 'lab', 'team': 0, 'id': 100},
            'box-add': {'name': 'staging-j01', 'team': 0, 'network': '', 'cores': 2, 'memory': 2048},
            'scenario-add': {'name': 'web', 'team': 0, 'ip': 2, 'network': '', 'template': 'base', 'cores': 2, 'memory': 2048, 'health': [], 'guac_protocol': 'ssh'},
            'container-add': {'name': 'web', 'team': 0, 'ip': 2, 'network': '', 'template': 'base', 'cores': 2, 'memory': 2048, 'health': [], 'guac_protocol': 'ssh'},
        }
        for operation, options in operations.items():
            with self.subTest(operation=operation):
                original = copy.deepcopy(options)
                result = command(operation, 3, options)
                self.assertEqual(result[result.index('--node') + 1], 'auto')
                if operation=='box-add':self.assertEqual(result[-2:],['--preferred-node','proxmox4'])
                else:self.assertNotIn('--preferred-node', result)
                no_preference=command(operation,3,{**options,'preferred_node':''})
                if operation=='box-add':self.assertEqual(no_preference[-2:],['--preferred-node',''])
                self.assertEqual(options, original)
                preferred = command(operation, 3, {**options, 'node': 'auto', 'preferred_node': 'proxmox2'})
                self.assertEqual(preferred[-2:], ['--preferred-node', 'proxmox2'])
                legacy = command(operation, 3, {**options, 'node': 'proxmox4'})
                self.assertEqual(legacy[legacy.index('--node') + 1], 'proxmox4')
                with self.assertRaises(ValueError):
                    command(operation, 3, {**options, 'node': 'proxmox4', 'preferred_node': 'proxmox2'})
                with self.assertRaises(ValueError):
                    command(operation, 3, {**options, 'preferred_node': 'unapproved'})

    def test_edit_accepts_existing_auto_without_changing_intent(self):
        options = {'name': 't254-web', 'ip': 2, 'network': '', 'template': 'base', 'node': 'auto', 'cores': 2, 'memory': 2048, 'health': [], 'guac_protocol': 'ssh'}
        for operation in ['scenario-edit', 'container-edit']:
            result = command(operation, 3, options)
            self.assertEqual(result[result.index('--node') + 1], 'auto')
            preferred = command(operation, 3, {**options, 'preferred_node': 'proxmox2'})
            self.assertEqual(preferred[-2:], ['--preferred-node', 'proxmox2'])
            automatic = command(operation, 3, {**options, 'preferred_node': ''})
            self.assertEqual(automatic[-2:], ['--preferred-node', ''])
            with self.assertRaises(ValueError):
                command(operation, 3, {**options, 'preferred_node': 'unknown'})

    def test_custom_jumpbox_and_presets_retain_legacy_pins(self):
        box = {'kind': 'vm', 'name': 'desk', 'team': 0, 'template': 'base', 'network': '', 'cores': 2, 'memory': 2048, 'ip': 0, 'display_name': 'Desk', 'services': ['ssh'], 'credential_ref': '', 'sealed_credentials': ''}
        self.assertEqual(creation(box)['node'], 'auto')
        self.assertEqual(creation({**box, 'node': 'auto', 'preferred_node': 'proxmox2'})['preferred_node'], 'proxmox2')
        self.assertEqual(creation({**box, 'node': 'proxmox4'})['node'], 'proxmox4')
        preset = {key: box[key] for key in ['display_name', 'kind', 'template', 'cores', 'memory', 'services']}
        preset['key'] = 'desk'
        self.assertEqual(preset_settings(preset)['node'], 'auto')
        self.assertEqual(preset_settings({**preset, 'node': 'proxmox4'})['node'], 'proxmox4')
        with self.assertRaises(ValueError):
            preset_settings({**preset, 'node': 'proxmox4', 'preferred_node': 'proxmox2'})

    def test_installation_media_builds_remain_physically_scoped(self):
        with self.assertRaises(ValueError):
            validate({**DEFINITION, 'node': 'auto'})
        with self.assertRaises(ValueError):
            validate({**DEFINITION, 'preferred_node': 'proxmox2'})
        with self.assertRaises(ValueError):
            command('template-upload', 0, {'upload_id': 'a' * 32, 'node': 'auto'})
