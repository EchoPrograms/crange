import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_scheduling as scheduling
import crange_jobs
from crange_placement import PlacementError


class AppliedReservationReconcileTests(unittest.TestCase):
    def test_dashboard_action_keeps_project_as_cli_string(self):
        import crange
        args = crange.make_parser().parse_args(crange_jobs.command('reconcile-project-reservations', 100, {}))
        with patch.object(crange, 'project_dir', return_value=Path('/tmp/project-100')) as locate, \
             patch.object(scheduling, 'reconcile_applied_reservations', return_value=[]):
            args.func(args)
        locate.assert_called_once_with('100')

    def run_reconcile(self, *, tasks=(), node='proxmox1', marker='p100', reservation_id='r1'):
        resolved = {'node_name': 'proxmox1', 'datastore_id': 'local-lvm', 'clone_mode': 'linked'}
        placement = {'resource_key': 'vm:server', 'reservation_id': 'r1', 'resolved': resolved}
        ledger = Mock()
        ledger.get_job.return_value = {'project_id': 100, 'state': 'running',
            'allocations': [{**placement, 'reservation_id': reservation_id}]}
        api = Mock()
        api.request.side_effect = [{'tags': marker}, list(tasks)]
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / 'project-100'
            plans = project / '.crange-plans'; plans.mkdir(parents=True)
            (plans / 'abc.json').write_text(json.dumps({'plan_id': 'abc', 'placements': [placement]}))
            with patch.object(scheduling, 'ReservationLedger', return_value=ledger), \
                    patch.object(scheduling.time, 'monotonic', side_effect=[0, 91]), \
                    patch.object(scheduling, 'applied_resources', return_value={
                        'vm:server': {'vm_id': 129, 'node_name': node}}):
                result = scheduling.reconcile_applied_reservations(project, api=api)
        return result, ledger

    def test_confirmed_applied_guest_releases_retained_reservation(self):
        result, ledger = self.run_reconcile()
        self.assertEqual(result, ['abc'])
        ledger.reconcile_job.assert_called_once_with('abc', operation_terminal=True, allocations_reconciled=True)

    def test_active_provider_task_blocks_reconciliation(self):
        with self.assertRaisesRegex(PlacementError, 'still running'):
            self.run_reconcile(tasks=[{'id': '129'}])

    def test_wrong_node_or_ownership_blocks_reconciliation(self):
        with self.assertRaisesRegex(PlacementError, 'node differs'):
            self.run_reconcile(node='proxmox5')
        with self.assertRaisesRegex(PlacementError, 'ownership mismatch'):
            self.run_reconcile(marker='p101')

    def test_manifest_must_match_exact_reservation(self):
        with self.assertRaisesRegex(PlacementError, 'differs'):
            self.run_reconcile(reservation_id='other')

    def test_completion_waits_for_owned_task_then_accepts_terminal_state(self):
        api = Mock()
        api.request.side_effect = [[{'id': '129'}], []]
        pause = Mock()
        scheduling._wait_for_applied_tasks(api, 'proxmox1', {129}, sleep=pause, clock=lambda: 0)
        self.assertEqual(api.request.call_count, 2)
        pause.assert_called_once_with(2)

    def test_unrelated_project_activity_does_not_block_reviewed_allocation(self):
        api = Mock()
        api.request.return_value = [{'id': '128'}, {'id': '116'}]
        pause = Mock()
        scheduling._wait_for_applied_tasks(api, 'proxmox1', {129}, sleep=pause, clock=lambda: 0)
        pause.assert_not_called()

    def test_indeterminate_provider_response_fails_closed(self):
        api = Mock()
        api.request.return_value = None
        with self.assertRaisesRegex(PlacementError, 'Could not verify'):
            scheduling._wait_for_applied_tasks(api, 'proxmox1', {129}, clock=lambda: 0)


if __name__ == '__main__':
    unittest.main()
