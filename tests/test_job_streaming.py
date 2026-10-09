import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_jobs import JobStore


class StreamingTests(unittest.TestCase):
    def exercise(self, script, timeout=5, cancel=False):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / 'jobs.sqlite3')
            job = store.submit('alice', 3, 'bootstrap-configure', {})
            with store.connect() as db:
                db.execute("UPDATE jobs SET status='running' WHERE id=?", (job,))
            result = []
            thread = threading.Thread(target=lambda: result.append(store._run_streamed(
                job, [sys.executable, '-c', script], {**os.environ, 'PYTHONUNBUFFERED': '1'}, '', timeout)))
            thread.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                output = store.list(['3'])[0]['output']
                if 'ready' in output:
                    break
                time.sleep(0.03)
            self.assertIn('ready', output)
            self.assertTrue(thread.is_alive(), 'progress must persist before the child exits')
            if cancel:
                self.assertTrue(store.cancel(job, ['3']))
            thread.join(6)
            self.assertFalse(thread.is_alive())
            return result[0], store.list(['3'])[0]

    def test_live_merged_utf8_output(self):
        proc, record = self.exercise("import os,time; os.write(1,b'ready\\n'); os.write(2,b'error\\n'); os.write(1,b'\\xe2'); time.sleep(.4); os.write(1,b'\\x82\\xac\\n'); time.sleep(.6)")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(record['output'], 'ready\nerror\n€\n')

    def test_cancel_kills_process_group_and_retains_partial_output(self):
        proc, record = self.exercise("import signal,time,subprocess,sys; signal.signal(signal.SIGTERM,signal.SIG_IGN); subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print('ready'); time.sleep(30)", cancel=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(record['status'], 'interrupted')
        self.assertIn('ready', record['output'])

    def test_timeout_retains_partial_output(self):
        proc, record = self.exercise("import time; print('ready'); time.sleep(30)", timeout=.5)
        self.assertEqual(proc.returncode, 124)
        self.assertIn('ready', record['output'])
        self.assertIn('time limit', record['output'])

    def test_output_is_bounded(self):
        proc, record = self.exercise("import time; print('ready'); time.sleep(.5); print('x'*60000)")
        self.assertEqual(len(record['output']), 50000)
        self.assertEqual(proc.stdout, record['output'])
