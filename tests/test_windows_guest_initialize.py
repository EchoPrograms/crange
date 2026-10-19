import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import crange_windows_guest_initialize as init
import crange_windows_bootstrap as bootstrap


class WindowsGuestInitializeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal_path = Path(self.temp.name) / "private" / "journal.json"
        self.target = {"node": "proxmox3", "guest_id": 32100,
                       "network": {"address": "10.100.254.12", "gateway": "10.100.254.1"}}
        self.api = Mock()

    def tearDown(self):
        self.temp.cleanup()

    def test_fixed_script_waits_for_actual_oobe_and_scopes_by_mac(self):
        script = init._script()
        self.assertIn("IMAGE_STATE_COMPLETE", script)
        self.assertIn("OOBEInProgress", script)
        self.assertIn("$_.SID -match '-500$'", script)
        self.assertIn("New-LocalUser -Name 'user'", script)
        self.assertIn("S-1-5-32-555", script)
        self.assertIn("$p.mac", script)
        self.assertIn("$p.controller", script)
        self.assertIn("LogonUser($u,'.',[string]$p.verify_admin_password", script)
        self.assertIn("LogonUser('user','.',[string]$p.verify_rdp_password", script)
        self.assertIn("C:\\ProgramData\\CRANGE\\clone-unattend.xml", script)
        self.assertIn("C:\\Windows\\Panther\\UnattendGC\\Unattend.xml", script)
        self.assertIn("(Get-Item -LiteralPath $path).IsReadOnly=$false", script)
        self.assertIn("Remove-ItemProperty -Path $winlogon -Name DefaultPassword", script)
        self.assertLess(script.index("IMAGE_STATE_COMPLETE"), script.index("$ownedAnswerPaths="))
        self.assertLess(script.index("verify_rdp_password"), script.index("$ownedAnswerPaths="))
        self.assertNotIn("OOBEInProgress' -Value 0", script)

    def test_journal_is_private_and_pid_and_raw_response_are_saved(self):
        doc = {"100:box:32100:image": {"password": "secret-test-value", "state": "pending"}}
        init._save(self.journal_path, doc)
        self.assertEqual(self.journal_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.journal_path.parent.stat().st_mode & 0o777, 0o700)
        order = []

        def request(method, path, fields=None):
            if path.endswith("/get-osinfo"):
                return {"result": {"id": "mswindows"}}
            if method == "POST":
                return {"pid": 987}
            record = json.loads(self.journal_path.read_text())["100:box:32100:image"]
            self.assertEqual(record["pid"], 987)
            order.append("polled")
            return {"exited": True, "exitcode": 0,
                    "out-data": json.dumps({"ready": True, "account": "Administrator", "rdp_account": "user",
                                             "ipv4": "10.100.254.12", "gateway": "10.100.254.1",
                                             "dns": "10.60.2.1", "winrm": True})}

        self.api.request.side_effect = request
        journal = {"path": self.journal_path, "document": doc,
                   "record": doc["100:box:32100:image"]}
        result = init._execute(self.api, self.target, "AA:BB:CC:DD:EE:01", "secret-test-value",
                               rdp_password="synthetic-rdp-password-only",
                               set_rdp_password=True,
                               timeout=1, journal=journal)
        self.assertTrue(result["ready"])
        self.assertEqual(order, ["polled"])
        saved = json.loads(self.journal_path.read_text())["100:box:32100:image"]
        self.assertEqual(json.loads(saved["raw_terminal_response"])["ready"], True)
        self.assertEqual(saved["exitcode"], 0)
        post = next(call for call in self.api.request.call_args_list if call.args[0] == "POST")
        payload = json.loads(post.args[2]["input-data"])
        self.assertEqual(payload["address"], "10.100.254.12")
        self.assertEqual(payload["verify_admin_password"], "secret-test-value")
        self.assertEqual(payload["verify_rdp_password"], "synthetic-rdp-password-only")
        self.assertNotIn("secret-test-value", post.args[2]["command"][-1])

    def test_failed_guest_credentials_keep_the_private_retry_journal(self):
        doc = {"100:box:32100:image": {"password": "private-admin-test-value",
                                         "rdp_password": "private-rdp-test-value",
                                         "state": "pending"}}
        init._save(self.journal_path, doc)
        def request(method, path, fields=None):
            if path.endswith("/get-osinfo"):
                return {"result": {"id": "mswindows"}}
            if method == "POST":
                return {"pid": 988}
            return {"exited": True, "exitcode": 1, "out-data": "credential verification failed"}
        self.api.request.side_effect = request
        journal = {"path": self.journal_path, "document": doc,
                   "record": doc["100:box:32100:image"]}
        with self.assertRaisesRegex(bootstrap.WindowsBootstrapError, "guest output was withheld"):
            init._execute(self.api, self.target, "AA:BB:CC:DD:EE:01", "private-admin-test-value",
                          rdp_password="private-rdp-test-value", set_rdp_password=True,
                          timeout=1, journal=journal)
        saved = json.loads(self.journal_path.read_text())["100:box:32100:image"]
        self.assertEqual(saved["password"], "private-admin-test-value")
        self.assertEqual(saved["rdp_password"], "private-rdp-test-value")
        self.assertEqual(saved["pid"], 988)
        self.assertEqual(saved["exitcode"], 1)

    def test_persisted_live_handle_is_polled_without_resubmission(self):
        record = {'pid': 987, 'state': 'running', 'mac': 'AA:BB:CC:DD:EE:01'}
        doc = {'identity': record}
        init._save(self.journal_path, doc)
        self.api.request.side_effect = OSError('temporary observation failure')
        with self.assertRaises(OSError):
            init._execute(self.api, self.target, 'AA:BB:CC:DD:EE:01', 'private-admin-test-value',
                          rdp_password='private-rdp-test-value', timeout=1,
                          journal={'path': self.journal_path, 'document': doc, 'record': record})
        self.assertEqual(self.api.request.call_args.args, ('GET', '/nodes/proxmox3/qemu/32100/agent/exec-status', {'pid':987}))
        self.assertEqual(record['pid'], 987)

    def test_uncertain_submission_is_never_replayed(self):
        record = {'state':'submitting', 'mac':'AA:BB:CC:DD:EE:01'}
        with self.assertRaisesRegex(bootstrap.WindowsBootstrapError, 'no duplicate'):
            init._execute(self.api, self.target, 'AA:BB:CC:DD:EE:01', 'private-admin-test-value',
                          rdp_password='private-rdp-test-value', timeout=1,
                          journal={'path': self.journal_path, 'document': {'identity':record}, 'record':record})
        self.api.request.assert_not_called()

    def test_submission_intent_is_durable_before_mutation(self):
        record = {'state':'pending'}
        doc = {'identity':record}
        def request(method, path, fields=None):
            if method == 'GET': return {'result': {'id':'mswindows'}}
            saved = json.loads(self.journal_path.read_text())['identity']
            self.assertEqual(saved['state'], 'submitting')
            self.assertEqual(saved['mac'], 'AA:BB:CC:DD:EE:01')
            raise OSError('submission outcome unavailable')
        self.api.request.side_effect = request
        with self.assertRaises(OSError):
            init._execute(self.api, self.target, 'AA:BB:CC:DD:EE:01', 'private-admin-test-value',
                          rdp_password='private-rdp-test-value', timeout=1,
                          journal={'path':self.journal_path,'document':doc,'record':record})
        self.assertEqual(record['state'], 'submitting')

    def test_generated_password_is_mixed_and_at_least_16_characters(self):
        value = init._password()
        self.assertGreaterEqual(len(value), 16)
        self.assertTrue(any(c.islower() for c in value))
        self.assertTrue(any(c.isupper() for c in value))
        self.assertTrue(any(c.isdigit() for c in value))
        self.assertTrue(any(c in "!@#$%^&*()-_=+" for c in value))

    def test_live_ownership_failure_prevents_execution_and_secret_creation(self):
        from unittest.mock import patch
        import crange
        cfg = {"scenario_boxes": {"win1": {"team_id": "1"}}}
        approvals = {"windows": {"profile": {"os_type": "win11"}}}
        outputs = {}
        with patch.object(crange, "project_dir", return_value=Path(self.temp.name)), \
             patch.object(crange, "json_config", return_value=cfg), \
             patch.object(crange, "templates_for", return_value=approvals), \
             patch.object(crange, "output_json", return_value=outputs), \
             patch.object(init, "desired_target", return_value={"project_id": 100,
                   "box_key": "win1", "guest_id": 32100, "image_id": "approved", "name": "p100t001-win1",
                   "node": "proxmox3", "credential_ref": {"project_id": 100, "box_key": "win1"},
                   "network": {"address": "10.100.1.12", "gateway": "10.100.1.1"},
                   "os_type": "win11"}), \
             patch.object(init, "_owned_live_config", side_effect=bootstrap.WindowsBootstrapError("ownership mismatch")), \
             patch("crange_windows_guest_admin.store") as store, \
             patch("crange_windows_guest_admin.resolve", side_effect=ValueError("missing")), \
             patch.object(init, "_password") as password, \
             self.assertRaisesRegex(bootstrap.WindowsBootstrapError, "ownership mismatch"):
            init.initialize(Mock(), 100, "win1")
        store.assert_not_called()
        password.assert_not_called()

    def test_success_returns_no_secret_and_existing_binding_is_not_rotated(self):
        from unittest.mock import patch
        import crange
        cfg = {"scenario_boxes": {"win1": {"team_id": "1"}}}
        with patch.object(crange, "project_dir", return_value=Path(self.temp.name)), \
             patch.object(crange, "json_config", return_value=cfg), \
             patch.object(crange, "templates_for", return_value={"windows": {"profile": {"os_type": "win11"}}}), \
             patch.object(crange, "output_json", return_value={}), \
             patch.object(init, "desired_target", return_value={"project_id": 100,
                   "box_key": "win1", "guest_id": 32100, "image_id": "approved", "name": "p100t001-win1",
                   "node": "proxmox3", "credential_ref": {"project_id": 100, "box_key": "win1"},
                   "network": {"address": "10.100.1.12", "gateway": "10.100.1.1"},
                   "os_type": "win11"}), \
             patch.object(init, "_owned_live_config", return_value=({"same": True}, "AA:BB:CC:DD:EE:01")), \
             patch.object(init, "_journal_path", return_value=self.journal_path), \
             patch.object(init, "_execute", return_value={"ready": True, "account": "Administrator", "rdp_account": "user"}) as execute, \
             patch("crange_windows_guest_admin.resolve", side_effect=[ValueError("missing"),
                   {"username": "Administrator", "password": "private-test-secret"}]), \
             patch("crange_windows_guest_admin.store") as store, \
             patch("crange_windows_guest_rdp.resolve", side_effect=[ValueError("missing"),
                   {"username": "user", "password": "private-rdp-secret", "show_credentials": False}]), \
             patch("crange_windows_guest_rdp.store") as store_rdp, \
             patch.object(init, "_password", side_effect=["private-test-secret", "private-rdp-secret"]):
            api = Mock()
            api.request.side_effect = lambda method, path, fields=None: (
                {"same": True} if path.endswith("/config") else
                [{"type": "qemu", "vmid": 32100, "pool": "crange-iac",
                  "node": "proxmox3", "name": "p100t001-win1"}]
                if path == "/cluster/resources?type=vm" else {"status": "running"})
            first = init.initialize(api, 100, "win1")
            second = init.initialize(api, 100, "win1")
        self.assertNotIn("password", json.dumps(first))
        self.assertNotIn("private-test-secret", json.dumps(first))
        self.assertTrue(first["initialized"])
        self.assertFalse(second["initialized"])
        store.assert_called_once()
        store_rdp.assert_called_once()
        self.assertTrue(execute.call_args_list[0].kwargs["set_password"])
        self.assertFalse(execute.call_args_list[1].kwargs["set_password"])
        self.assertTrue(execute.call_args_list[0].kwargs["set_rdp_password"])
        self.assertFalse(execute.call_args_list[1].kwargs["set_rdp_password"])
        self.assertEqual(execute.call_args_list[1].kwargs["verify_admin_password"], "private-test-secret")
        self.assertEqual(execute.call_args_list[1].kwargs["verify_rdp_password"], "private-rdp-secret")


if __name__ == "__main__":
    unittest.main()
