from pathlib import Path
import sys, json, tempfile, unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange_templates as t
import crange_template_media as media
import crange_guac_broker as broker
from crange_jobs import command

DEFINITION={'name':'custom-windows-v1','node':'proxmox4','iso':'local:iso/windows.iso','driver_iso':'local:iso/virtio.iso',
            'os':'windows','cores':2,'memory':4096,'disk':40,'disk_bus':'sata','bios':'ovmf','machine':'q35',
            'cpu':'host','nic_models':['e1000','virtio'],'agent':False,'cloud_init':False,'tpm':True}

class AdminTemplateTests(unittest.TestCase):
    def test_typed_jobs_reject_arbitrary_options(self):
        self.assertEqual(command('template-create',0,{'definition':DEFINITION})[:1],['template-create'])
        for patch_value in ({'node':'outside-range'},{'iso':'local:iso/../../etc/passwd.iso'}, {'nic_models':['virtio,bridge=vmbr0']}, {'memory':True}, {'tpm':'yes'}, {'cores':999}, {'shell':'touch /root/hacked'}):
            with self.subTest(patch=patch_value), self.assertRaises(ValueError):t.validate({**DEFINITION,**patch_value})
        with self.assertRaises(ValueError):command('plan',0,{})

    def test_installer_uses_only_range_pool_and_build_network(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(t,'BUILDS',Path(directory)):
            api=Mock()
            resources=[]
            def request(method,path,fields=None):
                if path.endswith('/content'):return [{'volid':DEFINITION['iso'],'size':4096},{'volid':DEFINITION['driver_iso'],'size':4096}]
                if path=='/cluster/sdn/vnets':return [{'vnet':'craccess','zone':'crange'}]
                if path=='/cluster/resources':return list(resources)
                if path=='/cluster/nextid':return '3333'
                if method=='POST' and path.endswith('/qemu'):
                    resources.append({'vmid':3333,'node':'proxmox4','type':'qemu','name':'crange-build-custom-windows-v1'})
                return 'UPID:created'
            api.request.side_effect=request
            api.vm.side_effect=lambda *args:{'name':'crange-build-custom-windows-v1','tags':next(c.args[2]['tags'] for c in api.request.call_args_list if c.args[0]=='POST')}
            record=t.create(DEFINITION,api)
            fields=next(c.args[2] for c in api.request.call_args_list if c.args[0]=='POST')
            self.assertEqual(fields['pool'],'crange-iac')
            self.assertEqual(fields['net0'],'e1000,bridge=craccess,firewall=1')
            self.assertEqual(fields['net1'],'virtio,bridge=craccess,firewall=1')
            self.assertEqual(fields['sata0'],'local-lvm:40')
            self.assertIn('tpmstate0',fields)
            self.assertEqual(t.load(record['id'])['status'],'installing')

    def test_publish_requires_stopped_owned_guest_and_preserves_other_approvals(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(t,'BUILDS',Path(directory)/'builds'):
            root=Path(directory)/'project';root.mkdir()
            approved={'existing':{'template_id':112,'template_node':'proxmox4'}}
            (root/'approved-scenario-templates.json').write_text(json.dumps(approved))
            record={'id':'a'*32,'definition':DEFINITION,'vmid':3333,'guest_name':'crange-build-custom-windows-v1','ownership_tag':'crange-build-'+'a'*32,'status':'installing'};t.save(record)
            api=Mock();api.request.return_value=[{'vmid':3333,'node':'proxmox4','type':'qemu'}];api.vm.return_value={'name':record['guest_name'],'tags':'crange-template-build;'+record['ownership_tag'],'template':0};api.status.return_value='running'
            with patch('crange.project_dir',return_value=root):
                with self.assertRaisesRegex(ValueError,'Shut down'):t.publish(record['id'],[3],True,api)
                self.assertFalse(any(call.args[0] in ('PUT','POST') for call in api.request.call_args_list))
                api.status.return_value='stopped';api.vm.side_effect=[{'name':record['guest_name'],'tags':'crange-template-build;'+record['ownership_tag'],'template':0},{'name':record['guest_name'],'tags':'crange-template-build;'+record['ownership_tag'],'template':1}]
                t.publish(record['id'],[3],True,api)
            result=json.loads((root/'approved-scenario-templates.json').read_text());self.assertEqual(result['existing'],approved['existing'])
            self.assertFalse(result['custom-custom-windows-v1']['profile']['cloud_init'])
            self.assertEqual(result['custom-custom-windows-v1']['profile']['boot_order'],['sata0'])
            api.vm.side_effect=None;api.vm.return_value={'name':'unrelated-server','tags':'crange-template-build;'+record['ownership_tag']}
            with self.assertRaisesRegex(ValueError,'identity'):t.owned(api,record)

    def test_stale_build_vmid_reused_by_library_container_is_not_linked(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(t,'BUILDS',Path(directory)):
            record={'id':'b'*32,'definition':DEFINITION,'vmid':145,'guest_name':'crange-build-validation-builder',
                    'ownership_tag':'crange-build-'+'b'*32,'status':'installing'}
            t.save(record)
            api=Mock()
            def request(method,path,fields=None):
                if path.endswith('/content'):return []
                if path=='/cluster/resources':return [{'vmid':145,'node':'proxmox5','type':'lxc','name':'crange-library'}]
                return []
            api.request.side_effect=request
            result=t.options(api)
            build=result['builds'][0]
            self.assertFalse(build['console_available'])
            self.assertIsNone(build['console_node'])
            self.assertIn('missing or no longer matches',build['error'])
            self.assertEqual(json.loads((Path(directory)/(record['id']+'.json')).read_text())['vmid'],145)
            api.vm.assert_not_called()
            with patch('crange.project_dir',return_value=Path(directory)/'project'), self.assertRaisesRegex(ValueError,'missing or no longer matches'):
                t.publish(record['id'],[3],True,api)
            api.vm.assert_not_called()
            self.assertTrue((Path(directory)/(record['id']+'.json')).exists())

    def test_proxmox_console_uses_public_origin_and_authenticated_gui_route(self):
        self.assertEqual(broker.public_proxmox_origin(''), 'https://proxmox.brogano.dev')
        self.assertEqual(broker.public_proxmox_origin('https://PVE.example.test/'), 'https://pve.example.test')
        self.assertEqual(broker.proxmox_console_url('https://proxmox.brogano.dev',145),
                         'https://proxmox.brogano.dev/#v1:0:=qemu%2F145:4:::::=console')
        for invalid in ('http://pve.example.test','https://pve.example.test:8006','https://user@pve.example.test',
                        'https://127.0.0.1','https://pve.example.test/path','https://proxmox'):
            with self.subTest(invalid=invalid),self.assertRaisesRegex(Exception,'public HTTPS origin'):
                broker.public_proxmox_origin(invalid)

    def test_iso_upload_offsets_ownership_limits_and_completion(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(media,'SPOOL',Path(directory)):
            created=media.begin('admin','my-os.iso',6);upload=created['upload_id']
            self.assertEqual(media.chunk('admin',upload,0,b'abc'),{'offset':3})
            for actor,offset,data in [('other',3,b'def'),('admin',0,b'def'),('admin',3,b'toolong')]:
                with self.assertRaises(ValueError):media.chunk(actor,upload,offset,data)
            with self.assertRaises(ValueError):media.finish('admin',upload)
            media.chunk('admin',upload,3,b'def');self.assertEqual(media.finish('admin',upload)['status'],'ready')
            with self.assertRaises(ValueError):media.begin('admin','../../bad.iso',6)
            self.assertEqual(media.pending('admin')[0]['status'],'ready')
            with self.assertRaises(ValueError):media.cancel('other',upload)
            media.cancel('admin',upload);self.assertEqual(media.pending('admin'),[])
            self.assertEqual(t.validate({**DEFINITION,'iso':'local:iso/WINDOWS.ISO'})['os'],'windows')

if __name__=='__main__':unittest.main()
