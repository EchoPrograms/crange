from pathlib import Path
import os
import runpy
import unittest
from unittest.mock import patch

class RouteRetirementTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ,{'OPNSENSE_API_KEY':'fixture-key','OPNSENSE_API_SECRET':'fixture-secret'}):
            values=runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/opnsense-project-routes.py'))
        self.retire=values['retire_project_network'];self.globals=self.retire.__globals__
        self.gateway={'name':'P100','gateway':'10.60.2.32','interface':'opt2','descr':'CRANGE managed project p100'}
        self.routes={'owned':{'network':'10.100.0.0/16','gateway':'P100','descr':'CRANGE managed project p100'}};self.has_gateway=True;self.writes=[];self.groups={}
        self.globals['api']=self.api

    def api(self,method,path,payload=None):
        if method=='POST':
            self.writes.append(path)
            if '/del_gateway/' in path and self.groups:
                raise RuntimeError('Gateway is in use on a Gateway Group')
            if '/delroute/' in path:self.routes={}
            if '/del_gateway/' in path:self.has_gateway=False
            return {'result':'deleted'}
        if path=='/api/routes/routes/get':return {'route':{'route':self.routes}}
        if path=='/api/routing/settings/get':return {'gateways':{'gateway_item':{'own':self.gateway} if self.has_gateway else {}}}
        if path=='/api/routing/group_settings/get':return self.groups
        raise AssertionError(path)

    def test_deletes_only_managed_route_and_gateway_without_firewall_changes(self):
        self.retire(100,'10.60.2.32/24','opt2')
        self.assertFalse(self.has_gateway);self.assertFalse(self.routes)
        self.assertTrue(self.writes);self.assertFalse(any('firewall' in p for p in self.writes))
        count=len(self.writes);self.retire(100,'10.60.2.32/24','opt2');self.assertEqual(len(self.writes),count)

    def test_preflight_refuses_changed_gateway_foreign_routes_and_groups(self):
        for change in ({'gateway':'10.60.2.99'},{'descr':'Manual production gateway'},{'upstream':'1'}):
            original=self.gateway.copy();self.gateway.update(change)
            with self.assertRaises(RuntimeError):self.retire(100,'10.60.2.32/24','opt2')
            self.gateway=original;self.assertFalse(self.writes)
        self.routes['other']={'network':'192.0.2.0/24','gateway':'P100'}
        with self.assertRaises(RuntimeError):self.retire(100,'10.60.2.32/24','opt2')
        self.assertFalse(self.writes);del self.routes['other'];self.groups={'group':{'gateways':'P100|1'}}
        with self.assertRaises(RuntimeError):self.retire(100,'10.60.2.32/24','opt2')
        self.assertTrue(self.has_gateway)
        self.assertNotIn('/api/routing/settings/reconfigure',self.writes)

    def test_dry_run_makes_no_changes(self):
        self.retire(100,'10.60.2.32/24','opt2',dry_run=True)
        self.assertFalse(self.writes);self.assertTrue(self.routes);self.assertTrue(self.has_gateway)

    def test_legacy_gateway_with_reused_name_is_preserved_if_new_route_absent(self):
        self.gateway.update(gateway='10.60.2.30',interface='lan',descr='Project router')
        self.routes={}
        self.retire(100,'10.60.2.32/24','opt2',dry_run=True)
        self.retire(100,'10.60.2.32/24','opt2')
        self.assertTrue(self.has_gateway);self.assertFalse(self.writes)
        self.routes={'owned':{'network':'10.100.0.0/16','gateway':'P100','descr':'CRANGE managed project p100'}}
        with self.assertRaises(RuntimeError):self.retire(100,'10.60.2.32/24','opt2')
        self.assertFalse(self.writes)
