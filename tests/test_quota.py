import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange import CrangeError, save_config


class QuotaTests(unittest.TestCase):
    def test_rejects_excess_before_writing_desired_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'quota-policy.json').write_text('''{"teams":2,"custom_networks":2,"jump_boxes":1,"scenario_boxes":2,"scenario_containers":2,"cores":2,"memory_mb":2048}''')
            config = {'project_id': 3, 'team_ids': ['1'], 'jump_boxes': {
                't001-j01': {'cores': 2, 'memory_mb': 1024},
                't001-j02': {'cores': 2, 'memory_mb': 1024}}}
            with self.assertRaisesRegex(CrangeError, 'jump_boxes quota exceeded'):
                save_config(path, config)
            self.assertFalse((path / 'crange.auto.tfvars.json').exists())
