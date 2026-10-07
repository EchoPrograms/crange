from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_cache_ownership import cache_owned, cache_tag, cache_tags


class CacheOwnershipTests(unittest.TestCase):
    def test_compact_labels_bind_exact_vmid_and_full_job(self):
        config = {'tags': 'crange-image;crange-cache-200',
                  'description': 'CRANGE distribution cache-abc image base-v1'}
        self.assertTrue(cache_owned(config, 200, 'cache-abc'))
        self.assertFalse(cache_owned(config, 201, 'cache-abc'))
        self.assertFalse(cache_owned(config, 200, 'cache-ab'))
        self.assertFalse(cache_owned(config, 200, 'other'))
        self.assertFalse(cache_owned({**config, 'description': ''}, 200, 'cache-abc'))
        self.assertFalse(cache_owned({**config, 'tags': 'crange-cache-200'}, 200, 'cache-abc'))
        self.assertFalse(cache_owned(config, 200, ''))

    def test_legacy_exact_job_tags_remain_valid(self):
        config = {'tags': 'crange-image;crange-job-cache-abc'}
        self.assertTrue(cache_owned(config, 200, 'cache-abc'))
        self.assertFalse(cache_owned(config, 200, 'cache-other'))

    def test_new_generation_drops_inherited_ownership(self):
        self.assertEqual(cache_tags({'tags': 'approved;crange-job-old;crange-cache-100;crange-image'}, 200),
                         'approved;crange-cache-200;crange-image')
        for vmid in [True, '200', 99]:
            with self.assertRaises(ValueError):
                cache_tag(vmid)
