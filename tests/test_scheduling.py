import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import crange_scheduling as scheduling
from crange_placement import PlacementError

class API:
    def request(self, method, path, fields=None):
        if path == '/nodes': return [{'node':'proxmox2','status':'online'}]
        if path == '/cluster/resources': return [{'node':'proxmox2','type':'qemu','vmid':101},{'node':'proxmox2','type':'lxc','vmid':102}]
        if path == '/storage': return [{'storage':'local-lvm','content':'images,rootdir'}]
        if path == '/cluster/sdn/vnets': return [{'vnet':'p003t254','zone':'crange'}]
        if path.endswith('/status'): return {'cpuinfo':{'cpus':4},'memory':{'total':8*2**30,'used':2*2**30},'loadavg':[0,0,0]}
        if path.endswith('/storage'): return [{'storage':'local-lvm','active':1,'total':100*2**30,'avail':80*2**30}]
        if path.endswith('/101/config'): return {'memory':2048,'cores':2,'template':0,'scsi0':'local-lvm:vm-101-disk-0,size=20G','tags':'p003'}
        if path.endswith('/102/config'): return {'memory':512,'cores':1,'template':1,'rootfs':'local-lvm:vm-102-disk-0,size=8G'}
        raise AssertionError(path)

class SchedulingTests(unittest.TestCase):
    def test_discovery_counts_stopped_guests_and_template_disks_without_double_ram(self):
        policy = dict(scheduling.DEFAULT_POLICY, allowed_nodes=['proxmox2'])
        result = scheduling.discover(API(),policy)
        node = result['nodes']['proxmox2']
        self.assertEqual(node['committed_memory_mb'],2048)
        self.assertEqual(node['observed_memory_mb'],2048)
        self.assertEqual(node['committed_cpu_cores'],2)
        self.assertEqual(node['storages']['local-lvm']['committed_gb'],28)
        self.assertFalse(node['management_verified'])
        self.assertEqual(node['verified_networks'],[])

    def test_container_capability_requires_pinned_provider_acceptance(self):
        policy=dict(scheduling.DEFAULT_POLICY,allowed_nodes=['proxmox2'])
        self.assertNotIn('container',scheduling.discover(API(),policy)['nodes']['proxmox2']['guest_types'])
        policy['nodes']={'proxmox2':{'provider_container_verified':True}}
        self.assertIn('container',scheduling.discover(API(),policy)['nodes']['proxmox2']['guest_types'])

    def test_excluded_node_workloads_still_count_toward_project_quota(self):
        class ExcludedAPI(API):
            def request(self, method, path, fields=None):
                if path == '/nodes':
                    return [{'node': 'proxmox2', 'status': 'online'}, {'node': 'proxmox4', 'status': 'online'}]
                if path == '/cluster/resources':
                    return [{'node': 'proxmox4', 'type': 'qemu', 'vmid': 101}]
                return super().request(method, path, fields)
        result = scheduling.discover(ExcludedAPI(), dict(scheduling.DEFAULT_POLICY, allowed_nodes=['proxmox2']))
        self.assertEqual(result['quota_usage']['3']['memory_mb'], 2048)
        self.assertEqual(result['nodes']['proxmox4']['committed_cpu_cores'], 2)
        self.assertEqual(result['guests']['101']['node_name'], 'proxmox4')

    def test_offline_workloads_cannot_silently_disappear_from_capacity(self):
        class OfflineAPI(API):
            def request(self, method, path, fields=None):
                if path == '/nodes': return [{'node': 'proxmox2', 'status': 'offline'}]
                return super().request(method, path, fields)
        with self.assertRaisesRegex(PlacementError, 'offline node proxmox2'):
            scheduling.discover(OfflineAPI(), dict(scheduling.DEFAULT_POLICY, allowed_nodes=['proxmox2']))

    def test_offline_optional_node_remains_excluded_and_its_guests_count(self):
        class OfflineSixAPI(API):
            def request(self, method, path, fields=None):
                if path == '/nodes':
                    return [{'node': 'proxmox2', 'status': 'online'}, {'node': 'proxmox6', 'status': 'offline'}]
                if path == '/cluster/resources':
                    return [{'node': 'proxmox6', 'type': 'qemu', 'vmid': 600,
                             'template': 0, 'maxmem': 4096 * 2**20, 'maxcpu': 2,
                             'maxdisk': 20 * 2**30, 'tags': 'p003', 'name': 'parked'}]
                return super().request(method, path, fields)
        result = scheduling.discover(OfflineSixAPI(),
            dict(scheduling.DEFAULT_POLICY, allowed_nodes=['proxmox2']))
        self.assertFalse(result['nodes']['proxmox6']['online'])
        self.assertEqual(result['quota_usage']['3']['memory_mb'], 4096)
        self.assertEqual(result['quota_usage']['global']['disk_gb'], 20)
        self.assertEqual(result['guests']['600']['status'], 'offline')

    def test_existing_placement_ignores_changed_preference(self):
        row={'resource_key':'vm:web','image':'base-v1','reference':{},'box':{'node_name':'auto','placement':{'mode':'auto','preferred_node':'proxmox5'}}}
        live={'node_name':'proxmox2','clone':[{'vm_id':301,'node_name':'proxmox2','full':True}],'disk':[{'datastore_id':'local-lvm'}]}
        result=scheduling.freeze(row,live)
        self.assertEqual(result['resolved']['node_name'],'proxmox2')
        self.assertEqual(result['resolved']['template_id'],301)
        self.assertEqual(row['box']['placement']['preferred_node'],'proxmox5')

    def test_legacy_pin_and_soft_preference_are_distinct(self):
        self.assertEqual(scheduling.placement_intent('proxmox2'),{'mode':'pin','pinned_node':'proxmox2'})
        self.assertEqual(scheduling.placement_intent('auto','proxmox5'),{'mode':'auto','preferred_node':'proxmox5'})
        with self.assertRaises(PlacementError): scheduling.placement_intent('proxmox2','proxmox5')

    def test_public_summary_directory_is_readable_without_exposing_private_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            scheduling.atomic_json(root/'public'/'plan.json',{},0o640)
            scheduling.atomic_json(root/'private'/'catalog.json',{})
            self.assertEqual((root/'public').stat().st_mode & 0o777,0o750)
            self.assertEqual((root/'private').stat().st_mode & 0o777,0o700)

if __name__=='__main__': unittest.main()
