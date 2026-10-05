import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange

class PlanInputTests(unittest.TestCase):
    def test_approval_and_module_edits_invalidate_reviewed_plan(self):
        for changed in ('approved-container-templates.json','module'):
            with self.subTest(changed=changed),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);project=root/'project';project.mkdir()
                modules=root/'terraform/modules/sample';modules.mkdir(parents=True)
                (project/'crange.auto.tfvars.json').write_text('{}')
                approval=project/'approved-container-templates.json';approval.write_text('{"base":{"template_id":120}}')
                source=modules/'main.tf';source.write_text('terraform {}')
                plans=root/'.local/share/crange/plans/project-003';plans.mkdir(parents=True)
                plan_id='a'*32;plan=plans/(plan_id+'.tfplan');plan.write_bytes(b'binary plan')
                with patch.object(crange,'ROOT',root),patch.object(Path,'home',return_value=root):
                    record={'project':3,'plan_id':plan_id,'created':time.time(),'sha256':hashlib.sha256(plan.read_bytes()).hexdigest(),
                        'inputs_sha256':hashlib.sha256((project/'crange.auto.tfvars.json').read_bytes()).hexdigest(),
                        'automation_inputs_sha256':crange.automation_inputs_digest(project),'changes':[]}
                    (plans/(plan_id+'.json')).write_text(json.dumps(record))
                    if changed=='module':source.write_text('terraform { required_version = ">= 1.0" }')
                    else:approval.write_text('{"base":{"template_id":3000}}')
                    with patch.object(crange,'tf_run') as terraform:
                        with self.assertRaisesRegex(crange.CrangeError,'changed since review'):
                            crange.cmd_apply_saved(SimpleNamespace(project='3',plan_id=plan_id,allow_destroy=True,allow_replace=True),project)
                        terraform.assert_not_called()
                    self.assertTrue(plan.exists())
