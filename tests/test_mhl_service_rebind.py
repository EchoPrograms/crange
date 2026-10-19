import json,pathlib,sys,tempfile,unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import crange_mhl_services as s

class RestoredServiceRebindTests(unittest.TestCase):
    def run_case(self,fail=False):
        calls=[]
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp);canonical=root/'canonical.json';canonical.write_text('{}')
            secrets=root/'vars.yml';password='x'*32;secrets.write_text(f"mhl_db_runtime_password: '{password}'\nmhl_ci_synthetic_token: '{'y'*32}'\n")
            inventory={'all':{'vars':{'mhl_db_ip':'10.100.100.30'},'children':{'mhl_databases':{'hosts':{'MHL-DB01':{'ansible_host':'10.100.100.30'}}}}}}
            def run(args,**kwargs):
                calls.append(pathlib.Path(args[5]));self.assertEqual(json.loads(pathlib.Path(args[4]).read_text()),inventory)
                return SimpleNamespace(returncode=1 if fail else 0,stdout='failure '+password if fail else 'ok',stderr='')
            with patch.object(s,'_validate_selected',return_value=inventory),patch.object(s,'_private_vars',return_value=secrets),patch.object(s,'_record_profile') as record,patch.object(s,'recipe_digest',return_value='a'*64):
                if fail:
                    with self.assertRaises(s.ServiceConfigurationError) as e:s._configure_business(root,100,run=run,canonical_path=canonical,test_scope={'network_id':100},recipe_root=root/'immutable')
                    self.assertNotIn(password,str(e.exception));self.assertIn('[redacted]',str(e.exception));record.assert_not_called()
                else:
                    s._configure_business(root,100,run=run,canonical_path=canonical,test_scope={'network_id':100},recipe_root=root/'immutable')
                    record.assert_called_once()
            return calls,root

    def test_fixed_rebind_precedes_the_unchanged_archived_recipe(self):
        calls,root=self.run_case();self.assertEqual(calls,[s.ROOT/'ansible/linux/project-100-service-rebind.yml',root/'immutable/ansible/linux/project-100-business.yml'])

    def test_rebind_failure_blocks_recipe_and_profile_and_redacts_secrets(self):
        calls,_=self.run_case(fail=True);self.assertEqual(len(calls),1)
