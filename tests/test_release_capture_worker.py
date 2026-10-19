import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_capture as capture
from crange_distribution import DistributionError, _source_config_identity


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.config = {'name': 'release', 'agent': '1', 'memory': 1024, 'cores': 1,
                       'scsi0': 'local-lvm:vm-201-disk-0,size=4G',
                       'net0': 'virtio=AA:BB:CC:DD:EE:FF,bridge=p003t254'}
        outer = self
        class API:
            running = False
            def vm(self, node, vmid):
                return dict(outer.config)
            def status(self, node, vmid):
                return 'running' if self.running else 'stopped'
            def request(self, method, path, fields=None):
                outer.events.append((method, path, fields))
                if path in ('/nodes/proxmox4/status','/nodes/proxmox5/status'):return {'cpuinfo':{'cpus':4},'loadavg':[0.4]}
                if path.endswith('/status'):
                    return {'active': 1, 'shared': 1, 'content': 'backup', 'total': 100 * 1024**3, 'avail': 90 * 1024**3}
                if path == '/cluster/nextid': return 201
                if path == '/pools/crange-probes':
                    return {'members': [{'node': 'proxmox4', 'type': 'qemu', 'vmid': 201}]}
                if method == 'PUT': outer.config.update(fields); return None
                if path.endswith('/status/start'):
                    assert 'bridge=isolated,link_down=1' in outer.config['net0']
                    self.running = True
                if path.endswith('/status/shutdown'): self.running = False
                if path.endswith('/template'): outer.config['template'] = 1
                return 'UPID:proxmox4:1:1:1:test:201:root:'
            def task(self, *args, **kwargs): pass
        self.api = API()
        class Ledger:
            def __init__(self, *args): pass
            def reserve_source_probe(self, **kwargs):
                outer.events.append(('reserve', kwargs['capacity']))
                return {'reservation_id': 'r'}
            def mark_running(self, job): pass
            def reconcile_job(self, job, **kwargs): outer.events.append(('reconcile', job))
            def close(self): pass
        self.ledger = Ledger

    def tearDown(self): self.tmp.cleanup()

    def invoke(self, ledger=None, identity=None, init_system="systemd", destination='proxmox4', transport=None,
               probe_storage=None, approve_probe=True):
        def default_identity(api, **kwargs):
            self.events.append(('identity', api.running))
            self.events.append(('init_system', kwargs['init_system']))
            return {'source_config_sha256': _source_config_identity(self.config, 201)}
        def export(api, **kwargs):
            self.events.append(('export', api.running, self.config['template']))
            (kwargs['journal'].directory/(kwargs['job_id']+'.json')).write_text('{}')
        policy = {'allowed_nodes': ['proxmox4', 'proxmox5'], 'approved_backup_storage': [['proxmox4', 'archive']]}
        if probe_storage and approve_probe:
            policy['approved_storage'] = [[destination, probe_storage]]
        with patch.object(capture, 'DATA', Path(self.tmp.name)), \
             patch.object(capture, '_capture_destination', return_value=destination), \
             (patch.object(capture, '_capture_transport', side_effect=transport) if transport else contextlib.nullcontext()), \
             patch.object(capture, 'capture_policy', return_value=(policy, 'isolated', 'local-lvm', 'archive')), \
             patch.object(capture, 'ReservationLedger', ledger or self.ledger), \
             patch.object(capture, 'discover', return_value={'guests': {'201': {}}}), \
             patch.object(capture, 'node_locks', return_value=contextlib.nullcontext()), \
             patch.object(capture, 'capture_source_identity', side_effect=identity or default_identity), \
             patch.object(capture, '_verified_capture_probe'), \
             patch.object(capture, '_retire_capture_transport'), \
             patch.object(capture, 'export_image', side_effect=export), \
             patch('crange_distribution.main', side_effect=lambda args: self.events.append(('probe', args))):
            return capture.capture_vm(self.api, project=3, node='proxmox4', source_id=115,
                name='release', description='release', slug='abc', profile={'agent_enabled': True, 'init_system': init_system,
                    **({'_probe_datastores': {destination: probe_storage}} if probe_storage else {})})

    def test_independent_probe_prefers_approved_ssd_without_relocating_source(self):
        self.invoke(probe_storage='ssd-lvm')
        argv = next(event[1] for event in self.events if event[0] == 'probe')
        self.assertEqual(argv[argv.index('--datastore-id') + 1], 'ssd-lvm')
        self.assertEqual(self.config['scsi0'].split(':')[0], 'local-lvm')
        receipt = json.loads((Path(self.tmp.name) / 'capture-journal/capture-p003-abc.json').read_text())
        self.assertEqual(receipt['probe_datastore'], 'ssd-lvm')

    def test_unapproved_probe_preference_retains_source_without_allocating_probe(self):
        with self.assertRaisesRegex(DistributionError, 'probe storage is not approved'):
            self.invoke(probe_storage='ssd-lvm', approve_probe=False)
        self.assertFalse(any(event[0] == 'probe' for event in self.events))

    def test_frozen_backup_recovery_requires_absent_copy_and_terminal_backup(self):
        allocation = {'reservation_id': 'r', 'operation': 'source_probe', 'project_id': 3,
            'image': 'source:proxmox4:qemu:115', 'artifact_sha256': _source_config_identity(self.config, 115),
            'resolved': {'node_name': 'proxmox5', 'datastore_id': 'local-lvm'}}
        receipt = {'job_id': 'capture-p003-abc', 'source_id': 115, 'source_node': 'proxmox4',
            'node': 'proxmox5', 'capture_name': 'release', 'source_config_sha256': _source_config_identity(self.config, 115),
            'state': 'allocated', 'marker': 'crange-capture-abc', 'template_id': 201, 'reservation': allocation,
            'transport': {'source_node': 'proxmox4', 'source_id': 115, 'state': 'exporting', 'content_before': []},
            'provider_tasks': [{'path': '/nodes/proxmox4/vzdump', 'upid': 'UPID:proxmox4:1:2:3:vzdump:115:user:'}]}
        folder=Path(self.tmp.name)/'capture-journal';folder.mkdir()
        journal=folder/'capture-p003-abc.json';journal.write_text(json.dumps(receipt))
        class Ledger(self.ledger):
            def get_job(self, job):return {'state':'running','project_id':3,'owner':'release-capture','allocations':[allocation]}
        original_request=self.api.request
        def requests(method,path,fields=None):
            if path=='/cluster/resources':return []
            if path.endswith('/tasks'):return []
            if '/tasks/' in path:return {'status':'stopped','exitstatus':'OK'}
            return original_request(method,path,fields)
        self.api.request=requests
        transport=Mock(side_effect=DistributionError('reached frozen transport'))
        with self.assertRaisesRegex(DistributionError,'reached frozen transport'):
            self.invoke(ledger=Ledger,destination='proxmox5',transport=transport)
        transport.assert_called_once()
        self.assertFalse(any(e[0]=='POST' for e in self.events))
        journal.write_text(json.dumps(receipt))
        self.api.request=lambda method,path,fields=None: ([{'vmid':201}] if path=='/cluster/resources'
                                                        else requests(method,path,fields))
        transport.reset_mock()
        with self.assertRaisesRegex(DistributionError,'absence or backup termination'):
            self.invoke(ledger=Ledger,destination='proxmox5',transport=transport)
        transport.assert_not_called()
        # A restore already present can resume only the exact pre-boot machine
        # incompatibility. PVE's status text has no terminal punctuation.
        receipt['provider_tasks'].extend([
            {'path':'/nodes/proxmox5/qemu','upid':'UPID:proxmox5:1:2:3:qmrestore:201:user:'},
            {'path':'/nodes/proxmox5/qemu/201/status/start','upid':'UPID:proxmox5:1:2:3:qmstart:201:user:'}])
        journal.write_text(json.dumps(receipt))
        source=dict(self.config)
        self.config.update(machine='pc-q35-10.1',tags='crange-image;crange-capture-abc;p003',
                           net0='virtio=AA:BB:CC:DD:EE:FF,bridge=isolated,link_down=1')
        self.api.vm=lambda node,vmid:dict(source if vmid==115 else self.config)
        def recovered_requests(method,path,fields=None):
            if path=='/cluster/resources':return [{'vmid':201,'node':'proxmox5'}]
            if path.endswith('/tasks'):return []
            if '/tasks/' in path:
                return {'status':'stopped','exitstatus':
                    "Installed QEMU version '10.0.2' is too old to run machine type 'pc-q35-10.1+pve0', please upgrade node 'proxmox5'"
                    if 'qmstart' in path else 'OK'}
            if path.endswith('/capabilities/qemu/machines'):return [{'id':'pc-q35-9.2+pve1','type':'q35'}]
            if path=='/pools/crange-probes':return {'members':[{'node':'proxmox5','type':'qemu','vmid':201}]}
            return original_request(method,path,fields)
        self.api.request=recovered_requests
        vmid,evidence=self.invoke(ledger=Ledger,destination='proxmox5',transport=transport)
        self.assertEqual(vmid,201)
        self.assertEqual(evidence['node'],'proxmox5')
        transport.assert_not_called()
        self.assertEqual(self.config['machine'],'pc-q35-9.2+pve1')

    def test_reserves_before_clone_and_isolates_before_boot_then_publishes_evidence(self):
        vmid, evidence = self.invoke()
        self.assertEqual(vmid, 201)
        reserve = next(i for i, e in enumerate(self.events) if e[0] == 'reserve')
        clone = next(i for i, e in enumerate(self.events) if e[0] == 'POST' and e[1].endswith('/clone'))
        self.assertLess(reserve, clone)
        self.assertEqual(self.events[clone][2]['pool'], 'crange-probes')
        self.assertIn(('GET', '/nodes/proxmox4/storage/archive/status', None), self.events)
        self.assertFalse(any(event[1] == '/storage/archive' for event in self.events if len(event) > 1))
        self.assertIn(('identity', True), self.events)
        self.assertIn(('export', False, 1), self.events)
        self.assertEqual(evidence['probe_job'], 'capture-p003-abc-probe')
        receipt = json.loads((Path(self.tmp.name) / 'capture-journal/capture-p003-abc.json').read_text())
        self.assertEqual(receipt['state'], 'verified')
        self.assertTrue(all(t['state'] == 'confirmed-terminal' for t in receipt['provider_tasks']))

    def test_openrc_profile_uses_reviewed_openrc_identity_checks(self):
        self.invoke(init_system='openrc')
        self.assertIn(('init_system', 'openrc'), self.events)

    def test_capacity_failure_never_clones(self):
        class FailedLedger(self.ledger):
            def reserve_source_probe(self, **kwargs): raise DistributionError('capacity denied')
        with self.assertRaisesRegex(DistributionError, 'capacity denied'): self.invoke(ledger=FailedLedger)
        self.assertFalse(any(e[0] == 'POST' for e in self.events))

    def test_changed_fingerprint_retains_allocation_without_export(self):
        with self.assertRaisesRegex(DistributionError, 'configuration changed'):
            self.invoke(identity=lambda *a, **k: {'source_config_sha256': '0' * 64})
        self.assertFalse(any(e[0] in ('export', 'reconcile', 'probe') for e in self.events))
        receipt = json.loads((Path(self.tmp.name) / 'capture-journal/capture-p003-abc.json').read_text())
        self.assertEqual(receipt['state'], 'allocated')

    def test_agent_disabled_profile_fails_before_policy_or_cluster_mutation(self):
        with patch.object(capture, 'read_settings', return_value=({}, {})):
            with self.assertRaisesRegex(DistributionError, 'QGA-enabled'):
                capture.capture_policy('proxmox4', {'agent_enabled': False})
