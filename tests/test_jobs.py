import os
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_jobs import JobStore, command
from crange_server import job_query_params


class JobTests(unittest.TestCase):
    def test_capture_settings_are_platform_admin_scope_and_bounded(self):
        options={'bridge':'crprobe1450','storage':'local-lvm','backup':'crange-library'}
        self.assertEqual(command('template-capture-settings',0,options),
                         ['capture-settings','--bridge','crprobe1450','--storage','local-lvm','--backup','crange-library'])
        for project,value in [(100,options),(0,{**options,'storage':'../../private'}),(0,{**options,'bridge':'x; reboot'})]:
            with self.assertRaises(ValueError):command('template-capture-settings',project,value)

    def test_capture_selection_is_optional_distinct_and_not_shell_input(self):
        self.assertEqual(command('release-capture',100,{'version':'0.0.1','boot_order':[]}),
                         ['release-capture','100','0.0.1'])
        self.assertEqual(command('release-capture',100,{'version':'0.0.1','boot_order':[],
                                                      'machines':['t254-mhl-app01']}),
                         ['release-capture','100','0.0.1','--machine','t254-mhl-app01'])
        for value in [['t254-mhl-app01']*2,[[]],['../../secrets'],[True]]:
            with self.assertRaises(ValueError):command('release-capture',100,{'version':'0.0.1','boot_order':[],'machines':value})

    def test_snapshot_probe_retry_is_exact_and_admin_scoped(self):
        options={'source_job_id':'cache-'+'a'*32}
        self.assertEqual(command('template-cache-snapshot-retry',0,options),['cache-snapshot-probe-retry',options['source_job_id']])
        for project,value in ((100,options),(0,{'source_job_id':'../secret'})):
            with self.assertRaises(ValueError):command('template-cache-snapshot-retry',project,value)

    def test_windows_cache_recovery_is_bound_to_one_image_and_source_job(self):
        options = {'image': 'mhl-windows-server-2025-base-p100-v1',
                   'source_job_id': 'cache-15f3498e404342fbb096bd1f9012b4b2',
                   'action': 'resume'}
        self.assertEqual(command('template-cache-recovery', 0, options),
            ['cache-windows-p100-recovery', options['image'], '--source-job-id',
             options['source_job_id'], '--action', 'resume'])
        for project, changed in ((3, options), (0, {**options, 'image': '/tmp/image'}),
                                 (0, {**options, 'source_job_id': '../secrets'}),
                                 (0, {**options, 'action': 'delete'})):
            with self.subTest(project=project, options=changed), self.assertRaises(ValueError):
                command('template-cache-recovery', project, changed)

    def test_capacity_policy_job_is_admin_scope_and_bounded(self):
        self.assertEqual(command('template-capacity-policy-set', 0,
            {'cpu_ratio': 5, 'memory_ratio': 2, 'disk_ratio': 2}),
            ['cluster-capacity-policy-set', '--cpu-ratio', '5', '--memory-ratio', '2', '--disk-ratio', '2'])
        for project, options in ((3, {'cpu_ratio': 5, 'memory_ratio': 2, 'disk_ratio': 2}),
                                 (0, {'cpu_ratio': 6.25, 'memory_ratio': 2, 'disk_ratio': 2}),
                                 (0, {'cpu_ratio': 5, 'memory_ratio': 2.25, 'disk_ratio': 2}),
                                 (0, {'cpu_ratio': 5, 'memory_ratio': 2, 'disk_ratio': 2.25})):
            with self.subTest(project=project, options=options), self.assertRaises(ValueError):
                command('template-capacity-policy-set', project, options)

    def test_job_query_parameters_are_bounded_and_project_authorized(self):
        manager = {'admin': False, 'managed_projects': ['3']}
        self.assertEqual(job_query_params(parse_qs('page=2&page_size=50&project=3'), manager), (2, 50, 3))
        self.assertEqual(job_query_params({}, manager), (1, 100, None))
        for query in ('page=0', 'page=-1', 'page=1000001', 'page=1.5', 'page_size=9',
                      'page_size=101', 'page_size=10&page_size=25', 'project=3&project=3', 'project=3x'):
            with self.subTest(query=query), self.assertRaises(ValueError):
                job_query_params(parse_qs(query, keep_blank_values=True), manager)
        with self.assertRaises(PermissionError):
            job_query_params(parse_qs('project=4'), manager)

    def test_rejects_arbitrary_commands_and_fields(self):
        for op, fields in [('shell', {}), ('team-add', {'team': '; id'}),
                           ('box-add', {'name': 'x', 'team': 1, 'node': 'proxmox4', 'command': 'id'}),
                           ('network-add', {'name': '../x', 'team': 1, 'id': 100, 'node': 'proxmox4'})]:
            with self.subTest(op=op, fields=fields), self.assertRaises(ValueError):
                command(op, 3, fields)

    def test_network_verification_accepts_only_fixed_project_scope(self):
        self.assertEqual(command('network-verify', 3,
                                 {'node': 'proxmox2', 'network': 'p003t254'}),
                         ['network-verify', '3', '--node', 'proxmox2', '--network', 'p003t254'])
        self.assertEqual(command('network-verify', 3,
                                 {'node': 'proxmox2', 'network': 'p003t253'})[0], 'network-verify')
        self.assertEqual(command('network-verify', 3,
                                 {'node': 'proxmox2', 'network': 'p003c100'}),
                         ['network-verify', '3', '--node', 'proxmox2', '--network', 'p003c100'])
        for options in ({'node': 'proxmox2', 'network': 'p004t254'},
                        {'node': 'proxmox7', 'network': 'p003t254'},
                        {'node': 'proxmox2', 'network': 'p004c100'},
                        {'node': 'proxmox2', 'network': 'p003t255'},
                        {'node': 'proxmox2', 'network': 'p003t254', 'command': 'id'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                command('network-verify', 3, options)

    def test_network_cancel_is_durable_cooperative_and_stays_running(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / 'jobs.sqlite3')
            job = store.submit('alice', 3, 'network-verify',
                               {'node': 'proxmox2', 'network': 'p003t254'})
            with store.connect() as db:
                db.execute("UPDATE jobs SET status='running' WHERE id=?", (job,))
            proc = SimpleNamespace(pid=12345)
            store._processes[job] = proc
            with patch('crange_jobs.os.killpg') as kill:
                self.assertTrue(store.cancel(job, ['3']))
                kill.assert_not_called()
            self.assertTrue(store.cancel_requested(job))
            self.assertEqual(store.get(job, ['3'])['status'], 'running')
            self.assertFalse(store.cancel(job, ['4']))

    def test_network_worker_deadline_never_kills_cooperative_child(self):
        import sys
        with tempfile.TemporaryDirectory() as directory:
            store=JobStore(Path(directory)/'jobs.sqlite3')
            job=store.submit('alice',3,'network-verify',
                             {'node':'proxmox2','network':'p003t254'})
            with store.connect() as db:
                db.execute("UPDATE jobs SET status='running' WHERE id=?",(job,))
            result=store._run_streamed(job,[sys.executable,'-c','print("cleanup-complete")'],
                                      dict(os.environ),'',timeout=-1)
            self.assertEqual(result.returncode,0)
            self.assertIn('cleanup-complete',result.stdout)

    def test_infrastructure_workflows_have_bounded_long_timeout_without_extending_ordinary_jobs(self):
        for operation,options,expected in [('release-capture',{'version':'0.1.1','boot_order':[]},43200),
                                           ('stage-changes',{},43200),
                                           ('release-stage',{'teams':[1]},43200),
                                           ('release-configure',{'teams':[1]},43200),
                                           ('release-apply',{'teams':[1], 'plan_id':'a'*32, 'allow_destroy':False, 'allow_replace':False},43200),
                                           ('apply',{'plan_id':'a'*32, 'allow_destroy':False, 'allow_replace':False},43200),
                                           ('stage-deploy',{'plan_id':'a'*32, 'allow_destroy':False, 'allow_replace':False},43200),
                                           ('team-add',{'team':1},3600)]:
            with self.subTest(operation=operation),tempfile.TemporaryDirectory() as directory:
                store=JobStore(Path(directory)/'jobs.sqlite3')
                job=store.submit('alice',100,operation,options)
                with patch('crange_jobs.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='',stderr='')) as execute:
                    self.assertTrue(store.run_next())
                invocation=next(c for c in execute.call_args_list if ('infra-apply' if operation=='apply' else operation) in c.args[0])
                self.assertEqual(invocation.kwargs['timeout'],expected)
                self.assertEqual(store.get(job,['100'])['status'],'succeeded')

    def test_worker_lanes_do_not_claim_network_verification_as_unprivileged_job(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / 'jobs.sqlite3')
            network = store.submit('alice', 3, 'network-verify',
                                   {'node': 'proxmox2', 'network': 'p003t254'})
            normal = store.submit('alice', 3, 'team-add', {'team': 1})
            with patch('crange_jobs.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')) as execute:
                self.assertTrue(store.run_next())
                self.assertEqual(execute.call_args.args[0][1], str(Path(__file__).resolve().parents[1] / 'scripts/crange.py'))
            self.assertEqual(store.get(network, ['3'])['status'], 'queued')

    def test_process_argument_type_error_fails_one_job_without_crashing_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / 'jobs.sqlite3')
            job = store.submit('alice', 100, 'mhl-business-configure', {'network_id': 254})
            with patch('crange_jobs.subprocess.run', side_effect=TypeError(
                    'expected str, bytes or os.PathLike object, not int')):
                self.assertTrue(store.run_next())
            result = store.get(job, ['100'])
            self.assertEqual(result['status'], 'failed')
            self.assertIn('expected str, bytes or os.PathLike object, not int', result['output'])

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

    def test_paginated_history_keeps_authorized_active_jobs_and_counts_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / 'jobs.sqlite3')
            ids = []
            for i in range(35):
                job = store.submit('alice', 3 if i < 30 else 4, 'team-add', {'team': i % 10 + 1})
                ids.append(job)
            active = store.submit('alice', 3, 'team-add', {'team': 1})
            with store.connect() as db:
                db.execute("UPDATE jobs SET status='running' WHERE id=?", (active,))
                for index, job in enumerate(ids):
                    db.execute("UPDATE jobs SET status='succeeded',created=?,finished=? WHERE id=?",
                               (1000 + index, 1000 + index, job))

            first = store.list_page(['3'], page=1, page_size=10, project=3)
            second = store.list_page(['3'], page=2, page_size=10, project=3)
            self.assertEqual((first['total'], first['page_count']), (30, 3))
            self.assertTrue(first['has_next'])
            self.assertTrue(second['has_previous'])
            first_terminal = [job for job in first['jobs'] if job['status'] == 'succeeded']
            second_terminal = [job for job in second['jobs'] if job['status'] == 'succeeded']
            self.assertEqual(len(first_terminal), 10)
            self.assertEqual(len(second_terminal), 10)
            self.assertGreater(first_terminal[0]['created'], second_terminal[0]['created'])
            self.assertEqual([job['id'] for job in first['jobs'] if job['status'] == 'running'], [active])
            self.assertNotIn(4, {job['project'] for job in first['jobs']})

            manager_page = store.list_page(['3'], page=1, page_size=25)
            self.assertEqual(manager_page['total'], 30)
            self.assertNotIn(4, {job['project'] for job in manager_page['jobs'] if job['status'] != 'running'})

    def test_active_job_completion_and_pinned_lookup_survive_older_history_page(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / 'jobs.sqlite3')
            for index in range(35):
                job = store.submit('alice', 3, 'team-add', {'team': index % 10 + 1})
                with store.connect() as db:
                    db.execute("UPDATE jobs SET status='succeeded',created=?,finished=? WHERE id=?",
                               (1000 + index, 1000 + index, job))
            running = store.submit('alice', 3, 'team-add', {'team': 1})
            with store.connect() as db:
                db.execute("UPDATE jobs SET status='running' WHERE id=?", (running,))
            page_two = store.list_page(['3'], page=2, page_size=10, project=3)
            self.assertNotIn(running, {job['id'] for job in page_two['jobs'] if job['status'] != 'running'})
            with store.connect() as db:
                db.execute("UPDATE jobs SET status='failed',output='worker failed',finished=? WHERE id=?",
                           (time.time(), running))

            current_page = store.list_page(['3'], page=2, page_size=10, project=3)
            self.assertNotIn(running, {job['id'] for job in current_page['jobs']})
            pinned = store.get(running, ['3'])
            self.assertEqual((pinned['status'], pinned['output']), ('failed', 'worker failed'))
            self.assertIsNone(store.get(running, ['4']))

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

    def test_external_dashboard_stop_reaches_the_owning_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'jobs.sqlite3'
            worker, dashboard = JobStore(database), JobStore(database)
            job = worker.submit('alice', 3, 'inventory', {})
            with worker.connect() as db:
                db.execute("UPDATE jobs SET status='running' WHERE id=?", (job,))
            result = []
            thread = threading.Thread(target=lambda:result.append(worker._run_streamed(
                job, [sys.executable, '-u', '-c',
                      'import threading; print("child ready", flush=True); threading.Event().wait(30)'],
                os.environ.copy(), '', timeout=15)), daemon=True)
            thread.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and 'child ready' not in worker.get(job, ['3'])['output']:
                time.sleep(.02)
            self.assertIn('child ready', worker.get(job, ['3'])['output'])
            self.assertFalse(dashboard._processes)
            self.assertFalse(dashboard.cancel(job, ['4']))
            self.assertTrue(dashboard.cancel(job, ['3']))
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result[0].returncode, 130)
            self.assertEqual(worker.get(job, ['3'])['status'], 'interrupted')
            self.assertTrue(worker.cancel_requested(job))

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

    def test_router_bootstrap_jobs_are_fixed_and_review_bound(self):
        self.assertEqual(command('bootstrap-plan', 3, {}), ['bootstrap-plan', '3'])
        plan_id = 'b' * 32
        self.assertEqual(command('bootstrap-apply', 3, {'plan_id': plan_id}),
                         ['infra-bootstrap-apply', '3', '--yes', '--plan-id', plan_id])
        self.assertEqual(command('bootstrap-configure', 3, {}), ['bootstrap-configure', '3'])
        for operation, options in (
                ('bootstrap-plan', {'command': 'id'}),
                ('bootstrap-apply', {'plan_id': plan_id, 'allow_destroy': True}),
                ('bootstrap-apply', {'plan_id': '../../etc/passwd'}),
                ('bootstrap-configure', {'shell': 'id'})):
            with self.subTest(operation=operation, options=options), self.assertRaises(ValueError):
                command(operation, 3, options)

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
