"""Execute the guest's actual disk recovery helper with measured free space."""
import ast
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_linux_jump_initialize import guest_script


class LinuxJumpGrowTests(unittest.TestCase):
    def helper(self):
        function = next(node for node in ast.parse(guest_script()).body
                        if isinstance(node, ast.FunctionDef) and node.name == 'ensure_disk_space')
        namespace = {'run': Mock()}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'guest-disk-grow', 'exec'), namespace)
        return namespace['ensure_disk_space'], namespace['run']

    @staticmethod
    def measurement(gib):
        return SimpleNamespace(f_bavail=gib * 2**30, f_frsize=1)

    def test_finishes_growth_after_the_clone_already_completed_first_boot(self):
        grow, run = self.helper()
        with patch('os.statvfs', side_effect=[self.measurement(5), self.measurement(21)]):
            self.assertEqual(grow(), 21)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ['cloud-init', 'single', '--name', 'growpart', '--frequency', 'always'],
            ['cloud-init', 'single', '--name', 'resizefs', '--frequency', 'always']])

    def test_ready_disk_is_unchanged_on_retry(self):
        grow, run = self.helper()
        with patch('os.statvfs', return_value=self.measurement(21)):
            self.assertEqual(grow(), 21)
        run.assert_not_called()

    def test_insufficient_space_after_expansion_cannot_be_reported_ready(self):
        grow, _ = self.helper()
        with patch('os.statvfs', return_value=self.measurement(5)):
            with self.assertRaisesRegex(RuntimeError, 'at least 16 GiB'):
                grow()
