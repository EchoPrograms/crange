import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_apply_resume import resume


class ApplyResumeTests(unittest.TestCase):
    def test_completed_apply_resumes_only_its_reviewed_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = path / 'crange.auto.tfvars.json'; config.write_text('{}')
            record = {'project': 100, 'plan_id': 'a' * 32, 'applied_at': 'completed',
                      'inputs_sha256': hashlib.sha256(config.read_bytes()).hexdigest(),
                      'automation_inputs_sha256': 'source',
                      'changes': [{'address': 'module.jumpbox["t001-j01"].proxmox_virtual_environment_vm.jump', 'actions': ['create']}]}
            c = SimpleNamespace(CrangeError=ValueError, automation_inputs_digest=Mock(return_value='source'),
                                _configure_applied_access=Mock(), tf_run=Mock())
            args = SimpleNamespace(project='100', plan_id='a' * 32)
            manifest = path / 'receipt.json'
            resume(c, args, path, manifest, copy.deepcopy(record))
            expected = {'resource_changes': [{'address': record['changes'][0]['address'], 'change': {'actions': ['create']}}]}
            c._configure_applied_access.assert_called_once_with(args, expected)
            c.tf_run.assert_not_called()
            self.assertIn('configuration_completed_at', json.loads(manifest.read_text()))
            for key, value in [('project', 99), ('inputs_sha256', 'changed'),
                               ('automation_inputs_sha256', 'changed'), ('applied_at', None),
                               ('changes', [{'address': 'other', 'actions': ['delete']}])]:
                c._configure_applied_access.reset_mock()
                invalid = {**record, key: value}
                with self.subTest(key=key), self.assertRaises(ValueError):
                    resume(c, args, path, manifest, invalid)
                c._configure_applied_access.assert_not_called()
                c.tf_run.assert_not_called()
