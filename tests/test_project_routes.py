import importlib.util,json,os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange
from crange_jobs import command

class RouteTests(unittest.TestCase):
    def helper(self):
        spec=importlib.util.spec_from_file_location('route_helper',Path(__file__).resolve().parents[1]/'scripts/opnsense-project-routes.py')
        helper=importlib.util.module_from_spec(spec)
        with patch.dict(os.environ,{'OPNSENSE_API_KEY':'fixture','OPNSENSE_API_SECRET':'fixture'}):spec.loader.exec_module(helper)
        return helper
    def spec(self):
        return {'project_id':3,'network':'10.3.0.0/16','gateway_name':'P003','gateway_address':'10.60.2.32','interface':'opt2'}
    def test_matching_routes_do_not_reconfigure_or_touch_firewall(self):
        helper=self.helper();calls=[]
        def api(method,path,payload=None):
            calls.append((method,path))
            if path=='/api/routing/settings/get':return {'gateways':{'gateway_item':{'gw':{'name':'P003','gateway':'10.60.2.32','interface':'opt2'}}}}
            return {'route':{'route':{'route-id':{'network':'10.3.0.0/16','gateway':'P003'}}}}
        with patch.object(helper,'api',side_effect=api):helper.ensure(self.spec())
        self.assertTrue(all(method=='GET' for method,_ in calls))
    def test_unmanaged_or_duplicate_route_refusal_has_no_mutations(self):
        for routes in ([{'network':'10.3.0.0/16','gateway':'OTHER','descr':'Unmanaged'}],
                       [{'network':'10.3.0.0/16','gateway':'P003'}]*2):
            helper=self.helper();calls=[]
            def api(method,path,payload=None):
                calls.append((method,path))
                if path=='/api/routing/settings/get':return {'gateways':{'gateway_item':{}}}
                return {'route':{'route':{str(i):r for i,r in enumerate(routes)}}}
            with patch.object(helper,'api',side_effect=api),self.assertRaises(RuntimeError):helper.ensure(self.spec())
            self.assertTrue(all(method=='GET' for method,_ in calls))
    def test_new_gateway_waits_for_route_choices_before_creation(self):
        helper=self.helper();calls=[];created=False;polls=0
        def api(method,path,payload=None):
            nonlocal created,polls
            calls.append((method,path))
            if path=='/api/routing/settings/get':
                return {'gateways':{'gateway_item':{'gw':{'name':'P003','gateway':'10.60.2.32','interface':'opt2'}} if created else {}}}
            if path=='/api/routing/settings/add_gateway':created=True
            if path=='/api/routes/routes/get':return {'route':{'route':{}}}
            if path=='/api/routes/routes/getroute':
                polls+=1
                return {'route':{'gateway':{'P003':{}} if polls==2 else {}}}
            if path=='/api/routes/routes/addroute':
                self.assertEqual(polls,2)
                self.assertEqual(payload['route']['gateway'],'P003')
            return {'result':'saved'}
        with patch.object(helper,'api',side_effect=api),patch.object(helper.time,'sleep') as sleep:
            helper.ensure(self.spec())
        sleep.assert_called_once_with(1)
        self.assertEqual(sum(path=='/api/routes/routes/addroute' for _,path in calls),1)

    def test_worker_accepts_no_route_parameters_and_refuses_foreign_outputs(self):
        self.assertEqual(command('route-sync',3,{}),['route-sync','3'])
        with self.assertRaises(ValueError):command('route-sync',3,{'network':'10.0.0.0/8'})
        wrong=self.spec();wrong['network']='10.4.0.0/16'
        with patch.object(crange,'project_dir',return_value=Path('/tmp/project-003')), \
             patch.object(crange,'json_config',return_value={'project_id':3,'transit_address':'10.60.2.32/24'}), \
             patch.object(crange,'output_json',return_value={'opnsense_route':{'value':wrong}}),patch.object(crange.subprocess,'run') as process:
            with self.assertRaisesRegex(crange.CrangeError,'approved project topology'):crange.cmd_route_sync(SimpleNamespace(project='3'))
            process.assert_not_called()
