import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from crange_jobs import JobStore

class EmergencyPowerTests(unittest.TestCase):
    def test_stop_supersedes_restart_so_it_cannot_restart_after_emergency_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            store=JobStore(Path(directory)/'jobs.sqlite3')
            restart=store.submit('alice',3,'guac-box-control',{'resource_key':'vm:web','action':'restart'})
            store.submit('alice',3,'guac-box-control',{'resource_key':'vm:web','action':'stop'})
            self.assertEqual(store.get(restart,['3'])['status'],'interrupted')

    def test_stop_supersedes_only_its_boxes_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / 'jobs.sqlite3')
            shutdown = store.submit('alice', 3, 'guac-box-control', {'resource_key': 'vm:web', 'action': 'shutdown'})
            other = store.submit('alice', 3, 'guac-box-control', {'resource_key': 'vm:other', 'action': 'shutdown'})
            store.submit('alice', 3, 'guac-box-control', {'resource_key': 'vm:web', 'action': 'stop'})
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?', (shutdown,)).fetchone()[0], 'interrupted')
                self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?', (other,)).fetchone()[0], 'queued')

    def test_force_stop_runs_while_graceful_shutdown_is_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / 'jobs.sqlite3')
            waiting, forced, stop = threading.Event(), threading.Event(), threading.Event()
            shutdown = store.submit('alice', 3, 'guac-box-control', {'resource_key': 'vm:web', 'action': 'shutdown'})
            def execute(args, **kwargs):
                if args[-1] == 'shutdown':
                    waiting.set()
                    if not forced.wait(3):
                        raise RuntimeError('Force Stop was blocked by shutdown')
                elif args[-1] == 'stop':
                    forced.set(); stop.set()
                else:
                    raise AssertionError(args)
                return subprocess.CompletedProcess(args, 0, 'done', '')
            with patch('crange_jobs.subprocess.run', side_effect=execute):
                worker = threading.Thread(target=store.worker, args=(stop,), daemon=True)
                worker.start()
                self.assertTrue(waiting.wait(2))
                job = store.submit('alice', 3, 'guac-box-control', {'resource_key': 'vm:web', 'action': 'stop'})
                self.assertTrue(forced.wait(2))
                worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?', (shutdown,)).fetchone()[0], 'interrupted')
                self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?', (job,)).fetchone()[0], 'succeeded')
