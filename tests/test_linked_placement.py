import copy
from pathlib import Path
import sys
import unittest
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from crange_placement import schedule_batch, PlacementError, ReservationLedger
from crange_scheduling import freeze
from test_placement import fixtures

class LinkedPlacementTests(unittest.TestCase):
    def test_default_links_local_cache_and_reserves_full_growth(self):
        r, inv, cat, pol=fixtures()
        result=schedule_batch([r],inv,cat,pol,now=100)[0]
        self.assertEqual(result['resolved']['clone_mode'],'linked')
        self.assertTrue(result['resolved']['verified_local_cache'])
        self.assertEqual(result['resources']['disk_gb'],22)
        self.assertIn('verified local linked',result['reason'])

    def test_missing_linked_proof_or_wrong_datastore_cannot_be_scheduled(self):
        for change in ({'clone_modes_verified':[]},{'datastore_id':'other'}):
            r,inv,cat,pol=fixtures()
            for replica in cat['images']['base-v1']['replicas']:replica.update(change)
            with self.assertRaisesRegex(PlacementError,'preparation'):
                schedule_batch([r],inv,cat,pol,now=100)
        r,inv,cat,pol=fixtures()
        for node in inv['nodes'].values():node['storages']['local']['snapshots_verified']=False
        with self.assertRaises(PlacementError):schedule_batch([r],inv,cat,pol,now=100)

    def test_full_mode_requires_explicit_policy_and_is_visible(self):
        r,inv,cat,pol=fixtures();pol['clone_mode']='full'
        for replica in cat['images']['base-v1']['replicas']:replica['clone_modes_verified']=[]
        result=schedule_batch([r],inv,cat,pol,now=100)[0]
        self.assertEqual(result['resolved']['clone_mode'],'full')
        self.assertIn('explicit full-clone',result['reason'])
        r['resolved_clone_mode']='linked'
        with self.assertRaises(PlacementError):schedule_batch([r],inv,cat,pol,now=100)

    def test_freeze_uses_previous_source_metadata_when_provider_omits_clone(self):
        previous={'node_name':'n1','datastore_id':'ssd','template_id':301,'template_node':'n1','clone_mode':'linked','verified_local_cache':True}
        row={'resource_key':'vm:a','image':'base'}
        frozen=freeze(row,{'node_name':'n1'},previous)
        self.assertEqual(frozen['resolved'],previous)
        self.assertEqual(previous['clone_mode'],'linked')

class CacheCapacityTests(unittest.TestCase):
    def test_capacity_reservation_needs_no_fake_replica_and_prevents_overbooking(self):
        r,inv,cat,pol=fixtures()
        cat['images']['base-v1']['replicas']=[]
        with tempfile.TemporaryDirectory() as directory:
            ledger=ReservationLedger(Path(directory)/'ledger.db')
            try:
                args=dict(project_id=3,owner='worker',image_id='base-v1',catalog=cat,node_name='n1',datastore_id='local',capacity=dict(memory_mb=6000,cpu_cores=2,disk_gb=42,clone_mode='linked'),inventory=inv,policy=pol,now=100)
                result=ledger.reserve_preparation(job_id='cache1',**args)
                self.assertEqual(result['operation'],'cache_prepare')
                self.assertEqual(cat['images']['base-v1']['replicas'],[])
                with self.assertRaisesRegex(PlacementError,'headroom'):
                    ledger.reserve_preparation(job_id='cache2',**args)
                ledger.mark_running('cache1',now=100)
                self.assertEqual(ledger.validate_job('cache1',now=9999)[0]['resources']['disk_gb'],42)
            finally:ledger.close()
