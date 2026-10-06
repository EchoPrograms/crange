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
