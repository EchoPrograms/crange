import hashlib
import json
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

    def test_wrong_owner_vnet_and_extra_nic_reject_before_guest_execution(self):
        for mutation in ('owner','vnet','nic'):
            transport=Transport(self.fixture.scope)
            if mutation=='owner':transport.tags='terraform'
            if mutation=='vnet':transport.bridge='p003t254'
            if mutation=='nic':transport.extra_nic=True
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                adapters.OwnedQGADiagnostics(transport).run(self.fixture.scope,'dns')
            self.assertFalse(any(call[0]=='POST' for call in transport.calls))

    def test_arbitrary_diagnostic_and_unbounded_timeout_reject(self):
        transport=Transport(self.fixture.scope)
        for diagnostic,timeout in [('shell',30),('dns',61)]:
            with self.assertRaises(ValueError):adapters.OwnedQGADiagnostics(transport).run(self.fixture.scope,diagnostic,timeout=timeout)
        self.assertEqual(transport.calls,[])

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
                    lines.append('[ 1] 10.240.254.98 22/tcp ALLOW FWD '+source+' # '+owner)
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
        self.assertTrue(all(command[0]=='/usr/sbin/ufw' for command in calls))

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

    def test_destroy_timeout_does_not_claim_cleanup(self):
        self.backend.fail=True
        with self.assertRaises(ValueError):self.reconcile()
        self.assertTrue(self.api.present)
        self.assertEqual(self.record()['state'],'cleanup_uncertain')
        self.assertNotIn('private-detail',(self.root/'journal.json').read_text())


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
                    {'vmid':9911,'node':'proxmox2','type':'qemu'},
                    {'vmid':110,'node':'proxmox1','type':'qemu'},
                    {'vmid':114,'node':'proxmox1','type':'lxc'}]
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

    def test_guac_test_uses_vm110_qga_source_bind_and_disables_credentials(self):
        checker=adapters.FixedManagementChecks(self.transport,self.origins,sleep=lambda _:None)
        result=checker.run(self.target,'guacamole')
        self.assertTrue(result['passed'])
        execute=[call for call in self.transport.calls if call[0]=='POST'][0]
        self.assertEqual(execute[1],'/nodes/proxmox1/qemu/110/agent/exec')
        command=execute[2]['command']
        self.assertIn('10.60.0.10',command)
        self.assertIn('PreferredAuthentications=none',command)
        self.assertIn('PubkeyAuthentication=no',command)
        self.assertEqual(command[-1],'root@10.240.254.98')

    def test_unexpected_guac_source_is_rejected_before_exec(self):
        self.transport.guac_ip='10.60.0.11'
        checker=adapters.FixedManagementChecks(self.transport,self.origins)
        with self.assertRaises(ValueError):checker.run(self.target,'guacamole')
        self.assertFalse(any(call[0]=='POST' for call in self.transport.calls))

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
                        self.assertEqual(backend.manifest['plans']['delete']['json_sha256'],plan_digest(result))
                        self.assertEqual(backend.manifest['plans']['delete']['review'],'fixed-owned-probe-delete')
                        self.assertTrue((root/'delete-plan.json').exists())
                        self.assertTrue((root/'review.json').exists())
                self.assertEqual(calls[0][1:4],['plan','-destroy','-no-color'])
                self.assertFalse(any(argv[1]=='apply' for argv in calls))
