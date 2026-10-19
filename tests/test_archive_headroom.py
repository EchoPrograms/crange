import pathlib
import sys
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from crange_capture import archive_headroom

class ArchiveHeadroomTests(unittest.TestCase):
    def test_full_uncompressed_archive_and_reserve_fit_without_overcommit(self):
        gib=1024**3; reserve=archive_headroom(392.5*gib)
        self.assertAlmostEqual(reserve/gib,39.25)
        self.assertGreaterEqual((137.8-70.4)*gib,reserve)
        self.assertLess((100-70.4)*gib,reserve)
    def test_minimum_reserve_and_unavailable_capacity(self):
        self.assertEqual(archive_headroom(100*1024**3),20*1024**3)
        for total in (0,-1,float('nan'),float('inf')):
            with self.assertRaises(ValueError):archive_headroom(total)
