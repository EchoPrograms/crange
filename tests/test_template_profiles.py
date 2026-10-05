import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from crange import template_identity

class TemplateProfilesTests(unittest.TestCase):
    def test_display_label_is_not_image_identity(self):
        ref={'template_id':3010,'template_node':'proxmox4'}
        self.assertEqual(template_identity(ref),template_identity({**ref,'display_name':'UbuntuServer24.04-VM'}))

    def test_profile_is_part_of_pinned_image_identity(self):
        ref={'template_id':3010,'template_node':'proxmox4','profile':{'bios':'seabios','machine':'q35','agent_enabled':True}}
        self.assertEqual(template_identity(ref)['profile']['machine'],'q35')
        self.assertNotEqual(template_identity(ref),template_identity({**ref,'profile':{**ref['profile'],'agent_enabled':False}}))
        self.assertNotEqual(template_identity(ref),template_identity({'template_id':3010,'template_node':'proxmox4'}))

if __name__=='__main__':unittest.main()
