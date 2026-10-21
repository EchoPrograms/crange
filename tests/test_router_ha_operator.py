"""The operator bridge accepts only reviewed router identities, never guests by tag alone."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange_ct_read_relay as relay
spec=importlib.util.spec_from_file_location('router_operator',Path(__file__).resolve().parents[1]/'scripts/setup-router-ha.py')
operator=importlib.util.module_from_spec(spec);spec.loader.exec_module(operator)


def state():
    return {'lineage':'real-project-lineage','resources':[{'module':'module.team_router["2"]',
        'type':'proxmox_virtual_environment_container','name':'router','instances':[{'attributes':{
            'id':'118','vm_id':118,'node_name':'proxmox2','clone':[],
            'disk':[{'datastore_id':'local-lvm','size':8}],
            'initialization':[{'hostname':'p100t002-router'}]}}]}]}


class RouterOperatorTests(unittest.TestCase):
    def test_void_mutations_succeed_but_empty_reads_are_invalid(self):
        for method in ('create', 'set', 'delete'):
            with patch.object(operator, 'run', return_value='\n') as run:
                self.assertIsNone(operator.api('/cluster/ha/resources', method))
                self.assertEqual(run.call_count, 1)
        with patch.object(operator, 'run', return_value=''):
            with self.assertRaises(json.JSONDecodeError):
                operator.api('/cluster/ha/resources')

    def test_proxied_result_is_reconciled_against_exact_source_task_without_replay(self):
        for result in (None, 'proxy-task-result', {'proxy':'completed'}):
            with tempfile.TemporaryDirectory() as temp:
                p=Path(temp)/'receipt.json';plan={'node':'proxmox2','vmid':118}
                task={'type':'move_volume','id':'118','starttime':1000,'status':'OK','upid':'UPID:proxmox2:ABC:'}
                with patch.object(operator.time,'time',return_value=1000),patch.object(operator,'api',side_effect=[
                        result,[task],{'status':'stopped','exitstatus':'OK'}]) as api:
                    operator.task(plan,p,'move-rootfs','/nodes/proxmox2/lxc/118/move_volume',volume='rootfs')
                self.assertEqual(len([c for c in api.call_args_list if len(c.args)>1 and c.args[1]=='create']),1)
                self.assertEqual(plan['upid'],task['upid'])
        with tempfile.TemporaryDirectory() as temp,patch.object(operator.time,'time',return_value=1000),\
                patch.object(operator,'api',side_effect=[None,[{**task,'id':'999'}]]):
            with self.assertRaisesRegex(ValueError,'uncertain'):
                operator.task({'node':'proxmox2','vmid':118},Path(temp)/'receipt.json','move-rootfs','/nodes/proxmox2/lxc/118/move_volume')
    def test_proxied_start_is_observed_while_pending_without_resubmission(self):
        with tempfile.TemporaryDirectory() as temp:
            plan={'node':'proxmox2','vmid':118}
            native={'type':'vzstart','id':'118','starttime':1000,'upid':'UPID:proxmox2:ABC:'}
            with patch.object(operator.time,'time',return_value=1000),patch.object(operator.time,'sleep'),\
                    patch.object(operator,'api',side_effect=[None,[native],{'status':'running'},
                        {'status':'stopped','exitstatus':'OK'}]) as api:
                operator.task(plan,Path(temp)/'receipt.json','start','/nodes/proxmox2/lxc/118/status/start')
            self.assertEqual(sum(len(c.args)>1 and c.args[1]=='create' for c in api.call_args_list),1)

    def test_systemd_warning_requires_exact_log_and_running_guest(self):
        warning='WARN: Systemd 252 detected. You may need to enable nesting.'
        for messages,running,accepted in [([warning,'','TASK WARNINGS: 1'],True,True),
                ([warning,'TASK WARNINGS: 1'],False,False),
                (['WARN: disk unavailable','TASK WARNINGS: 1'],True,False)]:
            with self.subTest(messages=messages,running=running),tempfile.TemporaryDirectory() as temp:
                answers=['UPID:proxmox2:ABC:',{'status':'stopped','exitstatus':'WARNINGS: 1'},
                    [{'t':message} for message in messages],{'status':'running' if running else 'stopped'}]
                with patch.object(operator,'api',side_effect=answers):
                    args=({'node':'proxmox2','vmid':118},Path(temp)/'receipt.json','start','/nodes/proxmox2/lxc/118/status/start')
                    if accepted:operator.task(*args)
                    else:
                        with self.assertRaisesRegex(ValueError,'task failed'):operator.task(*args)

    def test_wrong_operator_host_and_invalid_scope_never_call_api(self):
        for uid,host,project,key in [(1000,'proxmox4',100,'router:project'),(0,'proxmox2',100,'router:project'),
                (0,'proxmox4',60,'router:project'),(0,'proxmox4',100,'vm:server'),
                (0,'proxmox4',100,'router:team:254')]:
            with self.subTest(scope=(uid,host,project,key)),patch.object(operator.os,'geteuid',return_value=uid),\
                    patch.object(operator.socket,'gethostname',return_value=host),patch.object(operator,'api') as api:
                with self.assertRaises(ValueError):operator.guard(project,key)
                api.assert_not_called()

    def test_wrong_creation_identity_clone_and_deposed_instance_never_select(self):
        original=state()
        for field,value in [('vm_id',119),('clone',[{'vm_id':333}])]:
            changed=copy.deepcopy(original);changed['resources'][0]['instances'][0]['attributes'][field]=value
            with self.assertRaises(ValueError):operator.router(changed,'router:team:2')
        original['resources'][0]['instances'][0]['deposed']='old'
        with self.assertRaises(ValueError):operator.router(original,'router:team:2')

    def test_offline_source_is_never_selected_for_recovery(self):
        with patch.object(operator,'snapshot',return_value=state()),patch.object(operator,'api',return_value=[{'node':'proxmox2','status':'offline'}]) as api:
            with self.assertRaisesRegex(ValueError,'never start a second copy'):operator.inspect_router(100,'router:team:2')
            self.assertEqual(api.call_count,1)

    def test_pending_jobs_restore_submission_services_without_stopping_workers(self):
        commands=[]
        def controller(code,**kwargs):
            commands.append(code)
            return 1 if 'SELECT COUNT' in code else {}
        with patch.object(operator,'controller',side_effect=controller):
            with self.assertRaisesRegex(ValueError,'jobs are active'):
                with operator.pause_controller():self.fail('An active job cannot be interrupted.')
        self.assertFalse(any('crange-worker.service' in c for c in commands))
        for name in ('crange.service','crange-guac-broker.service'):
            self.assertTrue(any("'start'" in c and name in c for c in commands))

    def test_relay_requires_exact_peer_scope_and_local_core(self):
        value={'command':'router-ha-retire','node':'proxmox4','project_id':100,'vmid':118}
        config={'peer_uid':101000}
        self.assertEqual(relay.validate_request(value,config,101000),('proxmox4',118,'router-ha-retire',None))
        for change in ({'node':'proxmox2'},{'project_id':True},{'project_id':60},{'vmid':'118'},{'shell':'reboot'}):
            with self.assertRaises(ValueError):relay.validate_request({**value,**change},config,101000)
        with self.assertRaises(ValueError):relay.validate_request(value,config,101001)
        with patch.object(relay.socket,'gethostname',return_value='proxmox3'),patch.object(relay,'host_read') as read:
            with self.assertRaises(ValueError):relay.dispatch(value,config,101000)
            read.assert_not_called()

    def test_root_helper_is_pinned_to_the_reviewed_repository_source(self):
        self.assertEqual(relay.ROUTER_HA_OPERATOR_SHA256,hashlib.sha256(Path(operator.__file__).read_bytes()).hexdigest())

    def test_retirement_rejects_foreign_pool_before_mutation(self):
        registry={'project_id':100,'routers':{'router-address':{'vmid':118,'identity_sha256':'abc'}}}
        def api(endpoint,method='get',**kwargs):
            self.assertEqual(method,'get')
            if endpoint=='/cluster/resources':return [{'vmid':118,'node':'proxmox2','type':'lxc','pool':'foreign'}]
            if endpoint=='/nodes':return [{'node':'proxmox2','status':'online'}]
            self.fail('Foreign guest must not be inspected or changed.')
        with patch.object(operator,'guard'),patch.object(operator,'controller',return_value=registry),\
                patch.object(operator,'api',side_effect=api),patch.object(operator,'run') as run:
            with self.assertRaisesRegex(ValueError,'exclusive assignment'):operator.retire(100,118)
            run.assert_not_called()


if __name__=='__main__':unittest.main()
