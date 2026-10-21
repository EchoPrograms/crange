"""Only positively registered native HA router moves may update state metadata."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_router_ha as ha
from crange_placement import PlacementError

ADDRESS = 'module.team_router["2"].proxmox_virtual_environment_container.router'


def fixture():
    attrs = {'id': '118', 'vm_id': 118, 'node_name': 'proxmox2',
             'disk': [{'datastore_id': ha.STORAGE, 'size': 8}], 'clone': []}
    state = {'version': 4, 'serial': 40, 'lineage': 'project-100-lineage',
             'outputs': {'retained': {'value': 'untouched'}}, 'resources': [
        {'module': 'module.team_router["2"]', 'type': 'proxmox_virtual_environment_container',
         'name': 'router', 'mode': 'managed', 'instances': [{'attributes': attrs}]},
        {'type': 'terraform_data', 'name': 'sentinel', 'mode': 'managed',
         'instances': [{'attributes': {'id': 'preserved', 'input': {'keep': True}}}]}]}
    config = {'hostname': 'p100t002-router', 'tags': 'network-infrastructure;p100;range-lan;terraform',
              'unprivileged': 1, 'rootfs': ha.STORAGE + ':118/vm-118-disk-0.raw,size=8G',
              'net0': 'name=eth0,bridge=p100tr,ip=10.100.0.3/24',
              'net1': 'name=eth1,bridge=p100t002,ip=10.100.2.1/24'}
    registry = {'schema_version': 1, 'project_id': 100, 'state_lineage': state['lineage'],
                'routers': {ADDRESS: {'vmid': 118, 'identity_sha256': ha.digest(ha.config_identity(config)),
                                     'eligible_nodes': ['proxmox2', 'proxmox3', 'proxmox4']}}}
    resource = {'vmid': 118, 'type': 'lxc', 'node': 'proxmox4', 'status': 'running', 'name': 'p100t002-router', 'pool':'crange-iac'}
    status = [{'type': 'quorum', 'quorate': 1},
              {'type': 'master', 'node': 'proxmox3', 'status': 'proxmox3 (active, now)', 'timestamp': 995},
              {'type': 'lrm', 'node': 'proxmox4', 'status': 'proxmox4 (active, now)', 'timestamp': 995},
              {'type': 'service', 'sid': 'ct:118', 'node': 'proxmox4', 'state': 'started', 'request_state': 'started'}]
    nodes = [{'node': n, 'status': 'online'} for n in ('proxmox2', 'proxmox3', 'proxmox4')]
    api = Mock()
    def request(method, path, params=None):
        if method != 'GET':
            raise AssertionError('Adoption must never issue a provider mutation.')
        if path == '/cluster/resources': return [copy.deepcopy(resource)]
        if path == '/nodes': return copy.deepcopy(nodes)
        if path.endswith('/config'): return copy.deepcopy(config)
        if path == '/cluster/ha/status/current': return copy.deepcopy(status)
        raise AssertionError(path)
    api.request.side_effect = request
    return state, config, registry, resource, status, nodes, api


class RouterHAStateTests(unittest.TestCase):
    def test_foundation_accepts_only_a_proven_adopted_ha_placement(self):
        for change in (None,'vmid','disk','identity','unadopted','quorum'):
            state,config,registry,resource,status,nodes,api=fixture()
            attrs=ha.container_states(state)[ADDRESS]
            attrs['node_name']='proxmox4'
            if change=='disk':attrs['disk'][0]['datastore_id']='foreign'
            if change=='identity':config['hostname']='unrelated'
            if change=='unadopted':attrs['node_name']='proxmox2'
            if change=='quorum':status[0]['quorate']=0
            tf=Mock(return_value=SimpleNamespace(stdout=json.dumps(state)))
            with self.subTest(change=change),patch.object(ha,'protected_registration',return_value=registry), \
                    patch.object(ha.time,'time',return_value=1000):
                if change:
                    with self.assertRaises(PlacementError):
                        ha.confirmed_router_placement(Path('/tmp/project-100'),'router:team:2',119 if change=='vmid' else 118,api,tf=tf)
                else:
                    self.assertEqual(ha.confirmed_router_placement(Path('/tmp/project-100'),'router:team:2',118,api,tf=tf),
                                     {'node_name':'proxmox4','datastore_id':ha.STORAGE})
            self.assertFalse(any(call.args[0]!='GET' for call in api.request.call_args_list))

    def test_offline_original_router_does_not_block_unrelated_lab_work_or_infer_recovery(self):
        state, _, registry, resource, status, nodes, api = fixture()
        resource.update(node='proxmox2',status='unknown')
        nodes[0]['status']='offline'
        status[3].update(node='proxmox2',state='fence')
        self.assertEqual(ha.prove_moves(state,registry,api,now=1000),{})
        self.assertFalse(any(call.args[1].endswith('/config') for call in api.request.call_args_list))

    def test_registered_router_changes_are_fenced_while_unrelated_guests_remain_usable(self):
        registry={'routers':{ADDRESS:{'vmid':118}}}
        original=Path.exists
        with patch.object(Path,'exists',lambda p: True if str(p).startswith('/etc/crange/router-ha/') else original(p)),\
                patch.object(Path,'read_text',return_value=json.dumps(registry)):
            for actions in (['create'],['update'],['delete'],['delete','create']):
                with self.assertRaisesRegex(PlacementError,'Native HA owns'):
                    ha.protect_registered_plan(Path('/tmp/project-100'),{'resource_changes':[{'address':ADDRESS,'change':{'actions':actions}}]})
            ha.protect_registered_plan(Path('/tmp/project-100'),{'resource_changes':[
                {'address':ADDRESS,'change':{'actions':['no-op']}},
                {'address':'module.scenario_box["new"].proxmox_virtual_environment_vm.scenario','change':{'actions':['create']}}]})

    def test_returning_original_host_must_still_agree_with_native_ha(self):
        state, _, registry, resource, status, _, api = fixture()
        resource['node'] = 'proxmox2'
        # Terraform still says P2; HA says its recovered owner is P4.
        with self.assertRaisesRegex(PlacementError, 'exclusive'):
            ha.prove_moves(state, registry, api, now=1000)
        status[2]['node'] = 'proxmox2'
        status[3]['node'] = 'proxmox2'
        self.assertEqual(ha.prove_moves(state, registry, api, now=1000), {})

    def test_disk_move_refresh_is_locked_and_preserves_other_guest_and_image_identity(self):
        for corrupt in (None, 'unrelated', 'mutating-plan'):
            state, config, *_ = fixture()
            attrs = ha.container_states(state)[ADDRESS]
            attrs['disk'][0]['datastore_id'] = 'local-lvm'
            attrs['operating_system'] = [{'template_file_id':'media-mirror4:vztmpl/approved.tar.zst','type':'debian'}]
            attrs['initialization'] = [{'hostname':'p100t002-router'}]
            sentinel={'mode':'managed','type':'proxmox_virtual_environment_vm','name':'unrelated',
                      'instances':[{'attributes':{'id':'200','vm_id':200,'node_name':'proxmox6'}}]}
            state['resources'].append(sentinel)
            current=copy.deepcopy(state);calls=[]
            def terraform(path,args,**kwargs):
                nonlocal current
                calls.append(args)
                if args==['state','pull']:return SimpleNamespace(stdout=json.dumps(current))
                if args[0]=='show':return SimpleNamespace(stdout=json.dumps({'resource_changes':[
                    {'change':{'actions':['update']}}] if corrupt=='mutating-plan' else []}))
                if args[0]=='apply':
                    current=copy.deepcopy(state);current['serial']+=1
                    ha.container_states(current)[ADDRESS]['disk'][0]['datastore_id']=ha.STORAGE
                    if corrupt=='unrelated':current['resources'][-1]['instances'][0]['attributes']['node_name']='proxmox5'
                return SimpleNamespace(stdout='')
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as temp:
                p=Path(temp)/'project-100';p.mkdir()
                metadata={'router:team:2':{'guest_id':118,'node_name':'proxmox2','datastore_id':'local-lvm','image_id':'immutable-original'}}
                f=p/'crange-applied-placements.json';f.write_text(json.dumps(metadata))
                api=Mock();api.request.return_value=config
                with patch.object(Path,'home',return_value=Path(temp)):
                    if corrupt:
                        with self.assertRaises(PlacementError):ha.refresh_storage(p,ADDRESS,118,ha.STORAGE,tf=terraform,api=api)
                        self.assertEqual(json.loads(f.read_text()),metadata)
                    else:
                        self.assertEqual(ha.refresh_storage(p,ADDRESS,118,ha.STORAGE,tf=terraform,api=api)['phase'],'verified')
                        saved=json.loads(f.read_text());self.assertEqual(saved['router:team:2']['image_id'],'immutable-original')
                        self.assertEqual(saved['router:team:2']['datastore_id'],ha.STORAGE)
                self.assertTrue(any('-refresh-only' in args for args in calls))
                self.assertFalse(any('import' in args or '-lock=false' in args for args in calls))
                if corrupt=='mutating-plan':self.assertFalse(any(args[0]=='apply' for args in calls))

    def test_native_proof_and_patch_preserve_everything_except_node_and_serial(self):
        state, _, registry, _, _, _, api = fixture()
        original = copy.deepcopy(state)
        moves = ha.prove_moves(state, registry, api, now=1000)
        updated = ha.relocated_snapshot(state, moves)
        self.assertEqual(updated['serial'], 41)
        self.assertEqual(ha.container_states(updated)[ADDRESS]['node_name'], 'proxmox4')
        updated['serial'] = original['serial']
        ha.container_states(updated)[ADDRESS]['node_name'] = 'proxmox2'
        self.assertEqual(updated, original)
        self.assertEqual(state, original)

    def test_unmanaged_or_wrong_project_lineage_never_adopts(self):
        state, _, registry, _, _, _, api = fixture()
        registry['state_lineage'] = 'another-project'
        with self.assertRaisesRegex(PlacementError, 'lineage'):
            ha.prove_moves(state, registry, api, now=1000)
        api.request.assert_not_called()
        registry['state_lineage'] = state['lineage']
        registry['routers']['module.scenario_container["pretend"].proxmox_virtual_environment_container.scenario'] = registry['routers'].pop(ADDRESS)
        with self.assertRaisesRegex(PlacementError, 'Registered router'):
            ha.prove_moves(state, registry, api, now=1000)

    def test_conflicting_guest_disk_or_vlan_identity_is_not_recovery(self):
        for field, value in [('rootfs', 'local-lvm:vm-118-disk-0,size=8G'),
                             ('net1', 'name=eth1,bridge=p100t003,ip=10.100.3.1/24'),
                             ('tags', 'p101;terraform;range-lan;network-infrastructure')]:
            state, config, registry, _, _, _, api = fixture()
            config[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(PlacementError, 'identity differs'):
                ha.prove_moves(state, registry, api, now=1000)

    def test_missing_fencing_authority_stale_status_or_incomplete_start_blocks(self):
        for index, field, value in [(0, 'quorate', 0), (1, 'timestamp', 900),
                                    (1, 'timestamp', 1010), (1, 'timestamp', 'bad'),
                                    (2, 'status', 'proxmox4 (idle, now)'),
                                    (3, 'state', 'migrate'), (3, 'node', 'proxmox2'),
                                    (3, 'request_state', 'stopped')]:
            state, _, registry, _, status, _, api = fixture()
            status[index][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(PlacementError, 'exclusive'):
                ha.prove_moves(state, registry, api, now=1000)

    def test_duplicate_identity_offline_destination_and_unregistered_node_block(self):
        state, _, registry, _, _, nodes, api = fixture()
        nodes[-1]['status'] = 'offline'
        with self.assertRaisesRegex(PlacementError, 'online node'):
            ha.prove_moves(state, registry, api, now=1000)
        nodes[-1]['status'] = 'online'
        registry['routers'][ADDRESS]['eligible_nodes'] = ['proxmox2', 'proxmox3']
        with self.assertRaisesRegex(PlacementError, 'enrolled'):
            ha.prove_moves(state, registry, api, now=1000)
        api.request.side_effect = lambda method, path, params=None: [
            {'vmid': 118, 'type': 'lxc'}, {'vmid': 118, 'type': 'qemu'}] if path == '/cluster/resources' else nodes
        with self.assertRaisesRegex(PlacementError, 'unique native'):
            ha.prove_moves(state, registry, api, now=1000)

    def test_stats_changes_are_allowed_but_a_second_move_during_verification_is_not(self):
        state, _, registry, _, _, _, api = fixture()
        original = api.request.side_effect
        calls = 0
        def read(method, path, params=None):
            nonlocal calls
            result = original(method, path, params)
            if path == '/cluster/resources':
                calls += 1
                result[0]['cpu'] = calls / 100
                if calls > 3:result[0]['node'] = 'proxmox3'
            return result
        api.request.side_effect = read
        self.assertEqual(ha.prove_moves(state, registry, api, now=1000)[ADDRESS]['to'], 'proxmox4')
        with self.assertRaisesRegex(PlacementError, 'changed during verification'):
            ha.prove_moves(state, registry, api, now=1000)

    def test_nullable_imported_vmid_is_allowed_but_an_explicit_conflict_is_not(self):
        state, _, _, _, _, _, _ = fixture()
        attrs = ha.container_states(state)[ADDRESS]
        attrs['vm_id'] = None  # Pinned provider's native import leaves this nullable.
        move = {ADDRESS: {'vmid': 118, 'from': 'proxmox2', 'to': 'proxmox4'}}
        self.assertEqual(ha.container_states(ha.relocated_snapshot(state, move))[ADDRESS]['id'], '118')
        attrs['vm_id'] = 119
        with self.assertRaises(PlacementError):ha.relocated_snapshot(state, move)

    def test_deposed_state_or_a_local_disk_cannot_be_pushed(self):
        for field, value in [('deposed', 'old'), ('status', 'tainted')]:
            state, *_ = fixture()
            state['resources'][0]['instances'][0][field] = value
            with self.assertRaises(PlacementError):ha.container_states(state)
        state, *_ = fixture()
        ha.container_states(state)[ADDRESS]['disk'][0]['datastore_id'] = 'local-lvm'
        with self.assertRaises(PlacementError):
            ha.relocated_snapshot(state, {ADDRESS: {'vmid': 118, 'from': 'proxmox2', 'to': 'proxmox4'}})

    def test_unregistered_projects_are_a_noop_and_registration_cannot_be_user_owned(self):
        with tempfile.TemporaryDirectory() as directory:
            registration = Path(directory) / 'registry.json'
            api, tf = Mock(), Mock()
            self.assertEqual(ha.reconcile_locations(Path(directory) / 'project-100', api=api, tf=tf,
                                                   registry_path=registration), [])
            api.request.assert_not_called(); tf.assert_not_called()
            registration.write_text('{}')
            with patch.object(Path, 'stat', return_value=SimpleNamespace(st_uid=1000, st_mode=0o100600)):
                with self.assertRaisesRegex(PlacementError, 'root-owned'):
                    ha.reconcile_locations(Path(directory) / 'project-100', api=api, tf=tf, registry_path=registration)

    def test_native_serial_increment_does_not_hide_unrelated_state_changes(self):
        state, *_ = fixture()
        candidate = ha.relocated_snapshot(state, {ADDRESS: {'vmid': 118, 'from': 'proxmox2', 'to': 'proxmox4'}})
        observed = copy.deepcopy(candidate)
        observed['serial'] += 1
        observed['terraform_version'] = 'newer-native-cli'
        self.assertTrue(ha.candidate_matches(candidate, observed))
        observed['outputs']['retained']['value'] = 'unexpected'
        self.assertFalse(ha.candidate_matches(candidate, observed))
        observed = copy.deepcopy(candidate)
        observed['serial'] += 2
        self.assertFalse(ha.candidate_matches(candidate, observed))

    def test_check_sets_may_reorder_but_check_results_cannot_change(self):
        state, *_ = fixture()
        state['check_results'] = [
            {'config_addr':'var.one', 'status':'pass', 'objects':[
                {'object_addr':'one[1]', 'status':'pass'}, {'object_addr':'one[2]', 'status':'pass'}]},
            {'config_addr':'var.two', 'status':'pass', 'objects':[]}]
        reordered = copy.deepcopy(state)
        reordered['check_results'][0]['objects'].reverse()
        reordered['check_results'].reverse()
        self.assertEqual(ha.normalized_snapshot(state), ha.normalized_snapshot(reordered))
        self.assertTrue(ha.candidate_matches(state, reordered))
        reordered['check_results'][0]['status'] = 'fail'
        self.assertFalse(ha.candidate_matches(state, reordered))

    def test_transaction_uses_native_locking_and_never_forces_over_other_state(self):
        state, _, registry, _, _, _, api = fixture()
        current = copy.deepcopy(state)
        calls = []
        def terraform(path, args, **kwargs):
            nonlocal current
            calls.append(args)
            if args == ['state', 'pull']:
                return SimpleNamespace(stdout=json.dumps(current))
            self.assertEqual(args[:3], ['state', 'push', '-lock-timeout=30s'])
            current = json.loads(Path(args[3]).read_text())
            current['serial'] += 1
            return SimpleNamespace(stdout='')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'project-100'
            path.mkdir()
            metadata = {'router:team:2': {'guest_id': 118, 'node_name': 'proxmox2',
                        'datastore_id': ha.STORAGE, 'image_id': 'original-router-image', 'declared_template': 'approved'},
                        'vm:unrelated': {'guest_id': 200, 'node_name': 'proxmox6'}}
            (path / 'crange-applied-placements.json').write_text(json.dumps(metadata))
            registration = Path(directory) / 'registry.json'
            registration.write_text(json.dumps(registry))
            original = Path.stat
            def stat(p, **kwargs):
                if p == registration:
                    return SimpleNamespace(st_uid=0, st_mode=0o100640)
                return original(p, **kwargs)
            with patch.object(Path, 'stat', stat), patch.object(ha.time, 'time', return_value=1000):
                abandoned = Path(directory) / 'journals/project-100/abandoned'
                abandoned.mkdir(parents=True)
                (abandoned / 'receipt.json').write_text(json.dumps({'phase':'prepared'}))
                result = ha.reconcile_locations(path, api=api, tf=terraform, registry_path=registration,
                                                journal_root=Path(directory) / 'journals')
            self.assertEqual(result, [ADDRESS])
            self.assertEqual(len([c for c in calls if c[:2] == ['state', 'push']]), 1)
            self.assertEqual(json.loads((abandoned / 'receipt.json').read_text())['phase'], 'aborted-before-submit')
            receipt = next(p for p in (Path(directory) / 'journals').glob('*/*/receipt.json') if p.parent != abandoned)
            self.assertEqual(json.loads(receipt.read_text())['phase'], 'verified')
            self.assertEqual(current['outputs'], state['outputs'])
            updated = json.loads((path / 'crange-applied-placements.json').read_text())
            self.assertEqual(updated['router:team:2']['node_name'], 'proxmox4')
            self.assertEqual(updated['router:team:2']['image_id'], 'original-router-image')
            self.assertEqual(updated['vm:unrelated'], metadata['vm:unrelated'])


if __name__ == '__main__':
    unittest.main()
