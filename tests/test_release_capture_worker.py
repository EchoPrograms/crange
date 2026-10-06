import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
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
                if path.endswith('/status'):
                    return {'active': 1, 'content': 'backup', 'total': 100 * 1024**3, 'avail': 90 * 1024**3}
                if path == '/storage/archive': return {'shared': 1}
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

    def invoke(self, ledger=None, identity=None):
        def default_identity(api, **kwargs):
            self.events.append(('identity', api.running))
            return {'source_config_sha256': _source_config_identity(self.config, 201)}
        def export(api, **kwargs):
            self.events.append(('export', api.running, self.config['template']))
        policy = {'approved_backup_storage': [['proxmox4', 'archive']]}
        with patch.object(capture, 'DATA', Path(self.tmp.name)), \
             patch.object(capture, 'capture_policy', return_value=(policy, 'isolated', 'local-lvm', 'archive')), \
             patch.object(capture, 'ReservationLedger', ledger or self.ledger), \
             patch.object(capture, 'discover', return_value={'guests': {'201': {}}}), \
             patch.object(capture, 'node_locks', return_value=contextlib.nullcontext()), \
             patch.object(capture, 'capture_source_identity', side_effect=identity or default_identity), \
             patch.object(capture, 'export_image', side_effect=export), \
             patch('crange_distribution.main', side_effect=lambda args: self.events.append(('probe', args))):
            return capture.capture_vm(self.api, project=3, node='proxmox4', source_id=115,
                name='release', description='release', slug='abc', profile={'agent_enabled': True})

    def test_reserves_before_clone_and_isolates_before_boot_then_publishes_evidence(self):
        vmid, evidence = self.invoke()
        self.assertEqual(vmid, 201)
        reserve = next(i for i, e in enumerate(self.events) if e[0] == 'reserve')
        clone = next(i for i, e in enumerate(self.events) if e[0] == 'POST' and e[1].endswith('/clone'))
        self.assertLess(reserve, clone)
        self.assertEqual(self.events[clone][2]['pool'], 'crange-probes')
        self.assertIn(('identity', True), self.events)
        self.assertIn(('export', False, 1), self.events)
        self.assertEqual(evidence['probe_job'], 'capture-p003-abc-probe')
        receipt = json.loads((Path(self.tmp.name) / 'capture-journal/capture-p003-abc.json').read_text())
        self.assertEqual(receipt['state'], 'verified')
        self.assertTrue(all(t['state'] == 'confirmed-terminal' for t in receipt['provider_tasks']))

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
