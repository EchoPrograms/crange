import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange_linux_jump_initialize as module


class LinuxJumpInitializeTests(unittest.TestCase):
    def fixture(self):
        placement={'node_name':'proxmox6','image_id':'parrot','template_id':169,'template_node':'proxmox6'}
        cfg={'team_ids':['6'],'jump_boxes':{'linux':{'team_id':'6','jump_number':1,'image':'parrot'}}}
        outputs={'jump_boxes':{'value':{'linux':{'id':292,'address':'10.100.6.11','network_vnet':'p100t006','placement':placement}}}}
        applied={'jumpbox:linux':{'guest_id':292,**placement}}
        catalog={'images':{'parrot':{'verified':True,'kind':'vm','init_system':'systemd','approved_projects':[100],
            'artifact_sha256':'abc','replicas':[{'state':'ready','node':'proxmox6','template_id':169,'artifact_sha256':'abc'}]}}}
        resource={'vmid':292,'node':'proxmox6','type':'qemu','pool':'crange-iac','name':'p100t006-jump-01','status':'running'}
        native={'name':resource['name'],'ostype':'l26','tags':'jumpbox;terraform;p100;p100t006',
                'net0':'virtio=BC:24:11:52:E8:80,bridge=p100t006','ipconfig0':'gw=10.100.6.1,ip=10.100.6.11/24'}
        api=Mock();api.request.side_effect=lambda method,path: [resource] if path.startswith('/cluster') else native
        return cfg,outputs,applied,catalog,api,native

    def test_explicit_disk_must_match_owned_native_capacity(self):
        cfg,outputs,applied,catalog,api,native=self.fixture()
        cfg['jump_boxes']['linux']['disk_gb']=48
        native['scsi0']='size=51539607552,file=local-lvm:vm-292-disk-0'
        result=module.targets(Path('/repo/project-100'),['linux'],cfg=cfg,outputs=outputs,applied=applied,catalog=catalog,api=api)
        self.assertEqual(result[0]['disk_gb'],48)
        for value in ('local-lvm:vm-292-disk-0,size=32G','local-lvm:vm-293-disk-0,size=48G'):
            native['scsi0']=value
            with self.assertRaisesRegex(ValueError,'disk differs'):
                module.targets(Path('/repo/project-100'),['linux'],cfg=cfg,outputs=outputs,applied=applied,catalog=catalog,api=api)
        self.assertIn('16*2**30',module.guest_script())

    def test_approved_applied_native_generation_derives_fixed_network(self):
        cfg,outputs,applied,catalog,api,native=self.fixture()
        result=module.targets(Path('/project-100'),['linux'],cfg=cfg,outputs=outputs,applied=applied,catalog=catalog,api=api)
        self.assertEqual(result,[{'key':'linux','node':'proxmox6','guest_id':292,'mac':'BC:24:11:52:E8:80','address':'10.100.6.11','gateway':'10.100.6.1'}])
        self.assertTrue(all(c.args[0]=='GET' for c in api.request.call_args_list))

    def test_foreign_image_generation_network_or_template_refuses_writes(self):
        for mutation in ('image','replica','generation','pool','vnet','address','mac','template','extra_nic'):
            cfg,outputs,applied,catalog,api,native=self.fixture()
            if mutation=='image':catalog['images']['parrot']['approved_projects']=[3]
            if mutation=='replica':catalog['images']['parrot']['replicas'][0]['artifact_sha256']='other'
            if mutation=='generation':applied['jumpbox:linux']['guest_id']=123
            if mutation=='pool':api.request.side_effect=lambda m,p: [{'vmid':292,'pool':'other'}] if p.startswith('/cluster') else native
            if mutation=='vnet':native['net0']='virtio=BC:24:11:52:E8:80,bridge=p100t005'
            if mutation=='address':native['ipconfig0']='gw=10.100.6.1,ip=10.100.6.12/24'
            if mutation=='mac':native['net0']='virtio=invalid,bridge=p100t006'
            if mutation=='template':native['template']=1
            if mutation=='extra_nic':native['net1']='virtio=BC:24:11:52:E8:81,bridge=craccess'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                module.targets(Path('/project-100'),['linux'],cfg=cfg,outputs=outputs,applied=applied,catalog=catalog,api=api)
            self.assertTrue(all(c.args[0]=='GET' for c in api.request.call_args_list))

    def test_inventory_excludes_stopped_boxes_but_rejects_unmapped_or_ambiguous_hosts(self):
        cfg={'jump_boxes':{'ready':{'started':True},'stopped':{'started':False}}}
        outputs={'jump_boxes':{'value':{'ready':{'address':'10.100.6.11'},'stopped':{'address':'10.100.6.13'}}}}
        hosts={'one':{'ansible_host':'10.100.6.11'},'two':{'ansible_host':'10.100.6.13'}}
        with patch('crange.json_config',return_value=cfg),patch('crange.output_json',return_value=outputs),patch.object(module,'initialize') as initialize:
            module.initialize_inventory(Path('/project-100'),hosts)
            initialize.assert_called_once_with(Path('/project-100'),['ready']);initialize.reset_mock()
            for bad in ({'foreign':{'ansible_host':'10.100.1.11'}},{'missing':{}}):
                with self.assertRaisesRegex(ValueError,'unique declared'):
                    module.initialize_inventory(Path('/project-100'),bad)
                initialize.assert_not_called()
            outputs['jump_boxes']['value']['duplicate']={'address':'10.100.6.11'}
            with self.assertRaisesRegex(ValueError,'unique declared'):
                module.initialize_inventory(Path('/project-100'),hosts)
            initialize.assert_not_called()

    def test_team_initialization_prevalidates_scope_and_skips_stopped_or_removed_boxes(self):
        cfg={'jump_boxes':{'ready':{'team_id':6},'stopped':{'team_id':6,'started':False},'foreign':{'team_id':5}}}
        with patch.object(module,'initialize') as initialize:
            module.initialize_selected(Path('/project-100'),cfg,['container:app','jumpbox:removed','jumpbox:ready','jumpbox:stopped'],6)
            initialize.assert_called_once_with(Path('/project-100'),['ready']);initialize.reset_mock()
            with self.assertRaisesRegex(ValueError,'another team'):
                module.initialize_selected(Path('/project-100'),cfg,['jumpbox:ready','jumpbox:foreign'],6)
            initialize.assert_not_called()

    def test_guest_repairs_only_matching_cloud_init_profile_and_rejects_mismatch(self):
        script=module.guest_script()
        compile(script,'guest-bootstrap','exec')
        # Execute the real guest payload against a modeled stale DHCP boot.
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);net=root/'net'/'ens18';net.mkdir(parents=True);(net/'address').write_text('bc:24:11:52:e8:80')
            profiles=root/'profiles';profiles.mkdir()
            (profiles/'cloud-init.nmconnection').write_text('[user]\norg.freedesktop.NetworkManager.origin=cloud-init\n[ethernet]\nmac-address=BC:24:11:52:E8:80\n[ipv4]\nmethod=manual\naddress1=10.100.6.11/24\ngateway=10.100.6.1\n[connection]\nuuid=owned-profile\n')
            original_path=Path
            def mapped(value):
                return net.parent if value=='/sys/class/net' else profiles if value=='/etc/NetworkManager/system-connections' else original_path(value)
            events=[];state={'active':False}
            def run(args,**kwargs):
                events.append(args)
                if args[:2]==['cloud-init','status']:out='{"status":"done","errors":[]}'
                elif args[:4]==['ip','-j','-4','addr']:out=json.dumps([{'addr_info':[{'local':'10.100.6.11' if state['active'] else '10.100.6.110','scope':'global'}]}])
                elif args[:3]==['ip','-j','-4']:out='[{"gateway":"10.100.6.1"}]'
                elif args[:2]==['nmcli','-g']:out='owned-profile' if state['active'] else 'dhcp'
                else:out=''
                if args[:3]==['nmcli','connection','up']:state['active']=True
                return Mock(stdout=out,returncode=2 if args[:2]==['cloud-init','status'] else 0)
            import io,contextlib
            payload={'mac':'BC:24:11:52:E8:80','address':'10.100.6.11','gateway':'10.100.6.1'}
            with patch('pathlib.Path',side_effect=mapped),patch('subprocess.run',side_effect=run),patch('sys.stdin',io.StringIO(json.dumps(payload))),contextlib.redirect_stdout(io.StringIO()) as out:
                exec(script,{})
            self.assertTrue(json.loads(out.getvalue())['network_changed'])
            self.assertIn(['systemctl','add-wants','multi-user.target','cloud-init.target'],events)
            self.assertIn(['nmcli','connection','up','uuid','owned-profile','ifname','ens18'],events)
            payload['address']='10.100.6.12';events.clear()
            with patch('pathlib.Path',side_effect=mapped),patch('subprocess.run',side_effect=run),patch('sys.stdin',io.StringIO(json.dumps(payload))),self.assertRaisesRegex(RuntimeError,'No unique'):
                exec(script,{})
            self.assertFalse(any(e[:3]==['nmcli','connection','up'] for e in events))
