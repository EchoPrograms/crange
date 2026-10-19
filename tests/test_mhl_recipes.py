"""Release recipes remain reproducible after edits and reject unsafe archives."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_mhl_recipes as recipes


class RecipeArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in recipes.WINDOWS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('---\n# original fixed recipe\n')
        self.sha = recipes.digest('windows', self.root)

    def test_replay_original_after_current_recipe_changes(self):
        recipes.preserve('windows', self.sha, self.root)
        path = self.root / sorted(recipes.WINDOWS)[0]
        path.write_text('---\n# changed current recipe\n')
        recipes.verify('windows', self.sha, self.root)
        with recipes.source_directory('windows', self.sha, self.root) as archived:
            self.assertIn('original', (archived / sorted(recipes.WINDOWS)[0]).read_text())
            self.assertNotEqual(archived, self.root)
        self.assertFalse(archived.exists())
        self.assertIn('changed', path.read_text())

    def test_cannot_invent_archive_for_changed_recipe(self):
        (self.root / sorted(recipes.WINDOWS)[0]).write_text('changed')
        with self.assertRaises(recipes.RecipeError):
            recipes.preserve('windows', self.sha, self.root)
        self.assertFalse(recipes.archive_path(self.sha, self.root).exists())

    def test_tampering_permissions_and_non_object_rejected(self):
        recipes.preserve('windows', self.sha, self.root)
        path = recipes.archive_path(self.sha, self.root)
        original = path.read_text()
        for content in ('[]', original.replace('original fixed', 'tampered fixed')):
            path.write_text(content)
            with self.assertRaises(recipes.RecipeError):
                recipes.load('windows', self.sha, self.root)
        path.write_text(original)
        path.chmod(0o666)
        with self.assertRaises(recipes.RecipeError):
            recipes.load('windows', self.sha, self.root)

    def test_paths_and_source_symlinks_are_rejected(self):
        for name in ('../../etc/passwd', '/etc/passwd', 'ansible/windows/evil.yml'):
            self.assertFalse(recipes.allowed('windows', name))
        path = self.root / sorted(recipes.WINDOWS)[0]
        path.unlink()
        path.symlink_to(self.root / sorted(recipes.WINDOWS)[1])
        with self.assertRaises(recipes.RecipeError):
            recipes.preserve('windows', self.sha, self.root)

    def test_existing_valid_archive_is_not_overwritten(self):
        recipes.preserve('windows', self.sha, self.root)
        path = recipes.archive_path(self.sha, self.root)
        inode = path.stat().st_ino
        recipes.preserve('windows', self.sha, self.root)
        self.assertEqual(inode, path.stat().st_ino)

    def test_capture_requires_current_but_old_release_replay_remains_valid(self):
        import crange_mhl_services as services
        release = {'project_id':100, 'scenario':{'service_profiles':{'windows':{
            'profile':services.PROFILE_IDS['windows'], 'source_sha256':self.sha}}}}
        with patch.object(services, 'ROOT', self.root):
            services.verify_release_profiles(release, require_current=True, preserve_recipes=True)
            (self.root / sorted(recipes.WINDOWS)[0]).write_text('new reviewed recipe')
            self.assertEqual(services.verify_release_profiles(release), ['windows'])
            with self.assertRaisesRegex(services.ServiceConfigurationError, 'configure staging'):
                services.verify_release_profiles(release, require_current=True)
