from pathlib import Path
import json
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode, parse_qs
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_proxmox_compat as compat


class CompatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.materials = tempfile.TemporaryDirectory()
        root = Path(cls.materials.name)
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(root / 'key.pem'), '-out', str(root / 'cert.pem'),
                        '-days', '1', '-subj', '/CN=127.0.0.1',
                        '-addext', 'subjectAltName=IP:127.0.0.1'],
                       check=True, capture_output=True)
        cls.certificate = (root / 'cert.pem').read_bytes()
        cls.key = (root / 'key.pem').read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.materials.cleanup()

    def write_config(self, path, config=None):
        path.write_text(json.dumps(config or self.config()))
        path.chmod(0o600)
        path.with_suffix('.crt').write_bytes(self.certificate)
        path.with_suffix('.key').write_bytes(self.key)
        path.with_suffix('.key').chmod(0o600)

    def config(self):
        return {'revision': 'measured-9.0.3-v1', 'upstream': 'https://proxmox1:8006/',
            'port': 18766, 'nodes': {'proxmox2': {'version': '9.0.3',
                'host_managed_supported': False, 'schema_sha256': 'a'*64, 'job_id': 'acceptance'}}}

    def test_installer_explicit_owner_and_runtime_owner_are_checked(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'compat.json'
            self.write_config(path)
            with patch.object(Path, 'stat', return_value=SimpleNamespace(st_mode=0o100600, st_uid=1234)):
                with patch.object(compat.os, 'geteuid', return_value=0):
                    self.assertEqual(compat.load_config(path, owner_uid=1234)['port'], 18766)
                    with self.assertRaises(compat.CompatError): compat.load_config(path)
                with patch.object(compat.os, 'geteuid', return_value=5678):
                    with self.assertRaises(compat.CompatError): compat.load_config(path, owner_uid=1234)

    def test_only_exact_false_on_measured_old_ct_config_is_omitted(self):
        body = urlencode({'net0': 'name=eth0,bridge=p003t254,host-managed=0,firewall=1,mtu=1450',
                          'description': 'host-managed=0'}).encode()
        rewritten, node = compat.rewrite('PUT', '/api2/json/nodes/proxmox2/lxc/8804/config',
                                         'application/x-www-form-urlencoded', body, self.config())
        self.assertEqual(node, 'proxmox2')
        parsed = parse_qs(rewritten.decode())
        self.assertEqual(parsed['net0'], ['name=eth0,bridge=p003t254,firewall=1,mtu=1450'])
        self.assertEqual(parsed['description'], ['host-managed=0'])
        for path in ('/api2/json/nodes/proxmox1/lxc/8804/config',
                     '/api2/json/nodes/proxmox2/qemu/8804/config',
                     '/api2/json/nodes/proxmox2/lxc/8804/clone'):
            self.assertEqual(compat.rewrite('PUT', path, 'application/x-www-form-urlencoded', body, self.config()), (body, None))

    def test_true_host_managed_is_preserved_and_json_is_refused(self):
        body = urlencode({'net0': 'name=eth0,host-managed=1'}).encode()
        path = '/api2/json/nodes/proxmox2/lxc'
        self.assertEqual(compat.rewrite('POST', path, 'application/x-www-form-urlencoded', body, self.config()), (body, None))
        with self.assertRaises(compat.CompatError): compat.rewrite('POST', path, 'application/json', b'{}', self.config())

    def test_ssrf_traversal_and_malformed_encoding_are_rejected(self):
        for path in ('https://evil/api2/json/', '//evil/api2/json/', '/api2/json/../nodes',
                     '/api2/json/nodes/%2e%2e/foo', '/api2/json/nodes//evil'):
            with self.subTest(path=path), self.assertRaises(compat.CompatError): compat.validate_path(path)
        with self.assertRaises(compat.CompatError):
            compat.rewrite('POST', '/api2/json/nodes/proxmox2/lxc', 'application/x-www-form-urlencoded', b'net0=%xx', self.config())

    def test_live_version_check_and_caller_token_passthrough(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'compat.json'
            self.write_config(path)
            adapter = compat.Adapter(path)
            adapter._request = Mock(side_effect=[(200, {}, b'{"data":{"pveversion":"pve-manager/9.0.3/build"}}'), (200, {}, b'{"data":null}')])
            body = urlencode({'net0': 'name=eth0,host-managed=0'}).encode()
            headers = {'Authorization': 'PVEAPIToken=caller-secret', 'Content-Type': 'application/x-www-form-urlencoded'}
            self.assertEqual(adapter.forward('PUT', '/api2/json/nodes/proxmox2/lxc/8804/config', headers, body)[0], 200)
            self.assertEqual(adapter._request.call_args.args[2]['Authorization'], headers['Authorization'])
            adapter._request.reset_mock()
            adapter._request.side_effect = [(200, {}, b'{"data":{"pveversion":"pve-manager/9.1.1/build"}}')]
            with self.assertRaisesRegex(compat.CompatError, 'version changed'):
                adapter.forward('PUT', '/api2/json/nodes/proxmox2/lxc/8804/config', headers, body)
            self.assertEqual(adapter._request.call_count, 1)

    def test_config_changes_fail_closed_and_redirects_are_never_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'compat.json'
            self.write_config(path)
            adapter = compat.Adapter(path)
            changed = self.config(); changed['revision'] = 'new'
            path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(compat.CompatError, 'changed'): adapter.assert_unchanged()
            path.chmod(0o666)
            with self.assertRaisesRegex(compat.CompatError, 'not writable'): compat.load_config(path)
        self.assertIsNone(compat.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil/'))

    def test_plan_identity_matches_actual_running_service(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'compat.json'
            self.write_config(path)
            response = Mock()
            response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
            response.read.return_value = json.dumps(compat.fingerprint(path)).encode()
            opener = Mock(open=Mock(return_value=response))
            with patch.object(compat.request, 'build_opener', return_value=opener):
                self.assertEqual(compat.reviewed_identity(path), compat.fingerprint(path))
                response.read.return_value = b'{}'
                with self.assertRaisesRegex(compat.CompatError, 'differ'): compat.reviewed_identity(path)

    def test_tls_identity_is_pinned_and_private_key_is_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'compat.json'
            self.write_config(path)
            before = compat.fingerprint(path)
            self.assertEqual(before['endpoint'], 'https://127.0.0.1:18766/')
            path.with_suffix('.crt').write_bytes(self.certificate + b'\n')
            self.assertNotEqual(compat.fingerprint(path)['tls_certificate_sha256'], before['tls_certificate_sha256'])
            path.with_suffix('.key').chmod(0o644)
            with self.assertRaisesRegex(compat.CompatError, 'TLS material'):
                compat.load_config(path)
