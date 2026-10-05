import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('guest_identity',Path(__file__).resolve().parents[1]/'scripts/crange_guest_identity.py')
helper=importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

class GuestIdentityTests(unittest.TestCase):
    def test_captured_clone_rotates_keys_and_repeated_configuration_preserves_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/'source'
            (source/'etc/ssh').mkdir(parents=True)
            (source/'etc/machine-id').write_text('a'*32)
            def run_for(root):
                def run(command,**kwargs):
                    self.assertEqual(command,['/usr/bin/ssh-keygen','-A'])
                    return subprocess.run(command+['-f',str(root)],**kwargs)
                return run
            with patch.object(helper.socket,'gethostname',return_value='source'):
                self.assertTrue(helper.ensure_identity(source,run_for(source))['rotated'])
                original={p.name:p.read_bytes() for p in (source/'etc/ssh').glob('*_key.pub')}
                self.assertTrue({'ssh_host_rsa_key.pub','ssh_host_ecdsa_key.pub','ssh_host_ed25519_key.pub'} <= set(original))
                self.assertFalse(helper.ensure_identity(source,run_for(source))['rotated'])
                self.assertEqual(original,{p.name:p.read_bytes() for p in (source/'etc/ssh').glob('*_key.pub')})
            clone=Path(temporary)/'clone'
            shutil.copytree(source,clone)
            (clone/'etc/machine-id').write_text('b'*32)
            with patch.object(helper.socket,'gethostname',return_value='clone'):
                self.assertTrue(helper.ensure_identity(clone,run_for(clone))['rotated'])
                copied={p.name:p.read_bytes() for p in (clone/'etc/ssh').glob('*_key.pub')}
                self.assertTrue(all(copied[name]!=key for name,key in original.items()))
                self.assertFalse(helper.ensure_identity(clone,run_for(clone))['rotated'])
                self.assertEqual(copied,{p.name:p.read_bytes() for p in (clone/'etc/ssh').glob('*_key.pub')})
            marker=clone/'var/lib/crange/ssh-identity.json'
            self.assertEqual(marker.stat().st_mode&0o777,0o600)
            self.assertEqual(json.loads(marker.read_text())['hostname'],'clone')

    def test_missing_identity_refuses_to_remove_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            key=root/'etc/ssh/ssh_host_ed25519_key'
            key.parent.mkdir(parents=True);key.write_text('preserve')
            with self.assertRaisesRegex(RuntimeError,'identity is unavailable'):
                helper.ensure_identity(root)
            self.assertEqual(key.read_text(),'preserve')
