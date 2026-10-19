import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import crange_network_adapters as adapters
from crange_network_runner import plan_digest
import test_network_acceptance as evidence_test


class Transport:
    def __init__(self,target):
        self.target=target
        self.calls=[]
        self.tags='crange-network-probe;'+target['job_id']
        self.bridge=target['vnet']
        self.extra_nic=False
        self.polls=0
    def request(self,method,path,fields,deadline):
        self.calls.append((method,path,fields,deadline))
        if path.endswith('/config'):
            cfg={'tags':self.tags,'net0':'virtio=AA:BB:CC:DD:EE:FF,bridge='+self.bridge,
                 'ipconfig0':'ip='+self.target['address']+',gw=10.240.254.1'}
            if self.extra_nic: cfg['net1']='virtio=AA:BB:CC:DD:EE:01,bridge=craccess'
            return cfg
        if path.endswith('/status/current'): return {'status':'running'}
        if path.endswith('/agent/exec'): return {'pid':77}
        if path.endswith('/agent/exec-status'):
            self.polls+=1
            if self.polls==1:return {'exited':False}
            return {'exited':True,'exitcode':0,'out-data':'1450\n'}
        raise AssertionError(path)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.fixture=evidence_test.NetworkAcceptanceTests()
        self.fixture.setUp()

    def test_fixed_owned_diagnostic_keeps_pid_across_poll_responses(self):
        transport=Transport(self.fixture.scope)
        result=adapters.OwnedQGADiagnostics(transport,sleep=lambda _:None).run(self.fixture.scope,'mtu')
        self.assertTrue(result['passed'])
        execute=[call for call in transport.calls if call[1].endswith('/agent/exec')]
        self.assertEqual(execute[0][2]['command'],['/bin/busybox','cat','/sys/class/net/eth0/mtu'])
        polls=[call for call in transport.calls if call[1].endswith('/agent/exec-status')]
        self.assertEqual([call[2]['pid'] for call in polls],[77,77])

    def test_terraform_apply_failure_is_classified_without_persisting_raw_output(self):
        output='Error: HTTP 400 Parameter verification failed; Authorization: PVEAPIToken=private-secret'
        cause=adapters.safe_terraform_apply_cause(output,1)
        self.assertEqual(cause,'proxmox_http_400_validation')
        self.assertNotIn('private-secret',cause)
        self.assertNotIn('Authorization',cause)

    def test_wrong_owner_vnet_and_extra_nic_reject_before_guest_execution(self):
        for mutation in ('owner','vnet','nic'):
            transport=Transport(self.fixture.scope)
            if mutation=='owner':transport.tags='terraform'
            if mutation=='vnet':transport.bridge='p003t254'
            if mutation=='nic':transport.extra_nic=True
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                adapters.OwnedQGADiagnostics(transport).run(self.fixture.scope,'dns')
            self.assertFalse(any(call[0]=='POST' for call in transport.calls))

    def test_real_provider_process_exec_failure_is_not_api_authorization(self):
        output = ('Error: Failed to load plugin schemas\nFailed to instantiate provider '
                  'fork/exec .terraform/providers/terraform-provider-proxmox_v0.104.0: permission denied')
        self.assertEqual(adapters.safe_terraform_apply_cause(output, 1),
                         'terraform_local_provider_exec_permission')
        self.assertEqual(adapters.safe_terraform_apply_cause('permission denied', 1),
                         'terraform_apply_exit_1_detail_redacted')

    def test_apply_retains_private_raw_error_before_safe_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = object.__new__(adapters.SavedTerraformProbe)
            backend.root = pathlib.Path(directory)
            backend.target = self.fixture.scope
            backend.inspect = lambda *a, **kw: self.fixture.create
            backend._env = lambda: {}
            raw = 'HTTP 403 private-token provider diagnostic'
            backend.run = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, 'before error', raw)
            real_fstat = os.fstat
            def root_stat(fd):
                fields = list(real_fstat(fd))
                fields[4] = 0
                return os.stat_result(fields)
            with patch.object(adapters.os, 'fstat', side_effect=root_stat):
                with self.assertRaises(ValueError) as failure:
                    backend.apply_saved_plan(backend.target, 'create', plan_digest(self.fixture.create), 60)
            self.assertEqual(failure.exception.safe_cause, 'proxmox_http_403_authorization')
            self.assertNotIn('private-token', str(failure.exception))
            self.assertEqual((backend.root/'create-apply.stderr').read_text(), raw)
            self.assertEqual((backend.root/'create-apply.stdout').read_text(), 'before error')
            self.assertEqual((backend.root/'create-apply.stderr').stat().st_mode & 0o777, 0o600)

    def test_arbitrary_diagnostic_and_unbounded_timeout_reject(self):
        transport=Transport(self.fixture.scope)
        for diagnostic,timeout in [('shell',30),('dns',61)]:
            with self.assertRaises(ValueError):adapters.OwnedQGADiagnostics(transport).run(self.fixture.scope,diagnostic,timeout=timeout)
        self.assertEqual(transport.calls,[])

    def test_bounded_http_failure_keeps_status_without_private_details(self):
        import urllib.error
        import time
        class API:
            endpoint='https://proxmox.invalid/api2/json'
            token='private-token'
            ssl_context=None
        def denied(*args,**kwargs):
            raise urllib.error.HTTPError('https://private.invalid',403,'private detail',{},None)
        backend=adapters.BoundedPVE(API(),open_url=denied)
        with self.assertRaises(ValueError) as caught:
            backend.request('POST','/nodes/proxmox1/qemu/9911/agent/exec',
                            {'command':['/bin/busybox','true']},deadline=time.monotonic()+10)
        self.assertEqual(caught.exception.safe_cause,'guest_api_http_403')
        self.assertNotIn('private',str(caught.exception))

    def test_transient_get_poll_retries_without_reexecuting_guest_command(self):
        import io, time
        from unittest.mock import Mock
        api=Mock(endpoint='https://proxmox.invalid/api2/json',token='private-token',ssl_context=None)
        opener=Mock(side_effect=[TimeoutError('private detail'),io.StringIO('{"data":{"exited":true}}')])
        backend=adapters.BoundedPVE(api,open_url=opener)
        result=backend.request('GET','/nodes/proxmox1/qemu/9911/agent/exec-status',{'pid':22},deadline=time.monotonic()+30)
        self.assertTrue(result['exited'])
        self.assertEqual(opener.call_count,2)
        self.assertTrue(all(call.args[0].method=='GET' for call in opener.call_args_list))

    def test_exec_timeout_is_not_replayed_and_has_safe_diagnostic(self):
        import time
        from unittest.mock import Mock
        api=Mock(endpoint='https://proxmox.invalid/api2/json',token='private-token',ssl_context=None)
        opener=Mock(side_effect=TimeoutError('private detail'))
        backend=adapters.BoundedPVE(api,open_url=opener)
        with self.assertRaises(ValueError) as caught:
            backend.request('POST','/nodes/proxmox1/qemu/9911/agent/exec',{'command':['true']},deadline=time.monotonic()+30)
        self.assertEqual(opener.call_count,1)
        self.assertEqual(caught.exception.safe_cause,'guest_api_timeout')
        self.assertNotIn('private',str(caught.exception))

    def test_unprotected_real_file_rejects(self):
        with tempfile.NamedTemporaryFile() as stream:
            pathlib.Path(stream.name).chmod(0o666)
            with self.assertRaises(ValueError):adapters.protected(stream.name)

    def test_saved_plan_digests_and_configuration_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            job=root/self.fixture.scope['job_id']
            job.mkdir()
            inputs={'main.tf':b'fixed reviewed configuration','.terraform.lock.hcl':b'provider "registry.terraform.io/bpg/proxmox" { version = "0.104.0" }'}
            for filename,value in inputs.items():(job/filename).write_bytes(value)
            (job/'create.tfplan').write_bytes(b'binary plan')
            review={'scope':self.fixture.scope,'input_sha256':{key:hashlib.sha256(value).hexdigest() for key,value in inputs.items()},
                    'plans':{'create':{'binary_sha256':hashlib.sha256(b'binary plan').hexdigest(),'json_sha256':plan_digest(self.fixture.create)}}}
            (job/'review.json').write_text(json.dumps(review))
            calls=[]
            def run(argv,**kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(argv,0,json.dumps(self.fixture.create),'')
            with patch.object(adapters,'ROOT',root),patch.object(adapters,'protected',side_effect=lambda p,**_:pathlib.Path(p)):
                backend=adapters.SavedTerraformProbe(self.fixture.scope['job_id'],run=run)
                self.assertEqual(backend.inspect('create'),self.fixture.create)
                self.assertEqual(calls[0][:3],['terraform','show','-json'])
                (job/'extra.auto.tfvars.json').write_text('{}')
                with self.assertRaises(ValueError):backend.inspect('create')
                (job/'extra.auto.tfvars.json').unlink()
                (job/'create.tfplan').write_bytes(b'replaced')
                with self.assertRaises(ValueError):backend.inspect('create')


class RouterAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        key=pathlib.Path(temporary.name)/'key'
        key.write_text('test-only-placeholder')
        key.chmod(0o600)
        worker_patcher=patch.object(adapters,'worker_file',side_effect=lambda p,**_:pathlib.Path(p))
        worker_patcher.start()
        self.addCleanup(worker_patcher.stop)
        patcher=patch.object(adapters.FixedRouterUFW,'KEY',key)
        patcher.start()
        self.addCleanup(patcher.stop)
        fixture=evidence_test.NetworkAcceptanceTests()
        fixture.setUp()
        self.target=fixture.scope
        self.rule={'owner':self.target['job_id'],'sources':['10.60.0.20','10.60.0.10'],
                   'destination':'10.240.254.98/32','protocol':'tcp','port':22}

    def test_fixed_management_rules_and_exact_delete(self):
        active={}
        calls=[]
        def run(argv,**kwargs):
            import shlex
            command=shlex.split(argv[-1])
            calls.append(command)
            if command[1:]==['status','numbered']:
                lines=['Status: active']
                for owner,source in active.items():
                    lines.append('[ 1] 10.240.254.98 22/tcp           ALLOW FWD   '+source+'                 # '+owner)
                return subprocess.CompletedProcess(argv,0,'\n'.join(lines),'')
            owner=command[-1]
            source=command[command.index('from')+1]
            if 'delete' in command:active.pop(owner)
            else:active[owner]=source
            return subprocess.CompletedProcess(argv,0,'Rules updated','')
        with patch.object(adapters,'protected',side_effect=lambda p,**_:pathlib.Path(p)):
            backend=adapters.FixedRouterUFW(self.target,project_router='10.60.2.240',team_router='10.240.0.4',run=run)
        backend.install_management_rule(self.target,self.rule)
        self.assertEqual(len(active),2)
        backend.remove_management_rule(self.target,self.rule)
        self.assertEqual(active,{})
        self.assertTrue(backend.temporary_rules_absent(self.target))
        self.assertEqual(sum('delete' in command for command in calls),2)
        self.assertTrue(all(command[1:5]==['--force','route','delete','allow']
                            for command in calls if 'delete' in command))
        self.assertTrue(all(command[0]=='/usr/sbin/ufw' for command in calls))

    def test_padded_status_accepts_host_masks_but_rejects_owned_rule_drift(self):
        owner=self.target['job_id']+'-0'
        valid='[11] 10.240.254.98/32 22/tcp   ALLOW   FWD   10.60.0.20/32   # '+owner
        for line in (valid,valid.replace('22/tcp','22/udp'),
                     valid.replace('10.60.0.20/32','10.60.0.0/24'),
                     valid.replace('ALLOW','DENY'),valid.replace(' FWD',' IN FWD'),
                     valid+' unrelated',valid.replace('10.240.254.98/32','10.240.254.99')):
            with self.subTest(line=line):
                active=[line]
                deleted=[]
                def run(argv,**kwargs):
                    import shlex
                    command=shlex.split(argv[-1])
                    if command[1:]==['status','numbered']:
                        return subprocess.CompletedProcess(argv,0,'Status: active\n'+'\n'.join(active),'')
                    deleted.append(command)
                    active.clear()
                    return subprocess.CompletedProcess(argv,0,'Rules updated','')
                backend=adapters.FixedRouterUFW(self.target,project_router='10.60.2.240',team_router='10.240.0.4',run=run)
                if line==valid:
                    backend.remove_management_rule(self.target,self.rule)
                    self.assertEqual(len(deleted),1)
                    self.assertIn('comment',deleted[0])
                    self.assertEqual(deleted[0][-1],owner)
                else:
                    with self.assertRaises(ValueError):backend.remove_management_rule(self.target,self.rule)
                    self.assertEqual(deleted,[])

    def test_rule_parameters_cannot_expand_source_or_ports(self):
        calls=[]
        with patch.object(adapters,'protected',side_effect=lambda p,**_:pathlib.Path(p)):
            backend=adapters.FixedRouterUFW(self.target,project_router='10.60.2.240',team_router='10.240.0.4',run=lambda *a,**k:calls.append(a))
        rule=dict(self.rule,port=3389)
        with self.assertRaises(ValueError):backend.install_management_rule(self.target,rule)
        self.assertEqual(calls,[])

    def test_wrong_project_router_target_rejected(self):
        with patch.object(adapters,'protected',side_effect=lambda p,**_:pathlib.Path(p)):
            with self.assertRaises(ValueError):
                adapters.FixedRouterUFW(self.target,project_router='10.60.2.240',team_router='10.3.0.4')

    def test_custom_router_ssh_preserves_the_allowed_project_router_source(self):
        target = {**self.target, 'vnet': 'p240c100', 'address': '10.240.100.98/24'}
        with patch.object(adapters,'protected',side_effect=lambda p,**_:pathlib.Path(p)):
            backend = adapters.FixedRouterUFW(target, project_router='10.60.2.240',
                parent_team_router='10.240.0.4', team_router='10.240.254.2')
            self.assertEqual(backend.prefix[-1], 'root@10.240.254.2')
            proxy = backend.prefix[backend.prefix.index('-o', backend.prefix.index('-i')+2)+1]
            self.assertNotIn('10.240.0.4', proxy)
            self.assertIn('10.60.2.240', proxy)
            for address in ('10.241.254.2', '10.240.0.2', '10.240.254.101'):
                with self.subTest(address=address), self.assertRaises(ValueError):
                    adapters.FixedRouterUFW(target, project_router='10.60.2.240',
                        parent_team_router='10.240.0.4', team_router=address)


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        fixture=evidence_test.NetworkAcceptanceTests()
        fixture.setUp()
        self.fixture=fixture
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=pathlib.Path(temporary.name)
        self.rule={'owner':fixture.scope['job_id'],'sources':['10.60.0.20','10.60.0.10'],
                   'destination':'10.240.254.98/32','protocol':'tcp','port':22}
        self.journal={'scope':fixture.scope,'state':'create_pending','rule':self.rule}
        (self.root/'journal.json').write_text(json.dumps(self.journal))
        outer=self
        class API:
            present=True
            owner=fixture.scope['job_id']
            def request(self,method,path,fields=None):
                if path=='/cluster/resources':
                    return [{'vmid':9911,'node':'proxmox2','type':'qemu'}] if self.present else []
                return {'tags':'crange-network-probe;'+self.owner,'net0':'virtio=x,bridge=p240t254',
                        'ipconfig0':'ip=10.240.254.98/24,gw=10.240.254.1'}
        self.api=API()
        class Backend:
            root=outer.root
            target=fixture.scope
            addresses=[adapters.PROBE_ADDRESS]
            generated=False
            applied=False
            fail=False
            def state_addresses(self):return self.addresses
            def generate_delete_plan(self):
                self.generated=True
                return fixture.delete
            def apply_saved_plan(self,target,operation,digest,timeout):
                assert operation=='delete'
                assert digest==plan_digest(fixture.delete)
                self.applied=True
                if self.fail:raise TimeoutError('private-detail')
                outer.api.present=False
                self.addresses=[]
        self.backend=Backend()
        class Router:
            removed=False
            fail=False
            def remove_management_rule(self,target,rule):
                assert rule==outer.rule
                self.removed=True
                if self.fail:raise ValueError('private-detail')
            def temporary_rules_absent(self,target):return not self.fail
        self.router=Router()

    def reconcile(self):
        with patch.object(adapters,'protected',side_effect=lambda p,**_:pathlib.Path(p)):
            return adapters.reconcile_owned_probe(self.backend,self.api,self.router)

    def record(self):return json.loads((self.root/'journal.json').read_text())

    def test_machine_review_owned_destroy_finishes_without_human_approval(self):
        result=self.reconcile()
        self.assertTrue(self.backend.generated)
        self.assertTrue(self.backend.applied)
        self.assertTrue(self.router.removed)
        self.assertEqual(self.record()['state'],'cleanup_verified_reservation_pending')
        self.assertFalse(result['reservation_released'])
        self.assertFalse(result['policy_changed'])

    def test_wrong_live_owner_blocks_all_cleanup_mutations(self):
        self.api.owner='other-job'
        with self.assertRaises(ValueError):self.reconcile()
        self.assertFalse(self.router.removed)
        self.assertFalse(self.backend.generated)

    def test_router_cleanup_failure_still_destroys_probe_and_keeps_uncertain(self):
        self.router.fail=True
        with self.assertRaises(ValueError):self.reconcile()
        self.assertTrue(self.backend.applied)
        self.assertFalse(self.api.present)
        self.assertEqual(self.record()['state'],'cleanup_uncertain')

    def test_absent_guest_with_retained_state_never_blindly_drops_state(self):
        self.api.present=False
        with self.assertRaises(ValueError):self.reconcile()
        self.assertFalse(self.backend.generated)
        self.assertIn('absent_guest_retained_state',self.record()['cleanup_errors'])

    def test_absent_guest_and_empty_state_can_reconcile_remaining_rule(self):
        self.api.present=False
        self.backend.addresses=[]
        result=self.reconcile()
        self.assertTrue(self.router.removed)
        self.assertFalse(self.backend.generated)
        self.assertFalse(self.backend.applied)
        self.assertTrue(result['rules_absent'])
        self.assertFalse(result['reservation_released'])
        self.assertEqual(self.record()['state'],'cleanup_verified_reservation_pending')

    def test_destroy_timeout_does_not_claim_cleanup(self):
        self.backend.fail=True
        with self.assertRaises(ValueError):self.reconcile()
        self.assertTrue(self.api.present)
        self.assertEqual(self.record()['state'],'cleanup_uncertain')
        self.assertEqual(self.record()['destroy_failure_cause'],'timeout_detail_redacted')
        self.assertNotIn('private-detail',(self.root/'journal.json').read_text())

    def test_local_destroy_validation_failure_records_only_stage(self):
        self.backend.generate_delete_plan=lambda: (_ for _ in ()).throw(ValueError('private detail'))
        with self.assertRaises(ValueError):self.reconcile()
        self.assertEqual(self.record()['destroy_failure_cause'],'reconcile_destroy_plan_generation_validation')
        self.assertNotIn('private detail',(self.root/'journal.json').read_text())


class PartialCreateRecoveryTests(unittest.TestCase):
    def setUp(self):
        fixture=evidence_test.NetworkAcceptanceTests(); fixture.setUp()
        self.fixture=fixture
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=pathlib.Path(temporary.name)
        self.plan=fixture.create
        planned=self.plan['resource_changes'][0]['change']['after']
        planned.update({'name':'crange-network-p240-p2-staging',
                        'clone':[{'vm_id':149,'node_name':fixture.scope['node'],'full':True}],
                        'cpu':[{'cores':1}], 'memory':[{'dedicated':512}],
                        'disk':[{'datastore_id':'local-lvm','size':20}]})
        self.expected=adapters.probe_from_plan(self.plan,'create',fixture.scope)
        self.source={'template':1,'tags':'crange-cache-149;crange-image',
                     'net0':'virtio=AA:BB:CC:DD:EE:FF,bridge=crprobe1450,firewall=1,link_down=1,mtu=1450',
                     'ipconfig0':'ip=10.3.254.96/24,gw=10.3.254.1'}
        manifest={'source':{'template_id':149,'config_sha256':adapters._config_digest(self.source)}}
        expected=self.expected
        class Backend:
            root=self.root
            target=fixture.scope
            def inspect(inner,operation):
                assert operation=='create'
                return self.plan
            def state_addresses(inner):return [adapters.PROBE_ADDRESS]
        self.backend=Backend()
        self.backend.manifest=manifest
        attrs={key:expected.get(key) for key in ('vm_id','node_name','name','pool_id','tags','clone')}
        attrs['id']=str(fixture.scope['vmid'])
        state={'resources':[{'mode':'managed','type':'proxmox_virtual_environment_vm',
                'name':'network_acceptance_probe','instances':[{'attributes':attrs}]}]}
        (self.root/'terraform.tfstate').write_text(json.dumps(state))
        self.journal={'scope':fixture.scope,'state':'cleanup_uncertain',
                      'cause':'proxmox_http_403_authorization','create_plan_sha256':plan_digest(self.plan)}
        self.live={'name':expected['name'],'tags':self.source['tags'],'template':0,'lock':None,
                   'cores':expected['cpu'][0]['cores'],'memory':str(expected['memory'][0]['dedicated']),
                   'net0':'virtio=AA:BB:CC:DD:EE:01,bridge=crprobe1450,firewall=1,link_down=1,mtu=1450',
                   'ipconfig0':self.source['ipconfig0']}
        outer=self
        class API:
            def request(inner,method,path,fields=None):
                if path=='/cluster/resources':
                    return [{'vmid':fixture.scope['vmid'],'node':fixture.scope['node'],'type':'qemu',
                             'name':expected['name'],'pool':'crange-probes','status':'stopped'}]
                if path.endswith('/qemu/149/config'):return outer.source
                if path.endswith('/qemu/'+str(fixture.scope['vmid'])+'/config'):return outer.live
                raise AssertionError(path)
        self.api=API()

    def check(self):
        with patch.object(adapters,'protected',side_effect=lambda p,**_:pathlib.Path(p)):
            return adapters.partial_create_probe(self.backend,self.api,self.fixture.scope,self.journal)

    def test_exact_stopped_source_clone_is_cleanup_owned(self):
        self.assertEqual(self.check()['live'],self.live)

    def test_partial_delete_plan_accepts_only_source_baseline_single_delete(self):
        before={'id':str(self.fixture.scope['vmid']),'vm_id':self.fixture.scope['vmid'],
                'node_name':self.fixture.scope['node'],'pool_id':'crange-probes',
                'name':self.expected['name'],'tags':self.source['tags'].split(';'),'template':False,
                'clone':[{'vm_id':149,'node_name':self.fixture.scope['node'],'full':True}],
                'disk':[{'datastore_id':'local-lvm','size':20,'path_in_datastore':f'vm-{self.fixture.scope["vmid"]}-disk-0'}],
                'network_device':[{'bridge':'crprobe1450','disconnected':True}],
                'initialization':[{'ip_config':[{'ipv4':[{'address':'10.3.254.96/24','gateway':'10.3.254.1'}]}]}]}
        delete={'errored':False,'resource_changes':[{'address':adapters.PROBE_ADDRESS,'mode':'managed',
                'type':'proxmox_virtual_environment_vm','change':{'actions':['delete'],'before':before}}]}
        self.assertEqual(adapters.partial_delete_plan(delete,self.fixture.scope,self.plan,self.source),before)
        before['network_device'][0]['disconnected']=False
        with self.assertRaises(ValueError):adapters.partial_delete_plan(delete,self.fixture.scope,self.plan,self.source)

    def test_saved_plan_inspection_requires_explicit_partial_proof(self):
        before={'id':str(self.fixture.scope['vmid']),'vm_id':self.fixture.scope['vmid'],
                'node_name':self.fixture.scope['node'],'pool_id':'crange-probes',
                'name':self.expected['name'],'tags':self.source['tags'].split(';'),'template':False,
                'clone':[{'vm_id':149,'node_name':self.fixture.scope['node'],'full':True}],
                'disk':[{'datastore_id':'local-lvm','size':20,'path_in_datastore':f'vm-{self.fixture.scope["vmid"]}-disk-0'}],
                'network_device':[{'bridge':'crprobe1450','disconnected':True}],
                'initialization':[{'ip_config':[{'ipv4':[{'address':'10.3.254.96/24','gateway':'10.3.254.1'}]}]}]}
        delete={'errored':False,'resource_changes':[{'address':adapters.PROBE_ADDRESS,'mode':'managed',
                'type':'proxmox_virtual_environment_vm','change':{'actions':['delete'],'before':before}}]}
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            create_bytes=b'approved create plan'; delete_bytes=b'approved delete plan'
            (root/'create.tfplan').write_bytes(create_bytes); (root/'delete.tfplan').write_bytes(delete_bytes)
            plans={'create':{'binary_sha256':hashlib.sha256(create_bytes).hexdigest(),
                             'json_sha256':plan_digest(self.plan)},
                   'delete':{'binary_sha256':hashlib.sha256(delete_bytes).hexdigest(),
                             'json_sha256':plan_digest(delete)}}
            saved=adapters.SavedTerraformProbe.__new__(adapters.SavedTerraformProbe)
            saved.root=root; saved.target=self.fixture.scope; saved.manifest={'plans':plans}
            def run_saved(argv,**kwargs):
                if argv[1:3]==['state','list']:
                    return subprocess.CompletedProcess(argv,0,adapters.PROBE_ADDRESS+'\n','')
                return subprocess.CompletedProcess(argv,0,
                    json.dumps(self.plan if 'create.tfplan' in argv[-1] else delete),'')
            saved.run=run_saved
            with patch.object(adapters.SavedTerraformProbe,'_check_inputs',return_value=None), \
                 patch.object(adapters,'protected',side_effect=lambda p,**_:pathlib.Path(p)):
                with self.assertRaises(ValueError):saved.inspect('delete')
                self.assertEqual(saved.inspect('delete',partial_source=self.source),delete)

    @unittest.skipUnless(os.geteuid()==0,'Protected fixture integration requires root-owned inputs.')
    def test_protected_partial_clone_fixture_validates_full_saved_delete_path(self):
        before={'id':str(self.fixture.scope['vmid']),'vm_id':self.fixture.scope['vmid'],
                'node_name':self.fixture.scope['node'],'pool_id':'crange-probes',
                'name':self.expected['name'],'tags':self.source['tags'].split(';'),'template':False,
                'clone':[{'vm_id':149,'node_name':self.fixture.scope['node'],'full':True}],
                'disk':[{'datastore_id':'local-lvm','size':20,'path_in_datastore':f'vm-{self.fixture.scope["vmid"]}-disk-0'}],
                'network_device':[{'bridge':'crprobe1450','disconnected':True}],
                'initialization':[{'ip_config':[{'ipv4':[{'address':self.source['ipconfig0'].split('=')[1].split(',')[0],
                                                           'gateway':self.source['ipconfig0'].split('gw=')[1]}]}]}]}
        delete={'errored':False,'resource_changes':[{'address':adapters.PROBE_ADDRESS,'mode':'managed',
                'type':'proxmox_virtual_environment_vm','change':{'actions':['delete'],'before':before}}]}
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            (root/'terraform.tfstate').write_text((self.root/'terraform.tfstate').read_text())
            create_bytes=b'protected reviewed create plan'; delete_bytes=b'protected reviewed delete plan'
            (root/'create.tfplan').write_bytes(create_bytes); (root/'delete.tfplan').write_bytes(delete_bytes)
            plans={'create':{'binary_sha256':hashlib.sha256(create_bytes).hexdigest(),
                             'json_sha256':plan_digest(self.plan)},
                   'delete':{'binary_sha256':hashlib.sha256(delete_bytes).hexdigest(),
                             'json_sha256':plan_digest(delete)}}
            saved=adapters.SavedTerraformProbe.__new__(adapters.SavedTerraformProbe)
            saved.root=root; saved.target=self.fixture.scope
            saved.manifest={'plans':plans,'source':{'template_id':149,
                'config_sha256':adapters._config_digest(self.source)}}
            def run_saved(argv,**kwargs):
                if argv[1:3]==['state','list']:
                    return subprocess.CompletedProcess(argv,0,adapters.PROBE_ADDRESS+'\n','')
                return subprocess.CompletedProcess(argv,0,
                    json.dumps(self.plan if 'create.tfplan' in argv[-1] else delete),'')
            saved.run=run_saved
            with patch.object(saved,'_check_inputs',return_value=None):
                proof=adapters.partial_create_probe(saved,self.api,self.fixture.scope,self.journal)
                recheck_journal=dict(self.journal,state='reconcile_destroy_pending')
                rechecked=adapters.partial_create_probe(saved,self.api,self.fixture.scope,recheck_journal)
                checked=saved.inspect('delete',partial_source=proof['source'])
            self.assertEqual(rechecked['live'],proof['live'])
            self.assertEqual(checked,delete)
            self.assertEqual(adapters.partial_delete_plan(checked,self.fixture.scope,self.plan,self.source),before)

    def test_partial_create_proof_rejects_changed_network_or_journal(self):
        self.live['net0']=self.live['net0'].replace('crprobe1450','p240t254')
        with self.assertRaises(ValueError):self.check()
        self.live['net0']=self.live['net0'].replace('p240t254','crprobe1450')
        self.journal['cause']='proxmox_http_400_validation'
        with self.assertRaises(ValueError):self.check()


class ManagementOriginTests(unittest.TestCase):
    def setUp(self):
        fixture=evidence_test.NetworkAcceptanceTests()
        fixture.setUp()
        self.target=fixture.scope
        self.origins={'controller':{'node':'proxmox1','vmid':114,'address':'10.60.0.20','kind':'lxc'},
                      'guacamole':{'node':'proxmox1','vmid':110,'address':'10.60.0.10','kind':'qemu'}}
        outer=self
        class Transport:
            guac_ip='10.60.0.10'
            calls=[]
            def request(self,method,path,fields,deadline):
                self.calls.append((method,path,fields))
                if path=='/cluster/resources':return [
                    {'vmid':9911,'node':'proxmox2','type':'qemu','status':'running'},
                    {'vmid':110,'node':'proxmox1','type':'qemu','status':'running'},
                    {'vmid':114,'node':'proxmox1','type':'lxc','status':'running'}]
                if path.endswith('/9911/config'):return {
                    'tags':'crange-network-probe;'+outer.target['job_id'],
                    'net0':'virtio=x,bridge=p240t254','ipconfig0':'ip=10.240.254.98/24,gw=10.240.254.1'}
                if path.endswith('/114/config'):return {'net0':'ip=10.60.0.20/24,bridge=craccess'}
                if path.endswith('/status/current'):return {'status':'running'}
                if path.endswith('/agent/network-get-interfaces'):
                    return {'result':[{'ip-addresses':[{'ip-address':self.guac_ip,'ip-address-type':'ipv4'}]}]}
                if path.endswith('/agent/exec'):return {'pid':55}
                if path.endswith('/agent/exec-status'):return {'exited':True,'exitcode':255,
                    'err-data':'debug1: Connection established.\ndebug1: Remote protocol version 2.0\nPermission denied'}
                raise AssertionError(path)
        self.transport=Transport()
        self.transport.calls=[]

    def test_controller_test_binds_actual_controller_source(self):
        class Socket:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def settimeout(self,value):self.timeout=value
            def bind(self,address):self.source=address
            def connect(self,address):self.destination=address
            def recv(self,count):return b'SSH-2.0-OpenSSH_test\r\n'
        client=Socket()
        checker=adapters.FixedManagementChecks(self.transport,self.origins,connect_socket=lambda *args:client)
        result=checker.run(self.target,'controller')
        self.assertTrue(result['passed'])
        self.assertEqual(client.source,('10.60.0.20',0))
        self.assertEqual(client.destination,('10.240.254.98',22))
        self.assertFalse(any(call[0]=='POST' for call in self.transport.calls))

    def test_guac_test_uses_fixed_relay_and_never_calls_guest_exec_api(self):
        calls=[]
        def relay(target,origin,*,timeout):
            calls.append((target,origin,timeout))
            return {'passed':True,'origin':origin,'output':'SSH-2.0-test'}
        checker=adapters.FixedManagementChecks(self.transport,self.origins,guac_probe=relay)
        result=checker.run(self.target,'guacamole')
        self.assertTrue(result['passed'])
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][:2],(self.target,self.origins['guacamole']))
        self.assertTrue(1 <= calls[0][2] <= 30)
        self.assertFalse(any('/agent/' in call[1] for call in self.transport.calls))
        self.assertFalse(any(call[0]=='POST' for call in self.transport.calls))

    def test_unexpected_guac_source_is_rejected_before_exec(self):
        self.transport.guac_ip='10.60.0.11'
        checker=adapters.FixedManagementChecks(self.transport,self.origins)
        with self.assertRaisesRegex(ValueError,'relay is unavailable'):
            checker.run(self.target,'guacamole')
        self.assertFalse(any('/agent/' in call[1] for call in self.transport.calls))

    def test_origin_ids_cannot_be_substituted(self):
        self.origins['guacamole']['vmid']=125
        with self.assertRaises(ValueError):adapters.FixedManagementChecks(self.transport,self.origins)


class GeneratedDestroyPlanTests(unittest.TestCase):
    def test_programmatic_review_rejects_extra_mutation_before_recording_approval(self):
        fixture=evidence_test.NetworkAcceptanceTests()
        fixture.setUp()
        for extra in (False,True):
            with self.subTest(extra=extra),tempfile.TemporaryDirectory() as directory:
                root=pathlib.Path(directory)
                backend=adapters.SavedTerraformProbe.__new__(adapters.SavedTerraformProbe)
                backend.root=root
                backend.target=fixture.scope
                backend.manifest={'scope':fixture.scope}
                backend._check_inputs=lambda:None
                backend.state_addresses=lambda:[adapters.PROBE_ADDRESS]
                backend._env=lambda:{}
                plan=json.loads(json.dumps(fixture.delete))
                if extra:plan['resource_changes'].append(plan['resource_changes'][0])
                calls=[]
                def run(argv,**kwargs):
                    calls.append(argv)
                    if argv[1]=='plan':
                        (root/'delete.tfplan').write_bytes(b'fixed generated saved plan')
                        (root/'delete.tfplan').chmod(0o660)
                        return subprocess.CompletedProcess(argv,0,'','')
                    return subprocess.CompletedProcess(argv,0,json.dumps(plan),'')
                backend.run=run
                with patch.object(adapters,'protected',side_effect=lambda p,**_:pathlib.Path(p)):
                    if extra:
                        with self.assertRaises(ValueError):backend.generate_delete_plan()
                        self.assertNotIn('plans',backend.manifest)
                    else:
                        result=backend.generate_delete_plan()
                        self.assertEqual(result,fixture.delete)
                        self.assertEqual((root/'delete.tfplan').stat().st_mode & 0o777, 0o600)
                        self.assertEqual(backend.manifest['plans']['delete']['json_sha256'],plan_digest(result))
                        self.assertEqual(backend.manifest['plans']['delete']['review'],'fixed-owned-probe-delete')
                        self.assertTrue((root/'delete-plan.json').exists())
                        self.assertTrue((root/'review.json').exists())
                self.assertEqual(calls[0][1:4],['plan','-destroy','-no-color'])
                self.assertFalse(any(argv[1]=='apply' for argv in calls))
