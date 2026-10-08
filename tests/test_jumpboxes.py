import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_jumpboxes import connection_records, resource_identifiers, router_path, settings
from crange_jobs import command
import test_crange as fixtures
import test_guac_users as user_fixtures
crange = fixtures.crange


class JumpBoxSettingsTests(unittest.TestCase):
    setUp = fixtures.CrangeConfigTests.setUp
    tearDown = fixtures.CrangeConfigTests.tearDown

    def test_creating_custom_jumpbox_allocates_ip_and_retains_other_guests(self):
        from crange_jumpboxes import create
        original=crange.json_config(self.project)
        definition={'kind':'vm','name':'custom-desk','team':0,'template':'approved-base','node':'proxmox4','network':'','cores':2,'memory':2048,
                    'ip':0,'display_name':'Custom desktop','services':['rdp','ssh'],'credential_ref':'','sealed_credentials':''}
        with patch.dict(sys.modules,{'crange':crange}):create(3,definition)
        cfg=crange.json_config(self.project);box=cfg['scenario_boxes']['t254-custom-desk']
        self.assertEqual(box['ip_last_octet'],2);self.assertTrue(box['is_jumpbox']);self.assertEqual(box['guac_services'],['rdp','ssh'])
        self.assertEqual(cfg['jump_boxes'],original['jump_boxes'])
        with patch.dict(sys.modules,{'crange':crange}),self.assertRaisesRegex(ValueError,'already exists'):create(3,definition)
        self.assertEqual(crange.json_config(self.project),cfg)

    def test_custom_jumpbox_failure_restores_prior_configuration(self):
        from crange_jumpboxes import create
        original=crange.json_config(self.project)
        definition={'kind':'vm','name':'custom-desk','team':0,'template':'not-approved','node':'proxmox4','network':'','cores':2,'memory':2048,
                    'ip':0,'display_name':'Custom desktop','services':['rdp'],'credential_ref':'','sealed_credentials':''}
        with patch.dict(sys.modules,{'crange':crange}),self.assertRaisesRegex(crange.CrangeError,'not approved'):create(3,definition)
        self.assertEqual(crange.json_config(self.project),original)

    def test_project_jumpbox_presets_preserve_base_and_existing_boxes(self):
        from crange_jumpboxes import save_preset,presets,remove_preset
        original=crange.json_config(self.project)
        definition={'key':'parrot-jumpbox','display_name':'ParrotOS-Jumpbox','kind':'vm','template':'approved-base','node':'proxmox4','cores':2,'memory':2048,'services':['rdp','ssh']}
        with patch.dict(sys.modules,{'crange':crange}):
            save_preset(3,definition);self.assertEqual(presets(3)['parrot-jumpbox']['source']['template_id'],112)
            remove_preset(3,'parrot-jumpbox');self.assertEqual(presets(3),{})
        self.assertEqual(crange.json_config(self.project),original)

    def test_promoting_vm_keeps_identity_network_and_other_guests(self):
        crange.cmd_scenario_add(SimpleNamespace(project='3', name='desktop', team=0, ip=25,
            network='', template='approved-base', node='proxmox4', cores=2, memory=1024,
            full_clone=None, health='[]'))
        before = crange.json_config(self.project)
        args = SimpleNamespace(project='3', name='t254-desktop', kind='vm',
                               display_name='Training desktop', is_jumpbox='true', services='["ssh","rdp","vnc"]')
        with patch.object(crange, 'output_json', return_value={}), patch.object(crange, 'cmd_guac_refresh') as refresh:
            crange.cmd_jumpbox_settings(args)
            refresh.assert_called_once_with(args)
        after = crange.json_config(self.project)
        self.assertEqual(after['jump_boxes'], before['jump_boxes'])
        metadata = after['scenario_boxes']['t254-desktop']
        self.assertTrue(metadata['is_jumpbox'])
        for key, value in before['scenario_boxes']['t254-desktop'].items():
            if key == 'guac_services':
                continue
            self.assertEqual(metadata[key], value)
        self.assertEqual(metadata['guac_services'], ['ssh','rdp','vnc'])

    def test_rejects_missing_box_without_persisting(self):
        original = crange.json_config(self.project)
        args = SimpleNamespace(project='3', name='missing', kind='vm', display_name='Box',
                               is_jumpbox='true', services='["ssh"]')
        with self.assertRaisesRegex(crange.CrangeError, 'declared'):
            crange.cmd_jumpbox_settings(args)
        self.assertEqual(crange.json_config(self.project), original)

    def test_cross_kind_connection_name_collision_does_not_persist(self):
        crange.cmd_scenario_add(SimpleNamespace(project='3', name='desktop', team=0, ip=25,
            network='', template='approved-base', node='proxmox4', cores=2, memory=1024,
            full_clone=None, health='[]'))
        cfg = crange.json_config(self.project)
        cfg['jump_boxes']['team1_jump01']['display_name'] = 'VM · Training desktop'
        cfg['jump_boxes']['team1_jump01']['team_id'] = '254'
        crange.save_config(self.project, cfg)
        args = SimpleNamespace(project='3', name='t254-desktop', kind='vm',
                               display_name='Training desktop', is_jumpbox='true', services='["ssh"]')
        with self.assertRaisesRegex(crange.CrangeError, 'display name'):
            crange.cmd_jumpbox_settings(args)
        self.assertEqual(crange.json_config(self.project), cfg)

    def test_vm_and_container_releases_preserve_remote_settings(self):
        metadata = settings('vm', 'Training box', True, ['ssh','vnc'])
        source = {'name':'desktop','ip_last_octet':25,'template':'approved-base','node_name':'proxmox4',
                  'cores':2,'memory_mb':1024,'full_clone':False,'started':True,'health_checks':[{'type':'ping'}],
                  'team_id':'254', 'network_id':254, 'network_key':'', **metadata}
        ct_source = {**source, 'template':'approved-ct-base'}
        release = {'release_id':'p003-1.0.0','scenario':{'boxes':[source],'containers':[ct_source],
                   'templates':{'approved-base':{'template_id':112,'template_node':'proxmox4'},
                                'approved-ct-base':{'template_id':120,'template_node':'proxmox4','kind':'container'}}}}
        vm = next(iter(crange.release_scenario_boxes(self.project,release,'1','r1.0.0').values()))
        ct = next(iter(crange.release_scenario_containers(self.project,release,'1','r1.0.0').values()))
        for box in (vm,ct):
            self.assertEqual({k:box[k] for k in metadata}, metadata)
            self.assertEqual(box['team_id'], '1')


class RemoteServiceTests(unittest.TestCase):
    def test_release_display_name_can_repeat_in_a_different_team(self):
        metadata = settings('vm', 'Training desktop', True, ['ssh','rdp'])
        staging = connection_records(3, 'vm', 't254-desk', {'team_id':'254', **metadata}, '10.3.254.25')
        deployed = connection_records(3, 'vm', 'r1-desk-t001', {'team_id':'1', **metadata}, '10.3.1.25')
        self.assertTrue({c['name'] for c in staging}.isdisjoint({c['name'] for c in deployed}))
        self.assertIn('Team 0', staging[0]['name'])
        self.assertIn('Team 1', deployed[0]['name'])

    def test_services_have_distinct_names_and_one_box_assignment(self):
        records = connection_records(3, 'container', 't254-desktop',
            {'team_id': '254', 'network_id': 100, **settings('container', 'Lab desktop', True, ['ssh','rdp','vnc'])},
            '10.3.100.25')
        self.assertEqual([r['port'] for r in records], [22,3389,5900])
        self.assertEqual(len({r['name'] for r in records}), 3)
        self.assertEqual({r['key'] for r in records}, {'container:t254-desktop'})
        self.assertEqual({r['network_id'] for r in records}, {100})
        indexed = resource_identifiers([{**r, 'identifier': str(i)} for i,r in enumerate(records)])
        self.assertEqual(indexed['container:t254-desktop'], {'0','1','2'})

    def test_legacy_connection_names_and_protocols_remain_stable(self):
        self.assertEqual(connection_records(3,'jumpbox','team1_jump01',{'team_id':'1'},'10.3.1.11')[0]['name'], 'P003 · team1_jump01')
        self.assertEqual(connection_records(3,'vm','web',{'team_id':'1','guac_protocol':'rdp'},'10.3.1.20')[0]['protocol'], 'rdp')
        records = connection_records(3, 'vm', 'web',
            {'team_id':'1', 'guac_protocol':'ssh', 'guac_services':['ssh','rdp','vnc']}, '10.3.1.20')
        self.assertEqual([record['protocol'] for record in records], ['ssh','rdp','vnc'])
        self.assertEqual(records[0]['name'], 'P003 · VM · web')
        self.assertEqual([record['name'] for record in records[1:]],
                         ['P003 · VM · web · RDP', 'P003 · VM · web · VNC'])

    def test_invalid_settings_and_arbitrary_job_fields_are_rejected(self):
        good = {'name':'t254-web','kind':'vm','display_name':'Web','is_jumpbox':True,'services':['ssh','vnc']}
        self.assertIn('["ssh","vnc"]', command('jumpbox-settings',3,good))
        for change in ({'services':[]}, {'services':['ssh','ssh']}, {'services':['shell']},
                       {'is_jumpbox':'true'}, {'display_name':'bad\nname'}, {'command':'id'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                command('jumpbox-settings',3,{**good,**change})

    def test_released_guest_keys_are_valid_for_remote_and_power_jobs(self):
        key = 'r1.2.0-desktop-t001'
        self.assertEqual(command('machine-state',3,{'name':key,'kind':'vm','state':'stop'}),
                         ['machine-state','3',key,'--kind','vm','--state','stop'])
        self.assertIn(key, command('jumpbox-settings',3,{'name':key,'kind':'vm','display_name':'Desktop',
                                                     'is_jumpbox':True,'services':['ssh']}))
        with self.assertRaises(ValueError):
            command('machine-state',3,{'name':'../escape','kind':'vm','state':'stop'})

    def test_custom_router_path_does_not_include_other_team(self):
        groups = {'project_routers':{'hosts':{'p003_router':{}}},
                  'team_routers':{'hosts':{'p003t001_router':{},'p003t002_router':{}}},
                  'staging_routers':{'hosts':{'p003t254_router':{}}},
                  'custom_routers':{'hosts':{'staging_custom':{'router_owner_team_id':'254','router_team_network':'10.3.100.0/24'},
                                             'team1_custom':{'router_owner_team_id':'1','router_team_network':'10.3.101.0/24'}}}}
        self.assertEqual(router_path(3,{'team_id':'254','network_id':100},groups), ['p003_router','p003t254_router','staging_custom'])
        self.assertEqual(router_path(3,{'team_id':'1'},groups), ['p003_router','p003t001_router'])
        with self.assertRaisesRegex(ValueError,'missing or ambiguous'):
            router_path(3,{'team_id':'254','network_id':101},groups)


class MultiServiceUserTests(unittest.TestCase):
    setUp = user_fixtures.ProjectGuacamoleUsersTests.setUp
    apply = user_fixtures.ProjectGuacamoleUsersTests.apply

    def test_box_assignment_grants_every_service_and_removed_service_is_revoked(self):
        sidecar = json.loads(self.sidecar.read_text())
        sidecar['connections'] += [
            {'identifier':'45','resource_key':'vm:web','team_id':'1','default_for_team':False},
            {'identifier':'46','resource_key':'vm:web','team_id':'1','default_for_team':False}]
        self.sidecar.write_text(json.dumps(sidecar))
        self.users.create(3,'alice',self.sealed,[],['vm:web'])
        self.apply()
        self.assertEqual(set(self.client.permissions['p003-alice']['connectionPermissions']), {'44','45','46'})
        sidecar['connections'] = [r for r in sidecar['connections'] if r['identifier'] != '45']
        self.sidecar.write_text(json.dumps(sidecar))
        self.apply()
        self.assertEqual(set(self.client.permissions['p003-alice']['connectionPermissions']), {'44','46'})
        self.assertEqual(self.client.permissions['unrelated']['connectionPermissions'], {'99':['READ']})


if __name__ == '__main__':
    unittest.main()
