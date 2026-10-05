import importlib.util
from pathlib import Path
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('builder',Path(__file__).resolve().parents[1]/'scripts/build-container-base.py')
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)

class BuilderSafetyTests(unittest.TestCase):
    def test_private_password_file_is_checked_without_exposing_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'password';path.write_text('private guest credential\n');path.chmod(0o600)
            self.assertEqual(builder.private_file(path),'private guest credential')
            path.chmod(0o644)
            with self.assertRaisesRegex(builder.BuildError,'0600'):builder.private_file(path)
            path.chmod(0o600);path.write_text('user:injected\n')
            with self.assertRaisesRegex(builder.BuildError,'Invalid guest'):builder.private_file(path)
    def test_existing_id_is_refused_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            key=Path(directory)/'key';key.write_text('ssh-ed25519 AAAA fixture')
            args=SimpleNamespace(vmid=3000,archive='debian-13-standard_13.6-1_amd64.tar.zst',
                password_file=Path(directory)/'password',ssh_key_file=key)
            with patch.object(builder.os,'geteuid',return_value=0),patch.object(builder,'private_file',return_value='private'), \
                 patch.object(builder,'command',return_value='[{"vmid":3000}]') as command:
                with self.assertRaisesRegex(builder.BuildError,'already in use'):builder.build(args)
            self.assertEqual(command.call_count,1);self.assertEqual(command.call_args.args[0][0],'pvesh')
    def test_arbitrary_archive_commands_and_wrong_distributions_are_refused(self):
        for archive in ('../debian.tar.zst','ubuntu-24.04;id','centos-7-standard_1_amd64.tar.zst'):
            args=SimpleNamespace(vmid=3000,archive=archive)
            with patch.object(builder.os,'geteuid',return_value=0),patch.object(builder,'command') as command:
                with self.assertRaisesRegex(builder.BuildError,'Choose a Debian'):builder.build(args)
                command.assert_not_called()
