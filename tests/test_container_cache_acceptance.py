import json,pathlib,sys,tempfile,unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import crange_container_cache_acceptance as acceptance
from crange_jobs import command

class AcceptanceTests(unittest.TestCase):
    def test_acceptance_accepts_published_image_id_and_sixth_node(self):
        result = command('template-container-cache-acceptance', 0,
            {'project_id': 100, 'image': 'alpine-ct-portable-20261006', 'node': 'proxmox6', 'storage': 'local-hdd'})
        self.assertIn('alpine-ct-portable-20261006', result)
        self.assertIn('proxmox6', result)

    def test_admin_job_has_fixed_args_and_no_policy_override(self):
        options={'project_id':100,'image':'p100-image-abc','node':'proxmox5','storage':'crange-thin'}
        self.assertEqual(command('template-container-cache-acceptance',0,options)[0],'container-cache-acceptance')
        for project,extra in [(100,{}),(0,{'policy':'/tmp/policy'}),(0,{'storage':'shared'})]:
            with self.assertRaises(ValueError):command('template-container-cache-acceptance',project,{**options,**extra})

    def run_case(self,fail=False,bad_backing=False,flat=False):
        policy={'enabled':True,'revision':'before','allowed_nodes':['proxmox5'],'allowed_storages':['crange-thin'],
                'provider_version':'0.104.0',
                'nodes':{'proxmox5':{'provider_container_acceptance':{'provider_version':'0.104.0','node':'proxmox5','node_version':'pve-manager/9','disposable_terraform_ct':True,'network_config_verified':True,'job_id':'accepted'}}},
                'cache':{'isolated_bridges':['crprobe1450'],'tested_paths':['local-linked-vm','vm-archive-restore']},'cpu_overcommit_ratio':6}
        if flat:policy.update(policy.pop('cache'))
        image={'kind':'container','approved_projects':[100],'artifact_sha256':'b'*64,'artifact':{'complete_capture':True}}
        catalog={'images':{'image':image}};job='cache-'+'a'*32
        record={'job_id':job,'state':'ready','image_id':'image','node':'proxmox5','datastore_id':'crange-thin',
                'artifact_sha256':'b'*64,'linked_backing':{'verified':not bad_backing},'provider_tasks':[],
                'template_id':141,'probe_id':142}
        api=Mock();api.request.side_effect=lambda m,p,f=None:[] if p=='/cluster/resources' else {'template':1,'rootfs':'crange-thin:base-141-disk-0',
            'tags':'crange-image;crange-cache-141','description':'CRANGE distribution '+job+' image image'}
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d);path=root/'distribution-policy.json';path.write_text(json.dumps(policy));journal=root/'cache-journal';journal.mkdir();(journal/(job+'.json')).write_text(json.dumps(record))
            with patch('crange_scheduling.CONFIG',root),patch('crange_scheduling.DATA',root),patch('crange_scheduling.read_settings',return_value=(policy,catalog)), \
                 patch('crange.ProxmoxAPI',return_value=api),patch('crange_scheduling.discover',return_value={'nodes':{'proxmox5':{'storages':{'crange-thin':{'available':True,'shared':False,'snapshots_verified':True}}}}}), \
                 patch('crange_cache_jobs.prepare_cache',return_value={'job_id':job}) as prepare:
                if fail:prepare.side_effect=ValueError('restore failed')
                if fail or bad_backing:
                    with self.assertRaises(ValueError):acceptance.run(100,'image','proxmox5','crange-thin')
                else:acceptance.run(100,'image','proxmox5','crange-thin')
                scoped=prepare.call_args.kwargs['_controlled_acceptance_policy']
                self.assertEqual(scoped['allowed_nodes'],['proxmox5']);self.assertEqual(scoped['approved_storage'],[['proxmox5','crange-thin']])
                from crange_cache_jobs import select_probe_bridge
                self.assertEqual(select_probe_bridge(scoped),'crprobe1450')
            saved=json.loads(path.read_text())
        return saved

    def test_failed_restore_or_backing_does_not_publish_capabilities(self):
        for options in ({'fail':True},{'bad_backing':True}):
            saved=self.run_case(**options)
            self.assertEqual(saved['revision'],'before');self.assertNotIn('container-archive-restore',saved['cache']['tested_paths'])

    def test_actual_verified_cache_publishes_receipt_and_preserves_limits(self):
        saved=self.run_case();self.assertIn('container-archive-restore',saved['cache']['tested_paths'])
        self.assertEqual(saved['cpu_overcommit_ratio'],6)
        self.assertTrue(saved['nodes']['proxmox5']['container_cache_receipts']['crange-thin']['verified'])

    def test_flat_policy_keeps_approved_bridge_when_publishing_cache_settings(self):
        from crange_cache_jobs import select_probe_bridge
        saved=self.run_case(flat=True)
        self.assertEqual(select_probe_bridge(saved),'crprobe1450')
        self.assertEqual(saved['cache']['isolated_bridges'],['crprobe1450'])
