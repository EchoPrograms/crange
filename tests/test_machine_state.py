import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange
from crange_jobs import command

class MachineStateTests(unittest.TestCase):
    def test_fixed_job_vector_rejects_unknown_kind_and_state(self):
        self.assertEqual(command('machine-state',3,{'name':'t254-web','kind':'vm','state':'stop'}),
                         ['machine-state','3','t254-web','--kind','vm','--state','stop'])
        for key,value in [('kind','router'),('state','destroy')]:
            with self.assertRaises(ValueError):
                command('machine-state',3,dict(name='t254-web',kind='vm',state='start',**{} )|{key:value})

    def test_power_is_scoped_and_persisted_only_after_success(self):
        for kind in ('vm','container'):
            for state in ('start','stop'):
                with self.subTest(kind=kind,state=state), tempfile.TemporaryDirectory() as directory:
                    path=Path(directory)
                    field='scenario_boxes' if kind=='vm' else 'scenario_containers'
                    cfg={'project_id':3,field:{'t254-web':{'name':'web','team_id':'254','node_name':'proxmox4','started':state=='stop'}}}
                    live={field:{'value':{'t254-web':{'id':900,'address':'10.3.254.20'}}}}
                    api=Mock()
                    api.vm.return_value={'name':'p003t254-web'}
                    api.container.return_value={'hostname':'p003t254-web'}
                    getattr(api,'status' if kind=='vm' else 'container_status').side_effect=['stopped' if state=='start' else 'running','running' if state=='start' else 'stopped']
                    args=SimpleNamespace(project='3',name='t254-web',kind=kind,state=state)
                    with patch.object(crange,'project_dir',return_value=path),patch.object(crange,'json_config',return_value=cfg),patch.object(crange,'output_json',return_value=live),patch.object(crange,'ProxmoxAPI',return_value=api):
                        crange.cmd_machine_state(args)
                    stored=json.loads((path/'crange.auto.tfvars.json').read_text())
                    self.assertEqual(stored[field]['t254-web']['started'],state=='start')
                    action='start' if kind=='vm' and state=='start' else 'shutdown' if kind=='vm' else 'container_'+state
                    self.assertEqual(getattr(api,action).call_args.args[:2],('proxmox4',900))

    def test_automatic_guest_power_uses_applied_node(self):
        cfg = {'project_id': 3, 'scenario_containers': {'stage': {
            'name': 'web', 'team_id': '254', 'node_name': 'auto', 'started': True}}}
        live = {'scenario_containers': {'value': {'stage': {
            'id': 900, 'address': '10.3.254.20', 'placement': {'node_name': 'proxmox2'}}}}}
        api = Mock()
        api.container.return_value = {'hostname': 'p003t254-web'}
        api.container_status.side_effect = ['running', 'stopped']
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(crange, 'project_dir', return_value=Path(directory)), \
             patch.object(crange, 'json_config', return_value=cfg), \
             patch.object(crange, 'output_json', return_value=live), \
             patch.object(crange, 'ProxmoxAPI', return_value=api):
            crange.cmd_machine_state(SimpleNamespace(project='3', name='stage', kind='container', state='stop'))
        api.container_stop.assert_called_once_with('proxmox2', 900)

    def test_approved_template_ids_are_not_limited_to_guest_hostname_length(self):
        options = {'name': 'mhl-dc01', 'team': 0, 'ip': 10, 'network': '',
                   'template': 'mhl-server2025-desktop-experience', 'node': 'auto',
                   'cores': 2, 'memory': 4096, 'health': [], 'guac_protocols': ['rdp']}
        self.assertIn(options['template'], command('scenario-add', 100, options))
        for value in ('../escape', 'bad;command'):
            with self.assertRaises(ValueError):
                command('scenario-add', 100, {**options, 'template': value})

    def test_identity_mismatch_prevents_any_power_action(self):
        cfg={'scenario_boxes':{'t254-web':{'name':'web','team_id':'254','node_name':'proxmox4'}}}
        api=Mock();api.vm.return_value={'name':'unrelated-service'}
        with patch.object(crange,'project_dir',return_value=Path('/tmp')),patch.object(crange,'json_config',return_value=cfg),patch.object(crange,'output_json',return_value={'scenario_boxes':{'value':{'t254-web':{'id':900}}}}),patch.object(crange,'ProxmoxAPI',return_value=api):
            with self.assertRaisesRegex(crange.CrangeError,'identity'):
                crange.cmd_machine_state(SimpleNamespace(project='3',name='t254-web',kind='vm',state='stop'))
        api.shutdown.assert_not_called();api.start.assert_not_called()
