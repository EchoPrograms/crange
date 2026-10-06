import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_distribution import DistributionError, FileJournal


UPID = 'UPID:proxmox1:0031596C:1BECCA7D:6AC48195:vzdump:8830:crange-iac@pve!terraform:'


class FileJournalTests(unittest.TestCase):
    def exporting_record(self):
        return {
            'job_id': 'export-1', 'node': 'proxmox1', 'kind': 'vm', 'source_id': 8830,
            'storage': 'crange-library', 'state': 'exporting', 'disk_gb': 20,
            'source_identity': {'captured': 'opaque'},
            'identity_capture': {'job_id': 'capture-1', 'state': 'identity-captured'},
        }

    def test_export_job_can_record_upid_then_terminal_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = FileJournal(Path(directory))
            initial = self.exporting_record()
            journal(initial)
            submitted = {**initial, 'upid': UPID}
            journal(submitted)
            exported = {
                **submitted,
                'state': 'exported',
                'volume_id': 'crange-library:backup/vzdump-qemu-8830-2026_10_06.vma.zst',
                'artifact_sha256': 'a' * 64,
                'verified': True,
                'complete_capture': True,
            }
            journal(exported)
            self.assertEqual(json.loads((Path(directory) / 'export-1.json').read_text()), exported)

    def test_export_update_rejects_changed_source_and_upid(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = FileJournal(Path(directory))
            initial = self.exporting_record()
            journal(initial)
            with self.assertRaisesRegex(DistributionError, 'immutable job identity'):
                journal({**initial, 'source_id': 8831})
            journal({**initial, 'upid': UPID})
            with self.assertRaisesRegex(DistributionError, 'task identity'):
                journal({**initial, 'upid': UPID.replace('0031596C', '0031596D')})

    def test_export_state_cannot_skip_verified_terminal_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = FileJournal(Path(directory))
            initial = self.exporting_record()
            journal(initial)
            incomplete = {**initial, 'state': 'exported', 'upid': UPID}
            with self.assertRaisesRegex(DistributionError, 'verified artifact'):
                journal(incomplete)

    def test_provider_tasks_are_append_only_and_terminal_is_monotonic(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = FileJournal(Path(directory))
            initial = {'job_id': 'cache-1', 'state': 'allocated', 'node': 'proxmox1',
                       'provider_tasks': [{'upid': UPID, 'state': 'running'}]}
            journal(initial)
            journal({**initial, 'provider_tasks': [{'upid': UPID, 'state': 'confirmed-terminal'}]})
            with self.assertRaisesRegex(DistributionError, 'cannot move backwards'):
                journal({**initial, 'provider_tasks': [{'upid': UPID, 'state': 'running'}]})

    def test_source_probe_records_a_terminal_verified_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = FileJournal(Path(directory))
            initial = {'job_id': 'source-probe-1', 'state': 'allocated',
                       'node': 'proxmox1', 'source_id': 8830, 'probe_id': 990,
                       'probe_pool': 'crange-probes'}
            journal(initial)
            verifying = {**initial, 'state': 'verifying',
                         'provider_tasks': [{'upid': UPID, 'state': 'running'}]}
            journal(verifying)
            terminal = {**verifying, 'state': 'source-probed',
                'provider_tasks': [{'upid': UPID, 'state': 'confirmed-terminal'}],
                'verification': {'passed': True, 'identity_unique': True,
                                 'evidence': {'isolated_bridge': 'crprobe1450'}}}
            journal(terminal)
            with self.assertRaisesRegex(DistributionError, 'state transition'):
                journal({**terminal, 'state': 'failed-verification'})

    def test_job_state_cannot_move_backwards(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = FileJournal(Path(directory))
            initial = {'job_id': 'cache-1', 'state': 'allocated', 'node': 'proxmox1'}
            journal(initial)
            journal({**initial, 'state': 'converting-template'})
            with self.assertRaisesRegex(DistributionError, 'state transition'):
                journal(initial)

    def test_terminal_export_cannot_be_created_without_inflight_job(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = FileJournal(Path(directory))
            exported = {**self.exporting_record(), 'state': 'exported', 'upid': UPID,
                        'volume_id': 'crange-library:backup/vzdump-qemu-8830.vma.zst',
                        'artifact_sha256': 'a' * 64, 'verified': True, 'complete_capture': True}
            with self.assertRaisesRegex(DistributionError, 'initial journal state'):
                journal(exported)


if __name__ == '__main__':
    unittest.main()
