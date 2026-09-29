import sys
import tempfile
import unittest
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

    def test_apply_job_exposes_only_reviewed_plan_id(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / 'jobs.sqlite3')
            plan_id = 'a' * 32
            store.submit('alice', 3, 'apply', {'plan_id': plan_id, 'allow_destroy': False, 'allow_replace': False})
            job = store.list(['3'])[0]
            self.assertEqual(job['plan_id'], plan_id)
            self.assertNotIn('options', job)

    def test_fixed_cli_vector(self):
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
