import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange_windows_snapshot_guest as snapshot
from crange_windows_bootstrap import WindowsBootstrapError

class SnapshotGuestTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.target={'project_id':100,'box_key':'release-dc','guest_id':150,'image_id':'p100-image-abc',
                     'node':'proxmox2','credential_ref':{'project_id':100,'box_key':'release-dc'},
                     'network':{'address':'10.100.1.10','gateway':'10.100.1.1'}}
        self.payload={'mac':'AA:BB:CC:DD:EE:FF','old_address':'10.100.254.10','address':'10.100.1.10',
                      'gateway':'10.100.1.1','dns':'10.100.1.10','computer':'MHL-DC01','domain':'mhl.test',
                      'role':5,'admin_username':'Administrator','admin_domain':'mhl.test',
                      'admin_password':'synthetic-private-password'}
        self.env=patch.dict(os.environ,{'CRANGE_WINDOWS_SNAPSHOT_CREDENTIALS':str(self.root/'credentials.json'),
                                       'CRANGE_WINDOWS_SNAPSHOT_INIT_JOURNAL':str(self.root/'jobs.json')})
        self.env.start()
    def tearDown(self):self.env.stop();self.temp.cleanup()
    def terminal(self):
        expected={key:self.payload[key] for key in ('gateway','dns','computer','domain','role')}
        expected.update(ready=True,ipv4=self.payload['address'])
        return {'exited':True,'exitcode':0,'out-data':json.dumps(expected)}

    def test_credentials_freeze_once_and_remain_private_and_source_bound(self):
        with patch('crange_windows_guest_admin.resolve',return_value={'username':'Administrator','password':'old-private-password'}) as admin, \
             patch('crange_windows_guest_rdp.resolve',return_value={'username':'user','password':'private-rdp-password','show_credentials':False}):
            snapshot.capture_credentials(100,'source',128,'base','captured')
            snapshot.capture_credentials(100,'source',128,'base','captured')
            admin.assert_called_once()
            with self.assertRaises(WindowsBootstrapError):snapshot.capture_credentials(101,'source',128,'base','captured')
        saved=json.loads(snapshot.credentials_path().read_text())
        self.assertEqual(saved['captured']['admin']['password'],'old-private-password')
        self.assertEqual(snapshot.credentials_path().stat().st_mode & 0o777,0o600)

    def test_pid_is_saved_before_polling_and_password_is_only_on_stdin(self):
        api=Mock()
        def request(method,path,data=None):
            if method=='POST':return {'pid':25}
            records=json.loads((self.root/'jobs.json').read_text())
            self.assertEqual(next(iter(records.values()))['pid'],25)
            return self.terminal()
        api.request.side_effect=request
        with patch.object(snapshot,'_wait_for_agent'):
            result=snapshot.execute(api,self.target,self.payload)
        self.assertTrue(result['ready'])
        posted=api.request.call_args_list[0].args[2]
        self.assertNotIn(self.payload['admin_password'],posted['command'][-1])
        self.assertEqual(json.loads(posted['input-data'])['admin_password'],self.payload['admin_password'])
        self.assertNotIn(self.payload['admin_password'],(self.root/'jobs.json').read_text())

    def test_observation_timeout_resumes_same_pid_without_another_command(self):
        api=Mock();api.request.side_effect=[{'pid':25},{'exited':False}]
        with patch.object(snapshot,'_wait_for_agent'),patch.object(snapshot.time,'monotonic',side_effect=[0,2]):
            with self.assertRaisesRegex(WindowsBootstrapError,'same handle'):
                snapshot.execute(api,self.target,self.payload,timeout=1)
        api.request.side_effect=None;api.request.return_value=self.terminal()
        with patch.object(snapshot,'_wait_for_agent'):snapshot.execute(api,self.target,self.payload)
        self.assertEqual(sum(call.args[0]=='POST' for call in api.request.call_args_list),1)

    def test_uncertain_submission_is_not_repeated_and_failed_output_is_withheld(self):
        api=Mock();api.request.side_effect=TimeoutError('untrusted secret error')
        with patch.object(snapshot,'_wait_for_agent'):
            for _ in range(2):
                with self.assertRaisesRegex(WindowsBootstrapError,'uncertain') as caught:
                    snapshot.execute(api,self.target,self.payload)
                self.assertNotIn('untrusted secret',str(caught.exception))
        self.assertEqual(api.request.call_count,1)

    def test_snapshot_approval_cannot_cross_project_or_accept_unverified_image(self):
        identity={'identity_mode':'windows-scenario-snapshot-v1','snapshot_project_id':100}
        image={'source_identity':identity,'verified':True,'kind':'vm','approved_projects':[100]}
        with patch('crange_scheduling.read_settings',return_value=({}, {'images':{self.target['image_id']:image}})), \
             patch('crange_windows_snapshot.validate_captured_identity'):
            self.assertEqual(snapshot.snapshot_image(self.target),image)
            image['approved_projects']=[101]
            with self.assertRaises(WindowsBootstrapError):snapshot.snapshot_image(self.target)
        self.assertIsNone(snapshot.snapshot_image({**self.target,'image_id':'generic-windows-base'}))

    def test_credentials_require_one_matching_source_in_immutable_project_release(self):
        box={'release_id':'p100-1.0','release_scope':'test','release_source_name':'dc'}
        source={'name':'dc','image':self.target['image_id'],'key':'source-dc',
                'source_vm_id':128,'source_template':'base','source_image_id':'base-image'}
        release={'project_id':100,'release_id':'p100-1.0','scenario':{'boxes':[source]}}
        with patch('crange.load_release',return_value=release),patch('crange.templates_for',return_value={}):
            self.assertEqual(snapshot.release_source(self.root,{},box,self.target),(source,'base-image'))
            release['project_id']=101
            with self.assertRaises(WindowsBootstrapError):snapshot.release_source(self.root,{},box,self.target)
            release['project_id']=100;source['image']='other-image'
            with self.assertRaises(WindowsBootstrapError):snapshot.release_source(self.root,{},box,self.target)

    def test_member_dns_comes_from_single_controller_in_same_lan_and_release(self):
        target=self.target;member={'team_id':'1','network_id':1,'release_id':'p100-v1'}
        dc={**member,'template':'dc','ip_last_octet':10}
        cfg={'scenario_boxes':{'dc':dc}}
        image={'source_identity':{'domain_role':5,'domain_name':'mhl.test'}}
        with patch.object(snapshot,'managed_source',return_value=False),patch.object(snapshot,'snapshot_image',return_value=image):
            dns=snapshot.domain_dns(target,cfg,member,{'domain_role':3,'domain_name':'mhl.test'},{'dc':{'image_id':'captured'}})
            self.assertEqual(dns,'10.100.1.10')
            dc['team_id']='2'
            with self.assertRaises(WindowsBootstrapError):snapshot.domain_dns(target,cfg,member,{'domain_role':3,'domain_name':'mhl.test'},{'dc':{'image_id':'captured'}})
        self.assertEqual(snapshot.domain_dns(target,{},member,{'domain_role':2},{}),'10.60.2.1')

    def test_script_preserves_accounts_and_checks_identity_mac_credentials_and_real_network(self):
        script=snapshot.script()
        for prohibited in ('Set-LocalUser','New-LocalUser','Rename-Computer','Add-Computer','Install-ADDSForest','Disable-NetFirewallRule'):
            self.assertNotIn(prohibited,script)
        for required in ('$cs.DomainRole','$p.mac','PrefixOrigin','snapshot management credential did not authenticate','Get-DnsClientServerAddress','AddressState','RemoteAddress'):
            self.assertIn(required,script)


class SnapshotInitializationTests(unittest.TestCase):
    def test_captured_domain_controller_keeps_accounts_and_binds_qualified_management_login(self):
        target={'project_id':100,'box_key':'release-dc','guest_id':150,'image_id':'captured',
                'node':'proxmox2','credential_ref':{'project_id':100,'box_key':'release-dc'},
                'network':{'address':'10.100.1.10','gateway':'10.100.1.1'}}
        identity={'computer_name':'MHL-DC01','domain_name':'mhl.test','domain_role':5}
        image={'source_identity':identity}
        frozen={'admin':{'username':'Administrator','password':'private-synthetic-root'},
                'rdp':{'username':'user','password':'private-synthetic-rdp','show_credentials':False}}
        source={'team_id':'254','network_id':254,'ip_last_octet':10}
        with patch.object(snapshot,'_wait_for_agent'),patch('crange_windows_snapshot.collect',return_value=identity), \
             patch('crange_windows_snapshot.verify_clone') as verify, \
             patch.object(snapshot,'image_credentials',return_value=(frozen,source)), \
             patch.object(snapshot,'execute') as execute, \
             patch('crange_windows_guest_admin.store') as admin_store,patch('crange_windows_guest_rdp.store'):
            result=snapshot.initialize(Mock(),target,'AA:BB:CC:DD:EE:FF',{'vmgenid':'new'},path=Path('/project'),cfg={},box={},approvals={},image=image)
        verify.assert_called_once_with(image['source_identity'],identity)
        payload=execute.call_args.args[2]
        self.assertEqual(payload['old_address'],'10.100.254.10')
        self.assertEqual(payload['dns'],'10.100.1.10')
        self.assertEqual(result['admin_login'],'Administrator@mhl.test')
        self.assertNotIn('private-synthetic',json.dumps(result))
        self.assertEqual(admin_store.call_args.args[-2],'Administrator@mhl.test')

if __name__=='__main__':unittest.main()
