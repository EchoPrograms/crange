import copy
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange_router_ha as ha
import crange_router_ha_service as service
import crange_ct_read_relay as relay
spec=importlib.util.spec_from_file_location('ha_auto_operator',Path(__file__).resolve().parents[1]/'scripts/setup-router-ha.py')
operator=importlib.util.module_from_spec(spec);spec.loader.exec_module(operator)
from test_router_ha_state import fixture,ADDRESS


class RouterAutomationTests(unittest.TestCase):
    def test_returned_restricted_source_requires_root_registered_origin(self):
        state,config,registry,resource,status,nodes,api=fixture()
        ha.container_states(state)[ADDRESS]['node_name']='proxmox1'
        with self.assertRaises(ha.PlacementError):ha.prove_moves(state,registry,api,now=1000)
        registry['routers'][ADDRESS]['initial_node']='proxmox1'
        moves=ha.prove_moves(state,registry,api,now=1000)
        self.assertEqual(moves[ADDRESS]['from'],'proxmox1')
        resource['pool']='other-project'
        with self.assertRaises(ha.PlacementError):ha.prove_moves(state,registry,api,now=1000)

    def test_creation_enrollment_journal_survives_failure_and_retries_only_routers(self):
        changes=[{'address':ADDRESS,'actions':['create']},
                 {'address':'module.scenario_box["x"].proxmox_virtual_environment_vm.scenario','actions':['create']}]
        requests=[];good={'value':json.dumps({'phase':'enrolled','project_id':100,'router_key':'router:team:2'})}
        class Socket:
            def __init__(self,response):self.response=response
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def settimeout(self,*args):pass
            def connect(self,*args):pass
            def sendall(self,data):requests.append(json.loads(data))
            def recv(self,*args):return json.dumps(self.response).encode()+b'\n'
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'project-100';path.mkdir()
            with patch.object(ha,'automatic_enabled',return_value=True),patch.object(ha,'reconcile_locations'),\
                    patch('socket.socket',return_value=Socket({'error':'deferred'})):
                with self.assertRaises(ha.PlacementError):ha.enroll_created(path,'a'*32,changes)
            self.assertTrue((path/'.crange-router-ha-pending.json').exists())
            with patch.object(ha,'automatic_enabled',return_value=True),patch.object(ha,'reconcile_locations'),\
                    patch('socket.socket',return_value=Socket(good)):
                self.assertEqual(ha.enroll_created(path,'a'*32,changes),['router:team:2'])
            self.assertFalse((path/'.crange-router-ha-pending.json').exists())
            self.assertEqual(requests[0],requests[1])
            self.assertNotIn('vmid',requests[0])

    def test_project_operations_skip_busy_and_release_the_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            with ha.project_operation(100,root=directory) as acquired:
                self.assertTrue(acquired)
                with ha.project_operation(100,root=directory,blocking=False) as other:self.assertFalse(other)
            with ha.project_operation(100,root=directory,blocking=False) as acquired:self.assertTrue(acquired)

    def test_full_clone_creation_metadata_survives_move(self):
        state,config,registry,resource,status,nodes,api=fixture()
        attrs=ha.container_states(state)[ADDRESS]
        original=[{'full':True,'vm_id':999,'node_name':'proxmox2'}];attrs['clone']=original
        moves=ha.prove_moves(state,registry,api,now=1000)
        after=ha.relocated_snapshot(state,moves)
        self.assertEqual(ha.container_states(after)[ADDRESS]['clone'],original)
        attrs['clone'][0]['full']=False
        with self.assertRaisesRegex(ha.PlacementError,'Linked'):ha.prove_moves(state,registry,api,now=1000)

    def test_relay_enrollment_cannot_choose_guests_paths_nodes_or_commands(self):
        request={'command':'router-ha-enroll','node':'proxmox4','project_id':100,
                 'router_key':'router:custom:archive','plan_id':'a'*32}
        self.assertEqual(relay.validate_request(request,{'peer_uid':1000},1000)[2],'router-ha-enroll')
        for delta in ({'vmid':114},{'node':'proxmox6'},{'project_id':60},{'router_key':'vm:database'},
                      {'router_key':'router:custom:../../etc'},{'plan_id':'../../etc'},{'script':'shell'}):
            with self.subTest(delta=delta),self.assertRaises(ValueError):
                relay.validate_request({**request,**delta},{'peer_uid':1000},1000)
        with self.assertRaises(ValueError):relay.validate_request(request,{'peer_uid':1000},1001)

    def test_automatic_enrollment_needs_its_exact_successful_create_plan(self):
        from test_router_ha_operator import state
        for summary in ({}, {'plan_id':'a'*32,'applied_at':'2026-10-20T00:00:00+00:00','changes':[]},
                        {'plan_id':'b'*32,'applied_at':'2026-10-20T00:00:00+00:00','changes':[{'address':ADDRESS,'actions':['create']}]}):
            with patch.object(operator,'guard'),patch.object(operator,'controller',return_value=summary),\
                    patch.object(operator,'snapshot',return_value=state()),patch.object(operator,'review') as review:
                with self.assertRaises(ValueError):operator.automatic(100,'router:team:2','a'*32)
                review.assert_not_called()

    def test_affinity_must_remain_strict_and_bound_to_one_router(self):
        good={'type':'node-affinity','strict':1,'resources':'ct:118','nodes':'proxmox2,proxmox3'}
        operator.verify_rule(good,118,['proxmox2','proxmox3'])
        for delta in ({'strict':0},{'resources':'ct:114'},{'nodes':'proxmox1,proxmox2'},{'disable':1}):
            with self.assertRaises(ValueError):operator.verify_rule({**good,**delta},118,['proxmox2','proxmox3'])

    def test_background_adoption_only_runs_for_a_relocated_assignment(self):
        for moved in (False,True):
            state,config,registry,resource,status,nodes,api=fixture()
            if not moved:resource['node']='proxmox2'
            api.request.side_effect=lambda method,path,*args: (
                [resource] if path=='/cluster/resources' else status if path=='/cluster/ha/status/current' else nodes)
            @contextmanager
            def operation(*args,**kwargs):yield True
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);project=root/'project-100';project.mkdir();registries=root/'registry';registries.mkdir()
                (project/'terraform.tfstate').write_text(json.dumps(state))
                (registries/'project-100.json').write_text(json.dumps(registry))
                reconcile=Mock(return_value=[ADDRESS])
                refresh=Mock()
                report=service.reconcile_all(api,root,registries,reconcile=reconcile,operation=operation,refresh=refresh)
                self.assertEqual(reconcile.call_count,int(moved))
                self.assertEqual(report['state'],'checked')

    def test_background_waits_for_quorum_without_any_state_adoption(self):
        api=Mock();api.request.return_value=[];reconcile=Mock()
        self.assertEqual(service.reconcile_all(api,'/missing','/missing',reconcile=reconcile)['state'],'waiting-for-quorum')
        reconcile.assert_not_called()

    def test_refresh_cannot_accept_operator_supplied_nodes(self):
        value={'command':'router-ha-refresh','node':'proxmox4','project_id':100}
        relay.validate_request(value,{'peer_uid':1000},1000)
        with self.assertRaises(ValueError):relay.validate_request({**value,'nodes':['proxmox1']},{'peer_uid':1000},1000)

    def test_returning_unready_worker_is_not_added_to_affinity(self):
        from test_router_ha_operator import state
        entry={'vmid':118,'eligible_nodes':['proxmox2'],'identity_sha256':'fixed'}
        registry={'project_id':100,'state_lineage':'real-project-lineage','routers':{ADDRESS:entry}}
        def api(path,method='get',**params):
            self.assertEqual(method,'get')
            if path=='/nodes':return [{'node':n,'status':'online'} for n in ('proxmox2','proxmox3')]
            if path=='/cluster/resources':return [{'vmid':118,'node':'proxmox2','type':'lxc','pool':'crange-iac'}]
            if path.endswith('/config'):return {'net0':'bridge=p100tr','net1':'bridge=p100t002'}
            self.fail('An unverified worker must never update an affinity rule.')
        with patch.object(operator,'guard'),patch.object(operator,'controller',return_value=registry),\
                patch.object(operator,'snapshot',return_value=state()),patch.object(operator,'api',side_effect=api),\
                patch.object(operator,'sha',return_value='fixed'),patch.object(operator,'verify_clients',side_effect=ValueError('NFS unavailable')):
            self.assertEqual(operator.refresh_eligibility(100)['added'],{})

    def test_returning_ready_worker_updates_only_verified_affinity_and_registry(self):
        from test_router_ha_operator import state
        entry={'vmid':118,'eligible_nodes':['proxmox2'],'identity_sha256':'fixed'}
        registry={'project_id':100,'state_lineage':'real-project-lineage','routers':{ADDRESS:entry}}
        writes=[];publications=[]
        def api(path,method='get',**params):
            if method=='set':writes.append((path,params));return
            if path=='/nodes':return [{'node':n,'status':'online'} for n in ('proxmox1','proxmox2','proxmox3')]
            if path=='/cluster/resources':return [{'vmid':118,'node':'proxmox2','type':'lxc','pool':'crange-iac'}]
            if path.endswith('/config'):return {'net0':'bridge=p100tr','net1':'bridge=p100t002'}
            if path=='/cluster/ha/resources':return [{'sid':'ct:118','state':'started','failback':0}]
            if path=='/cluster/ha/rules':return [{'rule':'crange-p100-ct118','type':'node-affinity','strict':1,
                'resources':'ct:118','nodes':'proxmox2','digest':'compare-and-swap'}]
            self.fail(path)
        def controller(code,**kwargs):
            if "p.read_text()" in code and 'old=' not in code:return copy.deepcopy(registry)
            publications.append(code);return {}
        with patch.object(operator,'guard'),patch.object(operator,'controller',side_effect=controller),\
                patch.object(operator,'snapshot',return_value=state()),patch.object(operator,'api',side_effect=api),\
                patch.object(operator,'sha',return_value='fixed'),patch.object(operator,'verify_clients') as verify:
            result=operator.refresh_eligibility(100)
        self.assertEqual(result['added'],{'118':['proxmox3']})
        verify.assert_called_once_with(['proxmox3'],['p100tr','p100t002'])
        self.assertEqual(writes,[('/cluster/ha/rules/crange-p100-ct118',{'type':'node-affinity','nodes':'proxmox2,proxmox3','digest':'compare-and-swap'})])
        self.assertEqual(len(publications),1)

    def test_automatic_retry_resumes_only_its_own_ha_enrollment_journal(self):
        from test_router_ha_operator import state
        summary={'plan_id':'a'*32,'applied_at':'2026-10-20T00:00:00+00:00',
                 'changes':[{'address':ADDRESS,'actions':['create']}]}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);file=root/'own.json'
            plan={'origin_plan_id':'a'*32,'project_id':100,'router_key':'router:team:2','vmid':118,
                  'phase':'enrolling-native-ha','state_lineage':'real-project-lineage',
                  'eligible_nodes':['proxmox2','proxmox3'],'shared_config_sha256':'fixed'}
            file.write_text(json.dumps(plan))
            def api(path,**params):
                if path=='/nodes':return [{'node':'proxmox3','status':'online'}]
                if path=='/cluster/resources':return [{'vmid':118,'node':'proxmox3','pool':'crange-iac','type':'lxc'}]
                if path.endswith('/config'):return {'rootfs':'shared-exact-identity'}
                self.fail(path)
            with patch.object(operator,'ROOT',root),patch.object(operator,'guard'),\
                 patch.object(operator,'controller',side_effect=[summary,{}]),\
                 patch.object(operator,'snapshot',return_value=state()),patch.object(operator,'api',side_effect=api),\
                 patch.object(operator,'sha',return_value='fixed'),patch.object(operator,'finish_enrollment',return_value={'phase':'enrolled'}) as finish,\
                 patch.object(operator,'review') as review:
                self.assertEqual(operator.automatic(100,'router:team:2','a'*32),{'phase':'enrolled'})
                finish.assert_called_once();review.assert_not_called()
