import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_scheduling import disk_growth_requested, requested_disk_gb, PlacementError


class JumpboxDiskRequestTests(unittest.TestCase):
    def row(self, disk=48):
        # declarations() represents jumpboxes as VM workloads.
        return {'resource_key': 'jumpbox:t001-j01', 'kind': 'vm', 'box': {'disk_gb': disk}}

    def live(self, size=32):
        return {'disk': [{'size': size, 'datastore_id': 'local-hdd'}]}

    def test_declared_size_is_admitted_instead_of_the_smaller_base_size(self):
        self.assertEqual(requested_disk_gb(self.row(), {'disk_gb': 32}), 48)
        self.assertTrue(disk_growth_requested(self.row(), self.live()))

    def test_applied_size_and_omitted_size_keep_the_current_guest(self):
        self.assertFalse(disk_growth_requested(self.row(), self.live(48)))
        self.assertFalse(disk_growth_requested({'resource_key': 'jumpbox:old', 'box': {}}, {}))

    def test_shrink_invalid_request_and_unknown_actual_disk_are_rejected(self):
        for disk, live in ((48, self.live(64)), (True, self.live()), (48, {}), (48, self.live(float('nan')))):
            with self.subTest(disk=disk, live=live), self.assertRaises(PlacementError):
                disk_growth_requested(self.row(disk), live)

    def test_source_size_remains_a_minimum_for_new_jumpboxes(self):
        with self.assertRaises(PlacementError):
            requested_disk_gb(self.row(24), {'disk_gb': 32})
