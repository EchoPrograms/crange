import copy
import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from crange_resource_identity import automation_config, automation_response, category_updates, needs_previous, PREFIX


class ResourceIdentityTests(unittest.TestCase):
    def test_each_resource_marker_becomes_a_category_without_losing_provenance(self):
        for marker, expected in [('crange-cache-153','crange-cache'),('crange-build-'+'a'*32,'crange-template-build'),('crange-capture-abcdef123456','crange-capture')]:
            old={'tags':f'crange-image;{marker};p100','description':'Original notes\n','name':'exact-name','rootfs':'unchanged'}
            updated={**old,**category_updates({'tags':old['tags']},old)}
            self.assertEqual(set(updated['tags'].split(';')),{'crange-image',expected,'p100'})
            self.assertEqual(automation_config(updated),old)

    def test_no_description_key_and_existing_empty_description_remain_distinct(self):
        for old in ({'tags':'crange-image;crange-cache-153'},{'tags':'crange-image;crange-cache-153','description':''}):
            updated={**old,**category_updates({'tags':old['tags']},old)}
            self.assertEqual(automation_config(updated),old)

    def test_notes_cannot_grant_an_inherited_template_identity_to_a_clone(self):
        old={'tags':'crange-image;crange-cache-153','description':'original'}
        clone={**old,**category_updates(old),'tags':'p100;scenario'}
        self.assertNotIn('crange-cache-153',automation_config(clone)['tags'])

    def test_new_cache_marker_replaces_the_cloned_identity(self):
        old={'tags':'crange-image;crange-cache-153','description':'original'}
        native={**old,**category_updates(old)}
        changed=category_updates({'tags':'crange-image;crange-cache-199','description':'new job'},native)
        self.assertEqual(automation_config(changed),{'tags':'crange-image;crange-cache-199','description':'new job'})

    def test_description_edit_preserves_identity_and_is_visible_to_fingerprints(self):
        old={'tags':'crange-image;crange-cache-153','description':'original'}
        native={**old,**category_updates(old)}
        changed=category_updates({'description':'changed'},native)
        self.assertEqual(automation_config(changed),{**old,'description':'changed'})

    def test_malformed_metadata_is_not_silently_reinterpreted(self):
        with self.assertRaises(ValueError):automation_config({'description':'note'+PREFIX+'broken -->'})
        with self.assertRaises(ValueError):automation_config({'description':PREFIX+'{} -->'+PREFIX+'{} -->'})

    def test_only_guest_configuration_responses_are_adapted(self):
        native=category_updates({'tags':'crange-image;crange-cache-153'})
        self.assertIn('crange-cache-153',automation_response('/nodes/proxmox6/qemu/153/config',native)['tags'])
        self.assertEqual(automation_response('/cluster/resources',native),native)

    def test_pve_notes_final_newline_preserves_original_fingerprints(self):
        from crange_ct_read_relay import _sealed_config_identity, _network_config_digest
        old={'tags':'crange-image;crange-cache-153','description':'original\n','template':1}
        native={**old,**category_updates(old)}
        native['description']+='\n'
        self.assertEqual(automation_config(native),old)
        self.assertEqual(_sealed_config_identity(old,153),_sealed_config_identity(native,153))
        self.assertEqual(_network_config_digest(old),_network_config_digest(native))

    def test_remote_reader_bundles_metadata_support_for_returning_nodes(self):
        from crange_ct_read_relay import identity_module_preamble
        import subprocess
        source=identity_module_preamble()+'\nfrom crange_resource_identity import display_tags\nprint(display_tags("crange-image;crange-cache-153"))'
        result=subprocess.run([sys.executable,'-I','-c',source],capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.strip(),'crange-cache;crange-image')

    def test_only_partial_ownership_updates_need_an_extra_read(self):
        self.assertTrue(needs_previous({'description':'notes'}))
        self.assertTrue(needs_previous({'tags':'crange-image;crange-cache-153'}))
        self.assertFalse(needs_previous({'tags':'p100;scenario'}))
        self.assertFalse(needs_previous({'tags':'crange-image;crange-cache-153','description':'notes'}))
