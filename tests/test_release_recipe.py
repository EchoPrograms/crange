import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange
from crange_jobs import command
from crange_release_recipe import build, checksum, publish


class RecipeReleaseTests(unittest.TestCase):
    def fixture(self):
        cfg = {'project_id': 100, 'scenario_boxes': {}, 'scenario_containers': {}, 'custom_networks': {}}
        parent = {'project_id': 100, 'version': '0.1.2', 'release_id': 'p100-0.1.2', 'schema_version': 4,
                  'scenario': {'boxes': [], 'containers': [], 'networks': {}, 'templates': {},
                               'service_profiles': {}, 'boot_order_permutations': [['vm', 'ct']], 'team_overrides': {}}}
        outputs, catalog = {}, {'images': {}}
        for n, (label, field, kind, profile) in enumerate((('boxes', 'scenario_boxes', 'vm', 'windows'),
                ('containers', 'scenario_containers', 'container', 'business')), 1):
            key, base, alias = kind, 'base-' + kind, 'captured-' + kind
            box = {'team_id': '254', 'name': kind, 'ip_last_octet': 10 * n, 'cores': 2,
                   'memory_mb': 1024, 'node_name': 'auto', 'template': base}
            cfg[field][key] = box
            parent['scenario'][label].append({**box, 'key': key, 'source_template': base,
                'source_vm_id': 200 + n, 'source_image_id': base, 'template': alias, 'image': alias, 'full_clone': True})
            outputs[field] = {'value': {key: {'id': 200 + n, 'address': f'10.100.254.{10*n}',
                                             'placement': {'image_id': base}}}}
            parent['scenario']['templates'][alias] = {'image_id': alias, 'kind': kind}
            parent['scenario']['service_profiles'][profile] = {'profile': 'fixed-' + profile,
                'source_sha256': 'a' * 64, 'guests': {key: {'guest_id': 200 + n, 'image_id': base}}}
            catalog['images'][alias] = {'verified': True, 'approved_projects': [100],
                'artifact_sha256': str(n) * 64, 'retained_release_references': ['p100-0.1.2']}
        parent['integrity_sha256'] = checksum(parent)
        profiles = copy.deepcopy(parent['scenario']['service_profiles'])
        profiles['business']['source_sha256'] = 'b' * 64
        return parent, cfg, outputs, profiles, catalog

    def build(self, fixture):
        return build(*fixture, version='0.1.3', revision='c' * 40, dirty=True,
                     created_at='2026-10-18T19:00:00Z', actor='admin')

    def test_reuses_all_images_and_definitions_without_mutating_parent_or_live_teams(self):
        fixture = self.fixture()
        before = copy.deepcopy(fixture)
        fixture[1]['scenario_boxes']['workstation'] = {'team_id': '1'}
        release = self.build(fixture)
        for field in ('boxes', 'containers', 'networks', 'templates', 'boot_order_permutations'):
            self.assertEqual(release['scenario'][field], fixture[0]['scenario'][field])
        self.assertEqual(release['release_id'], 'p100-0.1.3')
        self.assertFalse(release['derivation']['staging_disks_captured'])
        self.assertEqual(fixture[0], before[0])
        self.assertEqual(fixture[4], before[4])

    def test_changed_staging_or_profile_scope_requires_capture_or_configuration(self):
        mutations = [lambda f: f[1]['scenario_boxes']['vm'].update(cores=4),
            lambda f: f[1]['scenario_boxes']['vm'].update(image='different-image'),
            lambda f: f[1]['scenario_boxes'].update(extra={'team_id': '254'}),
            lambda f: f[1]['custom_networks'].update(extra={'owner_team_id': '254', 'network_id': 100}),
            lambda f: f[2]['scenario_boxes']['value']['vm'].update(id=None),
            lambda f: f[2]['scenario_boxes']['value']['vm'].update(address='10.100.1.10'),
            lambda f: f[3]['business']['guests'].clear(),
            lambda f: f[3]['business']['guests']['container'].update(guest_id=999),
            lambda f: f[3]['business'].update(source_sha256='a' * 64)]
        for change in mutations:
            with self.subTest(change=change):
                fixture = self.fixture(); change(fixture)
                with self.assertRaises(ValueError): self.build(fixture)

    def test_tampering_unapproved_or_unretained_images_rejected(self):
        for change in (lambda f: f[0].update(version='tampered'),
                       lambda f: f[4]['images']['captured-vm'].update(verified=False),
                       lambda f: f[4]['images']['captured-vm'].update(approved_projects=[3]),
                       lambda f: f[4]['images']['captured-vm'].update(retained_release_references=[])):
            fixture = self.fixture(); change(fixture)
            with self.assertRaises(ValueError): self.build(fixture)

    def test_exclusive_publication_preserves_parent_and_recovers_catalog_write_failure(self):
        fixture = self.fixture(); release = self.build(fixture)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '0.1.3.json'
            updated = publish(path, release, fixture[4]); content = path.read_bytes()
            self.assertEqual(path.stat().st_mode & 0o777, 0o440)
            self.assertIn(release['release_id'], updated['images']['captured-vm']['retained_release_references'])
            retry = copy.deepcopy(release); retry['created_at'] = 'later'
            retry.pop('integrity_sha256'); retry['integrity_sha256'] = checksum(retry)
            # Catalog was not saved after the first exclusive manifest write.
            recovered = publish(path, retry, fixture[4])
            self.assertEqual(content, path.read_bytes())
            self.assertEqual(recovered['images']['captured-vm']['retained_release_references'].count('p100-0.1.3'), 1)
            collision = copy.deepcopy(release); collision['source_revision'] = 'd' * 40
            collision.pop('integrity_sha256'); collision['integrity_sha256'] = checksum(collision)
            with self.assertRaisesRegex(ValueError, 'already exists'): publish(path, collision, fixture[4])
            self.assertEqual(content, path.read_bytes())

    def test_catalog_revalidated_inside_publication_lock(self):
        fixture = self.fixture(); release = self.build(fixture)
        fixture[4]['images']['captured-vm']['approved_projects'] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '0.1.3.json'
            with self.assertRaises(ValueError): publish(path, release, fixture[4])
            self.assertFalse(path.exists())

    def test_api_accepts_only_fixed_project_and_version_inputs(self):
        self.assertEqual(command('release-recipe', 100, {'version':'0.1.3', 'parent_version':'0.1.2'}),
                         ['release-recipe','100','0.1.3','--parent-version','0.1.2'])
        for project, options in ((3, {'version':'1','parent_version':'0'}),
                                 (100, {'version':'../etc','parent_version':'0'}),
                                 (100, {'version':'1','parent_version':'0','script':'id'})):
            with self.assertRaises(ValueError): command('release-recipe', project, options)

    def test_command_requires_current_archived_recipe_before_publication(self):
        with patch.object(crange, 'project_dir'), patch.object(crange, 'json_config'), \
             patch.object(crange, 'load_release', return_value={'project_id':100}), \
             patch('crange_mhl_services.verify_release_profiles', side_effect=[['business'], ValueError('configure staging')]) as verify, \
             patch('crange_release_recipe.publish') as publication, patch.object(crange, 'cmd_apply') as apply:
            with self.assertRaisesRegex(crange.CrangeError, 'configure staging'):
                crange.cmd_release_recipe(SimpleNamespace(project='100', version='0.1.3', parent_version='0.1.2'))
            publication.assert_not_called(); apply.assert_not_called()
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(verify.call_args.kwargs, {'require_current':True, 'preserve_recipes':True})


if __name__ == '__main__':
    unittest.main()
