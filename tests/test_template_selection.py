import argparse,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from crange_template_selection import creation_templates,is_release_template
import crange
import crange_templates

class BaseTemplateSelectionTests(unittest.TestCase):
    def test_release_alias_and_renamed_release_reference_excluded(self):
        approved={'parrot':{'image_id':'parrot-v1'},'p100-image-deadbeef1234':{},
                  'friendly-dc':{'image_id':'p003-image-deadbeef1234'},'frozen':{'release_id':'p100-1.0.0'},
                  'debian':{'template_id':3000}}
        self.assertEqual(set(creation_templates(approved)),{'parrot','debian'})
        self.assertEqual(len(approved),5,'original release references must remain intact')
    def test_releases_remain_available_to_existing_release_operations(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);refs={'parrot':{'image_id':'parrot-v1'},'p100-image-abcdef123456':{'template_id':151}}
            for filename,reader in [('approved-scenario-templates.json',crange.templates_for),('approved-container-templates.json',crange.container_templates_for)]:
                (p/filename).write_text(json.dumps(refs))
                self.assertEqual(reader(p),refs)
                self.assertEqual(set(reader(p,creation=True)),{'parrot'})
    def test_direct_vm_and_container_creation_reject_release_before_writes(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);refs={'friendly-base':{'image_id':'p100-image-abcdef123456'}}
            for filename in ['approved-scenario-templates.json','approved-container-templates.json']:(p/filename).write_text(json.dumps(refs))
            args=argparse.Namespace(project='100',template='friendly-base',team=0,name='web-01')
            with patch.object(crange,'project_dir',return_value=p),patch.object(crange,'json_config',return_value={'team_ids':[]}),patch.object(crange,'save_config') as save:
                for command in [crange.cmd_scenario_add,crange.cmd_container_add]:
                    with self.assertRaises(crange.CrangeError):command(args)
                save.assert_not_called()
    def test_approval_cannot_reintroduce_capture_as_base(self):
        with patch('crange_scheduling.read_settings') as read:
            with self.assertRaisesRegex(ValueError,'through Releases'):
                crange_templates.approve_catalog_image('p100-image-abcdef123456',[100])
        read.assert_not_called()

    def test_edit_cannot_retarget_a_base_vm_or_container_to_release_image(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);refs={'parrot':{},'release-alias':{'image_id':'p100-image-abcdef123456'}}
            for filename in ['approved-scenario-templates.json','approved-container-templates.json']:(p/filename).write_text(json.dumps(refs))
            args=argparse.Namespace(project='100',name='t254-web',template='release-alias')
            cfg={'scenario_boxes':{'t254-web':{'template':'parrot'}},'scenario_containers':{'t254-web':{'template':'parrot'}}}
            with patch.object(crange,'project_dir',return_value=p),patch.object(crange,'json_config',return_value=cfg),patch.object(crange,'save_config') as save:
                for command in [crange.cmd_scenario_edit,crange.cmd_container_edit]:
                    with self.assertRaises(crange.CrangeError):command(args)
                save.assert_not_called()

    def test_alias_deduplication_preserves_distinct_hardware_profiles(self):
        from crange_template_selection import template_choices
        refs={'approved-base':{'image_id':'parrot-v1','profile':{'bios':'seabios'}},
              'parrot-v1':{'image_id':'parrot-v1','profile':{'bios':'seabios'}},
              'parrot-uefi':{'image_id':'parrot-v1','profile':{'bios':'ovmf'}},
              'approved-ct-base':{'template_id':3000,'template_node':'proxmox4'},
              'debian-13':{'template_id':3000,'template_node':'proxmox4'}}
        self.assertEqual(set(template_choices(refs)),{'approved-base','parrot-uefi','approved-ct-base'})
