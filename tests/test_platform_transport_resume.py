import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_capture as c
from crange_distribution import DistributionError


class PlatformResumeTests(unittest.TestCase):
    def fixture(self, root):
        source, node, vmid, temp = 'proxmox4', 'proxmox6', 175, 21000
        allocation = {'operation': 'source_probe', 'project_id': 1,
            'image': 'source:proxmox4:qemu:3015', 'artifact_sha256': 'a' * 64,
            'resolved': {'node_name': node, 'datastore_id': 'local-hdd'}}
        args = dict(project=1, job='capture-p001-123456abcdef', source_node=source,
            source_id=3015, source_sha='a' * 64, node=node, storage='local-hdd',
            name='crange-debian-base', slug='123456abcdef', bridge='isolated',
            identity_path=root/'identity.json')
        steps = [(source, f'/nodes/{source}/vzdump', 'vzdump', 3015),
            (node, f'/nodes/{node}/qemu', 'qmrestore', temp),
            (node, f'/nodes/{node}/qemu/{temp}/clone', 'qmclone', temp),
            (node, f'/nodes/{node}/qemu/{temp}', 'qmdestroy', temp),
            (node, f'/nodes/{node}/qemu/{vmid}/status/start', 'qmstart', vmid),
            (node, f'/nodes/{node}/qemu/{vmid}/status/shutdown', 'qmshutdown', vmid)]
        receipt = dict(job_id=args['job'], state='allocated', source_id=3015, source_node=source,
            source_config_sha256='a'*64, node=node, capture_name=args['name'], template_id=vmid,
            marker='crange-capture-123456abcdef', platform_source=True, reservation=allocation,
            transport={'state':'verified', 'derivative_ready':True, 'template_retired':True,
                'source_id':3015, 'source_node':source, 'artifact_sha256':'b'*64, 'template_id':temp},
            provider_tasks=[{'path':path,'upid':f'UPID:{host}:1:2:3:{kind}:{guest}:user:'}
                            for host,path,kind,guest in steps])
        saved = dict(state='running', owner='release-capture', project_id=1, allocations=[allocation])
        config = dict(name=args['name'], tags='crange-image;crange-capture-123456abcdef;p001',
            scsi0='local-hdd:vm-175-disk-0,size=20G', ide2='local-hdd:vm-175-cloudinit,media=cdrom',
            net0='virtio=AA:BB,bridge=isolated,link_down=1')
        api = Mock(); api.vm.return_value=config; api.status.return_value='stopped'; api.request.return_value=[]
        return api, receipt, saved, config, args

    def test_reuses_only_confirmed_owned_copy_without_new_provider_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            api,r,s,config,args=self.fixture(Path(directory))
            with patch.object(c,'_require_probe_pool'):
                result=c._resume_platform_transport_copy(api,r,s,**args)
            self.assertEqual(result['template_id'],175)
            self.assertEqual(api.task.call_count,6)
            self.assertTrue(all(call.args[0]=='GET' for call in api.request.call_args_list))
            self.assertEqual(result['recovery'],'owned-platform-transport-clean-identity-retry')

    def test_wrong_scope_or_already_generalized_copy_cannot_restart(self):
        for change in ('source','task','reservation','identity','generalization','transport'):
            with tempfile.TemporaryDirectory() as directory:
                api,r,s,config,args=self.fixture(Path(directory))
                if change=='source':r['source_id']=3014
                if change=='task':r['provider_tasks'][2]['upid']=r['provider_tasks'][2]['upid'].replace(':21000:',':21001:')
                if change=='reservation':s['state']='released'
                if change=='identity':args['identity_path'].write_text('{}')
                if change=='generalization':r['generalization']={'state':'generalized'}
                if change=='transport':r['transport']['template_retired']=False
                with self.subTest(change=change), patch.object(c,'_require_probe_pool'),self.assertRaises(DistributionError):
                    c._resume_platform_transport_copy(api,r,s,**args)
                self.assertFalse(any(call.args[0]!='GET' for call in api.request.call_args_list))

    def test_running_foreign_connected_or_shared_disk_guests_are_retained(self):
        for change in ('running','foreign','connected','disk','active'):
            with tempfile.TemporaryDirectory() as directory:
                api,r,s,config,args=self.fixture(Path(directory))
                if change=='running':api.status.return_value='running'
                if change=='foreign':config['tags']='foreign'
                if change=='connected':config['net0']='virtio=AA,bridge=isolated,link_down=0'
                if change=='disk':config['scsi0']='local-hdd:base-21000-disk-0,size=20G'
                if change=='active':api.request.return_value=[{'id':'175'}]
                with self.subTest(change=change), patch.object(c,'_require_probe_pool'),self.assertRaises(DistributionError):
                    c._resume_platform_transport_copy(api,r,s,**args)
                self.assertFalse(any(call.args[0]!='GET' for call in api.request.call_args_list))
