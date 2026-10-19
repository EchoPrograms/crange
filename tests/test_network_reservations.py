import copy
import pathlib
import tempfile
import unittest
import test_placement as placement_test
from crange_placement import PlacementError, ReservationLedger
from crange_network_acceptance import scope


class NetworkReservationTests(unittest.TestCase):
    def setUp(self):
        _,inventory,_,policy=placement_test.fixtures()
        node=inventory['nodes']['n1']
        node['architectures']=['amd64']
        node['storages']['local'].update(shared=False, snapshots_verified=True,
                                         clone_modes=['full','linked'])
        inventory.update(nodes={'proxmox2':node},guests={},declared_addresses=[])
        policy.update(allowed_nodes=['proxmox2'])
        self.inventory=inventory
        self.policy=policy
        self.target=scope(3,'proxmox2','p003t254','10.3.254.98/24',30000,'network-'+'a'*32)
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.ledger=ReservationLedger(pathlib.Path(temporary.name)/'ledger')
        self.addCleanup(self.ledger.close)

    def reserve(self,target=None,**kwargs):
        return self.ledger.reserve_network_probe(target=target or self.target,owner='controller-network',
            source_ref='source:proxmox2:qemu:150',source_sha256='b'*64,datastore_id='local',
            capacity=kwargs.get('capacity',{'memory_mb':512,'cpu_cores':1,'disk_gb':22}),
            inventory=self.inventory,policy=self.policy,now=100)

    def test_scope_capacity_and_identity_are_in_shared_allocation(self):
        allocation=self.reserve()
        self.assertEqual(allocation['operation'],'network_probe')
        self.assertEqual(allocation['network_scope'],self.target)
        self.assertEqual(allocation['resolved']['clone_mode'], 'linked')
        self.assertEqual(len(self.ledger.active_reservations()),1)
        self.assertEqual(self.ledger.active_reservations()[0]['memory_mb'],512)

    def test_other_jobs_cannot_reserve_same_vmid_or_lan_address(self):
        self.reserve()
        for changes in ({'address':'10.3.254.99/24'}, {'vmid':30001}):
            target=dict(self.target,job_id='network-'+'c'*32,**changes)
            with self.subTest(changes=changes),self.assertRaises(PlacementError):self.reserve(target)

    def test_existing_vmid_or_declared_address_or_missing_inventory_blocks(self):
        for mutation in ('vmid','address','missing'):
            inventory=copy.deepcopy(self.inventory)
            if mutation=='vmid':self.inventory['guests']={'30000':{'kind':'qemu'}}
            if mutation=='address':self.inventory['declared_addresses']=['10.3.254.98']
            if mutation=='missing':del self.inventory['declared_addresses']
            with self.subTest(mutation=mutation),self.assertRaises(PlacementError):self.reserve()
            self.inventory=inventory

    def test_no_network_trust_required_but_capacity_and_node_exclusion_still_apply(self):
        self.inventory['nodes']['proxmox2']['verified_networks']=[]
        with self.assertRaises(PlacementError):self.reserve(capacity={'memory_mb':999999,'cpu_cores':1,'disk_gb':22})
        self.policy['allowed_nodes']=[]
        with self.assertRaises(PlacementError):self.reserve()
        self.policy['allowed_nodes']=['proxmox2']
        self.assertEqual(self.reserve()['operation'],'network_probe')

    def test_release_requires_confirmed_terminal_cleanup(self):
        self.reserve()
        self.ledger.mark_running(self.target['job_id'],now=101)
        with self.assertRaises(PlacementError):
            self.ledger.reconcile_job(self.target['job_id'],operation_terminal=False,allocations_reconciled=True)
        self.assertEqual(len(self.ledger.active_reservations()),1)
        self.ledger.reconcile_job(self.target['job_id'],operation_terminal=True,allocations_reconciled=True)
        self.assertEqual(self.ledger.active_reservations(),[])

    def test_linked_budget_keeps_physical_floor_and_requires_local_snapshot_proof(self):
        storage = self.inventory['nodes']['proxmox2']['storages']['local']
        storage.update(total_gb=100, free_gb=80, committed_gb=95)
        self.policy['local_linked_disk_overcommit_ratio'] = 2
        self.assertEqual(self.reserve()['resolved']['clone_mode'], 'linked')
        self.ledger.reconcile_job(self.target['job_id'], operation_terminal=True, allocations_reconciled=True)
        for field, value in [('free_gb', 25), ('shared', True), ('snapshots_verified', False)]:
            original = storage[field]
            storage[field] = value
            target = dict(self.target, job_id='network-'+'d'*32)
            with self.subTest(field=field), self.assertRaises(PlacementError): self.reserve(target)
            storage[field] = original

    def test_unscoped_vm_publication_cannot_claim_network_linked_budget(self):
        with self.assertRaisesRegex(PlacementError, 'exact owned network scope'):
            self.ledger.reserve_source_probe(project_id=3, job_id='capture', owner='release-capture',
                source_ref='source:proxmox2:qemu:150', source_sha256='b'*64,
                node_name='proxmox2', datastore_id='local', kind='vm', clone_mode='linked',
                capacity={'memory_mb':512,'cpu_cores':1,'disk_gb':22},
                inventory=self.inventory, policy=self.policy, now=100)
