import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import scripts.crange as crange


class AnsibleLocaleTests(unittest.TestCase):
    def test_ansible_uses_available_c_utf8_locale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / 'ansible/inventory/generated/project-100.json'
            inventory.parent.mkdir(parents=True)
            inventory.write_text(json.dumps({'all': {'children': {
                'project_routers': {'hosts': {'p100_router': {}}}
            }}}))
            with patch.object(crange, 'ROOT', root), \
                    patch.object(crange, 'project_dir', return_value=root), \
                    patch.object(crange, 'run') as run:
                crange.ansible(SimpleNamespace(project=100), 'routers.yml', ['project_routers'])

        environment = run.call_args.kwargs['env']
        self.assertEqual(environment['LC_ALL'], 'C.UTF-8')
        self.assertEqual(environment['LANG'], 'C.UTF-8')
        self.assertEqual(environment['ANSIBLE_TIMEOUT'], '90')


if __name__ == '__main__':
    unittest.main()
