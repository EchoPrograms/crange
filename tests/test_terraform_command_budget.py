from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange


class TerraformCommandBudgetTests(unittest.TestCase):
    def test_only_apply_gets_the_bounded_batch_budget(self):
        for command, expected in [('apply', 43200), ('plan', 1800), ('init', 1800), ('show', 1800), ('output', 1800)]:
            with self.subTest(command=command), \
                 patch('crange_proxmox_compat.reviewed_identity', return_value={'enabled': False}), \
                 patch.object(crange, 'run') as execute:
                crange.tf_run(Path('/project'), [command, '-input=false'])
                self.assertEqual(execute.call_args.kwargs['timeout'], expected)
                if command == 'apply':
                    self.assertIn('exec terraform apply -no-color', execute.call_args.args[0][-1])
