import copy
from pathlib import Path
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from crange_windows_snapshot import validate,verify_clone,WindowsSnapshotError
class WindowsSnapshotTests(unittest.TestCase):
    def fixture(self):
        x={'identity_mode':'windows-scenario-snapshot-v1','domain_role':5,'part_of_domain':True,
           'machine_guid':'11111111-1111-4111-8111-111111111111','vm_generation_id':'22222222-2222-4222-8222-222222222222',
           'computer_name':'MHL-DC01','os_caption':'Microsoft Windows Server 2025','domain_name':'mhl.test',
           'directory_invocation_id':'33333333-3333-4333-8333-333333333333','generation_counter_present':True,
           'image_state':'IMAGE_STATE_COMPLETE','rdp_allowed_sources':['10.60.0.10']}
        x.update({k:0 for k in ('oobe_in_progress','system_setup_in_progress','setup_type')})
        x.update({k:True for k in ('qemu_guest_agent','qemu_guest_agent_automatic','term_service_running','term_service_automatic','winrm_service_running','winrm_service_automatic')});return x
    def test_preserves_business_identity_but_requires_new_instance_and_dc_invocation(self):
        source=self.fixture();clone=copy.deepcopy(source)
        with self.assertRaises(WindowsSnapshotError):verify_clone(source,clone)
        clone['vm_generation_id']='44444444-4444-4444-8444-444444444444'
        with self.assertRaises(WindowsSnapshotError):verify_clone(source,clone)
        clone['directory_invocation_id']='55555555-5555-4555-8555-555555555555'
        self.assertTrue(verify_clone(source,clone)['service_identity_preserved'])
        clone['domain_name']='another.test'
        with self.assertRaises(WindowsSnapshotError):verify_clone(source,clone)
    def test_rejects_unready_unsupported_or_unrestricted_sources(self):
        for changes in [{'identity_mode':None},{'generation_counter_present':False},{'setup_type':1},
                        {'rdp_allowed_sources':['Any']},{'domain_role':True},{'part_of_domain':False}]:
            with self.assertRaises(WindowsSnapshotError):validate({**self.fixture(),**changes})
    def test_existing_base_contract_still_rejects_domain_snapshots(self):
        from crange_catalog import validate_source_identity,CatalogError
        with self.assertRaises(CatalogError):validate_source_identity({**self.fixture(),'os_family':'windows'})
