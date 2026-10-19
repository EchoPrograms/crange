import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange

class ConfigureScopeTests(unittest.TestCase):
    def test_empty_scoped_windows_group_does_not_skip_linux_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);inventory=root/'ansible/inventory/generated/project-003.json'
            inventory.parent.mkdir(parents=True)
            inventory.write_text(json.dumps({'all':{'children':{'windows_guests':{'hosts':{}}}}}))
            with patch.object(crange,'ROOT',root),patch.object(crange,'project_dir',return_value=root), \
                 patch.object(crange,'json_config',return_value={'scenario_boxes':{}}), \
                 patch.object(crange,'run'),patch.object(crange,'ansible') as ansible, \
                 patch.object(crange,'cmd_route_sync'), \
                 patch.object(crange,'_windows_configure_credentials',return_value={}), \
                 patch.object(crange,'_run_windows_management_routes'),patch.object(crange,'_run_windows_guest_baseline'):
                crange.cmd_configure(SimpleNamespace(project=3,target='all',team_scope=1,yes=True))
            self.assertIn('jumpboxes.yml',[call.args[1] for call in ansible.call_args_list])
            self.assertIn('containers.yml',[call.args[1] for call in ansible.call_args_list])

    def test_staging_and_team_configuration_do_not_target_other_teams(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            inventory=root/'ansible/inventory/generated/project-003.json'
            inventory.parent.mkdir(parents=True)
            inventory.write_text(json.dumps({'all':{'children':{'routers':{'children':{
                'project_routers':{'hosts':{'p003_router':{}}},
                'team_routers':{'hosts':{'p003t001_router':{},'p003t002_router':{}}},
                'staging_routers':{'hosts':{'p003t254_router':{}}},
                'custom_routers':{'hosts':{'staging_custom':{'router_owner_team_id':'254'},
                                            'team1_custom':{'router_owner_team_id':'1'},
                                            'team2_custom':{'router_owner_team_id':'2'}}}}}}}}))
            for team,host,custom,scope in [(0,'p003t254_router','staging_custom','staging'),
                                          (1,'p003t001_router','team1_custom','team_001')]:
                with patch('crange_ssh_identity.refresh_created_guests', return_value=[]), \
                     patch.object(crange,'ROOT',root),patch.object(crange,'project_dir',return_value=root), \
                     patch.object(crange,'json_config',return_value={'project_id':3,'team_ids':['1','2']}), \
                     patch.object(crange,'cmd_route_sync'),patch.object(crange,'run'),patch.object(crange,'ansible') as ansible:
                    crange.cmd_configure_team(SimpleNamespace(project=3,team=team,yes=True))
                    self.assertEqual([(call.args[1],call.args[2]) for call in ansible.call_args_list],
                        [('router-routes.yml',['p003_router']),('router-routes.yml',[host]),
                         ('routers.yml',[host]),('routers.yml',[custom]),
                         ('jumpboxes.yml',[scope+'_jumpboxes']),('containers.yml',[scope+'_containers']),
                         ('root-access.yml',[scope+'_jumpboxes']),('root-access.yml',[scope+'_containers']),
                         ('root-access.yml',[scope+'_vms']),('vm-swap.yml',[scope+'_vms'])])
