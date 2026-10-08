import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from crange_guac_broker import native_inventory


class _Guac:
    data_source = 'mysql'

    def login(self):
        pass

    def _request(self, method, path):
        if path.endswith('/users'):
            return {'alice': {}}
        if path.endswith('/users/alice/permissions'):
            return {'connectionPermissions': {'1': ['READ'], '2': ['READ'], '3': ['READ']}}
        if path.endswith('/connections'):
            return {'1': {'name': 'live', 'protocol': 'rdp'},
                    '2': {'name': 'stopped', 'protocol': 'ssh'},
                    '3': {'name': 'missing', 'protocol': 'vnc'},
                    '4': {'name': 'not-granted', 'protocol': 'ssh'}}
        if path.endswith('/connections/1/parameters'):
            return {'hostname': '10.60.0.20'}
        if path.endswith('/connections/2/parameters'):
            return {'hostname': '10.60.0.21'}
        if path.endswith('/connections/3/parameters'):
            return {'hostname': '10.60.0.22'}
        return {}


class _Pve:
    def request(self, method, path, fields=None):
        if path == '/cluster/resources':
            return [{'node': 'proxmox4', 'vmid': 114, 'type': 'lxc'},
                    {'node': 'proxmox4', 'vmid': 200, 'type': 'qemu'},
                    {'node': 'proxmox4', 'vmid': 201, 'type': 'qemu'},
                    {'node': 'proxmox4', 'vmid': 202, 'type': 'qemu'}]
        if path.endswith('/lxc/114/config'):
            return {'hostname': 'crange-controller', 'net0': 'name=eth0,ip=10.60.0.20/24'}
        if path.endswith('/lxc/114/status/current'):
            return {'status': 'running'}
        if path.endswith('/qemu/200/config'):
            return {'name': 'stopped-box', 'ipconfig0': 'ip=10.60.0.21/24'}
        if path.endswith('/qemu/200/status/current'):
            return {'status': 'stopped'}
        if path.endswith('/qemu/201/config'):
            return {'name': 'template', 'template': 1, 'ipconfig0': 'ip=10.60.0.22/24'}
        if path.endswith('/qemu/202/config'):
            return {'name': 'cache-source', 'tags': 'crange-image;crange-cache',
                    'ipconfig0': 'ip=10.60.0.22/24'}
        return {}


class NativeInventoryTests(unittest.TestCase):
    def test_uses_current_guac_grants_and_unique_live_guest_addresses(self):
        with patch('crange_guac.GuacamoleClient', return_value=_Guac()), \
                patch('crange.ProxmoxAPI', return_value=_Pve()):
            rows = native_inventory('alice', 'mysql')
        self.assertEqual([row['identifier'] for row in rows], ['1', '2', '3'])
        self.assertEqual((rows[0]['hostname'], rows[0]['guest_name'], rows[0]['state']),
                         ('10.60.0.20', 'crange-controller', 'running'))
        self.assertEqual((rows[1]['guest_matched'], rows[1]['state']), (True, 'stopped'))
        self.assertEqual((rows[2]['guest_matched'], rows[2]['state']), (False, 'unmanaged'))

    def test_rejects_identity_without_guacamole_account(self):
        guac = _Guac()
        with patch('crange_guac.GuacamoleClient', return_value=guac), \
                patch('crange.ProxmoxAPI') as pve:
            with self.assertRaisesRegex(Exception, 'account not found'):
                native_inventory('mallory', 'mysql')
        pve.assert_not_called()

    def test_rejects_other_authenticated_data_source(self):
        with patch('crange_guac.GuacamoleClient', return_value=_Guac()), \
                patch('crange.ProxmoxAPI') as pve:
            with self.assertRaisesRegex(Exception, 'data source is unavailable'):
                native_inventory('alice', 'other')
        pve.assert_not_called()


if __name__ == '__main__':
    unittest.main()
