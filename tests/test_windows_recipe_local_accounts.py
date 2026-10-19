import json,pathlib,sys,tempfile,unittest
from unittest.mock import patch
from types import SimpleNamespace
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import crange_mhl_services as services

class WindowsRecipeLocalAccountsTests(unittest.TestCase):
    def test_qualified_authentication_names_are_normalized_only_for_local_account_management(self):
        for qualified in ('Administrator',r'MHL-FS01\Administrator','Administrator@mhl.test'):
            self.assertEqual(services._local_admin_username(qualified),'Administrator')
        for bad in ('',None,'../admin','admin;whoami','a'*21):
            with self.assertRaises(services.ServiceConfigurationError):services._local_admin_username(bad)

    def test_archived_recipe_runtime_supplies_each_hosts_local_account_name(self):
        bindings={
            'mhl_domain_controllers':{'inventory_name':'dc','box_key':'dc-key','values':{'crange_guest_id':189,'crange_image_id':'dc-image'}},
            'mhl_file_servers':{'inventory_name':'fs','box_key':'fs-key','values':{'crange_guest_id':188,'crange_image_id':'fs-image'}}}
        credentials={'dc-key':{'username':'Administrator@mhl.test','password':'x'*32},
                     'fs-key':{'username':r'MHL-FS01\FileAdmin','password':'y'*32}}
        captured=[]
        def resolve(ref,**kwargs):return credentials[kwargs['box_key']]
        def run(args,**kwargs):
            captured.append(json.loads(pathlib.Path(args[-1][1:]).read_text()))
            return SimpleNamespace(returncode=0,stdout='ok',stderr='')
        with tempfile.TemporaryDirectory() as tmp,patch.object(services,'_windows_scope_hosts',return_value=({'all':{'children':{}}},bindings)), \
             patch('crange_windows_guest_admin.resolve',side_effect=resolve), \
             patch.object(services,'_windows_forest_vault',return_value=(pathlib.Path(tmp)/'vault',pathlib.Path(tmp)/'password',{'mhl_local_admin_user':'Administrator@mhl.test'})), \
             patch.object(services,'_record_windows_profile'),patch.object(services,'recipe_digest',return_value='a'*64):
            services._configure_windows_inventory(pathlib.Path(tmp),100,{},secrets_root=pathlib.Path(tmp),run=run,test_scope={'network_id':100})
        self.assertEqual(len(captured),3)
        self.assertTrue(all(r['mhl_guest_admin_local_users']=={'dc':'Administrator','fs':'FileAdmin'} for r in captured))
        self.assertTrue(all(r['mhl_local_admin_user']=='{{ mhl_guest_admin_local_users[inventory_hostname] }}' for r in captured))
        self.assertEqual(bindings['mhl_file_servers']['values']['ansible_user'],r'MHL-FS01\FileAdmin')
