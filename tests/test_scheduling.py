import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import crange_scheduling as scheduling
from crange import CrangeError
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

    def test_bootstrap_routers_may_pin_to_multiple_individually_accepted_nodes(self):
        checks=sorted(scheduling.NETWORK_CHECKS)
        proof=lambda node:{'provider_version':'0.104.0','node':node,'node_version':'pve-manager/9.0.3',
            'disposable_terraform_ct':True,'network_config_verified':True,'job_id':'ct-'+node}
        policy={'provider_version':'0.104.0','allowed_nodes':['proxmox1','proxmox2'],
            'nodes':{'proxmox1':{'provider_container_acceptance':proof('proxmox1')},
                     'proxmox2':{'provider_container_acceptance':proof('proxmox2')}},
            'network_attestations':{node:{'verified':True,'mtu':1450,'zone':'crange','passed_checks':checks}
                                    for node in ('proxmox1','proxmox2')}}
        mapping={'router:project':'proxmox1','router:staging':'proxmox2'}
        requests=[{'kind':'container','resource_key':key,'project_id':100,'networks':nets,
                   'placement':{'mode':'pin','pinned_node':node}}
                  for (key,nets),node in zip([('router:project',['crtrans','p100tr']),
                                              ('router:staging',['p100tr','p100t254'])],mapping.values())]
        inventory={'nodes':{node:{'online':True,'draining':False,'verified_networks':['crtrans']}
                            for node in mapping.values()}}
        result=scheduling._bootstrap_planned_networks(inventory,policy,requests,
                                                     node_name='proxmox1',router_nodes=mapping)
        self.assertEqual(result['nodes']['proxmox1']['bootstrap_planned_networks'],
                         ['crtrans','p100t254','p100tr'])
        self.assertEqual(result['nodes']['proxmox2']['bootstrap_planned_networks'],
                         ['crtrans','p100t254','p100tr'])
        invalid=dict(mapping, **{'router:staging':'proxmox3'})
        with self.assertRaises(PlacementError):
            scheduling._bootstrap_planned_networks(inventory,policy,requests,
                                                   node_name='proxmox1',router_nodes=invalid)

    def test_bootstrap_auto_candidates_receive_only_transient_planned_network_access(self):
        checks=sorted(scheduling.NETWORK_CHECKS)
        proof=lambda node:{'provider_version':'0.104.0','node':node,'node_version':'pve-manager/9.0.3',
            'disposable_terraform_ct':True,'network_config_verified':True,'job_id':'ct-'+node}
        nodes=['proxmox1','proxmox2']
        policy={'provider_version':'0.104.0','allowed_nodes':nodes,
            'nodes':{n:{'provider_container_acceptance':proof(n)} for n in nodes},
            'network_attestations':{n:{'verified':True,'mtu':1450,'zone':'crange','passed_checks':checks} for n in nodes}}
        requests=[{'kind':'container','resource_key':'router:project','project_id':100,
                   'networks':['crtrans','p100tr'],
                   'placement':{'mode':'auto','allowed_nodes':nodes}}]
        inventory={'nodes':{n:{'online':True,'draining':False,'verified_networks':['crtrans']} for n in nodes}}
        result=scheduling._bootstrap_planned_networks(inventory,policy,requests,'proxmox1',nodes)
        self.assertEqual(result['nodes']['proxmox1']['bootstrap_planned_networks'],['crtrans','p100tr'])
        self.assertEqual(result['nodes']['proxmox2']['bootstrap_planned_networks'],['crtrans','p100tr'])
        requests[0]['placement']['allowed_nodes']=['proxmox1']
        with self.assertRaises(PlacementError):
            scheduling._bootstrap_planned_networks(inventory,policy,requests,'proxmox1',nodes)

    def test_router_declarations_honor_per_router_bootstrap_node_map(self):
        cfg={'project_id':100,'team_ids':['1'], 'router_node':'proxmox1',
             'bootstrap_router_nodes':{'router:project':'proxmox1','router:staging':'proxmox2',
                                       'router:team:1':'proxmox1'}}
        with patch('crange.templates_for',return_value={}), patch('crange.container_templates_for',return_value={}):
            rows=scheduling.declarations(pathlib.Path('.'),cfg)
        nodes={row['resource_key']:row['box']['node_name'] for row in rows}
        self.assertEqual(nodes,{'router:project':'proxmox1','router:staging':'proxmox2',
                                'router:team:1':'proxmox1'})

    def test_begin_apply_revalidates_bootstrap_reservations_with_bootstrap_scope(self):
        import hashlib, json
        policy={'enabled':True,'revision':'test'}
        catalog={'schema_version':1,'images':{}}
        inventory={'nodes':{}}
        request={'resource_key':'router:project','kind':'container','networks':['crtrans','p100tr']}
        prepared={'plan_id':'bootstrap-test','requests':[request], 'placements':[],
                  'bootstrap_node':'proxmox1','bootstrap_nodes':['proxmox1'],
                  'provider_compat':{'enabled':False},
                  'policy_sha256':hashlib.sha256(json.dumps(policy,sort_keys=True).encode()).hexdigest(),
                  'catalog_sha256':scheduling.catalog_identity(catalog)}
        class Ledger:
            def __init__(self,*args): self.kwargs=None
            def mark_running(self,*args,**kwargs): self.kwargs=kwargs
            def close(self): pass
        ledger=Ledger()
        with patch.object(scheduling,'read_settings',return_value=(policy,catalog)), \
             patch('crange_proxmox_compat.reviewed_identity',return_value={'enabled':False}), \
             patch.object(scheduling,'discover',return_value=inventory), \
             patch.object(scheduling,'_bootstrap_planned_networks',return_value=inventory) as planned, \
             patch.object(scheduling,'ReservationLedger',return_value=ledger):
            scheduling.begin_apply(pathlib.Path('.'),prepared,api=object())
        self.assertTrue(ledger.kwargs['bootstrap'])
        planned.assert_called_once()

    def test_finish_apply_uses_supported_active_task_filter_and_only_owned_guest_ids(self):
        class FinishAPI:
            def __init__(self): self.task_fields=None
            def request(self,method,path,fields=None):
                if path=='/nodes/proxmox1/lxc/113/config': return {'tags':'p100;crange-router'}
                if path=='/nodes/proxmox1/tasks': self.task_fields=fields; return [{'id':999,'status':'running'}]
                raise AssertionError(path)
        class Ledger:
            def __init__(self,*args): self.reconciled=None
            def reconcile_job(self,*args,**kwargs): self.reconciled=(args,kwargs)
            def close(self): pass
        api=FinishAPI(); ledger=Ledger()
        prepared={'plan_id':'bootstrap-finish-test','requests':[{'resource_key':'router:project'}],
                  'placements':[{'resource_key':'router:project','resolved':{'node_name':'proxmox1',
                      'datastore_id':'local-lvm'},'image':'debian-12-router-v1'}]}
        with patch.object(scheduling,'applied_resources',return_value={'router:project':
             {'node_name':'proxmox1','vm_id':113}}), \
             patch.object(scheduling,'ReservationLedger',return_value=ledger), \
             patch.object(scheduling,'atomic_json'):
            scheduling.finish_apply(pathlib.Path('/tmp/project-100'),prepared,succeeded=True,api=api)
        self.assertEqual(api.task_fields,{'source':'active','limit':1000})
        self.assertEqual(ledger.reconciled[0],('bootstrap-finish-test',))

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

    def test_discovery_ignores_only_a_guest_confirmed_absent_after_config_http_500(self):
        class RacyAPI(API):
            def __init__(self, refresh_rows):
                self.vm_list_calls=0; self.refresh_rows=refresh_rows
            def request(self, method, path, fields=None):
                if path=='/cluster/resources' and fields=={'type':'vm'}:
                    self.vm_list_calls += 1
                    if self.vm_list_calls==1:
                        return [{'node':'proxmox2','type':'qemu','vmid':145}]
                    return self.refresh_rows
                if path=='/nodes/proxmox2/qemu/145/config':
                    raise CrangeError('Proxmox API GET /nodes/proxmox2/qemu/145/config failed with HTTP 500.')
                return super().request(method,path,fields)
        policy=dict(scheduling.DEFAULT_POLICY,allowed_nodes=['proxmox2'])
        absent=scheduling.discover(RacyAPI([]),policy)
        self.assertNotIn('145',absent['guests'])
        self.assertEqual(absent['nodes']['proxmox2']['committed_memory_mb'],0)
        with self.assertRaises(CrangeError):
            scheduling.discover(RacyAPI([{'node':'proxmox2','type':'qemu','vmid':145}]),policy)

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

    def test_offline_workloads_are_accounted_from_authoritative_config(self):
        class OfflineAPI(API):
            def request(self, method, path, fields=None):
                if path == '/nodes': return [{'node': 'proxmox2', 'status': 'offline'}]
                if path == '/cluster/resources' and fields != {'type':'storage'}:
                    return [{'node':'proxmox2','type':'qemu','vmid':101,'template':0},
                            {'node':'proxmox2','type':'lxc','vmid':102,'template':1}]
                return super().request(method, path, fields)
        result=scheduling.discover(OfflineAPI(), dict(scheduling.DEFAULT_POLICY, allowed_nodes=['proxmox2']))
        self.assertFalse(result['nodes']['proxmox2']['online'])
        self.assertEqual(result['nodes']['proxmox2']['committed_memory_mb'],2048)
        self.assertEqual(result['nodes']['proxmox2']['committed_cpu_cores'],2)
        self.assertEqual(result['quota_usage']['global']['disk_gb'],28)

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

    def test_offline_guest_config_supplies_missing_maxima_and_node_stays_ineligible(self):
        class OfflineConfigAPI(API):
            def request(self, method, path, fields=None):
                if path == '/nodes':
                    return [{'node':'proxmox1','status':'online'}, {'node':'proxmox2','status':'offline'}]
                if path == '/cluster/resources' and fields != {'type':'storage'}:
                    return [{'node':'proxmox2','type':'lxc','vmid':113,'template':0,
                             'maxmem':None,'maxcpu':None,'maxdisk':None,'tags':'p100'}]
                if path == '/nodes/proxmox2/lxc/113/config':
                    return {'memory':512,'cores':1,'rootfs':'local-lvm:vm-113-disk-0,size=8G'}
                return super().request(method,path,fields)
        result=scheduling.discover(OfflineConfigAPI(),dict(scheduling.DEFAULT_POLICY,allowed_nodes=['proxmox1']))
        node=result['nodes']['proxmox2']
        self.assertFalse(node['online'])
        self.assertEqual(node['committed_memory_mb'],512)
        self.assertEqual(node['committed_cpu_cores'],1)
        self.assertEqual(result['quota_usage']['100']['disk_gb'],8)
        self.assertEqual(result['guests']['113']['status'],'offline')

    def test_offline_missing_maxima_still_fail_closed_when_authoritative_config_unavailable(self):
        class OfflineNoConfigAPI(API):
            def request(self, method, path, fields=None):
                if path == '/nodes': return [{'node':'proxmox2','status':'offline'}]
                if path == '/cluster/resources' and fields != {'type':'storage'}:
                    return [{'node':'proxmox2','type':'lxc','vmid':113,'template':0,
                             'maxmem':None,'maxcpu':None,'maxdisk':None,'tags':'p100'}]
                if path == '/nodes/proxmox2/lxc/113/config': raise CrangeError('unavailable')
                return super().request(method,path,fields)
        with self.assertRaisesRegex(PlacementError, r'Fixed offline config snapshot relay is unavailable'):
            scheduling.discover(OfflineNoConfigAPI(),dict(scheduling.DEFAULT_POLICY,allowed_nodes=['proxmox1']))

    def test_offline_discovery_uses_fresh_fixed_snapshot_when_node_config_api_fails(self):
        class OfflineAPI(API):
            def request(self, method, path, fields=None):
                if path == '/nodes':
                    return [{'node':'proxmox1','status':'online'}, {'node':'proxmox2','status':'offline'}]
                if path == '/cluster/resources' and fields != {'type':'storage'}:
                    return [{'node':'proxmox2','type':'lxc','vmid':113,'template':None,
                             'maxmem':None,'maxcpu':None,'maxdisk':None,
                             'tags':'network-infrastructure;p100;range-lan;terraform'}]
                if path == '/nodes/proxmox2/lxc/113/config':
                    raise CrangeError('Proxmox API GET failed with HTTP 500.')
                return super().request(method,path,fields)
        snapshot={'guest_node':'proxmox2','kind':'lxc','vmid':113,'status':'offline',
                  'tags':['network-infrastructure','p100'],'template':False,
                  'memory_bytes':512*2**20,'cpu_cores':1,'disk_bytes':8*2**30,
                  'config_sha256':'a'*64,'captured_at':scheduling.time.time()}
        policy=dict(scheduling.DEFAULT_POLICY,allowed_nodes=['proxmox1'])
        with patch.object(scheduling,'_offline_config_snapshot',return_value=snapshot):
            result=scheduling.discover(OfflineAPI(),policy)
        self.assertFalse(result['nodes']['proxmox2']['online'])
        self.assertEqual(result['nodes']['proxmox2']['committed_memory_mb'],512)
        self.assertEqual(result['nodes']['proxmox2']['committed_cpu_cores'],1)
        self.assertEqual(result['quota_usage']['100']['disk_gb'],8)

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

    def test_cache_planning_reports_node_network_gates_when_candidate_loop_is_skipped(self):
        policy={'enabled':True,'allowed_nodes':['proxmox1']}
        catalog={'images':{'stock':{'kind':'container','disk_gb':8,'required_features':None,
                                    'replicas':[{'node':'proxmox1','state':'ready',
                                                 'template_file_id':'crange-library:vztmpl/debian.tar.zst'}]}}}
        row={'resource_key':'container:t254-mhl-app01','kind':'container','image':'stock',
             'networks':['p100t254'],'box':{'node_name':'auto','placement':{'mode':'auto'},
                                            'memory_mb':1024,'cores':1}}
        node={'online':True,'draining':False,'guest_types':['container'],'management_verified':True,
              'verified_networks':[],'features':[],'storages':{}}
        class Ledger:
            def __init__(self,*args): pass
            def active_reservations(self): return []
            def close(self): pass
        with tempfile.TemporaryDirectory() as directory:
            path=pathlib.Path(directory)
            with patch.object(scheduling,'DATA',path/'data'), \
                 patch.object(scheduling,'read_settings',return_value=(policy,catalog)), \
                 patch.object(scheduling,'declarations',return_value=[row]), \
                 patch.object(scheduling,'applied_resources',return_value={}), \
                 patch.object(scheduling,'discover',return_value={'nodes':{'proxmox1':node}}), \
                 patch.object(scheduling,'ReservationLedger',return_value=Ledger()):
                with self.assertRaisesRegex(PlacementError,
                    r'node_exclusions.*required-networks-not-verified:p100t254'):
                    scheduling.required_preparations(path,{'project_id':100},api=API())

if __name__=='__main__': unittest.main()
