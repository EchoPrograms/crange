import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_service import unit_text, cluster_refresh_timer_text


class ServiceTests(unittest.TestCase):
    def test_service_is_unprivileged_and_persistent(self):
        unit = unit_text('/home/crange/crange', 'crange', '/home/crange', '/usr/bin/python3')
        self.assertIn('User=crange\n', unit)
        self.assertIn('Restart=on-failure', unit)
        self.assertIn('WantedBy=multi-user.target', unit)
        self.assertIn('serve --host 0.0.0.0 --port 8000', unit)
        self.assertIn('WorkingDirectory=/home/crange/crange', unit)
        self.assertIn('"/home/crange/crange/scripts/crange.py"', unit)

    def test_compat_service_is_unprivileged_and_uses_fixed_adapter(self):
        unit = unit_text('/repo', 'crange', '/home/crange', '/usr/bin/python3', role='proxmox-compat')
        self.assertIn('User=crange\n', unit)
        self.assertIn('scripts/crange_proxmox_compat.py', unit)
        self.assertIn('NoNewPrivileges=true', unit)
        self.assertNotIn('proxmox.env', unit)

    def test_rejects_unit_directive_injection(self):
        with self.assertRaises(ValueError):
            unit_text('/repo', 'crange\nUser=root', '/home/crange', '/usr/bin/python3')

    def test_worker_is_separate_from_web(self):
        web = unit_text('/home/crange/crange', 'crange-web', '/var/lib/crange-web', '/usr/bin/python3')
        worker = unit_text('/home/crange/crange', 'crange', '/home/crange', '/usr/bin/python3', role='worker')
        self.assertIn('User=crange-web', web)
        self.assertIn('CRANGE_WORKER_EXTERNAL=1', web)
        self.assertIn('User=crange\n', worker)
        self.assertIn('scripts/crange.py" worker', worker)
        self.assertNotIn('CRANGE_WORKER_EXTERNAL=1', worker)

    def test_guacamole_ticket_broker_uses_private_unix_socket(self):
        broker = unit_text('/home/crange/crange', 'crange', '/home/crange', '/usr/bin/python3', role='guac-broker')
        self.assertIn('User=crange\n', broker)
        self.assertIn('CRANGE_GUAC_JSON_KEY=/home/crange/.config/crange/guacamole-json.secret', broker)
        self.assertIn('RuntimeDirectory=crange-guac', broker)
        self.assertIn('RuntimeDirectoryMode=0770', broker)
        self.assertIn('scripts/crange.py" guac-ticket-broker', broker)
        self.assertNotIn('GUACAMOLE_PASSWORD', broker)

    def test_cluster_refresh_is_serialized_and_uses_worker_credentials(self):
        service = unit_text('/home/crange/crange', 'crange', '/home/crange',
                            '/usr/bin/python3', role='cluster-refresh')
        timer = cluster_refresh_timer_text()
        self.assertIn('Type=oneshot', service)
        self.assertIn('User=crange\n', service)
        self.assertIn('scripts/crange.py" cluster-refresh', service)
        self.assertIn('CRANGE_DISTRIBUTION_DATA=/var/lib/crange-distribution', service)
        self.assertNotIn('Restart=on-failure', service)
        self.assertIn('OnUnitInactiveSec=60s', timer)
        self.assertIn('OnBootSec=90s', timer)
        self.assertIn('Unit=crange-cluster-refresh.service', timer)
