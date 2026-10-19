import sys
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange_capture as capture
from crange_distribution import DistributionError

class ContainerTransportTests(unittest.TestCase):
    def fixture(self):
        api=Mock();api.container.return_value={'tags':'p100;scenario','unprivileged':1,
            'memory':1024,'cores':1,'rootfs':'local-lvm:vm-127-disk-0,size=8G','net0':'name=eth0,bridge=p100t254,host-managed=1,hwaddr=AA:BB:CC:DD:EE:FF'}
        api.container_status.return_value='stopped'
        archive='crange-library:backup/vzdump-lxc-127-2026_10_13-12_00_00.tar.zst'
        api.request.side_effect=[[],[{'volid':archive,'content':'backup','vmid':127}]]
        receipt={'job_id':'capture-p100-test','capture_name':'captured-db'}
        return api,archive,receipt
    def run_transport(self,api,receipt,hasher):
        task=Mock()
        with patch.object(capture,'SSHVolumeHasher',return_value=hasher),patch.object(capture,'read_settings',return_value=({'capture_isolated_bridge':'isolated'},{})):
            capture._container_transport(api,source_node='proxmox1',source_id=127,node='proxmox2',vmid=137,
                storage='local-lvm',backup='crange-library',project=100,receipt=receipt,save=Mock(),task=task)
        return task
    def test_restore_is_unprivileged_stopped_and_in_probe_pool_after_frozen_backup_hash(self):
        api,archive,receipt=self.fixture();task=self.run_transport(api,receipt,Mock(return_value='a'*64))
        self.assertEqual(task.call_args_list[0].args[0],'/nodes/proxmox1/vzdump')
        self.assertEqual(task.call_args_list[0].kwargs,{'task_node':'proxmox1'})
        path,args=task.call_args_list[1].args
        self.assertEqual(path,'/nodes/proxmox2/lxc')
        self.assertEqual(args['ostemplate'],archive)
        self.assertEqual((args['restore'],args['start'],args['unprivileged'],args['pool']),(1,0,1,'crange-probes'))
        self.assertEqual(receipt['transport']['artifact_sha256'],'a'*64)
        self.assertEqual(args['unique'],0)
        self.assertNotIn('host-managed',args['net0'])
        self.assertIn('bridge=isolated',args['net0'])
        self.assertIn('link_down=1',args['net0'])
    def test_foreign_running_or_privileged_sources_never_submit_backup(self):
        for change in ('foreign','running','privileged'):
            api,archive,receipt=self.fixture()
            if change=='foreign':api.container.return_value['tags']='p101;scenario'
            if change=='running':api.container_status.return_value='running'
            if change=='privileged':api.container.return_value['unprivileged']=0
            with self.assertRaises(DistributionError):self.run_transport(api,receipt,Mock())
            api.request.assert_not_called()
    def test_bad_checksum_prevents_restore(self):
        api,archive,receipt=self.fixture();task=Mock()
        with patch.object(capture,'SSHVolumeHasher',return_value=Mock(return_value='bad')):
            with self.assertRaisesRegex(DistributionError,'checksum'):
                capture._container_transport(api,source_node='proxmox1',source_id=127,node='proxmox2',vmid=137,
                    storage='local-lvm',backup='crange-library',project=100,receipt=receipt,save=Mock(),task=task)
        self.assertEqual(task.call_count,1)


class ContainerRestoreRecoveryTests(unittest.TestCase):
    def test_debian_minor_restore_requires_installed_fix_and_retains_exact_allocation(self):
        receipt={'state':'allocated','job_id':'capture-p001-base','kind':'container','source_node':'proxmox4',
                 'source_id':3000,'node':'proxmox6','template_id':170,'source_config_sha256':'hash',
                 'transport':{'state':'verified'},'reservation':{'id':'r'},'provider_tasks':[
                    {'path':'/nodes/proxmox4/vzdump','upid':'UPID:proxmox4:1:2:3:vzdump:3000:user:'},
                    {'path':'/nodes/proxmox6/lxc','upid':'UPID:proxmox6:1:2:3:vzrestore:170:user:'}]}
        ledger=Mock();ledger.get_job.return_value={'state':'running','project_id':1,'allocations':[receipt['reservation']]}
        api=Mock();failed={'status':'stopped','exitstatus':"unable to restore CT 170 - unsupported debian version '13.6'."}
        package={'Package':'pve-container','CurrentState':'Installed','Version':'6.1.14','OldVersion':'6.0.10'}
        with patch.object(capture,'_source_config_identity',return_value='hash'):
            api.request.side_effect=[failed,[package],[],[],[]]
            self.assertTrue(capture._ct_transport_restore_recovery(api,receipt,project=1,job='capture-p001-base',source_node='proxmox4',source_id=3000,node='proxmox6',storage='local-lvm',ledger=ledger))
            for change in [{'OldVersion':'6.0.9'},{'CurrentState':'UnPacked'}]:
                api.request.side_effect=[failed,[{**package,**change}]]
                with self.assertRaises(DistributionError):
                    capture._ct_transport_restore_recovery(api,receipt,project=1,job='capture-p001-base',source_node='proxmox4',source_id=3000,node='proxmox6',storage='local-lvm',ledger=ledger)
            api.request.side_effect=[failed,[package],[{'vmid':170}],[],[]]
            with self.assertRaisesRegex(DistributionError,'still has a guest'):
                capture._ct_transport_restore_recovery(api,receipt,project=1,job='capture-p001-base',source_node='proxmox4',source_id=3000,node='proxmox6',storage='local-lvm',ledger=ledger)
    def test_only_terminal_schema_failure_without_guest_volumes_or_tasks_can_reuse_reservation(self):
        receipt={'state':'allocated','job_id':'capture-p100-test','kind':'container','source_node':'proxmox1',
                 'source_id':127,'node':'proxmox3','template_id':137,'source_config_sha256':'hash',
                 'transport':{'state':'verified'},'reservation':{'id':'r'},'provider_tasks':[
                    {'path':'/nodes/proxmox1/vzdump','upid':'UPID:proxmox1:1:2:3:vzdump:127:user:'},
                    {'path':'/nodes/proxmox3/lxc','upid':'UPID:proxmox3:1:2:3:vzrestore:137:user:'}]}
        ledger=Mock();ledger.get_job.return_value={'state':'running','project_id':100,'allocations':[receipt['reservation']]}
        api=Mock();failed={'status':'stopped','exitstatus':'format error host-managed: property is not defined in schema'}
        with patch.object(capture,'_source_config_identity',return_value='hash'):
            api.request.side_effect=[failed,[],[],[]]
            self.assertTrue(capture._ct_transport_restore_recovery(api,receipt,project=100,job='capture-p100-test',source_node='proxmox1',source_id=127,node='proxmox3',storage='local-lvm',ledger=ledger))
            for position in (1,2,3):
                values=[failed,[],[],[]]
                values[position]=[{'vmid':137,'id':137,'volid':'local-lvm:vm-137-disk-0'}]
                api.request.side_effect=values
                with self.assertRaises(DistributionError):
                    capture._ct_transport_restore_recovery(api,receipt,project=100,job='capture-p100-test',source_node='proxmox1',source_id=127,node='proxmox3',storage='local-lvm',ledger=ledger)
            api.request.side_effect=[{'status':'running'}]
            with self.assertRaises(DistributionError):
                capture._ct_transport_restore_recovery(api,receipt,project=100,job='capture-p100-test',source_node='proxmox1',source_id=127,node='proxmox3',storage='local-lvm',ledger=ledger)

if __name__=='__main__':unittest.main()
