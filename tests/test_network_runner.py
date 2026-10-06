import json
import pathlib
import sys
import tempfile
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import test_network_acceptance as evidence_test
from crange_network_runner import NetworkProbeRunner


class Backend:
    def __init__(self, fixture):
        self.fixture=fixture
        self.exists=False
        self.rules=False
        self.calls=[]
        self.failure=None
        self.review=True
        self.released=False

    def vmid_absent(self, target): return not self.exists
    def reserve(self, target):
        self.calls.append('reserve')
        if self.failure=='reserve': raise ValueError('private provider detail')
    def create_plan(self, target): return self.fixture.create
    def reviewed_plan(self, target, operation, digest):
        self.calls.append(('review',operation,digest))
        return self.review
    def apply_saved_plan(self, target, operation, digest, timeout):
        self.calls.append(('apply',operation,timeout))
        if operation=='create': self.exists=True
        else:
            if self.failure=='destroy': raise RuntimeError('private provider detail')
            self.exists=False
        if self.failure=='create' and operation=='create': raise TimeoutError('private provider detail')
    def install_management_rule(self, target, rule):
        self.rules=True
        self.calls.append(('rule',rule))
        if self.failure=='rule_install': raise RuntimeError('private provider detail')
    def run_check(self, target, check, timeout):
        self.calls.append(('check',check,timeout))
        if self.failure=='test': raise RuntimeError('private provider detail')
        return self.fixture.report['checks'][check]
    def remove_management_rule(self, target, rule):
        self.calls.append('rule_remove')
        if self.failure=='rule_remove': raise RuntimeError('private provider detail')
        self.rules=False
    def temporary_rules_absent(self, target): return not self.rules
    def destroy_plan(self, target): return self.fixture.delete
    def probe_state_empty(self, target): return not self.exists
    def input_unchanged(self, target): return True
    def cleanup_artifact(self, target): return self.fixture.report['cleanup']['artifact_sha256']
    def release_reservation(self, target): self.released=True


class NetworkRunnerTests(unittest.TestCase):
    def setUp(self):
        fixture=evidence_test.NetworkAcceptanceTests()
        fixture.setUp()
        self.fixture=fixture
        self.backend=Backend(fixture)
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal=pathlib.Path(self.temp.name)/'journal.json'
        tick=iter(range(100))
        self.runner=NetworkProbeRunner(self.backend,self.journal,clock=lambda:next(tick))

    def run_probe(self):
        return self.runner.run(self.fixture.scope,source_revision='b'*40,tested_at='2026-10-06T12:00:00Z')

    def record(self): return json.loads(self.journal.read_text())

    def test_receipt_only_after_all_checks_owned_cleanup_and_reservation_release(self):
        receipt=self.run_probe()
        self.assertEqual(self.record()['state'],'accepted_cleaned')
        self.assertFalse(self.backend.exists)
        self.assertFalse(self.backend.rules)
        self.assertTrue(self.backend.released)
        self.assertEqual(set(receipt['checks']),set(self.fixture.report['checks']))
        self.assertTrue(all(call[2]==60 for call in self.backend.calls if isinstance(call,tuple) and call[0]=='check'))
        self.assertEqual(self.record()['rule']['destination'],'10.240.254.98/32')
        self.assertEqual(self.record()['rule']['sources'],['10.60.0.20','10.60.0.10'])

    def test_partial_create_rule_and_check_failure_all_attempt_owned_destroy(self):
        for failure in ('create','rule_install','test'):
            with self.subTest(failure=failure):
                if self.journal.exists(): self.journal.unlink()
                self.backend.failure=failure
                with self.assertRaises(ValueError): self.run_probe()
                self.assertEqual(self.record()['state'],'failed_cleaned')
                self.assertNotIn('receipt',self.record())
                self.assertFalse(self.backend.exists)
                self.assertFalse(self.backend.rules)
                self.assertNotIn('private provider detail',self.journal.read_text())
                self.assertEqual(self.record()['cause'],'timeout_detail_redacted' if failure=='create' else 'runtimeerror_detail_redacted')

    def test_reservation_error_is_bounded_in_owned_journal(self):
        self.backend.failure='reserve'
        with self.assertRaises(ValueError):self.run_probe()
        self.assertEqual(self.record()['state'],'reservation_uncertain')
        self.assertEqual(self.record()['cause'],'valueerror_detail_redacted')
        self.assertNotIn('private provider detail',self.journal.read_text())

    def test_rule_cleanup_failure_does_not_skip_destroy_and_retains_reservation(self):
        self.backend.failure='rule_remove'
        with self.assertRaises(ValueError): self.run_probe()
        self.assertFalse(self.backend.exists)
        self.assertFalse(self.backend.released)
        self.assertEqual(self.record()['state'],'cleanup_uncertain')
        self.assertNotIn('receipt',self.record())

    def test_destroy_failure_retains_reservation_and_suppresses_receipt(self):
        self.backend.failure='destroy'
        with self.assertRaises(ValueError): self.run_probe()
        self.assertTrue(self.backend.exists)
        self.assertFalse(self.backend.rules)
        self.assertFalse(self.backend.released)
        self.assertEqual(self.record()['state'],'cleanup_uncertain')

    def test_unreviewed_plan_never_applied(self):
        self.backend.review=False
        with self.assertRaises(ValueError): self.run_probe()
        self.assertFalse(any(isinstance(call,tuple) and call[0]=='apply' for call in self.backend.calls))

    def test_existing_journal_blocks_retry_before_any_mutation(self):
        self.journal.write_text('{"state":"create_pending"}')
        with self.assertRaises(ValueError): self.run_probe()
        self.assertEqual(self.backend.calls,[])

    def test_existing_guest_blocks_reservation(self):
        self.backend.exists=True
        with self.assertRaises(ValueError): self.run_probe()
        self.assertEqual(self.backend.calls,[])
        self.assertFalse(self.journal.exists())
