import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_guest_outputs import complete_metadata


class AppliedOutputMetadataTests(unittest.TestCase):
    def fixture(self):
        raw={'scenario_containers':{'value':{'db':{'id':127,'address':'10.100.254.30',
                                                  'placement':{'image_id':'','node_name':None}}}}}
        cfg={'scenario_containers':{'db':{'template':'debian-stock'}}}
        saved={'container:db':{'guest_id':127,'declared_template':'debian-stock',
            'image_id':'debian-image','node_name':'proxmox1','template_file_id':'library:vztmpl/debian.tar.zst'}}
        return raw,cfg,saved

    def test_unchanged_archive_guest_recovers_missing_identity_without_changing_input(self):
        fixture=self.fixture();before=copy.deepcopy(fixture)
        live=complete_metadata(*fixture)['scenario_containers']['value']['db']
        self.assertEqual(live['placement']['image_id'],'debian-image')
        self.assertEqual(live['placement']['node_name'],'proxmox1')
        self.assertIn('template_file_id',live['placement'])
        self.assertEqual(fixture,before)

    def test_explicit_conflicts_stay_visible(self):
        fixture=self.fixture();fixture[0]['scenario_containers']['value']['db']['placement']={'image_id':'foreign','node_name':'proxmox3'}
        live=complete_metadata(*fixture)['scenario_containers']['value']['db']
        self.assertEqual(live['placement']['image_id'],'foreign')
        self.assertEqual(live['placement']['node_name'],'proxmox3')

    def test_null_reallocated_or_changed_template_never_adopts_old_generation(self):
        for change in (lambda f:f[0]['scenario_containers']['value']['db'].update(id=None),
                       lambda f:f[0]['scenario_containers']['value']['db'].update(id=128),
                       lambda f:f[1]['scenario_containers']['db'].update(template='other'),
                       lambda f:f[1]['scenario_containers']['db'].update(image='other'),
                       lambda f:f[2]['container:db'].update(guest_id=True)):
            fixture=self.fixture();change(fixture)
            self.assertEqual(complete_metadata(*fixture),fixture[0])

    def test_vm_metadata_is_bound_the_same_way_and_non_guest_outputs_untouched(self):
        raw,cfg,saved=self.fixture()
        raw={'scenario_boxes':raw['scenario_containers'],'project_id':{'value':100}}
        cfg={'scenario_boxes':cfg['scenario_containers']};saved={'vm:db':saved['container:db']}
        completed=complete_metadata(raw,cfg,saved)
        self.assertEqual(completed['scenario_boxes']['value']['db']['placement']['image_id'],'debian-image')
        self.assertEqual(completed['project_id'],{'value':100})


if __name__=='__main__':unittest.main()
