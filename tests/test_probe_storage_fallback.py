import copy,pathlib,sys,tempfile,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from crange_distribution import reserve_probe_storage
from crange_placement import ReservationLedger,PlacementError
from test_placement import fixtures

class ProbeStorageFallbackTests(unittest.TestCase):
    def fixture(self):
        _,inv,_,policy=fixtures();node=inv['nodes']['n1'];node['architectures']=['amd64']
        node['storages']['local'].update(free_gb=50,shared=False)
        node['storages']['thin']={**node['storages']['local'],'total_gb':512,'free_gb':480,'committed_gb':32}
        node['storages']['unapproved']={**node['storages']['thin']}
        node['storages']['shared']={**node['storages']['thin'],'shared':True}
        policy.update(allowed_nodes=['n1'],allowed_storages=['local','thin','shared','unapproved'],
                      approved_storage=[['n1','local'],['n1','thin'],['n1','shared']])
        return dict(project_id=3,job_id='probe-fallback',owner='cli',source_ref='source:n1:qemu:151',
                    source_sha256='a'*64,node_name='n1',capacity={'memory_mb':4096,'cpu_cores':2,'disk_gb':64},
                    inventory=inv,policy=policy,now=100,kind='vm',clone_mode='full')
    def test_full_vm_probe_uses_another_local_store_without_duplicate_reservation(self):
        fields=self.fixture()
        with tempfile.TemporaryDirectory() as tmp:
            ledger=ReservationLedger(pathlib.Path(tmp)/'ledger')
            try:
                with self.assertRaisesRegex(PlacementError,'disk_gb'):reserve_probe_storage(ledger,datastore_id='local',**fields)
                allocation=reserve_probe_storage(ledger,datastore_id='local',auto_storage=True,**fields)
                self.assertEqual(allocation['resolved']['datastore_id'],'thin')
                self.assertEqual(len(ledger.status('probe-fallback')['allocations']),1)
                with self.assertRaisesRegex(PlacementError,'already has reservations'):
                    reserve_probe_storage(ledger,datastore_id='local',auto_storage=True,**fields)
            finally:ledger.close()
    def test_shared_or_unapproved_capacity_is_never_a_fallback(self):
        fields=self.fixture();fields['inventory']['nodes']['n1']['storages']['thin']['free_gb']=10
        with tempfile.TemporaryDirectory() as tmp:
            ledger=ReservationLedger(pathlib.Path(tmp)/'ledger')
            try:
                with self.assertRaisesRegex(PlacementError,'No approved local probe datastore'):
                    reserve_probe_storage(ledger,datastore_id='local',auto_storage=True,**fields)
                self.assertEqual(ledger.active_reservations(),[])
            finally:ledger.close()
    def test_compute_quota_and_linked_copy_constraints_remain_hard(self):
        for change in ['memory','quota','linked','container']:
            fields=self.fixture()
            if change=='memory':fields['capacity']['memory_mb']=64000
            elif change=='quota':fields['policy']['project_quotas']={'3':{'disk_gb':32}}
            elif change=='linked':fields['clone_mode']='linked'
            else:fields['kind']='container'
            with tempfile.TemporaryDirectory() as tmp:
                ledger=ReservationLedger(pathlib.Path(tmp)/'ledger')
                try:
                    with self.subTest(change=change),self.assertRaises(PlacementError):
                        reserve_probe_storage(ledger,datastore_id='local',auto_storage=True,**fields)
                    self.assertEqual(ledger.active_reservations(),[])
                finally:ledger.close()
