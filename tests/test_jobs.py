import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_jobs import JobStore, command


class JobTests(unittest.TestCase):
    def test_rejects_arbitrary_commands_and_fields(self):
        for op, fields in [('shell', {}), ('team-add', {'team': '; id'}),
                           ('box-add', {'name': 'x', 'team': 1, 'node': 'proxmox4', 'command': 'id'}),
                           ('network-add', {'name': '../x', 'team': 1, 'id': 100, 'node': 'proxmox4'})]:
            with self.subTest(op=op, fields=fields), self.assertRaises(ValueError):
                command(op, 3, fields)

    def test_project_visibility_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'jobs.sqlite3'
            store = JobStore(path)
            job = store.submit('alice', 3, 'team-add', {'team': 2})
            self.assertEqual(store.list(['3'])[0]['id'], job)
            self.assertEqual(store.list(['4']), [])
            self.assertEqual(JobStore(path).list(['3'])[0]['status'], 'queued')

    def test_active_jobs_are_returned_even_beyond_history_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / 'jobs.sqlite3')
            running = store.submit('alice', 3, 'team-add', {'team': 2})
            with store.connect() as db:
                db.execute("UPDATE jobs SET status='running' WHERE id=?", (running,))
            for team in (3, 4):
                old = store.submit('alice', 3, 'team-add', {'team': team})
                with store.connect() as db:
                    db.execute("UPDATE jobs SET status='succeeded',finished=? WHERE id=?", (team, old))
            rows = store.list(['3'], limit=1)
            self.assertEqual([row['id'] for row in rows if row['status'] == 'running'], [running])
            self.assertEqual(sum(row['status'] == 'succeeded' for row in rows), 1)

    def test_cancel_queued_job_is_project_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / 'jobs.sqlite3')
            job = store.submit('alice', 3, 'team-add', {'team': 2})
            self.assertFalse(store.cancel(job, ['4']))
            self.assertEqual(store.list(['3'])[0]['status'], 'queued')
            self.assertTrue(store.cancel(job, ['3']))
            record = store.list(['3'])[0]
            self.assertEqual(record['status'], 'interrupted')
            self.assertIn('Cancelled before starting', record['output'])
            self.assertFalse(store.cancel(job, ['3']))

    def test_apply_job_exposes_only_reviewed_plan_id(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / 'jobs.sqlite3')
            plan_id = 'a' * 32
            store.submit('alice', 3, 'apply', {'plan_id': plan_id, 'allow_destroy': False, 'allow_replace': False})
            job = store.list(['3'])[0]
            self.assertEqual(job['plan_id'], plan_id)
            self.assertNotIn('options', job)
            release = store.submit('alice', 3, 'release-apply', {'teams': [1], 'plan_id': plan_id, 'allow_destroy': True, 'allow_replace': False})
            result = next(item for item in store.list(['3']) if item['id'] == release)
            self.assertEqual(result['plan_id'], plan_id)
            self.assertNotIn('options', result)

    def test_deployment_refreshes_connections_and_reports_partial_failure(self):
        for failed_phase in (None, 'guac-sync', 'guac-apply'):
            with self.subTest(failed_phase=failed_phase), tempfile.TemporaryDirectory() as directory:
                store = JobStore(Path(directory) / 'jobs.sqlite3')
                store.submit('alice', 3, 'stage-deploy',
                             {'plan_id': 'a' * 32, 'allow_destroy': False, 'allow_replace': False})
                def run(argv, **kwargs):
                    operation = argv[2]
                    return SimpleNamespace(returncode=int(operation == failed_phase),
                                           stdout=operation+' completed', stderr='')
                with patch('crange_jobs.subprocess.run', side_effect=run) as process:
                    store.run_next()
                operations = [call.args[0][2] for call in process.call_args_list]
                expected = ['stage-deploy', 'guac-sync']
                if failed_phase != 'guac-sync':
                    expected.append('guac-apply')
                self.assertEqual(operations, expected)
                result = store.list(['3'])[0]
                self.assertEqual(result['status'], 'failed' if failed_phase else 'succeeded')
                if failed_phase:
                    self.assertIn('Infrastructure action completed', result['output'])

    def test_failed_deployment_does_not_refresh_guacamole(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / 'jobs.sqlite3')
            store.submit('alice', 3, 'configure-team', {'team': 0})
            with patch('crange_jobs.subprocess.run', return_value=SimpleNamespace(
                    returncode=1, stdout='', stderr='configuration failed')) as process:
                store.run_next()
            self.assertEqual(process.call_count, 1)
            self.assertEqual(store.list(['3'])[0]['status'], 'failed')

    def test_remote_service_edit_refreshes_once(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / 'jobs.sqlite3')
            store.submit('alice', 3, 'jumpbox-settings', {
                'name': 'web01', 'kind': 'vm', 'display_name': 'Web 01',
                'is_jumpbox': True, 'services': ['ssh', 'rdp', 'vnc'],
            })

            def run(argv, **kwargs):
                operation = argv[2]
                return SimpleNamespace(returncode=0, stdout=operation + ' completed', stderr='')

            with patch('crange_jobs.subprocess.run', side_effect=run) as process:
                store.run_next()

            operations = [call.args[0][2] for call in process.call_args_list]
            # The CLI already calls guac-refresh after changing services.
            self.assertEqual(operations, ['jumpbox-settings'])
            self.assertEqual(store.list(['3'])[0]['status'], 'succeeded')

    def test_fixed_cli_vector(self):
        self.assertEqual(command("guac-refresh", 3, {}), ["guac-refresh", "3"])
        self.assertEqual(command('network-add', 3, {'name': 'corp', 'team': 1, 'id': 100, 'node': 'proxmox4'}),
                         ['network-add', '3', 'corp', '--team', '1', '--id', '100', '--node', 'proxmox4'])

    def test_release_and_scenario_inputs_are_typed(self):
        self.assertEqual(command('release-deploy', 3, {'version': '1.2.0', 'teams': [1, 2]}),
                         ['release-deploy', '3', '1.2.0', '--teams', '1', '2'])
        self.assertEqual(command('scenario-remove', 3, {'name': 't254-web'}),
                         ['scenario-remove', '3', 't254-web'])
        self.assertEqual(command('release-capture', 3, {'version': '1.0.0', 'boot_order': ['web,db', 'db,web']}),
                         ['release-capture', '3', '1.0.0', '--boot-order', 'web,db', '--boot-order', 'db,web'])
        for options in ({'version': '../../tmp', 'teams': [1]},
                        {'version': '1.0.0', 'teams': [1, 1]}):
            with self.assertRaises(ValueError):
                command('release-deploy', 3, options)
