from pathlib import Path
import contextlib
import copy
import sys
import tempfile
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_image_cache import build_eviction_plan, apply_eviction_plan, activate_cached_version, CacheError
from crange_distribution import FileJournal, DistributionError


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {'schema_version': 1, 'images': {'base-v1': {
            'kind': 'vm', 'architecture': 'amd64', 'source_revision': 'rev', 'verified': True,
            'artifact_sha256': 'a'*64, 'approved_projects': [3], 'disk_gb': 8,
            'artifact': {'format': 'vzdump', 'complete_capture': True,
                         'volume_id': 'crange-library:backup/vzdump-qemu-100.vma.zst'},
            'replicas': [{'node': 'proxmox2', 'template_id': 200, 'datastore_id': 'local-lvm',
                          'state': 'ready', 'verified_at': 'now', 'artifact_sha256': 'a'*64,
                          'cache_local': True, 'clone_modes': ['linked'], 'clone_modes_verified': ['linked'], 'job_id': 'prepare'}]}}}
        self.evidence = dict(cluster_complete=True, snapshots_complete=True, backing_complete=True,
            reservations_complete=True, operations_complete=True, pins_complete=True,
            revision='fresh-complete-1', observed_at=100,
            candidate={'node': 'proxmox2', 'template_id': 200, 'volumes': ['local-lvm:base-200-disk-0']},
            guests=[], snapshots=[], reservations=[], operations=[], pins=[],
            central_verification={'verified': True, 'verified_at': 100, 'artifact_sha256': 'a'*64,
                                  'volume_id': 'crange-library:backup/vzdump-qemu-100.vma.zst'})
        self.api = Mock()
        self.api.request.side_effect = lambda method, path, fields=None: (
            {'template': 1, 'tags': 'crange-image;crange-job-prepare', 'scsi0': 'local-lvm:base-200-disk-0,size=8G'} if path.endswith('/config') else
            {'status': 'stopped'} if path.endswith('/status/current') else 'UPID:gc')
        self.hash = Mock(return_value='a'*64)
        self.journal = Mock()

    def plan(self):
        return build_eviction_plan(self.catalog, 'base-v1', 'proxmox2', 200, self.evidence, now=100)

    def apply(self, plan):
        return apply_eviction_plan(self.api, plan, refresh=lambda: (self.catalog, self.evidence),
            hash_volume=self.hash, journal=self.journal, lock=contextlib.nullcontext,
            artifact_pin=lambda *args: contextlib.nullcontext(), now=101)

    def test_no_dependencies_eviction_rechecks_checksum_and_preserves_version(self):
        result = self.apply(self.plan())
        self.hash.assert_called_once()
        self.api.request.assert_any_call('DELETE', '/nodes/proxmox2/qemu/200', {'purge': 1})
        self.assertEqual(result['images']['base-v1']['replicas'], [])
        self.assertEqual(len(self.catalog['images']['base-v1']['replicas']), 1)

    def test_compact_cache_marker_requires_exact_job_before_deletion(self):
        for job, allowed in [('prepare', True), ('other-job', False)]:
            with self.subTest(job=job):
                self.api.reset_mock()
                self.api.request.side_effect = lambda method, path, fields=None: (
                    {'template': 1, 'tags': 'crange-image;crange-cache-200',
                     'description': f'CRANGE distribution {job} image base-v1',
                     'scsi0': 'local-lvm:base-200-disk-0,size=8G'} if path.endswith('/config') else
                    {'status': 'stopped'} if path.endswith('/status/current') else 'UPID:gc')
                if allowed:
                    self.apply(self.plan())
                    self.api.request.assert_any_call('DELETE', '/nodes/proxmox2/qemu/200', {'purge': 1})
                else:
                    with self.assertRaisesRegex(CacheError, 'ownership'):
                        self.apply(self.plan())
                    self.assertFalse(any(c.args[0] == 'DELETE' for c in self.api.request.call_args_list))

    def test_central_unavailable_never_deletes(self):
        plan = self.plan()
        self.hash.side_effect = OSError('library offline')
        with self.assertRaises(OSError): self.apply(plan)
        self.assertFalse(any(c.args[0] == 'DELETE' for c in self.api.request.call_args_list))
        self.journal.assert_not_called()

    def test_insufficient_proof_refuses_empty_lists(self):
        self.evidence['backing_complete'] = False
        with self.assertRaisesRegex(CacheError, 'Complete'): self.plan()

    def test_stopped_guest_created_after_review_blocks_apply(self):
        plan = self.plan()
        self.evidence['guests'].append({'vmid': 201, 'node': 'proxmox3', 'status': 'stopped',
                                        'backing_volumes': ['local-lvm:base-200-disk-0']})
        with self.assertRaisesRegex(CacheError, 'guest'): self.apply(plan)
        self.api.request.assert_not_called()

    def test_snapshot_reservation_pin_and_uncertain_operation_each_block(self):
        for field, row in [
            ('snapshots', {'vmid': 201, 'name': 'saved', 'origin_template_id': 200}),
            ('reservations', {'image_id': 'base-v1', 'state': 'running'}),
            ('pins', {'image_id': 'base-v1', 'node': 'proxmox2'}),
            ('operations', {'node': 'proxmox2', 'state': 'timed-out'})]:
            with self.subTest(field=field):
                self.evidence[field] = [row]
                with self.assertRaisesRegex(CacheError, 'still required'): self.plan()
                self.evidence[field] = []

    def test_new_version_activation_never_reparents_old_guest(self):
        self.catalog['images']['base-v2'] = copy.deepcopy(self.catalog['images']['base-v1'])
        self.catalog['images']['base-v2']['replicas'][0]['template_id'] = 300
        old = {'image_id': 'base-v1', 'template_id': 200}
        selection = activate_cached_version(self.catalog, 'base-v2', 'proxmox2', current=old)
        self.assertEqual(selection['template_id'], 300)
        self.assertEqual(selection['previous'], old)
        self.evidence['guests'] = [{'vmid': 201, 'origin_template_id': 200}]
        with self.assertRaisesRegex(CacheError, 'guest'): self.plan()

    def test_uncertain_cancelled_prepare_blocks_duplicate_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = FileJournal(Path(directory))
            old = dict(job_id='first', image_id='base-v1', node='proxmox2', datastore_id='local-lvm', state='allocated', template_id=200)
            journal(old)
            journal({**old, 'state': 'cancelled-uncertain'})
            with self.assertRaisesRegex(DistributionError, 'uncertain'):
                journal({**old, 'job_id': 'second'})
