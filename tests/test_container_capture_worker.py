import contextlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange_capture as capture
from crange_distribution import DistributionError, _source_config_identity

class ContainerCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.events=[];self.source_id=125
        self.source={'hostname':'p100t254-mhl-app01','memory':1024,'cores':1,'unprivileged':1,
                     'rootfs':'local-lvm:vm-125-disk-0,size=8G','net0':'name=eth0,bridge=p100t254,ip=10.100.254.40/24,type=veth','template':0}
        self.clone={**self.source,'hostname':'capture','rootfs':'local-lvm:vm-201-disk-0,size=8G'}
        self.running=False;self.template_null=False
        owner=self
        class API:
            def container(self,node,vmid):return dict(owner.source if vmid==owner.source_id else owner.clone)
            def container_status(self,node,vmid):return 'running' if vmid==201 and owner.running else 'stopped'
            def request(self,method,path,fields=None):
                owner.events.append((method,path,fields))
                if path=='/nodes/proxmox2/status':return {'cpuinfo':{'cpus':4},'loadavg':[0.4]}
                if path=='/cluster/nextid':return 201
                if path.endswith('/storage/archive/status'):return {'shared':1,'active':1,'content':'backup','total':1024**4,'avail':900*1024**3}
                if path=='/pools/crange-probes':return {'members':[{'node':'proxmox2','type':'lxc','vmid':201}]}
                if method=='PUT':owner.clone.update(fields);return None
                if path.endswith('/status/start'):
                    assert 'bridge=isolated,ip=10.100.254.40/24,ip6=manual,link_down=1' in owner.clone['net0']
                    owner.running=True
                if path.endswith('/status/shutdown'):owner.running=False
                if path.endswith('/template'):
                    owner.clone['template']=1
                    if owner.template_null:return None
                return 'UPID:proxmox2:1:1:1:test:201:root:'
            def task(self,*args,**kwargs):pass
        class Ledger:
            def __init__(self,*args):pass
            def reserve_source_probe(self,**kwargs):assert kwargs['kind']=='container';owner.events.append(('reserve',kwargs));return {'reservation_id':'r'}
            def mark_running(self,*args):pass
            def reconcile_job(self,job_id,*,operation_terminal,allocations_reconciled,provider_tasks_running=False):owner.events.append(('reconcile',))
            def close(self):pass
        self.api,self.ledger=API(),Ledger
    def tearDown(self):self.tmp.cleanup()
    def invoke(self,probe_error=None,identity_error=None,platform_source=False):
        def identity(*args,**kwargs):
            self.events.append(('identity',self.running,kwargs['kind']))
            if identity_error:raise identity_error
            return {'source_config_sha256':_source_config_identity(self.clone,201),'source_identity':{'machine_id':'a'*32}}
        def export(*args,**kwargs):self.events.append(('export',self.running,kwargs['kind']))
        policy={'allowed_nodes':['proxmox2'],'approved_backup_storage':[['proxmox2','archive']]}
        with patch.object(capture,'_approved_platform_source'),patch.object(capture,'DATA',Path(self.tmp.name)),patch.object(capture,'_capture_destination',return_value='proxmox2'),patch.object(capture,'capture_policy',return_value=(policy,'isolated','local-lvm','archive')),patch.object(capture,'ReservationLedger',self.ledger),patch.object(capture,'discover',return_value={'guests':{'201':{}}}),patch.object(capture,'node_locks',return_value=contextlib.nullcontext()),patch.object(capture,'capture_source_identity',side_effect=identity),patch.object(capture.time,'monotonic',return_value=0,side_effect=[0,181] if identity_error else None),patch.object(capture,'export_image',side_effect=export),patch('crange_distribution.main',side_effect=probe_error) as probe_main:
            result=capture.capture_container(self.api,project=100,node='proxmox2',source_id=self.source_id,name='capture',description='test',slug='abc',profile={'init_system':'systemd'},platform_source=platform_source,policy_override=policy,ct_exec=Mock(prepare_capture_identity=lambda *a:{'prepared':True}),generalize=lambda *args:{'generalized':True,'machine_id_empty':True,'previous_machine_id':'a'*32})
            args=probe_main.call_args.args[0]
            self.assertEqual(args[:2],['--catalog',str(capture.CONFIG/'image-catalog.json')])
            self.assertIn('--policy',args);self.assertNotIn('--kind',args)
            return result
    def test_platform_debian_sets_nesting_on_the_container_before_start(self):
        self.source_id=3000;self.source['template']=1;self.clone['features']='keyctl=1'
        self.invoke(platform_source=True)
        index=next(i for i,event in enumerate(self.events) if len(event)>2 and event[0]=='PUT' and event[1]=='/nodes/proxmox2/lxc/201/config')
        self.assertEqual(self.events[index][2]['features'],'keyctl=1,nesting=1')
        self.assertLess(index,next(i for i,event in enumerate(self.events) if len(event)>1 and event[1]=='/nodes/proxmox2/lxc/201/status/start'))
        self.assertNotIn('features',self.source)

    def test_reserves_isolates_and_exports_only_after_clean_shutdown(self):
        vmid,evidence=self.invoke();self.assertEqual(vmid,201)
        self.assertLess(next(i for i,e in enumerate(self.events) if e[0]=='reserve'),next(i for i,e in enumerate(self.events) if len(e)>1 and str(e[1]).endswith('/clone')))
        self.assertIn(('identity',True,'container'),self.events)
        self.assertIn(('export',False,'container'),self.events)
        self.assertEqual(json.loads((Path(self.tmp.name)/'capture-journal/capture-p100-abc.json').read_text())['state'],'verified')
    def test_null_template_response_is_verified_without_fabricating_task_id(self):
        self.template_null=True
        self.invoke()
        receipt=json.loads((Path(self.tmp.name)/'capture-journal/capture-p100-abc.json').read_text())
        step=receipt['provider_tasks'][-1]
        self.assertIsNone(step['upid'])
        self.assertEqual(step['completion'],'verified-template-config')
        self.assertEqual(step['state'],'confirmed-terminal')

    def test_probe_failure_cannot_mark_capture_verified(self):
        with self.assertRaisesRegex(DistributionError,'duplicate'):self.invoke(probe_error=DistributionError('duplicate identity'))
        self.assertEqual(json.loads((Path(self.tmp.name)/'capture-journal/capture-p100-abc.json').read_text())['state'],'allocated')
    def test_identity_failure_stops_owned_clone_without_export_or_approval(self):
        with self.assertRaisesRegex(DistributionError,'readiness'):self.invoke(identity_error=DistributionError('readiness failed'))
        self.assertFalse(self.running)
        self.assertFalse(any(e[0]=='export' for e in self.events))
    def test_external_mount_fails_before_allocation_or_clone(self):
        self.source['mp0']='/host/private,mp=/data'
        with self.assertRaisesRegex(DistributionError,'mounts'):self.invoke()
        self.assertFalse(any(e[0]=='reserve' or len(e)>1 and str(e[1]).endswith('/clone') for e in self.events))
