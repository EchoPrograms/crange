import copy
import pathlib
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from crange_catalog import CatalogError, publish_image, validate_catalog
from crange_placement import PlacementError, ReservationLedger, schedule_batch


def fixtures():
    checksum = 'a' * 64
    catalog = {'schema_version': 1, 'images': {'base-v1': {'kind': 'vm', 'architecture': 'x86_64', 'artifact_sha256': checksum, 'source_revision': 'abc', 'verified': True, 'approved_projects': [3], 'replicas': [dict(node=n, template_id=300+i, datastore_id='local', clone_modes_verified=['linked'], state='ready', artifact_sha256=checksum, verified_at=1) for i, n in enumerate(('n1', 'n2'))]}}}
    node = dict(online=True, memory_mb=16384, cpu_cores=8, committed_memory_mb=4096, observed_memory_mb=3000, committed_cpu_cores=1, architectures=['x86_64'], guest_types=['vm'], features=[], networks=['p003t254'], verified_networks=['p003t254'], management_verified=True, storages={'local': dict(available=True, total_gb=200, free_gb=150, committed_gb=50, guest_types=['vm'], clone_modes=['full', 'linked'], snapshots_verified=True)})
    inventory = {'captured_at': 100, 'nodes': {'n1': copy.deepcopy(node), 'n2': copy.deepcopy(node)}}
    policy = {'revision': 'one', 'allowed_nodes': ['n1', 'n2'], 'allowed_storages': ['local']}
    request = dict(project_id=3, resource_key='box', image='base-v1', kind='vm', memory_mb=4096, cpu_cores=2, disk_gb=20, networks=['p003t254'])
    return request, inventory, catalog, policy


class PlacementTests(unittest.TestCase):
    def test_soft_preference_and_hard_pin(self):
        r, inv, cat, pol = fixtures()
        r['placement'] = {'preferred_node': 'n2'}
        self.assertEqual(schedule_batch([r], inv, cat, pol, now=100)[0]['resolved']['node_name'], 'n2')
        inv['nodes']['n2']['committed_memory_mb'] = 11000
        placed = schedule_batch([r], inv, cat, pol, now=100)[0]
        self.assertEqual(placed['resolved']['node_name'], 'n1')
        self.assertIn('headroom', placed['reason'])
        r['placement'] = {'pinned_node': 'n2', 'mode': 'pin'}
        with self.assertRaises(PlacementError):
            schedule_batch([r], inv, cat, pol, now=100)

    def test_stopped_commitments_observed_memory_and_stale(self):
        r, inv, cat, pol = fixtures()
        for node in inv['nodes'].values():
            node['committed_memory_mb'] = 9000  # Includes stopped homelab guests.
        with self.assertRaises(PlacementError):
            schedule_batch([r], inv, cat, pol, now=100)
        with self.assertRaises(PlacementError):
            schedule_batch([r], inv, cat, pol, now=500)
        for node in inv['nodes'].values():
            node['committed_memory_mb'] = 1000
            node['observed_memory_mb'] = 10000
        with self.assertRaises(PlacementError):
            schedule_batch([r], inv, cat, pol, now=100)

    def test_configured_overcommit_limits_and_linked_local_storage(self):
        r, inv, cat, pol = fixtures()
        pol['allowed_nodes'] = ['n1']
        node = inv['nodes']['n1']
        node['committed_cpu_cores'] = 20
        node['committed_memory_mb'] = 20000
        node['storages']['local'].update(committed_gb=250, free_gb=50)
        r.update(cpu_cores=2, memory_mb=4096, disk_gb=8)
        with self.assertRaises(PlacementError):
            schedule_batch([r], inv, cat, pol, now=100)
        pol.update(cpu_overcommit_ratio=3.5, memory_overcommit_ratio=2,
                   local_linked_disk_overcommit_ratio=1.5)
        placement = schedule_batch([r], inv, cat, pol, now=100)[0]
        self.assertEqual(placement['resolved']['clone_mode'], 'linked')
        self.assertGreaterEqual(placement['headroom']['disk_gb'], 0)
        pol['clone_mode'] = 'full'
        with self.assertRaises(PlacementError):
            schedule_batch([r], inv, cat, pol, now=100)
        pol['local_linked_disk_overcommit_ratio'] = 1.6
        with self.assertRaisesRegex(PlacementError, 'ceiling'):
            schedule_batch([r], inv, cat, pol, now=100)

    def test_batch_determinism_reserves_and_network_gate(self):
        r, inv, cat, pol = fixtures()
        requests = [dict(r, resource_key=k) for k in ('b', 'a', 'c', 'd')]
        result = schedule_batch(requests, inv, cat, pol, now=100)
        self.assertEqual(result, schedule_batch(list(reversed(requests)), inv, cat, pol, now=100))
        self.assertEqual({x['resolved']['node_name'] for x in result}, {'n1', 'n2'})
        self.assertTrue(all(x['headroom']['memory_mb'] >= 0 for x in result))
        requests.append(dict(r, resource_key='e'))
        with self.assertRaises(PlacementError):
            schedule_batch(requests, inv, cat, pol, now=100)
        inv['nodes']['n1']['verified_networks'] = []
        inv['nodes']['n2']['verified_networks'] = []
        with self.assertRaises(PlacementError):
            schedule_batch([r], inv, cat, pol, now=100)

    def test_quota_and_unverified_artifact(self):
        r, inv, cat, pol = fixtures()
        pol['project_quotas'] = {'3': {'memory_mb': 5000}}
        inv['quota_usage'] = {'3': {'memory_mb': 2000}}
        with self.assertRaises(PlacementError):
            schedule_batch([r], inv, cat, pol, now=100)
        with self.assertRaises(CatalogError):
            publish_image(cat, 'base-v1', cat['images']['base-v1'])
        cat['images']['base-v1']['replicas'][0]['artifact_sha256'] = 'b' * 64
        with self.assertRaises(CatalogError):
            validate_catalog(cat)

    def test_stock_archive_kind_policy_and_host_cpu(self):
        r, inv, cat, pol = fixtures()
        image = cat['images']['base-v1']
        image.update(kind='container', source_type='stock_archive')
        for replica in image['replicas']:
            replica.pop('template_id')
            replica['template_file_id'] = 'local:vztmpl/debian.tar.zst'
        r['kind'] = 'container'
        for node in inv['nodes'].values():
            node['guest_types'].append('container')
            node['storages']['local']['guest_types'].append('container')
        placed = schedule_batch([r], inv, cat, pol, now=100)[0]
        self.assertIsNone(placed['resolved']['template_id'])
        self.assertEqual(placed['resolved']['template_file_id'], 'local:vztmpl/debian.tar.zst')
        image['source_type'] = 'captured'
        with self.assertRaises(CatalogError):
            validate_catalog(cat)
        r, inv, cat, pol = fixtures()
        r['cpu_type'] = 'host'
        with self.assertRaises(PlacementError):
            schedule_batch([r], inv, cat, pol, now=100)
        cat['images']['base-v1']['host_compatibility_feature'] = 'host-family-a'
        inv['nodes']['n2']['features'] = ['host-family-a']
        self.assertEqual(schedule_batch([r], inv, cat, pol, now=100)[0]['resolved']['node_name'], 'n2')

    def test_exact_revalidation_and_single_claim(self):
        r, inv, cat, pol = fixtures()
        with tempfile.TemporaryDirectory() as temp:
            ledger = ReservationLedger(pathlib.Path(temp) / 'ledger')
            ledger.reserve_batch([r], inv, cat, pol, 3, 'job', 'owner', now=100)
            changed = dict(pol, revision='changed')
            with self.assertRaises(PlacementError):
                ledger.revalidate_job('job', [r], inv, cat, changed, now=101)
            changed_inv = copy.deepcopy(inv)
            changed_inv['nodes']['n1']['online'] = False
            with self.assertRaises(PlacementError):
                ledger.mark_running('job', now=101, requests=[r], inventory=changed_inv, catalog=cat, policy=pol)
            self.assertEqual(ledger.status('job')['state'], 'reviewed')
            ledger.mark_running('job', now=101, requests=[r], inventory=inv, catalog=cat, policy=pol)
            with self.assertRaises(PlacementError):
                ledger.mark_running('job', now=102, requests=[r], inventory=inv, catalog=cat, policy=pol)
            ledger.close()

    def test_partial_allocation_dedupes_only_confirmed_capacity(self):
        r, inv, cat, pol = fixtures()
        pol['allowed_nodes'] = ['n1']
        with tempfile.TemporaryDirectory() as temp:
            ledger = ReservationLedger(pathlib.Path(temp) / 'ledger')
            reserved = ledger.reserve_batch([r], inv, cat, pol, 3, 'job', 'owner', now=100)[0]
            actual = {'reservation_id': reserved['reservation_id'], **reserved['resolved'], **reserved['resources']}
            inv['nodes']['n1']['committed_memory_mb'] += r['memory_mb']
            inv['nodes']['n1']['committed_cpu_cores'] += r['cpu_cores']
            inv['nodes']['n1']['storages']['local']['committed_gb'] += actual['disk_gb']
            inv['nodes']['n1']['storages']['local']['free_gb'] -= actual['disk_gb']
            with self.assertRaises(PlacementError):
                schedule_batch([dict(r, resource_key='other')], inv, cat, pol, ledger.active_reservations(), now=100)
            result = schedule_batch([dict(r, resource_key='other')], inv, cat, pol, ledger.active_reservations(), now=100, known_existing_allocations=[actual])
            self.assertEqual(len(result), 1)
            ledger.revalidate_job('job', [r], inv, cat, pol, now=101, known_existing_allocations=[actual])
            self.assertEqual(len(ledger.active_reservations()), 1)
            ledger.close()

    def test_expired_review_requires_negative_evidence_and_never_purges_running(self):
        r, inv, cat, pol = fixtures()
        with tempfile.TemporaryDirectory() as temp:
            ledger = ReservationLedger(pathlib.Path(temp) / 'ledger')
            ledger.reserve_batch([r], inv, cat, pol, 3, 'job', 'owner', now=100)
            with self.assertRaises(PlacementError):
                ledger.purge_reviewed('job', operation_terminal=True, no_allocations_confirmed=False, now=10000)
            ledger.purge_reviewed('job', operation_terminal=True, no_allocations_confirmed=True, now=10000)
            self.assertEqual(ledger.status('job')['state'], 'released')
            ledger.reserve_batch([r], inv, cat, pol, 3, 'running', 'owner', now=100)
            ledger.mark_running('running', now=101)
            with self.assertRaises(PlacementError):
                ledger.purge_reviewed('running', operation_terminal=True, no_allocations_confirmed=True, now=10000)
            self.assertEqual(ledger.status('running')['state'], 'running')
            ledger.close()

    def test_atomic_revalidation_claim_race(self):
        r, inv, cat, pol = fixtures()
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'ledger'
            ledger = ReservationLedger(path)
            ledger.reserve_batch([r], inv, cat, pol, 3, 'job', 'owner', now=100)
            ledger.close()
            barrier = threading.Barrier(2)
            outcomes = []
            def claim():
                ledger = ReservationLedger(path)
                barrier.wait()
                try:
                    ledger.mark_running('job', now=101, requests=[r], inventory=inv, catalog=cat, policy=pol)
                    outcomes.append('ok')
                except PlacementError:
                    outcomes.append('failed')
                finally:
                    ledger.close()
            threads = [threading.Thread(target=claim) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sorted(outcomes), ['failed', 'ok'])

    def test_concurrent_batches_atomic_and_running_timeout_safe(self):
        r, inv, cat, pol = fixtures()
        pol['allowed_nodes'] = ['n1']
        requests = [dict(r, resource_key=k) for k in ('a', 'b')]
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'reservations.sqlite'
            ReservationLedger(path).db.close()
            barrier = threading.Barrier(2)
            outcomes = []
            def reserve(job):
                ledger = ReservationLedger(path)
                barrier.wait()
                try:
                    ledger.reserve_batch(requests, inv, cat, pol, 3, job, 'user', now=100)
                    outcomes.append(('ok', job))
                except PlacementError:
                    outcomes.append(('failed', job))
                finally:
                    ledger.db.close()
            threads = [threading.Thread(target=reserve, args=(str(i),)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sorted(x[0] for x in outcomes), ['failed', 'ok'])
            ledger = ReservationLedger(path)
            self.assertEqual(len(ledger.active_reservations()), 2)
            job = next(j for status, j in outcomes if status == 'ok')
            ledger.mark_running(job, now=101)
            self.assertEqual(len(ledger.validate_job(job, now=100000)), 2)
            with self.assertRaises(PlacementError):
                ledger.reconcile_job(job, operation_terminal=False, allocations_reconciled=False)
            self.assertEqual(len(ledger.active_reservations()), 2)
            ledger.reconcile_job(job, operation_terminal=True, allocations_reconciled=True)
            self.assertEqual(ledger.active_reservations(), [])
            ledger.db.close()


if __name__ == '__main__':
    unittest.main()
