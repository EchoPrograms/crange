import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from crange_configure_scope import configuration_resources


class ConfigureDiskScopeTests(unittest.TestCase):
    def change(self, before, after, actions=('update',), *, module='scenario_box', kind='vm'):
        resource = {'scenario_box': 'scenario', 'jumpbox': 'jump', 'scenario_container': 'scenario'}[module]
        return {
            'address': f'module.{module}["guest"].proxmox_virtual_environment_{kind}.{resource}',
            'change': {'actions': list(actions), 'before': before, 'after': after},
        }

    def keys(self, item):
        return configuration_resources({'resource_changes': [item]}, {'project_id': 100})

    def test_disk_update_with_only_computed_interface_outputs_skips_guest_configuration(self):
        before = {
            'name': 'p100-golden-guest',
            'cores': 2,
            'network_device': [{'bridge': 'p100t254', 'model': 'virtio'}],
            'disk': [{'interface': 'scsi0', 'size': 32}],
            'ipv4_addresses': [['10.100.254.11']],
            'ipv6_addresses': [[]],
            'mac_addresses': ['BC:24:11:AA:BB:CC'],
            'network_interface_names': ['eth0'],
        }
        after = {
            **before,
            'disk': [{'interface': 'scsi0', 'size': 48}],
            'ipv4_addresses': [['10.100.254.12']],
            'ipv6_addresses': [['fe80::1']],
            'mac_addresses': ['BC:24:11:DD:EE:FF'],
            'network_interface_names': ['ens18'],
        }
        self.assertEqual(self.keys(self.change(before, after)), [])

    def test_disk_update_with_network_input_change_still_selects_vm(self):
        before = {'disk': [{'size': 32}], 'network_device': [{'bridge': 'p100t254'}]}
        after = {'disk': [{'size': 48}], 'network_device': [{'bridge': 'p100t255'}]}
        self.assertEqual(self.keys(self.change(before, after)), ['vm:guest'])

    def test_vm_creation_keeps_existing_configuration_scope(self):
        self.assertEqual(self.keys(self.change(None, {'disk': [{'size': 32}]}, ('create',))), ['vm:guest'])

    def test_missing_update_values_conservatively_keep_configuration_scope(self):
        self.assertEqual(self.keys(self.change({'disk': [{'size': 32}]}, None)), ['vm:guest'])

    def test_non_scenario_guest_types_keep_existing_scope(self):
        item = self.change({'disk': [{'size': 32}]}, {'disk': [{'size': 48}]}, module='jumpbox')
        self.assertEqual(self.keys(item), ['jumpbox:guest'])

        item = self.change({'disk': [{'size': 32}]}, {'disk': [{'size': 48}]},
                           module='scenario_container', kind='container')
        self.assertEqual(self.keys(item), ['container:guest'])


if __name__ == '__main__':
    unittest.main()
