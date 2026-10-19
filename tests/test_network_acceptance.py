import copy
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import crange_network_acceptance as acceptance


class NetworkAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.scope = acceptance.scope(240, 'proxmox2', 'p240t254', '10.240.254.98/24', 9911, 'network-' + 'a'*32)
        self.value = {'node_name':'proxmox2', 'vm_id':9911, 'pool_id':'crange-probes',
                      'tags':['crange-network-probe', self.scope['job_id']],
                      'network_device':[{'bridge':'p240t254'}],
                      'initialization':[{'ip_config':[{'ipv4':[{'address':'10.240.254.98/24','gateway':'10.240.254.1'}]}]}]}
        self.create = self.plan('create')
        self.delete = self.plan('delete')
        self.report = {'schema_version':1, 'scope':self.scope, 'zone':'crange', 'mtu':1450,
                       'source_revision':'b'*40, 'tested_at':'2026-10-06T12:00:00Z',
                       'checks':{name:{'passed':True,'duration_seconds':10,'artifact_sha256':hashlib.sha256(b'bounded evidence').hexdigest()}
                                 for name in acceptance.CHECKS},
                       'cleanup':dict.fromkeys(('vmid_absent_before','vmid_absent_after','probe_state_empty',
                                                'temporary_rules_absent','input_unchanged'), True)}
        self.report['cleanup']['artifact_sha256'] = hashlib.sha256(b'bounded evidence').hexdigest()

    def test_schema_two_requires_exact_controller_and_actual_relay_binding(self):
        report=copy.deepcopy(self.report)
        report['schema_version']=2
        report['source_binding']={'schema_version':1,'controller_sha256':{
            name:'c'*64 for name in acceptance.CONTROLLER_SOURCE_FILES},'relay_sha256':'d'*64,'git_dirty':True}
        report['checks']['guacamole'].update(source_address='10.60.0.10',
            target_address=self.scope['address'].split('/')[0],target_port=22,relay_sha256='d'*64)
        acceptance.validate_receipt(report,self.create,self.delete)
        changed=copy.deepcopy(report);changed['checks']['guacamole']['relay_sha256']='e'*64
        with self.assertRaises(ValueError):acceptance.validate_receipt(changed,self.create,self.delete)
        changed=copy.deepcopy(report);del changed['source_binding']['controller_sha256']['crange_network_worker.py']
        with self.assertRaises(ValueError):acceptance.validate_receipt(changed,self.create,self.delete)
        changed=copy.deepcopy(report);changed['schema_version']=1
        with self.assertRaisesRegex(ValueError,'Legacy'):
            acceptance.validate_receipt(changed,self.create,self.delete)

    def plan(self, action):
        return {'resource_changes':[{'address':acceptance.PROBE_ADDRESS,'mode':'managed',
                'type':'proxmox_virtual_environment_vm','change':{'actions':[action],
                'after' if action == 'create' else 'before':copy.deepcopy(self.value)}}]}

    def test_complete_project240_tuple_passes_without_policy_write(self):
        self.assertEqual(acceptance.validate_receipt(self.report,self.create,self.delete),self.scope)

    def test_create_requires_probe_pool_but_owned_legacy_delete_remains_possible(self):
        for pool in (None,'crange-iac','other-pool'):
            with self.subTest(pool=pool):
                self.value['pool_id']=pool
                with self.assertRaises(ValueError):
                    acceptance.probe_from_plan(self.plan('create'),'create',self.scope)
                self.assertEqual(acceptance.probe_from_plan(self.plan('delete'),'delete',self.scope)['pool_id'],pool)

    def test_cross_project_or_node_or_address_proof_rejected(self):
        for key, value in [('node_name','proxmox3'),('vm_id',9912)]:
            with self.subTest(key=key):
                plan=copy.deepcopy(self.create)
                plan['resource_changes'][0]['change']['after'][key]=value
                with self.assertRaises(ValueError): acceptance.validate_receipt(self.report,plan,self.delete)
        plan=copy.deepcopy(self.create)
        plan['resource_changes'][0]['change']['after']['network_device'][0]['bridge']='p003t254'
        with self.assertRaises(ValueError): acceptance.validate_receipt(self.report,plan,self.delete)
        plan=copy.deepcopy(self.create)
        plan['resource_changes'][0]['change']['after']['initialization'][0]['ip_config'][0]['ipv4'][0]['address']='10.240.254.99/24'
        with self.assertRaises(ValueError): acceptance.validate_receipt(self.report,plan,self.delete)

    def test_unowned_probe_or_other_resource_mutation_rejected(self):
        for mutation in ('ownership','extra','replacement'):
            plan=copy.deepcopy(self.create)
            if mutation == 'ownership': plan['resource_changes'][0]['change']['after']['tags']=[]
            if mutation == 'extra': plan['resource_changes'].append(copy.deepcopy(plan['resource_changes'][0]))
            if mutation == 'replacement': plan['resource_changes'][0]['change']['actions']=['delete','create']
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                acceptance.validate_receipt(self.report,plan,self.delete)

    def test_missing_failed_unbounded_checks_and_incomplete_cleanup_rejected(self):
        for mutation in ('missing','failed','unbounded','nan','cleanup'):
            report=copy.deepcopy(self.report)
            if mutation=='missing': del report['checks']['guacamole']
            if mutation=='failed': report['checks']['team_isolation']['passed']=False
            if mutation=='unbounded': report['checks']['dns']['duration_seconds']=61
            if mutation=='nan': report['checks']['dns']['duration_seconds']=float('nan')
            if mutation=='cleanup': report['cleanup']['input_unchanged']=False
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                acceptance.validate_receipt(report,self.create,self.delete)

    def test_artifacts_must_match_and_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            for name, value in [('receipt.json',self.report),('create-plan.json',self.create),('delete-plan.json',self.delete)]:
                (root/name).write_text(json.dumps(value))
            for name in [*acceptance.CHECKS,'cleanup']: (root/(name+'.log')).write_bytes(b'bounded evidence')
            self.assertEqual(acceptance.validate_bundle(root),self.scope)
            (root/'dns.log').write_bytes(b'edited')
            with self.assertRaises(ValueError): acceptance.validate_bundle(root)
            (root/'dns.log').unlink()
            (root/'dns.log').symlink_to(root/'controller.log')
            with self.assertRaises(ValueError): acceptance.validate_bundle(root)

    def test_shared_transit_and_reserved_projects_cannot_be_lan_proofs(self):
        for project,vnet,address in [(3,'p003tr','10.3.0.98/24'),(60,'p060t254','10.60.254.98/24'),(3,'p003t000','10.3.0.98/24')]:
            with self.assertRaises(ValueError): acceptance.scope(project,'proxmox2',vnet,address,9911,'network-'+'a'*32)
