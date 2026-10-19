import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from crange_jobs import JobStore
from crange_network_worker import (declared_vnets, declared_router_path, probe_resources, scope_from_declared_state,
    reconcile_startup, _resource_config, _run, _git_revision_args)
from crange_network_guac_probe import request as guac_relay_request


class NetworkWorkerTests(unittest.TestCase):
    def test_probe_uses_small_approved_source_and_preserves_explicit_profile(self):
        self.assertEqual(probe_resources({}, {'memory': 256, 'cores': 1}), (512, 1))
        self.assertEqual(probe_resources({}, {'memory': '512', 'cores': '1'}), (512, 1))
        self.assertEqual(probe_resources({}, {'memory': 2048, 'cores': 2}), (2048, 2))
        self.assertEqual(probe_resources({'probe_resources': {'memory_mb': 1024, 'cpu_cores': 1}},
                                        {'memory': 2048, 'cores': 2}), (1024, 1))
        for image, source in [({}, {'memory': True}), ({}, {'memory': '512MB'}), ({}, {'memory': 8192}),
                              ({}, {'cores': 8}), ({'probe_resources': {'memory_mb': 128}}, {})]:
            with self.subTest(image=image, source=source), self.assertRaises(ValueError):
                probe_resources(image, source)

    def setUp(self):
        self.cfg = {'project_id': 3, 'team_ids': ['1', '2'], 'jump_boxes': {},
                    'scenario_boxes': {}, 'scenario_containers': {}}

    def test_scope_is_derived_from_declared_standard_network_and_free_allocations(self):
        target = scope_from_declared_state(3, 'proxmox2', 'p003t254', 'a' * 32,
            self.cfg, used_addresses={'10.3.254.2', '10.3.1.2'},
            used_vmids={9000, 9002}, reserved_scopes=[{'vmid': 9001}], next_vmid=9000)
        self.assertEqual(target, {'project_id': 3, 'node': 'proxmox2', 'vnet': 'p003t254',
                                  'address': '10.3.254.3/24', 'vmid': 9003,
                                  'job_id': 'network-' + 'a' * 32})
        self.assertEqual(declared_vnets(3, self.cfg), {'p003t001', 'p003t002', 'p003t254'})

    def test_sixth_node_is_a_valid_candidate_but_unknown_nodes_are_not(self):
        target = scope_from_declared_state(3, 'proxmox6', 'p003t254', 'c' * 32,
            self.cfg, used_addresses=set(), used_vmids=set(), reserved_scopes=[], next_vmid=9000)
        self.assertEqual(target['node'], 'proxmox6')

    def test_scope_refuses_cross_project_custom_or_undeclared_lans(self):
        for project, node, vnet in ((3, 'proxmox2', 'p004t254'),
                                    (3, 'proxmox2', 'p003c100'),
                                    (3, 'proxmox7', 'p003t254'),
                                    (4, 'proxmox2', 'p003t254')):
            with self.subTest(project=project, node=node, vnet=vnet), self.assertRaises(ValueError):
                scope_from_declared_state(project, node, vnet, 'b' * 32, self.cfg,
                    used_addresses=set(), used_vmids=set(), reserved_scopes=[], next_vmid=9000)

    def test_generated_cloud_init_gateway_is_bare_ipv4(self):
        target={'job_id':'network-'+'a'*32,'node':'proxmox1','vnet':'p100t254',
                'address':'10.100.254.2/24','vmid':900001}
        source={'template_id':700001,'datastore_id':'local-lvm'}
        config=_resource_config(target,source,{'net0':'virtio=AA,bridge=vmbr0'})
        self.assertIn('gateway = "10.100.254.1"',config)
        self.assertNotIn('gateway = "10.100.254.1/24"',config)

    def test_custom_probe_uses_declared_subnet_and_parent_chain(self):
        self.cfg['custom_networks'] = {'test': {'owner_team_id': '254',
            'network_id': 100, 'uplink_last_octet': 2}}
        routers = {'project': {'address': '10.60.2.32'},
                   'staging': {'lan': '10.3.254.0/24', 'address': '10.3.0.4'},
                   'custom_networks': {'test': {'vnet': 'p003c100', 'network_id': 100,
                       'owner_team_id': '254', 'lan': '10.3.100.0/24',
                       'address': '10.3.254.2', 'upstream_address': '10.3.254.2'}}}
        path = declared_router_path(3, self.cfg, routers, 'p003c100')
        self.assertEqual([row['address'] for row in path],
                         ['10.60.2.32', '10.3.0.4', '10.3.254.2'])
        target = scope_from_declared_state(3, 'proxmox2', 'p003c100', 'a'*32,
            self.cfg, used_addresses=set(), used_vmids=set(), reserved_scopes=[], next_vmid=9000)
        self.assertEqual(target['address'], '10.3.100.2/24')
        routers['custom_networks']['test']['upstream_address'] = '10.3.1.2'
        with self.assertRaisesRegex(ValueError, 'differs'):
            declared_router_path(3, self.cfg, routers, 'p003c100')

    def test_custom_network_collisions_and_undeclared_owners_are_rejected(self):
        for owner, number in [('99', 100), ('254', 254), ('254', 1), ('254', '100')]:
            self.cfg['custom_networks'] = {'test': {'owner_team_id': owner, 'network_id': number}}
            with self.subTest(owner=owner, number=number), self.assertRaises(ValueError):
                declared_vnets(3, self.cfg)

    def test_git_revision_uses_only_the_fixed_repository_as_safe_directory(self):
        repository=pathlib.Path('/home/crange/crange')
        self.assertEqual(_git_revision_args(repository),
            ['git','-c','safe.directory=/home/crange/crange','rev-parse','HEAD'])

    def test_failed_terraform_stage_stores_exact_stderr_only_in_private_bundle(self):
        import stat
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as directory:
            result=SimpleNamespace(returncode=1,stdout='provider details',stderr='Invalid gateway schema')
            with patch('crange_network_worker.subprocess.run',return_value=result), \
                 contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError,'command failed'):
                _run(['terraform','plan'],cwd=directory)
            artifact=pathlib.Path(directory)/'worker-plan.stderr'
            self.assertEqual(stat.S_IMODE(artifact.stat().st_mode),0o600)
            self.assertEqual(artifact.read_text(),'returncode=1\nInvalid gateway schema')
            self.assertNotIn('provider details',str(artifact.read_text()))

    def test_successful_root_verification_publishes_exact_source_without_second_click(self):
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            store=JobStore(pathlib.Path(temp)/'jobs.sqlite3')
            job=store.submit('alice',3,'network-verify',{'node':'proxmox2','network':'p003t254'})
            with patch('crange_jobs.subprocess.run',return_value=subprocess.CompletedProcess([],0,'accepted','')) as run, \
                 patch.object(store,'_network_journal_state',return_value='accepted_cleaned'):
                self.assertTrue(store.run_next(network_only=True))
            self.assertEqual(len(run.call_args_list),2)
            command=run.call_args_list[1].args[0]
            self.assertEqual(pathlib.Path(command[1]).name,'crange_network_publish.py')
            self.assertEqual(command[2:],[job,'--project','3'])
            self.assertEqual(store.get(job,[],admin=True)['status'],'succeeded')

    def test_root_lane_claims_only_network_jobs_and_never_accepts_a_missing_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(pathlib.Path(directory) / 'jobs.sqlite3')
            network = store.submit('alice', 3, 'network-verify',
                                   {'node': 'proxmox2', 'network': 'p003t254'})
            ordinary = store.submit('alice', 3, 'team-add', {'team': 1})
            with patch('crange_jobs.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')) as execute:
                self.assertTrue(store.run_next(network_only=True))
            args = execute.call_args.args[0]
            self.assertEqual(pathlib.Path(args[1]).name, 'crange_network_worker.py')
            self.assertEqual(args[2:4], ['run', network])
            self.assertEqual(store.get(network, ['3'])['status'], 'failed')
            self.assertEqual(store.get(ordinary, ['3'])['status'], 'queued')

    def test_reconciliation_required_stays_active_and_cannot_be_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(pathlib.Path(directory) / 'jobs.sqlite3')
            job = store.submit('alice', 3, 'network-verify',
                               {'node': 'proxmox2', 'network': 'p003t254'})
            with store.connect() as db:
                db.execute("UPDATE jobs SET status='reconcile-required' WHERE id=?", (job,))
            self.assertFalse(store.cancel(job, ['3']))
            page = store.list_page(['3'], page=1, project=3)
            self.assertEqual(page['jobs'][0]['status'], 'reconcile-required')
            self.assertEqual(page['total'], 0)

    def test_ordinary_worker_startup_preserves_running_network_cleanup_jobs(self):
        import threading
        with tempfile.TemporaryDirectory() as directory:
            store=JobStore(pathlib.Path(directory)/'jobs.sqlite3')
            verify=store.submit('alice',3,'network-verify',{'node':'proxmox2','network':'p003t254'})
            reconcile=store.submit('admin',3,'network-reconcile',{'source_job_id':'a'*32})
            ordinary=store.submit('alice',3,'team-add',{'team':1})
            with store.connect() as db:
                db.execute("UPDATE jobs SET status='running' WHERE id IN (?,?,?)",
                           (verify,reconcile,ordinary))
            stop=threading.Event(); stop.set()
            store.worker(stop)
            self.assertEqual(store.get(verify,['3'])['status'],'running')
            self.assertEqual(store.get(reconcile,['3'])['status'],'running')
            self.assertEqual(store.get(ordinary,['3'])['status'],'interrupted')

    def test_root_recovery_keeps_uncertain_job_active_and_does_not_touch_other_lanes(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            base=pathlib.Path(directory); store=JobStore(base/'jobs.sqlite3')
            network=store.submit('alice',3,'network-verify',{'node':'proxmox2','network':'p003t254'})
            other=store.submit('admin',0,'template-publish-sealed-windows-p100',{})
            with store.connect() as db:
                db.execute("UPDATE jobs SET status='running' WHERE id IN (?,?)",(network,other))
            bundle=base/('network-'+network);bundle.mkdir(mode=0o700)
            journal=bundle/'journal.json'
            journal.write_text(json.dumps({'state':'cleanup_uncertain'}));journal.chmod(0o600)
            with patch('crange_network_worker.PROBE_ROOT',base), \
                 patch('crange_network_worker.subprocess.run',return_value=SimpleNamespace(returncode=1)):
                reconcile_startup(store)
            self.assertEqual(store.get(network,['3'])['status'],'reconcile-required')
            self.assertEqual(store.get(other,[],admin=True)['status'],'running')

    def test_root_recovery_marks_terminal_only_after_terminal_journal(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            base=pathlib.Path(directory);store=JobStore(base/'jobs.sqlite3')
            network=store.submit('alice',3,'network-verify',{'node':'proxmox2','network':'p003t254'})
            with store.connect() as db:
                db.execute("UPDATE jobs SET status='running',cancel_requested=1 WHERE id=?",(network,))
            bundle=base/('network-'+network);bundle.mkdir(mode=0o700)
            journal=bundle/'journal.json'
            journal.write_text(json.dumps({'state':'failed_cleaned'}));journal.chmod(0o600)
            with patch('crange_network_worker.PROBE_ROOT',base):
                reconcile_startup(store)
            self.assertEqual(store.get(network,['3'])['status'],'interrupted')

    def test_real_dispatch_socket_response_decodes_in_actual_guac_client(self):
        import json
        from crange_ct_read_relay import dispatch
        from unittest.mock import patch
        target={'project_id':100,'node':'proxmox2','vnet':'p100t254',
                'address':'10.100.254.2/24','vmid':900001,'job_id':'network-'+'a'*32,
                'guac_node':'proxmox4','relay_sha256':'c'*64}
        diagnostic={'passed':True,'origin':{'node':'proxmox4','vmid':110,
            'address':'10.60.0.10','kind':'qemu'},'source_address':'10.60.0.10',
            'target_address':'10.100.254.2','target_port':22,'relay_sha256':'c'*64,'output':'SSH-2.0-owned'}
        scopes=[]
        def host_read(request,marker,policy):
            scopes.append(marker)
            return json.dumps(diagnostic)
        class Socket:
            def __init__(self,*args):self.sent=None
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def settimeout(self,value):pass
            def connect(self,path):pass
            def sendall(self,value):self.sent=json.loads(value)
            def recv(self,size):
                # Serialize the production dispatch response; no invented wrapper.
                return json.dumps(dispatch(self.sent,{'peer_uid':101000},101000)).encode()+b'\n'
        with patch('crange_ct_read_relay.socket.gethostname',return_value='proxmox4'), \
             patch('crange_ct_read_relay.host_read',side_effect=host_read):
            result=guac_relay_request(target,socket_factory=Socket)
        self.assertEqual(result,diagnostic)
        self.assertEqual(scopes,[{key:value for key,value in target.items() if key not in ('guac_node','relay_sha256')}])

    def test_guac_relay_client_sends_only_exact_probe_scope_and_checks_banner(self):
        fixture = __import__('test_network_acceptance').NetworkAcceptanceTests()
        fixture.setUp()
        target = {**fixture.scope, 'guac_node': 'proxmox4','relay_sha256':'c'*64}
        class Socket:
            def __init__(self, *args): self.sent = b''
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, value): self.timeout = value
            def connect(self, path): self.path = path
            def sendall(self, value): self.sent = value
            def recv(self, size):
                response = {'value': {'passed': True,
                    'origin': {'node': 'proxmox4', 'vmid': 110,
                               'address': '10.60.0.10', 'kind': 'qemu'},
                    'source_address': '10.60.0.10',
                    'target_address': target['address'].split('/')[0], 'target_port': 22,'relay_sha256':'c'*64,
                    'output': 'SSH-2.0-Guac-test'}}
                return json.dumps(response).encode() + b'\n'
        import json
        sockets = []
        def factory(*args):
            client = Socket(*args); sockets.append(client); return client
        result = guac_relay_request(target, socket_factory=factory)
        self.assertTrue(result['passed'])
        sent = json.loads(sockets[0].sent)
        self.assertEqual(sent, {'command': 'network-probe-guac-origin', 'node': 'proxmox4',
            'probe_node': fixture.scope['node'],
            'project_id': fixture.scope['project_id'], 'vnet': fixture.scope['vnet'],
            'job_id': fixture.scope['job_id'].removeprefix('network-'),
            'address': fixture.scope['address'].split('/')[0],
            'vmid': fixture.scope['vmid']})


if __name__ == '__main__':
    unittest.main()
