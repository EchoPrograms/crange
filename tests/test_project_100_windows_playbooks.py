import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class Project100WindowsPlaybookTests(unittest.TestCase):
    def test_domain_play_is_scoped_to_project_networks(self):
        source = (ROOT / 'ansible/windows/project-100-domain.yml').read_text()
        match = re.search(r"ansible_host is match\('(.*?)'\)", source)
        self.assertIsNotNone(match)
        scope = re.compile(match.group(1))
        for address in ('10.100.0.10', '10.100.5.10', '10.100.254.10'):
            self.assertIsNotNone(scope.match(address), address)
        for address in ('10.100.6.10', '10.100.10.10', '10.101.1.10'):
            self.assertIsNone(scope.match(address), address)

    def test_playbooks_rotate_each_guest_and_model_scoped_ad_mistake(self):
        domain = (ROOT / 'ansible/windows/project-100-domain.yml').read_text()
        members = (ROOT / 'ansible/windows/project-100-members.yml').read_text()
        self.assertGreaterEqual(
            (domain + members).count('mhl_guest_admin_passwords[inventory_hostname]'), 4
        )
        self.assertIn("name: WarehouseDeployers", domain)
        self.assertIn("name: svc_warehouse_deploy", domain)
        self.assertIn("-TargetName 'MHL\\WarehouseDeployers'", domain)
        self.assertIn('-PermissionLevel GpoEdit', domain)
        self.assertIn("$root = 'DC=mhl,DC=test'", domain)
        self.assertIn('New-GPLink -Name $name -Target $root', domain)
        self.assertNotIn('Domain Admins', domain)
        self.assertNotIn('SMB signing', domain + members)
        self.assertIn(r'MHL\MHL-Employees', members)

    def test_rdp_and_winrm_firewall_sources_are_split(self):
        members = (ROOT / 'ansible/windows/project-100-members.yml').read_text()
        baseline = (ROOT / 'ansible/windows/crange-guests.yml').read_text()
        for source in (members, baseline):
            self.assertIn('10.60.0.10', source)
            self.assertIn('10.60.0.20', source)
        self.assertIn("Name = 'RemoteDesktop*'", baseline)
        self.assertIn("Name = 'WINRM*'", baseline)
        self.assertNotIn('DisplayGroup', baseline)
        self.assertNotIn('mhl_admin_sources', members)
        self.assertLess(members.index('Enable-PSRemoting -Force'),
                        members.index('Set-NetFirewallAddressFilter -RemoteAddress'))


if __name__ == '__main__':
    unittest.main()
