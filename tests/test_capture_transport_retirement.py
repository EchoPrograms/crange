import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange_capture as capture
from crange_distribution import DistributionError

class TransportRetirementTests(unittest.TestCase):
    def fixture(self):
        volume='crange-library:backup/vzdump-qemu-128-2026_10_13-10_30_00.vma.zst'
        receipt={'state':'verified','source_id':128,'source_node':'proxmox1','template_id':132,
                 'transport':{'state':'verified','source_node':'proxmox1','source_id':128,
                              'volume_id':volume,'artifact_sha256':'b'*64}}
        export={'verified':True,'complete_capture':True,'source_id':132,'node':'proxmox5',
                'volume_id':volume.replace('-128-','-132-'),'artifact_sha256':'a'*64}
        present=[{'volid':volume,'content':'backup','vmid':128}]
        return receipt,export,present

    def run_retire(self, receipt,export,rows,*,images=None,hashes=None):
        api=Mock();api.request.side_effect=rows
        with patch.object(capture,'read_settings',return_value=({}, {'images':images or {}})), \
             patch.object(capture,'SSHVolumeHasher',return_value=Mock(side_effect=hashes or ['a'*64,'b'*64])):
            capture._retire_capture_transport(api,receipt,export,save=Mock())
        return api

    def test_only_transfer_archive_is_deleted_after_matching_replacement_checksums(self):
        receipt,export,present=self.fixture()
        api=self.run_retire(receipt,export,[present,None,[]])
        deletes=[call.args for call in api.request.call_args_list if call.args[0]=='DELETE']
        self.assertEqual(len(deletes),1)
        self.assertIn('vzdump-qemu-128-',deletes[0][1])
        self.assertNotIn('vzdump-qemu-132-',deletes[0][1])
        self.assertEqual(receipt['transport']['retirement'],'retired')

    def test_unverified_or_same_source_replacement_never_deletes(self):
        for mutation in ('state','same_source','foreign_volume','export_unverified'):
            receipt,export,present=self.fixture()
            if mutation=='state':receipt['state']='captured'
            if mutation=='same_source':receipt['template_id']=128
            if mutation=='foreign_volume':receipt['transport']['volume_id']=present[0]['volid'].replace('-128-','-129-')
            if mutation=='export_unverified':export['verified']=False
            with self.assertRaises(DistributionError):self.run_retire(receipt,export,[])

    def test_published_reference_and_changed_checksum_are_retained(self):
        receipt,export,present=self.fixture()
        images={'published':{'artifact':{'volume_id':present[0]['volid']}}}
        with self.assertRaisesRegex(DistributionError,'published'):
            self.run_retire(receipt,export,[],images=images)
        with self.assertRaisesRegex(DistributionError,'checksum changed'):
            self.run_retire(receipt,export,[present],hashes=['c'*64])
        self.assertNotIn('retirement',receipt['transport'])

    def test_uncertain_deletion_is_not_resubmitted_and_absence_finishes_receipt(self):
        receipt,export,present=self.fixture();receipt['transport']['retirement']='retiring'
        with self.assertRaisesRegex(DistributionError,'uncertain'):
            self.run_retire(receipt,export,[present])
        api=self.run_retire(receipt,export,[[]])
        self.assertEqual(receipt['transport']['retirement'],'retired')
        self.assertEqual(api.request.call_count,1)

    def test_completed_probe_requires_identity_cleanup_and_released_reservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'source-journal').mkdir()
            receipt={'node':'proxmox5','template_id':132,'probe_job':'probe'}
            identity={'source_config_sha256':'hash','job_id':'identity'}
            probe={'state':'source-probed','source_node':'proxmox5','source_id':132,'kind':'vm',
                   'source_config_sha256':'hash','source_capture_job_id':'identity','probe_id':20000,
                   'verification':{'identity_unique':True,'passed':True},'reservation':{'id':'r'},
                   'provider_tasks':[{'state':'confirmed-terminal','path':'/nodes/proxmox5/qemu/20000'}]}
            file=root/'source-journal/probe.json';file.write_text(json.dumps(probe))
            ledger=Mock();ledger.get_job.return_value={'state':'released','project_id':100,'allocations':[probe['reservation']]}
            with patch.object(capture,'DATA',root),patch.object(capture,'ReservationLedger',return_value=ledger):
                capture._verified_capture_probe(receipt,identity,100)
                ledger.get_job.return_value['state']='running'
                with self.assertRaisesRegex(DistributionError,'not reconciled'):
                    capture._verified_capture_probe(receipt,identity,100)
                probe['provider_tasks'][0]['state']='running';file.write_text(json.dumps(probe))
                with self.assertRaisesRegex(DistributionError,'cleanup evidence'):
                    capture._verified_capture_probe(receipt,identity,100)

if __name__=='__main__':unittest.main()
