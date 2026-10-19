import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
import test_network_acceptance as evidence
import crange_network_publish as publisher
from crange_network_acceptance import CHECKS, CONTROLLER_SOURCE_FILES
from crange_scheduling import network_attestation_valid


class NetworkPublicationTests(unittest.TestCase):
    def setUp(self):
        fixture=evidence.NetworkAcceptanceTests();fixture.setUp()
        self.receipt=copy.deepcopy(fixture.report);self.receipt['schema_version']=2
        self.receipt['source_binding']={'schema_version':1,'git_dirty':True,'relay_sha256':'d'*64,
            'controller_sha256':{name:'c'*64 for name in CONTROLLER_SOURCE_FILES}}
        self.receipt['checks']['guacamole'].update(source_address='10.60.0.10',
            target_address='10.240.254.98',target_port=22,relay_sha256='d'*64)
        self.policy={'revision':'capacity-v20261009-cpu5-ram2-disk2','allowed_nodes':['proxmox2'],
            'cpu_overcommit_ratio':5,'memory_overcommit_ratio':2,'local_linked_disk_overcommit_ratio':2,
            'allowed_storages':['local-lvm'],'nodes':{'proxmox2':{'cpu_reserve_cores':2}},
            'network_attestations':{'proxmox2':{'verified':True,'zone':'crange','mtu':1450,
                'passed_checks':sorted(CHECKS),'management_verified':True,'verified_networks':['p003t254'],
                'evidence':'existing reviewed evidence'}}}
        self.fixture=fixture

    def test_producer_consumer_merge_unlocks_only_exact_tuple_preserving_existing_policy(self):
        updated,changed=publisher.merge_receipt(self.policy,self.receipt,'e'*64)
        self.assertTrue(changed)
        node=updated['network_attestations']['proxmox2']
        self.assertTrue(network_attestation_valid(node))
        self.assertEqual(node['verified_networks'],['p003t254','p240t254'])
        self.assertNotIn('p240t001',node['verified_networks'])
        self.assertNotIn('proxmox1',updated['network_attestations'])
        self.assertEqual(node['evidence'],'existing reviewed evidence')
        for key in ('cpu_overcommit_ratio','memory_overcommit_ratio','local_linked_disk_overcommit_ratio',
                    'allowed_storages','nodes','allowed_nodes'):
            self.assertEqual(updated[key],self.policy[key])
        again,changed=publisher.merge_receipt(updated,self.receipt,'e'*64)
        self.assertFalse(changed);self.assertEqual(again,updated)
        invalid=copy.deepcopy(self.policy);invalid['network_attestations']['proxmox2']['verified']=False
        with self.assertRaises(ValueError):publisher.merge_receipt(invalid,self.receipt,'e'*64)

    def test_protected_publication_replays_without_changing_policy_owner_or_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);bundle=root/self.receipt['scope']['job_id'];bundle.mkdir(mode=0o700)
            project=root/'project';project.mkdir();(project/'main.tf').write_text('reviewed desired state')
            for name in CHECKS|{'cleanup'}:(bundle/(name+'.log')).write_bytes(b'bounded evidence')
            (bundle/'create-plan.json').write_text(json.dumps(self.fixture.create))
            (bundle/'delete-plan.json').write_text(json.dumps(self.fixture.delete))
            (bundle/'receipt.json').write_text(json.dumps(self.receipt))
            review={'scope':self.receipt['scope'],'source_binding':self.receipt['source_binding'],
                    'project_input_sha256':publisher.project_input_hashes(project)}
            (bundle/'review.json').write_text(json.dumps(review))
            (bundle/'journal.json').write_text(json.dumps({'scope':self.receipt['scope'],'state':'accepted_cleaned'}))
            policy=root/'policy.json';policy.write_text(json.dumps(self.policy));policy.chmod(0o600)
            original=policy.stat();original_receipt=(bundle/'receipt.json').read_bytes()
            database=root/'jobs.db';job_id='a'*32
            with sqlite3.connect(database) as db:
                db.execute('CREATE TABLE jobs(id TEXT,operation TEXT,status TEXT,project INTEGER,options TEXT,output TEXT)')
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(job_id,'network-verify','succeeded',240,
                    json.dumps({'node':'proxmox2','network':'p240t254'}),''))
            lease=Mock();lease.get_job.return_value={'state':'released'}
            api=Mock();api.request.return_value=[]
            with patch.object(publisher.os,'geteuid',return_value=0), \
                 patch.object(publisher,'protected',side_effect=lambda path,**kwargs:Path(path)), \
                 patch.object(publisher,'controller_source_hashes',return_value=self.receipt['source_binding']['controller_sha256']), \
                 patch.object(publisher,'trusted_relay_source_hash',return_value='d'*64), \
                 patch('crange.project_dir',return_value=project), \
                 patch('crange_placement.ReservationLedger',return_value=lease):
                result=publisher.publish(job_id,240,database=database,policy_path=policy,probe_root=root,api=api)
                self.assertTrue(result['changed']);first=policy.read_bytes()
                result=publisher.publish(job_id,240,database=database,policy_path=policy,probe_root=root,api=api)
                self.assertFalse(result['changed']);self.assertEqual(policy.read_bytes(),first)
                with self.assertRaises(ValueError):publisher.publish(job_id,3,database=database,policy_path=policy,probe_root=root,api=api)
            self.assertEqual(policy.stat().st_uid,original.st_uid)
            self.assertEqual(policy.stat().st_gid,original.st_gid)
            self.assertEqual(policy.stat().st_mode&0o777,0o600)
            self.assertEqual((bundle/'receipt.json').read_bytes(),original_receipt)
            record=json.loads((bundle/'publication.json').read_text())
            self.assertEqual(record['state'],'published')
            self.assertEqual(record['policy_after_sha256'],hashlib.sha256(first).hexdigest())
            self.assertNotEqual(record['policy_before_sha256'],record['policy_after_sha256'])
