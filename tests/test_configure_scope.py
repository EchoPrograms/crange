import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import copy
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange
from crange_configure_scope import configuration_inventory

class ConfigureScopeTests(unittest.TestCase):
    def fixture(self):
        cfg = {'project_id':3, 'team_ids':['1'], 'scenario_containers': {
            'new': {'team_id':'254'}, 'offline': {'team_id':'254'}, 'team': {'team_id':'1'}},
            'scenario_boxes': {'win': {'team_id':'254'}, 'oldwin': {'team_id':'254'}},
            'custom_networks': {'net': {'owner_team_id':'254','network_id':101},
                                'other': {'owner_team_id':'254','network_id':102}},
            'jump_boxes': {'jump': {'team_id':'254','jump_number':2,'network_key':'net'}}}
        hosts = {'p003_ct_new':{}, 'p003_ct_offline':{},'p003_ct_team':{}}
        windows = {'p003_vm_win':{'crange_box_key':'win'},'p003_vm_oldwin':{'crange_box_key':'oldwin'}}
        inventory = {'all':{'children': {
            'routers':{'vars':{'ansible_user':'root'},'children': {
                'project_routers': {'hosts': {'p003_router': {'router_routes':[{'network':'10.3.1.0/24'}]}}},
                'staging_routers': {'hosts': {'p003t254_router': {}}},
                'team_routers': {'hosts': {'p003t001_router': {}}},
                'custom_routers': {'hosts': {'p003c101_router': {'router_owner_team_id':'254'},
                                            'p003c102_router': {'router_owner_team_id':'254'}}}}},
            'scenario_containers': {'hosts':hosts,'children':{'staging_containers':{'hosts':hosts}}},
            'windows_guests': {'hosts':windows,'children':{'staging_windows':{'hosts':windows}}},
            'jumpboxes': {'hosts':{'p003c101_jump02':{}},'children':{}}
        }}}
        return cfg, inventory

    def test_scoped_inventory_keeps_full_route_data_but_only_selected_guest_and_path(self):
        cfg, inventory = self.fixture(); original = copy.deepcopy(inventory)
        result, needed = configuration_inventory(inventory, cfg, 254, ['container:new'])
        self.assertTrue(needed)
        groups = result['all']['children']
        self.assertEqual(groups['scenario_containers']['hosts'], {'p003_ct_new':{}})
        self.assertEqual(groups['scenario_containers']['children']['staging_containers']['hosts'], {'p003_ct_new':{}})
        self.assertEqual(groups['windows_guests']['hosts'], {})
        self.assertEqual(groups['routers']['children']['team_routers']['hosts'], {})
        self.assertEqual(groups['routers']['children']['custom_routers']['hosts'], {})
        self.assertEqual(groups['routers']['children']['project_routers']['hosts'],
                         inventory['all']['children']['routers']['children']['project_routers']['hosts'])
        self.assertEqual(inventory, original)

    def test_custom_jump_and_windows_selection_keeps_only_their_custom_router(self):
        cfg, inventory = self.fixture()
        result, _ = configuration_inventory(inventory, cfg, 254, ['jumpbox:jump','vm:win'])
        groups = result['all']['children']
        self.assertEqual(set(groups['jumpboxes']['hosts']), {'p003c101_jump02'})
        self.assertEqual(set(groups['windows_guests']['hosts']), {'p003_vm_win'})
        self.assertEqual(set(groups['routers']['children']['custom_routers']['hosts']), {'p003c101_router'})

    def test_deletions_and_empty_reviews_do_not_configure_unrelated_guests(self):
        cfg, inventory = self.fixture()
        for keys in ([], ['container:removed']):
            result, needed = configuration_inventory(inventory, cfg, 254, keys)
            self.assertFalse(needed)
            self.assertEqual(result['all']['children']['routers']['children']['project_routers']['hosts'], {})
        result, needed = configuration_inventory(inventory, cfg, 254, ['router:custom:removed'])
        self.assertTrue(needed)
        self.assertEqual(result['all']['children']['routers']['children']['custom_routers']['hosts'], {})

    def test_invalid_or_cross_team_scope_fails(self):
        cfg, inventory = self.fixture()
        for keys in ('container:new', ['container:team'], ['unknown:box'], ['container:new','container:new']):
            with self.assertRaises(ValueError):
                configuration_inventory(inventory, cfg, 254, keys)

    def test_missing_applied_host_cannot_silently_skip_configuration(self):
        cfg, inventory = self.fixture()
        inventory['all']['children'].pop('scenario_containers')
        with self.assertRaisesRegex(ValueError, 'missing from inventory: p003_ct_new'):
            configuration_inventory(inventory, cfg, 254, ['container:new'])

    def test_post_apply_configuration_uses_private_filtered_inventory_and_preserves_global(self):
        cfg, inventory = self.fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);target=root/'ansible/inventory/generated/project-003.json'
            target.parent.mkdir(parents=True);target.write_text(json.dumps(inventory))
            private_paths=[]
            def ansible(args, playbook, limits):
                private_paths.append(args.inventory_path)
                self.assertEqual(args.inventory_path.stat().st_mode & 0o777, 0o600)
                scoped=json.loads(args.inventory_path.read_text())['all']['children']
                self.assertEqual(set(scoped['scenario_containers']['hosts']), {'p003_ct_new'})
                self.assertFalse(scoped['windows_guests']['hosts'])
                self.assertFalse(scoped['routers']['children']['team_routers']['hosts'])
            with patch('crange_ssh_identity.refresh_created_guests',return_value=[]) as refresh, \
                 patch.object(crange,'ROOT',root),patch.object(crange,'project_dir',return_value=root), \
                 patch.object(crange,'json_config',return_value=cfg),patch.object(crange,'run'), \
                 patch.object(crange,'cmd_route_sync'),patch.object(crange,'ansible',side_effect=ansible), \
                 patch.object(crange,'cmd_configure') as windows:
                crange.cmd_configure_team(SimpleNamespace(project=3,team=0,yes=True,guest_keys=['container:new']))
            refresh.assert_called_once_with(root,254,resource_keys=['container:new'])
            windows.assert_not_called()
            self.assertTrue(private_paths)
            self.assertFalse(any(path.exists() for path in private_paths))
            self.assertEqual(json.loads(target.read_text()),inventory)

    def test_stage_deploy_passes_scope_derived_by_the_apply(self):
        args=SimpleNamespace(project=3)
        def apply(args):args.applied_guest_keys=['container:new']
        with patch.object(crange,'cmd_apply',side_effect=apply),patch.object(crange,'cmd_configure_team') as configure:
            crange.cmd_stage_deploy(args)
        self.assertEqual(configure.call_args.args[0].guest_keys,['container:new'])

    def test_private_windows_inventory_is_not_replaced_by_global_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);private=root/'inventory.json'
            private.write_text(json.dumps({'all':{'children':{'windows_guests':{'hosts':{}}}}}))
            with patch.object(crange,'project_dir',return_value=root),patch.object(crange,'run') as run, \
                 patch.object(crange,'json_config',return_value={'scenario_boxes':{}}):
                crange.cmd_configure(SimpleNamespace(project=3,target='windows',team_scope=254,yes=True,inventory_path=private))
            run.assert_not_called()

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
