import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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
                                _configure_applied_access=Mock(), tf_run=Mock(), json_config=Mock(return_value={}))
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

    def test_completed_vm_apply_retries_guest_growth_without_reapplying(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            cfg = {'scenario_boxes': {'web': {'team_id': 254, 'disk_gb': 40}}}
            config = path / 'crange.auto.tfvars.json'; config.write_text(json.dumps(cfg))
            record = {'project': 100, 'plan_id': 'b' * 32, 'applied_at': 'completed',
                      'inputs_sha256': hashlib.sha256(config.read_bytes()).hexdigest(),
                      'automation_inputs_sha256': 'source',
                      'changes': [{'address': 'module.scenario_box["web"].proxmox_virtual_environment_vm.scenario', 'actions': ['update']}]}
            manifest = path / 'receipt.json'; manifest.write_text(json.dumps(record))
            c = SimpleNamespace(CrangeError=ValueError, automation_inputs_digest=Mock(return_value='source'),
                                json_config=Mock(return_value=cfg), _configure_applied_access=Mock(), tf_run=Mock())
            args = SimpleNamespace(project='100', plan_id='b' * 32, staging_only=True)
            with patch('crange_vm_disk_grow.initialize_selected', side_effect=[RuntimeError('guest busy'), []]) as growth:
                with self.assertRaisesRegex(RuntimeError, 'guest busy'):
                    resume(c, args, path, manifest, copy.deepcopy(record))
                self.assertEqual(json.loads(manifest.read_text())['applied_at'], 'completed')
                c._configure_applied_access.assert_not_called()
                resume(c, args, path, manifest, copy.deepcopy(record))
                self.assertEqual(growth.call_count, 2)
                growth.assert_called_with(path, cfg, resource_keys=['vm:web'], team='254')
            c.tf_run.assert_not_called()
            c._configure_applied_access.assert_called_once()
