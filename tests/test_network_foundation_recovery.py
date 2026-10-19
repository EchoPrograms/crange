import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import crange
import crange_scheduling


class FoundationRecoveryTests(unittest.TestCase):
    def recover(self, *, bad=None, legacy=False):
        with tempfile.TemporaryDirectory() as temporary:
            home = pathlib.Path(temporary)
            project = home / 'project-100';project.mkdir()
            summaries = project / '.crange-plans';summaries.mkdir()
            if legacy:
                (summaries / '000-legacy.json').write_text('{}')
            directory = home / '.local/share/crange/plans/project-100';directory.mkdir(parents=True)
            identity = 'a'*32
            cfg = {'project_id': 100, 'custom_networks': {'test': {'owner_team_id': '254',
                'network_id': 100, 'uplink_last_octet': 2}}}
            inputs = project / 'crange.auto.tfvars.json';inputs.write_text(json.dumps(cfg))
            resource = 'router:custom:test'
            resolved = {'node_name': 'proxmox2', 'datastore_id': 'local-lvm'}
            plan = directory / (identity+'.tfplan');plan.write_bytes(b'reviewed-plan')
            record = {'project': 100, 'plan_id': identity, 'network_foundation': ['test'],
                'inputs_sha256': hashlib.sha256(inputs.read_bytes()).hexdigest(), 'automation_inputs_sha256': 'source',
                'sha256': hashlib.sha256(plan.read_bytes()).hexdigest(),
                'distribution': {'placements': [{'resource_key': resource, 'resolved': resolved}],
                                 'requests': [{'resource_key': resource, 'disk_gb': 4}]}}
            manifest = directory/(identity+'.json');manifest.write_text(json.dumps(record))
            summary = summaries/(identity+'.json');summary.write_text(json.dumps({
                'plan_id': identity, 'network_foundation': ['test'], 'reservation_state': 'released'}))
            actual = {resource: {'node_name': 'proxmox2', 'vm_id': 138,
                                 'disk': [{'datastore_id': 'local-lvm', 'size': 4}]}}
            config = {'unprivileged': 1, 'tags': 'p100;p100c100',
                'net0': 'bridge=p100t254,ip=10.100.254.2/24,gw=10.100.254.1',
                'net1': 'bridge=p100c100,ip=10.100.100.1/24'}
            tasks = []
            if bad == 'network': config['net1'] = 'bridge=p101c100,ip=10.100.100.1/24'
            if bad == 'active': tasks = [{'id': '138'}]
            if bad == 'storage': actual[resource]['disk'][0]['datastore_id'] = 'ssd-lvm'
            if bad == 'inputs': inputs.write_text('changed')
            if bad == 'plan': plan.write_bytes(b'not-reviewed')
            api = Mock()
            def request(method, path, params=None):
                if path == '/pools/crange-iac': return {'members': [{'type': 'lxc', 'vmid': 138}]}
                if path.endswith('/config'): return config
                if path.endswith('/status/current'): return {'status': 'running'}
                if path.endswith('/tasks'): return tasks
                raise AssertionError(path)
            api.request.side_effect = request
            read_text = pathlib.Path.read_text
            def owned_read(path, *args, **kwargs):
                if path.name == '000-legacy.json':
                    raise PermissionError('Administrator-owned historical review')
                return read_text(path, *args, **kwargs)
            with patch.object(pathlib.Path, 'home', return_value=home), \
                 patch.object(pathlib.Path, 'read_text', owned_read), \
                 patch.object(crange, 'json_config', return_value=cfg), \
                 patch.object(crange, 'automation_inputs_digest', return_value='source'), \
                 patch.object(crange, 'ProxmoxAPI', return_value=api), \
                 patch.object(crange, 'tf_run', return_value=SimpleNamespace(stdout='{}')) as terraform, \
                 patch('crange_network_foundation.validate_plan'), \
                 patch.object(crange_scheduling, 'applied_resources', return_value=actual), \
                 patch.object(crange, 'cmd_configure_network_foundation') as configure:
                if bad:
                    with self.assertRaises(crange.CrangeError):
                        crange.resume_network_foundations(SimpleNamespace(project='100'), project)
                    configure.assert_not_called()
                    self.assertTrue(plan.exists())
                else:
                    crange.resume_network_foundations(SimpleNamespace(project='100'), project)
                    self.assertFalse(plan.exists())
                    configure.assert_called_once()
                    self.assertTrue(json.loads(summary.read_text()).get('foundation_configured_at'))
                    self.assertEqual(terraform.call_args[0][1][:2], ['show', '-json'])
                    crange.resume_network_foundations(SimpleNamespace(project='100'), project)
                    configure.assert_called_once()

    def test_finishes_exact_applied_foundation_without_reapplying(self):
        self.recover()

    def test_unreadable_legacy_review_does_not_block_owned_recovery(self):
        self.recover(legacy=True)

    def test_changed_or_active_resources_do_not_trigger_configuration(self):
        for bad in ('network', 'active', 'storage', 'inputs', 'plan'):
            with self.subTest(bad=bad): self.recover(bad=bad)
