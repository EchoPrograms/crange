import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_ct_read_relay import (validate_request, validate_guest, dispatch,
    offline_config_snapshot, sealed_windows_proof_snapshot, audit_scope,
    network_guac_origin_probe)


class RelayTests(unittest.TestCase):
    def test_private_bridge_requests_have_no_arbitrary_paths_or_commands(self):
        config={'peer_uid':1000,'node_addresses':{'proxmox5':'10.0.0.105'}}
        prepare={'command':'capture-bridge-prepare','node':'proxmox5'}
        self.assertEqual(validate_request(prepare,config,1000)[:3],('proxmox5',0,'capture-bridge-prepare'))
        for invalid in ({**prepare,'script':'shell'}, {**prepare,'node':'proxmox6'}):
            with self.assertRaises(ValueError):validate_request(invalid,config,1000)
        with self.assertRaises(ValueError):validate_request(prepare,config,1001)
        proof={'command':'capture-bridge-proof','node':'proxmox5','vmid':131,'marker':'crange-capture-'+'a'*12}
        self.assertEqual(validate_request(proof,config,1000)[1],131)
        with self.assertRaises(ValueError):validate_request({**proof,'vmid':'131'},config,1000)

    def test_guac_qga_binds_actual_source_and_owned_target_and_polls_one_pid(self):
        from crange_ct_read_relay import fixed_guac_qga_handshake
        expected={'address':'10.100.254.2/24'}
        calls=[]; records=[]
        def run(command, **kwargs):
            calls.append(command)
            if command[1]=='create':
                self.assertEqual([row[0] for row in records], ['claim'])
                return subprocess.CompletedProcess(command,0,json.dumps({'pid':55}),'')
            self.assertEqual([row[0] for row in records][:3], ['claim','submit','pid'])
            value={'source':'10.60.0.10','target':'10.100.254.2','port':22,'banner':'SSH-2.0-owned'}
            return subprocess.CompletedProcess(command,0,json.dumps({'exited':True,'exitcode':0,'out-data':json.dumps(value)}),'')
        banner=fixed_guac_qga_handshake(expected,run=run,persist=lambda stage,value:records.append((stage,value)))
        self.assertEqual(banner,'SSH-2.0-owned')
        self.assertIn('/nodes/proxmox4/qemu/110/agent/exec',calls[0])
        script=calls[0][-1]
        self.assertIn("s.bind(('10.60.0.10',0))",script)
        self.assertIn("s.connect(('10.100.254.2',22))",script)
        self.assertEqual(calls[1][calls[1].index('--pid')+1],'55')
        self.assertEqual(records[-1][0],'result')

    def test_guac_qga_retains_raw_submit_before_invalid_pid_rejection(self):
        from crange_ct_read_relay import fixed_guac_qga_handshake
        records=[]
        with self.assertRaisesRegex(ValueError,'PID'):
            fixed_guac_qga_handshake({'address':'10.100.254.2/24'},
                run=lambda command,**kwargs:subprocess.CompletedProcess(command,0,json.dumps({'pid':'bad'}),''),
                persist=lambda stage,value:records.append((stage,value)))
        self.assertEqual([row[0] for row in records],['claim','submit'])
        self.assertIn('bad',records[1][1]['stdout'])

    def test_network_guac_request_requires_exact_declared_scope_and_peer(self):
        request={'command':'network-probe-guac-origin','node':'proxmox4','probe_node':'proxmox2','project_id':100,
                 'vnet':'p100t254','address':'10.100.254.2','vmid':900001,
                 'job_id':'a'*32}
        origin_node, vmid, command, expected = validate_request(request, {'peer_uid':101000}, 101000)
        self.assertEqual(origin_node, 'proxmox4')
        self.assertEqual(expected['node'], 'proxmox2')
        custom = {**request, 'vnet':'p100c100', 'address':'10.100.100.2'}
        self.assertEqual(validate_request(custom, {'peer_uid':101000}, 101000)[3]['vnet'], 'p100c100')
        with self.assertRaises(ValueError):
            validate_request({**custom,'address':'10.100.254.2'}, {'peer_uid':101000}, 101000)
        for mutation, uid in (({'node':'proxmox3'},101000), ({'probe_node':'evil'},101000), ({'port':22},101000),
                              ({'address':'10.100.1.2'},101000), ({'vmid':110},101000),
                              ({'job_id':'../etc/passwd'},101000), ({},0)):
            with self.subTest(mutation=mutation,uid=uid), self.assertRaises(ValueError):
                validate_request({**request,**mutation}, {'peer_uid':101000}, uid)

    def test_network_guac_probe_checks_owned_bundle_exact_live_tuple_and_revalidates(self):
        import tempfile
        from crange_ct_read_relay import _network_config_digest
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); root.chmod(0o700)
            expected={'project_id':100,'node':'proxmox4','vnet':'p100t254',
                      'address':'10.100.254.2/24','vmid':900001,
                      'job_id':'network-'+'a'*32}
            bundle=root/expected['job_id']; bundle.mkdir(mode=0o700)
            source={'template':1,'tags':'crange-image;approved'}
            manifest={'scope':expected,'source':{'template_id':800001,
                'config_sha256':_network_config_digest(source)}}
            (bundle/'review.json').write_text(json.dumps(manifest)); (bundle/'review.json').chmod(0o600)
            (bundle/'journal.json').write_text(json.dumps({'scope':expected,'state':'testing','active_check':'guacamole'}))
            (bundle/'journal.json').chmod(0o600)
            probe={'tags':f'crange-network-probe;{expected["job_id"]}', 'template':0,
                   'net0':'virtio=AA,bridge=p100t254,firewall=1',
                   'ipconfig0':'gw=10.100.254.1,ip=10.100.254.2/24'}
            origin={'name':'guac'}
            resources=[{'type':'qemu','node':'proxmox4','vmid':110}]
            interfaces={'result':[{'ip-addresses':[{'ip-address':'10.60.0.10'}]}]}
            observations = {'count':0}
            def get(path,*args):
                observations['count'] += 1
                if path.endswith('/900001/config'): return probe
                if path.endswith('/900001/status/current'): return {'status':'running'}
                if path=='/pools/crange-probes': return {'members':[{'type':'qemu','node':'proxmox4','vmid':900001,'cpu':observations['count']}]}
                if path.endswith('/800001/config'): return source
                if path.endswith('/110/config'): return origin
                if path.endswith('/110/status/current'): return {'status':'running'}
                if path=='/cluster/resources': return [{**row,'uptime':observations['count']} for row in resources]
                if path.endswith('/110/agent/network-get-interfaces'): return interfaces
                raise AssertionError((path,args))
            class Stream:
                def __enter__(self): return self
                def __exit__(self,*args): pass
                def settimeout(self,_value): pass
                def recv(self,_size): return b'SSH-2.0-fixed-guac\r\n'
            result=network_guac_origin_probe(expected,get=get,connect=lambda *_args,**_kwargs:Stream(),
                                             bundle_root=root,owner_uid=__import__('os').getuid(),guest_probe=lambda target:'SSH-2.0-fixed-guac')
            self.assertEqual(result['origin'],{'node':'proxmox4','vmid':110,
                'address':'10.60.0.10','kind':'qemu'})
            self.assertTrue(result['passed'])
            journal=json.loads((bundle/'journal.json').read_text())
            journal['active_check']='controller'
            (bundle/'journal.json').write_text(json.dumps(journal))
            with self.assertRaisesRegex(ValueError,'live-check phase'):
                network_guac_origin_probe(expected,get=get,connect=lambda *_args,**_kwargs:Stream(),
                                          bundle_root=root,owner_uid=__import__('os').getuid(),guest_probe=lambda target:'SSH-2.0-fixed-guac')
            (bundle/'journal.json').unlink()
            (bundle/'journal.json').symlink_to(bundle/'review.json')
            with self.assertRaises(OSError):
                network_guac_origin_probe(expected,get=get,connect=lambda *_args,**_kwargs:Stream(),
                                          bundle_root=root,owner_uid=__import__('os').getuid(),guest_probe=lambda target:'SSH-2.0-fixed-guac')

    def test_network_guac_relay_dispatch_is_local_p4_only(self):
        from unittest.mock import patch
        request={'command':'network-probe-guac-origin','node':'proxmox4','probe_node':'proxmox2','project_id':100,
                 'vnet':'p100t254','address':'10.100.254.2','vmid':900001,'job_id':'a'*32}
        policy={'peer_uid':101000}
        with patch('crange_ct_read_relay.socket.gethostname',return_value='proxmox4'), \
             patch('crange_ct_read_relay.host_read',return_value=json.dumps({'passed':True,'output':'SSH-2.0-owned'})) as read, \
             patch('crange_ct_read_relay.subprocess.run') as run:
            self.assertEqual(dispatch(request,policy,101000),{'value':{'passed':True,'output':'SSH-2.0-owned'}})
        read.assert_called_once()
        run.assert_not_called()
        with patch('crange_ct_read_relay.socket.gethostname',return_value='proxmox3'), \
             patch('crange_ct_read_relay.host_read') as read, \
             self.assertRaisesRegex(ValueError,'fixed local P4'):
            dispatch(request,policy,101000)
        read.assert_not_called()

    def test_sealed_windows_proof_audit_scope_does_not_invent_a_vmid(self):
        self.assertEqual(audit_scope({'node': 'proxmox4', 'command': 'sealed-windows-source-proof'}, True),
                         {'node': 'proxmox4', 'command': 'sealed-windows-source-proof'})

    def test_sealed_windows_snapshot_returns_only_fixed_protected_retirement_receipt_when_guests_absent(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        import crange_ct_read_relay as relay
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'retirement.json'
            path.write_text('{"schema_version":1,"state":"proof-guests-retired"}')
            path.chmod(0o600)
            receipt = {'schema_version': 1, 'state': 'proof-guests-retired'}
            with patch.object(relay, '_sealed_windows_retirement_receipt', return_value=receipt), \
                 patch.object(relay, '_sealed_config_identity', return_value=
                    '5e9fdb9337ec631b74100dc1c1f4d7ed') as digest:
                # A fake digest is rejected before any historical proof is returned.
                get = Mock(side_effect=lambda path, *args: (
                    {'status': 'stopped'} if path.endswith('/status/current') else
                    [] if path == '/cluster/resources' else {}))
                with self.assertRaisesRegex(ValueError, 'frozen source'):
                    relay.sealed_windows_proof_snapshot(get=get, read_files=lambda: {'proof': 1})
                digest.return_value = '5e9fdb9337ec631b74100dc1c1f47ffdfb02f8873b32ee6119b49bc7cef27e9d'
                result = relay.sealed_windows_proof_snapshot(get=get, read_files=lambda: {'proof': 1})
                self.assertEqual(result['retirement']['state'], 'proof-guests-retired')
                self.assertEqual(result['live']['probes'], [])
                self.assertEqual(result['files'], {'proof': 1})
            with patch.object(relay, 'SEALED_WINDOWS_RETIREMENT_PATH', str(path)), \
                 self.assertRaisesRegex(ValueError, 'ownership or mode'):
                relay._sealed_windows_retirement_receipt()

    def setUp(self):
        self.request = {'node': 'proxmox2', 'vmid': 8840, 'command': 'machine-id'}
        self.policy = {'peer_uid': 101000, 'allowed_guests': {'proxmox2': {'8840': 'crange-job-source'}},
                       'node_addresses': {'proxmox2': '10.0.0.102'}}
        self.guest = {'tags': 'crange-image;crange-job-source', 'unprivileged': 1,
                      'net0': 'name=eth0,bridge=crprobe1450,link_down=1', 'template': 0}
        self.members = [{'type': 'lxc', 'node': 'proxmox2', 'vmid': 8840}]

    def test_current_probe_marker_wins_over_inherited_capture_marker(self):
        from crange_distribution import owned_container_marker, DistributionError
        capture='crange-capture-'+'a'*12
        self.assertEqual(owned_container_marker({'tags':'crange-image;'+capture+';crange-job-probe'}),'crange-job-probe')
        self.assertEqual(owned_container_marker({'tags':'crange-image;'+capture}),capture)
        with self.assertRaises(DistributionError):owned_container_marker({'tags':'crange-job-one;crange-job-two'})

    def test_owned_capture_commands_remain_peer_scope_and_marker_bound(self):
        request={'node':'proxmox2','vmid':8841,'command':'capture-container-generalize',
                 'marker':'crange-capture-'+'a'*12}
        self.assertEqual(validate_request(request,self.policy,101000)[-1],request['marker'])
        for changes,uid in [({},0),({'marker':'crange-job-source'},101000),
                            ({'node':'proxmox1'},101000),({'path':'/etc/shadow'},101000),
                            ({'vmid':True},101000)]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                validate_request({**request,**changes},self.policy,uid)
        self.guest['tags']='crange-image;'+request['marker']
        validate_guest(self.guest,self.members,'proxmox2',8840,request['marker'])
        for changes in [{'unprivileged':0},{'template':1},{'net0':'name=eth0,bridge=vmbr0'},
                        {'mp0':'/private,mp=/private'}]:
            with self.assertRaises(ValueError):validate_guest({**self.guest,**changes},self.members,'proxmox2',8840,request['marker'])

    def test_compact_cache_reader_binds_vmid_and_hidden_job(self):
        job='cache-'+'a'*32
        request={'node':'proxmox2','vmid':8840,'command':'owned-image-read',
                 'marker':'crange-cache-8840','cache_job_id':job,'read':'machine-id'}
        validate_request(request,self.policy,101000)
        guest={**self.guest,'tags':'crange-image;crange-cache-8840',
               'description':'CRANGE verification '+job}
        validate_guest(guest,self.members,'proxmox2',8840,request['marker'],job)
        for changes in ({'cache_job_id':'wrong'},{'marker':'crange-cache-8841'},{'vmid':True}):
            with self.assertRaises(ValueError):validate_request({**request,**changes},self.policy,101000)
        with self.assertRaises(ValueError):
            validate_guest({**guest,'description':'CRANGE verification cache-'+'b'*32},
                           self.members,'proxmox2',8840,request['marker'],job)
        backing={k:v for k,v in request.items() if k!='read'}
        backing.update(command='owned-linked-backing',source_id=8839,source_marker='crange-cache-8839')
        validate_request(backing,self.policy,101000)
        with self.assertRaises(ValueError):
            validate_request({**backing,'source_marker':'crange-cache-8840'},self.policy,101000)

    def test_forwarded_owned_reader_program_runs_without_file_context(self):
        import shlex
        request={'node':'proxmox2','vmid':8841,'command':'owned-image-read',
                 'marker':'crange-capture-'+'a'*12,'read':'machine-id'}
        def run(command,**kwargs):
            args=shlex.split(command[-1]);self.assertEqual(args[:2],['python3','-c'])
            namespace={'__name__':'relay_test'}
            program=args[2].rsplit('\nprint(json.dumps(',1)[0]
            exec(program,namespace)
            self.assertEqual(len(namespace['RELAY_CODE_SHA256']),64)
            return subprocess.CompletedProcess(command,0,json.dumps({'value':'a'*32}),'')
        self.assertEqual(dispatch(request,self.policy,101000,run=run),{'value':'a'*32})

    def test_authentication_scope_and_commands_are_exact(self):
        validate_request(self.request, self.policy, 101000)
        for changes, uid in [({}, 0), ({'vmid': 8841}, 101000), ({'vmid': True}, 101000),
                ({'node': 'proxmox1'}, 101000), ({'command': 'sh'}, 101000),
                ({'argv': ['/bin/sh']}, 101000), ({'node': 'proxmox2;id'}, 101000)]:
            with self.subTest(changes=changes, uid=uid), self.assertRaises(ValueError):
                validate_request({**self.request, **changes}, self.policy, uid)

    def test_offline_snapshot_request_is_exactly_allowlisted_and_reader_pinned(self):
        policy={'peer_uid':101000,'offline_config_reader_node':'proxmox4',
            'offline_config_guests':{'proxmox2':{
                '113':{'kind':'lxc','required_tags':['p100','network-infrastructure']},
                '147':{'kind':'qemu','required_tags':['crange-image','crange-cache-147']}}}}
        request={'node':'proxmox4','guest_node':'proxmox2','kind':'lxc','vmid':113,
                 'command':'offline-config-snapshot'}
        validate_request(request,policy,101000)
        for mutation in ({'node':'proxmox2'}, {'vmid':114}, {'kind':'qemu'},
                         {'guest_node':'proxmox6'}, {'path':'/etc/shadow'}):
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                validate_request({**request,**mutation},policy,101000)
        with self.assertRaises(ValueError):
            validate_request({**request,'vmid':147,'kind':'lxc'},policy,101000)

    def test_sealed_windows_proof_request_is_one_fixed_peer_authenticated_operation(self):
        request = {'node': 'proxmox4', 'command': 'sealed-windows-source-proof'}
        policy = {'peer_uid': 101000, 'sealed_windows_reader_node': 'proxmox4'}
        validate_request(request, policy, 101000)
        for mutation, uid in (({'node': 'proxmox3'}, 101000), ({'path': '/etc/shadow'}, 101000),
                              ({'vmid': 8912}, 101000), ({}, 0)):
            with self.subTest(mutation=mutation, uid=uid), self.assertRaises(ValueError):
                validate_request({**request, **mutation}, policy, uid)

    def test_windows_seed_cleanup_relay_accepts_only_build_id_and_fixed_readiness(self):
        ready={'command':'windows-seed-cleanup-ready'}
        self.assertEqual(validate_request(ready,{'peer_uid':101000},101000)[:3],
                         ('proxmox4',0,'windows-seed-cleanup-ready'))
        cleanup={'command':'windows-seed-cleanup','build_id':'a'*32}
        self.assertEqual(validate_request(cleanup,{'peer_uid':101000},101000)[:3],
                         ('proxmox4',0,'windows-seed-cleanup'))
        for bad,uid in (({**cleanup,'volid':'local:iso/anything.iso'},101000),
                        ({**cleanup,'node':'proxmox5'},101000),
                        ({'command':'windows-seed-cleanup','build_id':'../etc/passwd'},101000),
                        (ready,0), ({**ready,'build_id':'a'*32},101000)):
            with self.subTest(bad=bad,uid=uid), self.assertRaises(ValueError):
                validate_request(bad,{'peer_uid':101000},uid)
        self.assertEqual(audit_scope(cleanup,True),{'command':'windows-seed-cleanup','build_id':'a'*32})

    def test_windows_seed_cleanup_readiness_dispatch_is_fixed_to_p4(self):
        from unittest.mock import patch
        request={'command':'windows-seed-cleanup-ready'}
        policy={'peer_uid':101000}
        response={'schema_version':1,'cleanup_supported':True,'relay_sha256':'a'*64}
        with patch('crange_ct_read_relay.socket.gethostname',return_value='proxmox4'), \
             patch('crange_ct_read_relay.host_read',return_value=json.dumps(response)) as read:
            self.assertEqual(dispatch(request,policy,101000),{'value':response})
        read.assert_called_once_with(request,None,policy=policy)
        with patch('crange_ct_read_relay.socket.gethostname',return_value='proxmox3'):
            with self.assertRaisesRegex(ValueError,'fixed local P4'):
                dispatch(request,policy,101000)

    def test_windows_seed_cleanup_resumes_one_durable_upid_without_resubmitting_delete(self):
        import tempfile
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        import crange_ct_read_relay as relay
        build='b'*32; node='proxmox5'; vmid=4421
        volid=f'local:iso/crange-win-{build}.iso'
        created=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
        secret={'build_id':build,'node':node,'vmid':vmid,'volid':volid,
            'artifact_name':build+'.iso','state':'upload_uncertain','created_at':created,
            'iso_size':1234,'iso_sha256':'c'*64}
        record={'id':build,'status':'failed','vmid':vmid,'definition':{
            'node':node,'windows_unattended':True}}
        request={'command':'windows-seed-cleanup','build_id':build}
        journal={}; calls=[]; inspections=iter([{'exists':True},{'exists':False}])
        def remote(target, payload, *, run):
            calls.append(payload.copy())
            if payload['action']=='inspect': return next(inspections)
            if payload['action']=='delete': return {'upid':'UPID:proxmox5:AB:CD:EF:imgcopy:4421:root@pam:'}
            if payload['action']=='task':
                if sum(call['action']=='task' for call in calls)==1: raise TimeoutError('interrupted poll')
                return {'status':'stopped','exitstatus':'OK'}
            raise AssertionError(payload['action'])
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(relay,'WINDOWS_SEED_CLEANUP_DIR',Path(temp)), \
             patch.object(relay,'_controller_ct_identity',return_value=(234,(1,2,100000))), \
             patch.object(relay,'_verify_controller_ct_identity'), \
             patch.object(relay,'_read_ct_json',side_effect=lambda _pid,path,*args: secret if 'windows-build-secrets' in path else record), \
             patch.object(relay,'_read_root_cleanup_journal',side_effect=lambda _path,_identity: journal.copy() if journal else None), \
             patch.object(relay,'_write_root_cleanup_journal',side_effect=lambda _path,value: journal.update(value)), \
             patch.object(relay,'_windows_seed_remote',side_effect=remote):
            with self.assertRaises(TimeoutError):
                relay.windows_seed_cleanup(request,101000,{'peer_uid':101000})
            self.assertEqual(journal['state'],'task_pending')
            result=relay.windows_seed_cleanup(request,101000,{'peer_uid':101000})
        self.assertTrue(result['verified_absent'])
        self.assertEqual(journal['state'],'cleaned')
        deletes=[call for call in calls if call['action']=='delete']
        self.assertEqual(len(deletes),1)
        tasks=[call for call in calls if call['action']=='task']
        self.assertEqual([call['upid'] for call in tasks],['UPID:proxmox5:AB:CD:EF:imgcopy:4421:root@pam:']*2)

    def test_sealed_windows_proof_dispatch_is_fixed_to_local_p4_reader(self):
        from unittest.mock import patch
        request = {'node': 'proxmox4', 'command': 'sealed-windows-source-proof'}
        policy = {'peer_uid': 101000, 'sealed_windows_reader_node': 'proxmox4'}
        with patch('crange_ct_read_relay.socket.gethostname', return_value='proxmox4'), \
             patch('crange_ct_read_relay.host_read', return_value='fixed-proof') as read, \
             patch('crange_ct_read_relay.subprocess.run') as run:
            self.assertEqual(dispatch(request, policy, 101000), {'value': 'fixed-proof'})
        read.assert_called_once_with(request, None, policy=policy)
        run.assert_not_called()

    def test_sealed_windows_snapshot_revalidates_fixed_files_source_clones_and_tasks(self):
        source = {'name': 'candidate', 'tags': 'crange-image;crange-capture-windows-server2025-locale-20261008',
                  'sata0': 'crange-library:8912/vm-8912-disk-1.raw,size=64G'}
        configs = {8913: {'name': 'crange-win-locale-proof-8913', 'tags': 'crange-image;p100',
                          'net0': 'e1000=BC:24:11:C7:52:9C', 'smbios1': 'uuid=first', 'vmgenid': 'first'},
                   8914: {'name': 'crange-win-locale-proof-8914', 'tags': 'crange-image;p100',
                          'net0': 'e1000=BC:24:11:3A:C4:7E', 'smbios1': 'uuid=second', 'vmgenid': 'second'}}
        files = {'probe8913': {'document': {'upid': 'UPID:proxmox4:AA:BB:CC:qmclone:8912:root@pam:'}},
                 'probe8914': {'document': {'upid': 'UPID:proxmox4:DD:EE:FF:qmclone:8912:root@pam:'}}}
        def read_files(): return copy.deepcopy(files)
        def get(path):
            if path == '/nodes/proxmox4/qemu/8912/config': return source
            if path == '/nodes/proxmox4/qemu/8912/status/current': return {'status': 'stopped'}
            if path == '/pools/crange-probes': return {'members': [
                {'type': 'qemu', 'node': 'proxmox4', 'vmid': 8913},
                {'type': 'qemu', 'node': 'proxmox4', 'vmid': 8914}]}
            for vmid in (8913, 8914):
                if path == f'/nodes/proxmox4/qemu/{vmid}/config': return configs[vmid]
                if path == f'/nodes/proxmox4/qemu/{vmid}/status/current': return {'status': 'running'}
                if path.endswith('/status') and 'qmclone:8912' in path:
                    return {'status': 'stopped', 'exitstatus': 'OK'}
            raise AssertionError(path)
        result = sealed_windows_proof_snapshot(get=get, read_files=read_files)
        self.assertEqual(set(result), {'files', 'live'})
        self.assertEqual(result['live']['source']['status'], 'stopped')
        self.assertTrue(all(row['probe_pool_member'] for row in result['live']['probes']))
        with self.assertRaisesRegex(ValueError, 'changed during snapshot'):
            snapshots = iter((files, {**files, 'probe8913': {'document': {'upid': 'changed'}}}))
            sealed_windows_proof_snapshot(get=get, read_files=lambda: next(snapshots))

    def test_offline_snapshot_returns_only_limits_after_offline_identity_and_digest_checks(self):
        from unittest.mock import Mock
        policy={'offline_config_guests':{'proxmox2':{
            '113':{'kind':'lxc','required_tags':['p100','network-infrastructure']}}}}
        request={'guest_node':'proxmox2','kind':'lxc','vmid':113}
        nodes=[{'node':'proxmox2','status':'offline'}]
        resource={'node':'proxmox2','type':'lxc','vmid':113,'status':'unknown',
                  'tags':'network-infrastructure;p100;range-lan;terraform'}
        config=(b'cores: 1\nmemory: 512\nrootfs: local-lvm:vm-113-disk-0,size=8G\n'
                b'tags: network-infrastructure;p100;range-lan;terraform\ntemplate: 0\n')
        get=Mock(side_effect=[nodes,[resource],nodes,[resource]])
        snapshot=offline_config_snapshot(request,policy,get=get,read_bytes=lambda _path:config,now=1000)
        self.assertEqual(snapshot['memory_bytes'],512*1024**2)
        self.assertEqual(snapshot['cpu_cores'],1)
        self.assertEqual(snapshot['disk_bytes'],8*1024**3)
        self.assertEqual(snapshot['status'],'offline')
        self.assertEqual(len(snapshot['config_sha256']),64)
        self.assertNotIn('config',snapshot)
        with self.assertRaisesRegex(ValueError,'offline'):
            offline_config_snapshot(request,policy,get=lambda _path,*_args:[{'node':'proxmox2','status':'online'}],
                                    read_bytes=lambda _path:config,now=1000)

    def test_offline_snapshot_accounts_template_disk_without_resource_ram_cpu(self):
        policy={'offline_config_guests':{'proxmox2':{
            '147':{'kind':'qemu','required_tags':['crange-image','crange-cache-147']}}}}
        request={'guest_node':'proxmox2','kind':'qemu','vmid':147}
        nodes=[{'node':'proxmox2','status':'offline'}]
        resource={'node':'proxmox2','type':'qemu','vmid':147,'status':'unknown',
                  'tags':'crange-cache-147;crange-image'}
        config=b'cores: 1\nmemory: 512\nscsi0: local-lvm:base-147-disk-0,size=20G\ntags: crange-cache-147;crange-image\ntemplate: 1\n'
        calls=iter([nodes,[resource],nodes,[resource]])
        snapshot=offline_config_snapshot(request,policy,get=lambda *_args:next(calls),
                                         read_bytes=lambda _path:config,now=1000)
        self.assertTrue(snapshot['template'])
        self.assertEqual(snapshot['disk_bytes'],20*1024**3)

    def test_offline_snapshot_dispatch_is_local_fixed_read_and_never_ssh_or_exec(self):
        from unittest.mock import patch
        policy={'peer_uid':101000,'offline_config_reader_node':'proxmox4',
            'offline_config_guests':{'proxmox2':{
                '113':{'kind':'lxc','required_tags':['p100','network-infrastructure']}}}}
        request={'node':'proxmox4','guest_node':'proxmox2','kind':'lxc','vmid':113,
                 'command':'offline-config-snapshot'}
        with patch('crange_ct_read_relay.socket.gethostname',return_value='proxmox4'), \
             patch('crange_ct_read_relay.host_read',return_value='{}') as read, \
             patch('crange_ct_read_relay.subprocess.run') as run:
            self.assertEqual(dispatch(request,policy,101000),{'value':'{}'})
        read.assert_called_once_with(request,None,policy=policy)
        run.assert_not_called()

    def test_pool_ownership_privilege_mounts_and_isolation_fail_closed(self):
        validate_guest(self.guest, self.members, 'proxmox2', 8840, 'crange-job-source')
        for mutation in [{'unprivileged': 0}, {'template': 1}, {'tags': 'crange-image'},
                {'mp0': '/root,mp=/data'}, {'dev0': '/dev/foo'}, {'lxc.mount.entry': '/etc'},
                {'net0': 'name=eth0,bridge=craccess,link_down=1'},
                {'net1': 'name=eth1,bridge=crprobe1450'}, {'net0': ''}]:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_guest({**self.guest, **mutation}, self.members, 'proxmox2', 8840, 'crange-job-source')
        for members in [[], self.members * 2, [{**self.members[0], 'type': 'qemu'}], [{**self.members[0], 'node': 'proxmox1'}]]:
            with self.assertRaises(ValueError):
                validate_guest(self.guest, members, 'proxmox2', 8840, 'crange-job-source')

    def test_backing_request_requires_exact_approved_source(self):
        self.policy['allowed_guests']['proxmox2']['20000'] = 'crange-job-probe'
        request = {'node': 'proxmox2', 'vmid': 20000, 'source_id': 8840, 'command': 'linked-backing'}
        validate_request(request, self.policy, 101000)
        for mutation in [{'source_id': 8810}, {'source_id': 20000}, {'source_id': True}]:
            with self.assertRaises(ValueError):
                validate_request({**request, **mutation}, self.policy, 101000)

    def test_physical_backing_proof_refuses_wrong_origin_and_never_executes_guest(self):
        from unittest.mock import patch
        from crange_ct_read_relay import host_read
        source = {**self.guest, 'template': 1, 'rootfs': 'local-lvm:base-8840-disk-0,size=4G'}
        probe = {**self.guest, 'tags': 'crange-image;crange-job-probe', 'rootfs': 'local-lvm:vm-20000-disk-0,size=4G'}
        members = self.members + [{'type': 'lxc', 'node': 'proxmox2', 'vmid': 20000}]
        origin = ['base-8840-disk-0']
        def call(argv, **kwargs):
            if argv[0] == 'pvesh':
                path = argv[2]
                if path == '/pools/crange-probes': return json.dumps({'members': members})
                if path.endswith('/status/current'): return json.dumps({'status': 'stopped'})
                return json.dumps(source if '/8840/' in path else probe)
            if argv[0] == 'pvesm': return '/dev/pve/' + argv[2].split(':')[1] + '\n'
            if argv[0] == 'lvs': return json.dumps({'report': [{'lv': [{'lv_name': 'vm-20000-disk-0', 'origin': origin[0]}]}]})
            self.fail('Unexpected guest execution or host command')
        request = {'node': 'proxmox2', 'vmid': 20000, 'source_id': 8840, 'command': 'linked-backing'}
        with patch('crange_ct_read_relay.socket.gethostname', return_value='proxmox2'), patch('crange_ct_read_relay.subprocess.check_output', side_effect=call):
            self.assertTrue(json.loads(host_read(request, 'crange-job-probe', 'crange-job-source'))['verified'])
            origin[0] = 'base-8810-disk-0'
            with self.assertRaisesRegex(ValueError, 'origin differs'):
                host_read(request, 'crange-job-probe', 'crange-job-source')

    def test_dedicated_thin_backing_requires_exact_p5_storage_and_origin(self):
        from unittest.mock import patch
        from crange_ct_read_relay import host_read
        source={**self.guest,'template':1,'rootfs':'crange-thin:base-8840-disk-0,size=4G'}
        probe={**self.guest,'tags':'crange-image;crange-job-probe','rootfs':'crange-thin:vm-20000-disk-0,size=4G'}
        members=[{**m,'node':'proxmox5'} for m in self.members]+[{'type':'lxc','node':'proxmox5','vmid':20000}]
        store={'type':'lvmthin','vgname':'hdd-lvm','thinpool':'crange_data','nodes':'proxmox5','shared':0}
        origin=['base-8840-disk-0']
        def call(argv,**kwargs):
            if argv[0]=='pvesh':
                path=argv[2]
                if path=='/storage/crange-thin':return json.dumps(store)
                if path=='/pools/crange-probes':return json.dumps({'members':members})
                if path.endswith('/status/current'):return json.dumps({'status':'stopped'})
                return json.dumps(source if '/8840/' in path else probe)
            if argv[0]=='pvesm':return '/dev/hdd-lvm/'+argv[2].split(':')[1]+'\n'
            if argv[0]=='lvs':return json.dumps({'report':[{'lv':[{'lv_name':'vm-20000-disk-0','origin':origin[0]}]}]})
            self.fail('Unexpected guest execution or host command')
        request={'node':'proxmox5','vmid':20000,'source_id':8840,'command':'linked-backing'}
        with patch('crange_ct_read_relay.socket.gethostname',return_value='proxmox5'),patch('crange_ct_read_relay.subprocess.check_output',side_effect=call):
            self.assertTrue(json.loads(host_read(request,'crange-job-probe','crange-job-source'))['verified'])
            store['shared']=1
            with self.assertRaisesRegex(ValueError,'storage changed'):host_read(request,'crange-job-probe','crange-job-source')
            store['shared']=0;origin[0]='wrong-source'
            with self.assertRaisesRegex(ValueError,'origin differs'):host_read(request,'crange-job-probe','crange-job-source')

    def test_container_exec_uses_bounded_unix_transport(self):
        import socket
        import tempfile
        import threading
        from unittest.mock import patch
        from crange_distribution import SSHContainerExec
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'relay.sock'
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path)); listener.listen(1)
            seen = []
            def server():
                conn, _ = listener.accept()
                with conn:
                    seen.append(json.loads(conn.recv(4096)))
                    conn.sendall(json.dumps({'value': 'a' * 32}).encode() + b'\n')
            thread = threading.Thread(target=server); thread.start()
            try:
                with patch('crange_distribution.Path', return_value=path):
                    result = SSHContainerExec()('proxmox2', 8840, ['/bin/cat', '/etc/machine-id'])
                self.assertEqual(result, 'a' * 32)
                self.assertEqual(seen, [self.request])
            finally:
                thread.join(timeout=3); listener.close()

    def test_dispatch_uses_fixed_remote_program_and_never_forwards_arbitrary_shell(self):
        run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({'value': 'a'*32}), ''))
        self.assertEqual(dispatch(self.request, self.policy, 101000, run)['value'], 'a'*32)
        argv = run.call_args.args[0]
        self.assertIn('root@10.0.0.102', argv)
        self.assertIn('validate_guest', argv[-1])
        run.reset_mock()
        with self.assertRaises(ValueError):
            dispatch({**self.request, 'command': 'machine-id;id'}, self.policy, 101000, run)
        run.assert_not_called()

class CompactBridgeOwnershipTests(unittest.TestCase):
    def test_compact_marker_requires_exact_probe_id_and_durable_job(self):
        from crange_ct_read_relay import validate_request
        config={'peer_uid':101000,'node_addresses':{'proxmox5':'10.0.0.105'}}
        request={'command':'capture-bridge-proof','node':'proxmox5','vmid':140,
                 'marker':'crange-cache-140','cache_job_id':'cache-'+'a'*32}
        self.assertEqual(validate_request(request,config,101000)[3],'crange-cache-140')
        for changed in ({'marker':'crange-cache-139'},{'cache_job_id':'foreign'},{'cache_job_id':None},{'shell':'id'}):
            with self.subTest(changed=changed),self.assertRaises(ValueError):validate_request({**request,**changed},config,101000)
