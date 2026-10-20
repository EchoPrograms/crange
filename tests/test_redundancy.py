"""Admission under node loss; does not claim runtime router recovery."""
import copy
import itertools
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_placement import PlacementError, ReservationLedger, schedule_batch, workload_node_allowed
from crange_scheduling import freeze
from crange_redundancy import RedundancyError, prepare_quorum_config, quorum_failure_matrix
from test_placement import fixtures


class WorkloadRoleTests(unittest.TestCase):
    def test_router_only_node_rejects_vms_and_nonrouter_containers_even_when_pinned(self):
        for kind, key in [('vm', 'vm:web'), ('vm', 'router:fake'),
                          ('container', 'container:web'), ('vm', 'jumpbox:one')]:
            with self.subTest(kind=kind, key=key):
                req, inv, cat, pol = fixtures()
                pol['router_only_nodes'] = ['n1']
                req.update(kind=kind, resource_key=key, placement={'mode': 'pin', 'pinned_node': 'n1'})
                cat['images']['base-v1']['kind'] = kind
                for node in inv['nodes'].values():
                    node['guest_types'] = node['storages']['local']['guest_types'] = [kind]
                with self.assertRaisesRegex(PlacementError, 'reserved for router'):
                    schedule_batch([req], inv, cat, pol, now=100)

    def test_router_container_remains_eligible_and_node_two_accepts_regular_work(self):
        req, inv, cat, pol = fixtures()
        pol['router_only_nodes'] = ['n1']
        result = schedule_batch([req], inv, cat, pol, now=100)
        self.assertEqual(result[0]['resolved']['node_name'], 'n2')
        req.update(kind='container', resource_key='router:project',
                   placement={'mode': 'pin', 'pinned_node': 'n1'})
        cat['images']['base-v1']['kind'] = 'container'
        for node in inv['nodes'].values():
            node['guest_types'] = node['storages']['local']['guest_types'] = ['container']
        result = schedule_batch([req], inv, cat, pol, now=100)
        self.assertEqual(result[0]['resolved']['node_name'], 'n1')

    def test_invalid_role_policy_does_not_silently_disable_the_restriction(self):
        for invalid in ('n1', [None], ['n1', 'n1'], ['unknown']):
            with self.subTest(invalid=invalid), self.assertRaises(PlacementError):
                workload_node_allowed({'allowed_nodes': ['n1'], 'router_only_nodes': invalid}, 'n1', 'vm:a', 'vm')

    def test_installer_cannot_bypass_role_policy(self):
        _, inv, _, pol = fixtures()
        pol['router_only_nodes'] = ['n1']
        with tempfile.TemporaryDirectory() as directory:
            ledger = ReservationLedger(Path(directory) / 'reservations.sqlite3')
            try:
                with self.assertRaisesRegex(PlacementError, 'reserved for router'):
                    ledger.reserve_external_capacity(project_id=0, job_id='installer', owner='admin',
                        operation='template-create', node_name='n1', datastore_id='local',
                        demand={'memory_mb':512, 'cpu_cores':1, 'disk_gb':8}, inventory=inv,
                        policy=pol, build_id='a'*32, vmid=1234, now=100)
                self.assertEqual(ledger.active_reservations(), [])
            finally:
                ledger.close()

    def test_existing_placement_is_retained_after_restricting_its_node(self):
        row = {'resource_key':'vm:existing', 'image':'base-v1'}
        previous = {'node_name':'n1', 'datastore_id':'local', 'template_id':300}
        self.assertEqual(freeze(row, {'node_name':'n1'}, previous)['resolved'], previous)


class FailureAdmissionTests(unittest.TestCase):
    def test_new_router_and_scenario_can_place_for_all_26_allowed_failure_sets(self):
        request, base, catalog, policy = fixtures()
        names = [f'proxmox{i}' for i in range(1, 7)]
        core = 'proxmox4'
        node = base['nodes']['n2']
        node['guest_types'] = node['storages']['local']['guest_types'] = ['vm', 'container']
        base['nodes'] = {name:copy.deepcopy(node) for name in names}
        # Match current enrollment: core hosts management, four workers accept
        # scenarios, and the unreliable node accepts only router containers.
        policy['allowed_nodes'] = [name for name in names if name != core]
        policy['router_only_nodes'] = ['proxmox1']
        image = catalog['images']['base-v1']
        prototype = image['replicas'][0]
        image['replicas'] = [{**prototype, 'node':name, 'template_id':300+i}
                             for i, name in enumerate(names)]
        catalog['images']['router-v1'] = {**copy.deepcopy(image), 'kind':'container'}
        for replica in catalog['images']['router-v1']['replicas']:
            replica['template_id'] += 1000
        request.update(resource_key='vm:staging', memory_mb=512, cpu_cores=1, disk_gb=4)
        router = {**request, 'resource_key':'router:project', 'kind':'container', 'image':'router-v1'}
        checked = 0
        for count in range(4):
            for failed in itertools.combinations([n for n in names if n != core], count):
                with self.subTest(failed=failed):
                    inventory = copy.deepcopy(base)
                    for name in failed:
                        inventory['nodes'][name]['online'] = False
                    allocations = schedule_batch([request, router], inventory, catalog, policy, now=100)
                    self.assertEqual(len(allocations), 2)
                    for allocation in allocations:
                        target = allocation['resolved']['node_name']
                        self.assertNotIn(target, failed)
                        if allocation['resource_key'] == 'vm:staging':
                            self.assertNotEqual(target, 'proxmox1')
                    checked += 1
        self.assertEqual(checked, 26)


class QuorumModelTests(unittest.TestCase):
    def config(self):
        nodes = '\n'.join('  node {\n    name: proxmox%d\n    nodeid: %d\n    quorum_votes: 1\n    ring0_addr: 10.0.0.%d\n  }' % (i, i, 100+i)
                          for i in range(1, 7))
        return 'nodelist {\n' + nodes + '\n}\nquorum {\n provider: corosync_votequorum\n}\ntotem {\n config_version: 22\n}\n'

    def test_current_equal_votes_fail_three_node_loss_but_core_weighting_handles_all_26_cases(self):
        votes = {f'proxmox{i}':1 for i in range(1, 7)}
        self.assertFalse(quorum_failure_matrix(votes, 'proxmox4')['all_cases_quorate'])
        result, audit = prepare_quorum_config(self.config())
        self.assertTrue(audit['all_cases_quorate'])
        self.assertEqual(len(audit['cases']), 26)
        self.assertEqual((audit['expected_votes'], audit['quorum']), (7, 4))
        self.assertEqual(audit['votes']['proxmox4'], 2)
        self.assertEqual(audit['config_version'], 23)
        # The two intended scalar edits are the complete configuration change.
        restored = result.replace('config_version: 23', 'config_version: 22').replace('quorum_votes: 2', 'quorum_votes: 1')
        self.assertEqual(restored, self.config())
        again, second = prepare_quorum_config(result)
        self.assertEqual(again, result)
        self.assertFalse(second['changed'])

    def test_no_disjoint_partition_can_also_hold_a_majority(self):
        _, audit = prepare_quorum_config(self.config())
        names = sorted(audit['votes'])
        for count in range(7):
            for component in itertools.combinations(names, count):
                votes = sum(audit['votes'][name] for name in component)
                self.assertFalse(votes >= audit['quorum'] and audit['expected_votes']-votes >= audit['quorum'])

    def test_alternate_or_ambiguous_voting_policy_is_never_overwritten(self):
        for text in [self.config().replace('provider:', 'expected_votes: 6\n provider:'),
                     self.config().replace('provider:', 'auto_tie_breaker: 1\n provider:'),
                     self.config().replace('quorum {', 'quorum {\n device { model: net }'),
                     self.config().replace('name: proxmox1', 'name: proxmox2'),
                     self.config().replace('quorum_votes: 1', 'quorum_votes: 2', 1)]:
            with self.subTest(text=text), self.assertRaises(RedundancyError):
                prepare_quorum_config(text)
