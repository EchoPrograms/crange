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
        if path == '/cluster/resources':
            if fields and fields.get('type') == 'storage':
                return [{'node':'proxmox2','storage':'local-lvm','maxdisk':100*2**30,'disk':20*2**30,'status':'available'}]
            return [{'node':'proxmox2','type':'qemu','vmid':101},{'node':'proxmox2','type':'lxc','vmid':102}]
        if path == '/storage': return [{'storage':'local-lvm','content':'images,rootdir'}]
        if path == '/cluster/sdn/vnets': return [{'vnet':'p003t254','zone':'crange'}]
        if path.endswith('/status'): return {'cpuinfo':{'cpus':4},'memory':{'total':8*2**30,'used':2*2**30},'loadavg':[0,0,0]}
        if path.endswith('/storage'): return [{'storage':'local-lvm','active':1,'total':100*2**30,'avail':80*2**30,'content':'images,rootdir','shared':0}]
        if path.endswith('/101/config'): return {'memory':2048,'cores':2,'template':0,'scsi0':'local-lvm:vm-101-disk-0,size=20G','tags':'p003'}
        if path.endswith('/102/config'): return {'memory':512,'cores':1,'template':1,'rootfs':'local-lvm:vm-102-disk-0,size=8G'}
        raise AssertionError(path)

class SchedulingTests(unittest.TestCase):
    def test_container_cpu_request_does_not_require_vm_host_feature(self):
        self.assertIsNone(scheduling.requested_cpu_type('container', {}))
        self.assertEqual(scheduling.requested_cpu_type('vm', {}), 'host')
        self.assertEqual(scheduling.requested_cpu_type('vm', {'cpu_type':'x86-64-v2-AES'}), 'x86-64-v2-AES')

    def test_bootstrap_eligibility_is_router_only_and_does_not_record_network_acceptance(self):
        checks=sorted(scheduling.NETWORK_CHECKS)
        policy={'provider_version':'0.104.0','allowed_nodes':['proxmox2'],
                'nodes':{'proxmox2':{'provider_container_acceptance':{
                    'provider_version':'0.104.0','node':'proxmox2','node_version':'pve-manager/9.0.3',
                    'disposable_terraform_ct':True,'network_config_verified':True,'job_id':'ct-test'}}},
                'network_attestations':{'proxmox2':{'verified':True,'mtu':1450,'zone':'crange','passed_checks':checks}}}
        inventory={'nodes':{'proxmox2':{'online':True,'draining':False,'networks':['crtrans'],
                                        'verified_networks':['crtrans']}}}
        requests=[{'kind':'container','resource_key':'router:project','project_id':3,
                   'networks':['p003tr','crtrans'],'placement':{'mode':'pin','pinned_node':'proxmox2'}},
                  {'kind':'container','resource_key':'router:staging','project_id':3,
                   'networks':['p003t254'],'placement':{'mode':'pin','pinned_node':'proxmox2'}}]
        result=scheduling._bootstrap_planned_networks(inventory,policy,requests,'proxmox2')
        self.assertEqual(result['nodes']['proxmox2']['verified_networks'],['crtrans'])
        self.assertEqual(set(result['nodes']['proxmox2']['bootstrap_planned_networks']),{'crtrans','p003tr','p003t254'})
        self.assertNotIn('p003t254',policy['network_attestations']['proxmox2'].get('vnets',[]))
        for bad in ([{**requests[0],'resource_key':'vm:scenario'}],
                    [dict(requests[0],project_id=4),requests[1]],
                    [dict(requests[0],placement={'mode':'auto'}),requests[1]]):
            with self.subTest(bad=bad), self.assertRaises(PlacementError):
                scheduling._bootstrap_planned_networks({'nodes':{'proxmox2':{'online':True,'verified_networks':['crtrans']}}},policy,bad,'proxmox2')

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
        policy=dict(scheduling.DEFAULT_POLICY,allowed_nodes=['proxmox2'],provider_version='0.104.0')
        self.assertNotIn('container',scheduling.discover(API(),policy)['nodes']['proxmox2']['guest_types'])
        policy['nodes']={'proxmox2':{'provider_container_verified':True}}
        self.assertNotIn('container',scheduling.discover(API(),policy)['nodes']['proxmox2']['guest_types'])
        policy['nodes']={'proxmox2':{'provider_container_acceptance':{
            'provider_version':'0.104.0','node':'proxmox2','node_version':'pve-manager/9.0.3/build',
            'disposable_terraform_ct':True,'network_config_verified':True,'job_id':'ct-accept-20261006'}}}
        self.assertIn('container',scheduling.discover(API(),policy)['nodes']['proxmox2']['guest_types'])
        policy['nodes']['proxmox2']['provider_container_acceptance']['node']='proxmox3'
        self.assertNotIn('container',scheduling.discover(API(),policy)['nodes']['proxmox2']['guest_types'])
        policy['nodes']['proxmox2']['provider_container_acceptance']['node']='proxmox2'
        policy['provider_version']='0.105.0'
        self.assertNotIn('container',scheduling.discover(API(),policy)['nodes']['proxmox2']['guest_types'])

    def test_attestation_admits_only_networks_actually_tested_on_node(self):
        policy=dict(scheduling.DEFAULT_POLICY,allowed_nodes=['proxmox2'])
        policy['network_attestations']={'proxmox2':{'verified':True,'mtu':1450,
            'zone':'crange','management_verified':True,'passed_checks':list(scheduling.NETWORK_CHECKS),'verified_networks':['p003t254']}}
        node=scheduling.discover(API(),policy)['nodes']['proxmox2']
        self.assertTrue(node['management_verified'])
        self.assertEqual(node['verified_networks'],['p003t254'])
        self.assertIn('crtrans',node['networks'])
        self.assertNotIn('crtrans',node['verified_networks'])

    def test_project_three_proof_cannot_admit_other_project_or_unattested_planned_vnet(self):
        policy=dict(scheduling.DEFAULT_POLICY,allowed_nodes=['proxmox2'])
        policy['network_attestations']={'proxmox2':{'verified':True,'mtu':1450,
            'zone':'crange','management_verified':True,'passed_checks':list(scheduling.NETWORK_CHECKS),
            'verified_networks':['p003t254']}}
        node=scheduling.discover(API(),policy,['p042t001','p042tr'])['nodes']['proxmox2']
        self.assertNotIn('p042t001',node['verified_networks'])
        self.assertNotIn('p042tr',node['verified_networks'])
        policy['network_attestations']['proxmox2']['passed_checks'].remove('team_isolation')
        node=scheduling.discover(API(),policy,['p003t254'])['nodes']['proxmox2']
        self.assertFalse(node['management_verified'])
        self.assertEqual(node['verified_networks'],[])

    def test_every_cluster_node_requires_its_own_exact_project_proof(self):
        class AllNodesAPI(API):
            def request(self, method, path, fields=None):
                if path == '/nodes': return [{'node':f'proxmox{i}','status':'online'} for i in range(1,6)]
                return super().request(method,path,fields)
        policy=dict(scheduling.DEFAULT_POLICY)
        policy['network_attestations']={'proxmox5':{'verified':True,'mtu':1450,
            'zone':'crange','management_verified':True,'passed_checks':list(scheduling.NETWORK_CHECKS),
            'verified_networks':['p042t001','p042tr']}}
        nodes=scheduling.discover(AllNodesAPI(),policy,['p042t001','p042tr'])['nodes']
        self.assertEqual(nodes['proxmox5']['verified_networks'],['p042t001','p042tr'])
        for i in range(1,5): self.assertEqual(nodes[f'proxmox{i}']['verified_networks'],[])

    def test_incomplete_or_shared_storage_cannot_claim_local_linked_capability(self):
        for mutation in ({'shared':None},{'content':''},{'avail':None},{'total':float('nan')},{'enabled':'0'},{'shared':1}):
            with self.subTest(mutation=mutation):
                class StorageAPI(API):
                    def request(self, method, path, fields=None):
                        rows=super().request(method,path,fields)
                        if path == '/nodes/proxmox2/storage': rows[0].update(mutation)
                        return rows
                policy=dict(scheduling.DEFAULT_POLICY,allowed_nodes=['proxmox2'],nodes={'proxmox2':{'snapshot_storages':['local-lvm']}})
                storage=scheduling.discover(StorageAPI(),policy)['nodes']['proxmox2']['storages']['local-lvm']
                if mutation != {'shared':1}: self.assertFalse(storage['available'])
                if mutation != {'enabled':'0'}: self.assertFalse(storage['snapshots_verified'])
                self.assertEqual(storage['committed_gb'],28)

    def test_storage_timeout_excludes_node_without_losing_guest_commitments(self):
        class SlowStorageAPI(API):
            def request(self, method, path, fields=None):
                if path == '/nodes/proxmox2/storage':
                    raise RuntimeError('storage API timeout')
                return super().request(method, path, fields)
        policy=dict(scheduling.DEFAULT_POLICY,allowed_nodes=['proxmox2'])
        node=scheduling.discover(SlowStorageAPI(),policy)['nodes']['proxmox2']
        self.assertTrue(node['storage_inventory_error'])
        self.assertFalse(node['storages']['local-lvm']['available'])
        self.assertEqual(node['storages']['local-lvm']['total_gb'],100)
        self.assertEqual(node['committed_memory_mb'],2048)
        self.assertEqual(node['storages']['local-lvm']['committed_gb'],28)

    def test_excluded_node_workloads_still_count_toward_project_quota(self):
        class ExcludedAPI(API):
            def request(self, method, path, fields=None):
                if path == '/nodes':
                    return [{'node': 'proxmox2', 'status': 'online'}, {'node': 'proxmox4', 'status': 'online'}]
                if path == '/cluster/resources':
                    if fields and fields.get('type') == 'storage':
                        return super().request(method, path, fields)
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
                    if fields and fields.get('type') == 'storage':
                        return super().request(method, path, fields)
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

    def test_admin_container_cache_flag_does_not_admit_normal_staging_guests(self):
        policy={'enabled':True,'revision':'ct-bootstrap','allowed_nodes':['proxmox2'],
                'nodes':{'proxmox2':{'container_cache_preparation_allowed':True}}}
        catalog={'images':{'ct':{'kind':'container','disk_gb':4,'replicas':[]}}}
        row={'resource_key':'container:test','kind':'container','image':'ct',
             'networks':['p003t254'],'box':{'node_name':'auto','memory_mb':512,'cores':1}}
        node={'guest_types':['vm'],'management_verified':True,'verified_networks':['p003t254'],
              'features':[],'storages':{'local-lvm':{}}}
        with tempfile.TemporaryDirectory() as directory:
            path=pathlib.Path(directory)
            with patch.object(scheduling,'DATA',path/'data'), \
                 patch.object(scheduling,'read_settings',return_value=(policy,catalog)), \
                 patch.object(scheduling,'declarations',return_value=[row]), \
                 patch.object(scheduling,'applied_resources',return_value={}), \
                 patch.object(scheduling,'discover',return_value={'nodes':{'proxmox2':node}}), \
                 patch('crange_distribution.required_capacity',side_effect=AssertionError('Guest cache planning bypassed provider gate')) as cache:
                with self.assertRaises(PlacementError):
                    scheduling.required_preparations(path,{'project_id':3},api=API())
                cache.assert_not_called()

if __name__=='__main__': unittest.main()
