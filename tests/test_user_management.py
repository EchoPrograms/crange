import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from crange_auth import AuthStore
from crange_guac_users import ProjectUsers,seal_password
from crange_user_management import expand_format,manage_users,preferences,save_preferences,user_stats
from test_guac_users import FakeGuacamole
from crange_guac_users import apply_users

class UserManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.auth=AuthStore(self.root/'auth.sqlite3')
        self.records=ProjectUsers(self.auth);self.cfg={'project_id':3,'team_ids':['1','2'],'jump_boxes':{}}
        self.secret='a1'*16

    def test_formats_support_padding_and_refuse_expression_access(self):
        self.assertEqual(expand_format('comp{user-number:02d}',3,1,1),'comp01')
        self.assertEqual(expand_format('p{project-number:03d}-t{team-number}u{user-number}',3,254,4),'p003-t0u4')
        for bad in ['{user-number.real}','{user-number[0]}','{user-number!r}','{unknown}','{user-number:999d}']:
            with self.assertRaises(ValueError):expand_format(bad,3,1,1)

    def test_six_character_password_explicit_logins_unique_across_projects(self):
        sealed=seal_password('comp01',self.secret,3,'comp01')
        self.records.create(3,'comp01',sealed,[],[],login='comp01')
        with self.assertRaisesRegex(ValueError,'already exists'):
            self.records.create(4,'COMP01',sealed,[],[],login='COMP01')
        self.assertFalse(self.records.list(3)[0]['allow_password_reset'])
        with self.assertRaises(ValueError):seal_password('short',self.secret,3,'comp02')

    def test_bulk_database_collision_rolls_back_entire_batch(self):
        from unittest.mock import Mock
        sealed=seal_password('comp01',self.secret,3,'comp01')
        self.records.create(3,'already',sealed,[],[],login='comp02')
        jobs=Mock()
        with patch('crange_user_management.available_logins'),patch('crange_user_management.request_broker',return_value={'users':[{'sealed_password':sealed},{'sealed_password':sealed}]}):
            with self.assertRaises(sqlite3.IntegrityError):
                manage_users(self.auth,jobs,'manager','session',3,{'action':'bulk-create','count':2,'start_number':1,'team':1,'username_template':'comp{user-number:02d}'},self.cfg)
        self.assertEqual([r['login'] for r in self.records.list(3)],['comp02'])
        jobs.submit.assert_not_called()

    def test_mass_delete_cannot_delete_users_from_another_project(self):
        from unittest.mock import Mock
        sealed=seal_password('comp01',self.secret,3,'comp01')
        self.records.create(3,'first',sealed,[],[]);self.records.create(4,'other',sealed,[],[])
        with self.assertRaises(ValueError):
            manage_users(self.auth,Mock(),'manager','session',3,{'action':'delete-many','usernames':['first','other']},self.cfg)
        self.assertEqual(len(self.records.list(3)),1)

    def test_password_change_permission_lock_and_rename_preserve_ownership(self):
        sealed=seal_password('comp01',self.secret,3,'user1')
        self.records.create(3,'user1',sealed,[],[],login='comp01',allow_password_reset=True)
        client=FakeGuacamole()
        apply_users(3,self.auth,client,self.root,self.secret)
        self.assertEqual(client.permissions['comp01']['userPermissions'],{'comp01':['UPDATE']})
        self.records.update(3,'user1',teams=[],connections=[],enabled=False,allow_password_reset=False)
        apply_users(3,self.auth,client,self.root,self.secret)
        self.assertTrue(client.users['comp01']['disabled'])
        self.assertEqual(client.permissions['comp01']['userPermissions'],{})
        self.records.update(3,'user1',teams=[],connections=[],enabled=True,login='renamed')
        apply_users(3,self.auth,client,self.root,self.secret)
        self.assertIn('renamed',client.users);self.assertNotIn('comp01',client.users)
        self.assertIn('unrelated',client.users)
        self.assertEqual(client.users['renamed']['password'],'comp01')

    def test_preferences_hide_password_template_and_keep_in_external_db(self):
        with patch('crange_user_management.request_sealed_password',return_value='encrypted-pattern'):
            value=save_preferences(self.auth,'session',3,{'display_name':'Training Lab','username_template':'comp{user-number:02d}','password_template':'comp{user-number:02d}','jumpboxes':[]},self.cfg)
        self.assertEqual(value['display_name'],'Training Lab');self.assertTrue(value['password_template_set'])
        self.assertNotIn('sealed_password_template',value)
        self.assertEqual(preferences(self.auth,3,private=True)['sealed_password_template'],'encrypted-pattern')

    def test_user_stats_filter_foreign_users_and_connections(self):
        from unittest.mock import Mock
        self.records.create(3,'one',seal_password('comp01',self.secret,3,'one'),[],[],login='comp01')
        side=self.root/'guacamole/access/project-003.applied.json';side.parent.mkdir(parents=True)
        side.write_text(json.dumps({'connections':[{'identifier':'42','name':'Owned jump box'}]}))
        client=Mock(data_source='postgresql')
        client._request.side_effect=[{'lastActive':123},[{'startDate':123,'endDate':456}],
            {'a':{'username':'comp01','connectionIdentifier':'42','startDate':321},
             'b':{'username':'other','connectionIdentifier':'42'},
             'c':{'username':'comp01','connectionIdentifier':'99'}}]
        stats=user_stats(self.auth,3,'one',client,self.root)
        self.assertEqual(len(stats['active_connections']),1)
        self.assertEqual(stats['last_login'],123)
        self.assertIsNone(stats['failed_login_attempts'])

    def test_default_multi_jumpbox_setup_allocates_addresses_without_mutating_live_config(self):
        from crange_user_management import plan_boxes
        cfg={'project_id':3,'team_ids':['1'],'jump_boxes':{'existing-j01':{'team_id':'1','jump_number':1,'node_name':'proxmox4'}},
             'scenario_boxes':{'web':{'team_id':'1','ip_last_octet':12}},'scenario_containers':{},'custom_networks':{}}
        users=[{'user_number':1,'home_team':1,'connections':[]},{'user_number':2,'home_team':1,'connections':[]}]
        spec={'node':'proxmox4','network':'','cores':2,'memory':2048}
        def directory(project):
            self.assertIsInstance(project,str)
            return self.root
        with patch('crange.project_dir',side_effect=directory),patch('crange.validate_project_quota') as quota:
            boxes=plan_boxes(cfg,users,[spec,spec])
        self.assertEqual([int(b['name'][-2:])+10 for b in boxes],[13,14,15,16])
        self.assertTrue(all(len(u['connections'])==2 for u in users))
        self.assertEqual(list(cfg['jump_boxes']),['existing-j01'])
        self.assertEqual(len(quota.call_args.args[1]['jump_boxes']),5)

    def test_default_setup_refuses_a_custom_network_owned_by_another_team(self):
        from crange_user_management import plan_boxes
        cfg={'project_id':3,'team_ids':['1','2'],'jump_boxes':{},'custom_networks':{'private':{'network_id':100,'owner_team_id':'2'}}}
        with self.assertRaisesRegex(ValueError,'another team'):
            plan_boxes(cfg,[{'user_number':1,'home_team':1}],[{'node':'proxmox4','network':'private','cores':2,'memory':2048}])
