import argparse
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('vm_builder', Path(__file__).resolve().parents[1] / 'scripts/build-vm-base.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class VMBuilderTests(unittest.TestCase):
    def test_only_official_matching_amd64_image_is_accepted(self):
        correct = 'https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-amd64.img'
        self.assertEqual(builder.validate_source('ubuntu-server-24-04', correct, 'a' * 64), 'sha256')
        for url in (correct.replace('cloud-images.ubuntu.com', 'cloud-images.ubuntu.com.evil.example'),
                    correct.replace('amd64', 'arm64'), correct + '?alternate=1', correct.replace('/release/', '/../')):
            with self.assertRaises(builder.BuildError):
                builder.validate_source('ubuntu-server-24-04', url, 'a' * 64)
        with self.assertRaises(builder.BuildError):
            builder.validate_source('ubuntu-server-24-04', correct, 'a' * 128)

    def test_alpine_source_is_pinned_to_generic_nocloud_revision(self):
        url = 'https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/cloud/alpine-3.24.2-x86_64-cloudinit-r0.qcow2'
        self.assertEqual(builder.validate_source('alpine-3-24', url, 'a' * 128), 'sha512')
        for invalid in (url.replace('-r0.', '-metal-r0.'), url.replace('x86_64', 'aarch64'), url.replace('3.24.2', 'edge')):
            with self.assertRaises(builder.BuildError):
                builder.validate_source('alpine-3-24', invalid, 'a' * 128)

    def test_alpine_machine_identity_is_stable_and_unique_per_clone(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'uuid'
            target = Path(root) / 'machine-id'
            script = builder.alpine_machine_identity_service().replace('/sys/class/dmi/id/product_uuid', str(source)).replace('/etc/machine-id', str(target))
            identities = []
            for uuid in ('E2B0534A-7E9E-4FE6-9E5C-0ECD6AEB4605', 'F76BF5DB-0C15-4A5A-A624-60ECDA3D2B59'):
                source.write_text(uuid + '\n')
                subprocess.run(['sh', '-c', script + '\nstart'], check=True, capture_output=True)
                identity = target.read_text()
                self.assertEqual(identity, uuid.lower().replace('-', ''))
                self.assertRegex(identity, r'^[a-f0-9]{32}$')
                subprocess.run(['sh', '-c', script + '\nstart'], check=True, capture_output=True)
                self.assertEqual(target.read_text(), identity)
                identities.append(identity)
            self.assertNotEqual(*identities)

    def test_password_file_must_be_private(self):
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / 'guest'; file.write_text('test-password'); file.chmod(0o600)
            builder.private_file(file)
            file.chmod(0o644)
            with self.assertRaises(builder.BuildError):
                builder.private_file(file)

    def test_occupied_guest_is_refused_before_download_or_disk_creation(self):
        with tempfile.TemporaryDirectory() as root:
            key = Path(root) / 'key'; key.write_text('ssh-ed25519 AAAATEST operator')
            args = argparse.Namespace(os='debian-13', vmid=3011,
                                      source='https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2',
                                      checksum='a' * 128, ssh_key_file=key, password_file=Path(root) / 'secret')
            with patch.object(builder.os, 'geteuid', return_value=0), \
                 patch.object(builder, 'private_file'), \
                 patch.object(builder.shutil, 'which', return_value='/bin/tool'), \
                 patch.object(builder.Path, 'exists', return_value=True), \
                 patch.object(builder, 'command', return_value=json.dumps([{'vmid': 3011}])) as command, \
                 patch.object(builder.urllib.request, 'urlopen') as download:
                with self.assertRaisesRegex(builder.BuildError, 'occupied'):
                    builder.build(args)
                self.assertEqual(command.call_count, 1)
                download.assert_not_called()


if __name__ == '__main__':
    unittest.main()
