import copy
import hashlib
import json
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import test_network_acceptance as evidence_test
import test_placement as placement_test
import crange_network_workflow as workflow
from crange_network_runner import NetworkProbeRunner
from crange_placement import ReservationLedger


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        fixture=evidence_test.NetworkAcceptanceTests()
        fixture.setUp()
        target=dict(fixture.scope,project_id=3,vnet='p003t254',address='10.3.254.98/24',vmid=30000)
        self.target=target
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=pathlib.Path(temporary.name)
        self.project=self.root/'project'
        self.project.mkdir()
        (self.project/'main.tf').write_text('reviewed project state')
        self.inputs={'main.tf':hashlib.sha256(b'reviewed project state').hexdigest()}
        value=copy.deepcopy(fixture.value)
        value.update(vm_id=30000,network_device=[{'bridge':'p003t254'}],
            initialization=[{'ip_config':[{'ipv4':[{'address':target['address'],'gateway':'10.3.254.1'}]}]}],
            clone=[{'vm_id':150,'node_name':'proxmox2','full':True}],
            memory=[{'dedicated':512}],cpu=[{'cores':1,'sockets':1}],disk=[{'size':20,'datastore_id':'local'}])
        self.create=copy.deepcopy(fixture.create)
        self.create['resource_changes'][0]['change']['after']=value
        self.delete=copy.deepcopy(fixture.delete)
        self.delete['resource_changes'][0]['change']['before']=value
        outer=self
        class API:
            live=False
            source={'template':1,'scsi0':'local:vm-150-disk-0,size=20G','tags':'crange-image'}
            def request(self,method,path,fields=None):
                if path.endswith('/150/config'):return self.source
                if path.endswith('/200/config'):return {'tags':'p003;p003t254','net1':'ip=10.3.254.1/24,bridge=p003t254'}
                if path.endswith('/30000/config'):return {'tags':'crange-network-probe;'+target['job_id'],
                    'net0':'virtio=x,bridge=p003t254','ipconfig0':'ip=10.3.254.98/24,gw=10.3.254.1'}
                if path.endswith('/tasks'):return []
                if path=='/cluster/resources':
                    rows=[{'vmid':150,'node':'proxmox2','type':'qemu'},{'vmid':200,'node':'proxmox1','type':'lxc'}]
                    if self.live:rows.append({'vmid':30000,'node':'proxmox2','type':'qemu'})
                    return rows
                raise AssertionError(path)
        self.api=API()
        class Saved:
            root=outer.root
            target=outer.target
            addresses=[]
            manifest={'scope':target,'project_input_sha256':outer.inputs,
                'source':{'image_id':'approved','template_id':150,'datastore_id':'local','config_sha256':workflow.digest(outer.api.source)},
                'other_team_id':1,'origins':{}}
            def inspect(self,operation):return outer.create if operation=='create' else outer.delete
            def state_addresses(self):return self.addresses
            def apply_saved_plan(self,target,operation,sha,timeout):
                outer.api.live=operation=='create'
                self.addresses=[workflow.scheduling.__name__] if outer.api.live else []
            def generate_delete_plan(self):return outer.delete
        self.saved=Saved()
        class Router:
            rules=False
            def __init__(self,*args,**kwargs):pass
            def install_management_rule(self,target,rule):self.rules=True
            def remove_management_rule(self,target,rule):self.rules=False
            def temporary_rules_absent(self,target):return not self.rules
        class Check:
            def __init__(self,*args,**kwargs):pass
            def run(self,target,check,timeout):return {'passed':True,'fixed_check':check,'output':'retained fake backend evidence'}
        self.Router=Router
        self.Check=Check
        _,inventory,_,policy=placement_test.fixtures()
        node=inventory['nodes']['n1']
        node['architectures']=['amd64']
        self.inventory={'captured_at':time.time(),'nodes':{'proxmox2':node},'guests':{}}
        self.policy=dict(policy,allowed_nodes=['proxmox2'])
        self.catalog={'images':{'approved':{'kind':'vm','verified':True,'approved_projects':[3],
            'artifact_sha256':'a'*64,'replicas':[{'node':'proxmox2','template_id':150,'datastore_id':'local',
                                            'state':'ready','artifact_sha256':'a'*64}]}}}
        self.ledger=ReservationLedger(self.root/'ledger')
        self.addCleanup(self.ledger.close)
        self.patches=[patch.object(workflow.scheduling,'read_settings',return_value=(self.policy,self.catalog)),
            patch('crange.project_dir',return_value=self.project),patch('crange.json_config',return_value={}),
            patch.object(workflow,'FixedRouterUFW',self.Router),patch.object(workflow,'FixedManagementChecks',self.Check),
            patch.object(workflow,'FixedGuestNetworkChecks',self.Check),
            patch.object(workflow.scheduling,'discover',return_value=self.inventory)]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        def run(argv,**kwargs):
            return subprocess.CompletedProcess(argv,0,json.dumps({'project':{'address':'10.60.2.32'},
                'staging':{'id':200,'placement':{'node_name':'proxmox1'},'address':'10.3.0.4','lan':'10.3.254.0/24'}}),'')
        self.run=run

    def backend(self):return workflow.NetworkAcceptanceBackend(self.saved,self.api,self.ledger,run=self.run)

    def test_active_task_check_uses_supported_proxmox_filter(self):
        calls=[]
        class API:
            def request(self,method,path,fields=None):
                calls.append((method,path,fields))
                return []
        self.assertTrue(workflow.no_active_node_tasks(API(),'proxmox1'))
        self.assertEqual(calls,[('GET','/nodes/proxmox1/tasks',{'source':'active'})])

        class BusyAPI:
            def request(self,method,path,fields=None):return [{'upid':'active-task'}]
        self.assertFalse(workflow.no_active_node_tasks(BusyAPI(),'proxmox2'))

    def test_full_lifecycle_reserves_runs_six_artifacts_cleans_and_releases(self):
        backend=self.backend()
        tick=iter(range(100))
        receipt=NetworkProbeRunner(backend,self.root/'journal.json',clock=lambda:next(tick)).run(self.target,
            source_revision='b'*40,tested_at='2026-10-06T12:00:00Z')
        self.assertEqual(len(receipt['checks']),6)
        self.assertEqual(self.ledger.active_reservations(),[])
        self.assertFalse(self.api.live)
        self.assertFalse(backend.router.rules)
        for name in receipt['checks']:
            artifact=self.root/(name+'.log')
            self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(),receipt['checks'][name]['artifact_sha256'])

    def test_unapproved_image_or_changed_source_prevents_allocation(self):
        self.catalog['images']['approved']['approved_projects']=[240]
        with self.assertRaises(ValueError):self.backend()
        self.catalog['images']['approved']['approved_projects']=[3]
        self.api.source=dict(self.api.source,tags='changed')
        with self.assertRaises(ValueError):self.backend()
        self.assertEqual(self.ledger.active_reservations(),[])

    def test_changed_project_inputs_or_linked_clone_prevents_allocation(self):
        (self.project/'main.tf').write_text('changed')
        with self.assertRaises(ValueError):self.backend()
        (self.project/'main.tf').write_text('reviewed project state')
        self.create['resource_changes'][0]['change']['after']['clone'][0]['full']=False
        with self.assertRaises(ValueError):self.backend()
        self.assertEqual(self.ledger.active_reservations(),[])

    def test_excessive_resources_rejected_before_reservation(self):
        self.create['resource_changes'][0]['change']['after']['memory'][0]['dedicated']=8192
        with self.assertRaises(ValueError):self.backend()
        self.assertEqual(self.ledger.active_reservations(),[])
