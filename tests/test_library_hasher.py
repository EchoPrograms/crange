import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_distribution import SSHVolumeHasher, DistributionError


class LibraryHasherTests(unittest.TestCase):
    def test_central_backup_uses_restricted_library_path(self):
        run = Mock(return_value=subprocess.CompletedProcess([], 0, 'a' * 64 + '  file\n', ''))
        digest = SSHVolumeHasher(run=run)(
            'proxmox2', 'crange-library:backup/vzdump-qemu-8811-2026_10_05.vma.zst')
        self.assertEqual(digest, 'a' * 64)
        command = run.call_args.args[0]
        self.assertIn('root@10.60.0.30', command)
        self.assertIn('/srv/crange-library/dump/vzdump-qemu-8811-2026_10_05.vma.zst', command[-1])
        self.assertNotIn('root@proxmox2', command)

    def test_library_path_cannot_escape_export(self):
        run = Mock()
        with self.assertRaises(DistributionError):
            SSHVolumeHasher(run=run)('proxmox2', 'crange-library:backup/../secret')
        run.assert_not_called()

    def test_invalid_digest_fails_closed(self):
        run = Mock(return_value=subprocess.CompletedProcess([], 0, 'not-a-digest\n', ''))
        with self.assertRaises(DistributionError):
            SSHVolumeHasher(run=run)('proxmox2', 'crange-library:backup/vzdump-qemu-8811.vma.zst')


if __name__ == '__main__':
    unittest.main()
