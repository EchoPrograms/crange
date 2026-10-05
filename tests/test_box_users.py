import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_box_users as users
from crange_jobs import command

class GuestUsersTests(unittest.TestCase):
    def test_protected_and_injected_names(self):
        for value in ['root','user','daemon','-x','alice;id','Alice','../root']:
            with self.assertRaises(ValueError): users.username(value)
        self.assertEqual(users.username('student01'),'student01')

    def test_ciphertext_bound_to_guest_and_login(self):
        from crange_guac_users import seal_password, open_password
        secret='01'*16
        identity=users.binding('vm','t254-web','student01')
        sealed=seal_password('example-password',secret,3,identity)
        self.assertEqual(open_password(sealed,secret,3,identity),'example-password')
        for project,kind,name,login in [(4,'vm','t254-web','student01'),(3,'container','t254-web','student01'),(3,'vm','t254-other','student01'),(3,'vm','t254-web','student02')]:
            with self.assertRaises(ValueError):open_password(sealed,secret,project,users.binding(kind,name,login))

    def test_guest_ownership_address_and_template_checks(self):
        import crange
        cfg={'scenario_containers': {'t254-web': {'team_id':'254','name':'web','node_name':'proxmox4'}}}
        live={'scenario_containers': {'value': {'t254-web': {'id':999,'address':'10.3.254.20'}}}}
        with patch.object(crange,'project_dir'),patch.object(crange,'json_config',return_value=cfg),patch.object(crange,'output_json',return_value=live),patch.object(crange,'ProxmoxAPI') as api:
            api.return_value.container.return_value={'hostname':'p003t254-web','template':0}
            api.return_value.container_status.return_value='running'
            self.assertEqual(users.resolve(3,'container','t254-web')[0],'10.3.254.20')
            for actual in [{'hostname':'unrelated-guest','template':0},{'hostname':'p003t254-web','template':1}]:
                api.return_value.container.return_value=actual
                with self.assertRaises(ValueError): users.resolve(3,'container','t254-web')
            api.return_value.container.return_value={'hostname':'p003t254-web','template':0}
            live['scenario_containers']['value']['t254-web']['address']='10.4.254.20'
            with self.assertRaises(ValueError): users.resolve(3,'container','t254-web')
            with self.assertRaises(ValueError): users.resolve(3,'container','other')

    def test_queue_accepts_only_fixed_operations_and_ciphertext(self):
        fields={'name':'t254-web','kind':'vm','action':'delete','username':'student01','sealed_password':''}
        self.assertEqual(command('box-users',3,fields)[0],'box-users')
        for mutation in [{'action':'shell'},{'password':'secret'},{'username':'root'},{'sealed_password':'$(id)'}]:
            with self.assertRaises(ValueError):command('box-users',3,{**fields,**mutation})

if __name__=='__main__':unittest.main()
