import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_service import unit_text


class ServiceTests(unittest.TestCase):
    def test_service_is_unprivileged_and_persistent(self):
        unit = unit_text('/home/crange/crange', 'crange', '/home/crange', '/usr/bin/python3')
        self.assertIn('User=crange\n', unit)
        self.assertIn('Restart=on-failure', unit)
        self.assertIn('WantedBy=multi-user.target', unit)
        self.assertIn('serve --host 0.0.0.0 --port 8000', unit)
        self.assertIn('WorkingDirectory=/home/crange/crange', unit)
        self.assertIn('"/home/crange/crange/scripts/crange.py"', unit)

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
