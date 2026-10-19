import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import crange_windows_bootstrap as bootstrap


class WindowsBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.project = 100
        self.box_key = "t001-win-jump-01"
        self.vm_id = 30120
        self.template_id = 900
        self.image_id = "mhl-windows-server-v1"
        self.mac = "AA:BB:CC:DD:EE:01"
        self.ref = {"project_id": self.project, "box_key": self.box_key}
        self.cfg = {"team_ids": ["1"], "custom_networks": {}, "scenario_boxes": {
            self.box_key: {"team_id": "1", "network_key": "", "name": "win-jump-01",
                           "ip_last_octet": 12, "template": "windows-server",
                           "is_jumpbox": True, "windows_admin_credential_ref": self.ref}
        }}
        self.approvals = {"windows-server": {
            "template_id": self.template_id, "template_node": "proxmox4", "image_id": self.image_id,
            "profile": {"os_type": "win11", "cloud_init": False, "agent_enabled": True}
        }}
        self.outputs = {"scenario_boxes": {"value": {self.box_key: {
            "id": self.vm_id, "address": "10.100.1.12",
            "placement": {"node_name": "proxmox3", "template_id": self.template_id,
                          "template_node": "proxmox4", "image_id": self.image_id}
        }}}}
        self.config = {
            "name": "p100t001-win-jump-01", "tags": "range-lan;scenario;p100;p100t001;terraform",
            "ostype": "win11", "template": 0, "agent": "enabled=1,type=virtio",
            "net0": f"virtio={self.mac},bridge=p100t001,firewall=1,mtu=1450"
        }
        self.readback = {
            "ipv4": "10.100.1.12", "prefix_length": 24, "gateway": "10.100.1.1",
            "dns_servers": [bootstrap.WINDOWS_DNS], "winrm_running": True,
            "winrm_allowed_sources": [bootstrap.CONTROLLER_IP],
            "qemu_agent_running": True, "rdp_service_running": True,
        }
        self.api = Mock()
        def request(method, path, fields=None):
            if method == "GET" and path == "/cluster/resources?type=vm":
                return [{"type": "qemu", "vmid": self.vm_id, "node": "proxmox3", "name": self.config["name"]}]
            if method == "GET" and path.endswith("/config"):
                return dict(self.config)
            if method == "GET" and path.endswith("/status/current"):
                return {"status": "running"}
            if method == "POST" and path.endswith("/agent/exec"):
                return {"pid": 1234}
            if method == "GET" and path.endswith("/agent/exec-status"):
                return {"exited": True, "exitcode": 0, "out-data": json.dumps(self.readback)}
            raise AssertionError("unexpected Proxmox request")
        self.api.request.side_effect = request
        self.temporary = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def run_bootstrap(self):
        return bootstrap.bootstrap(self.api, self.project, self.box_key)

    def patched_inputs(self):
        return (
            patch("crange.project_dir", return_value=self.project_dir),
            patch("crange.json_config", return_value=self.cfg),
            patch("crange.output_json", return_value=self.outputs),
            patch("crange.templates_for", return_value=self.approvals),
            patch("crange_windows_guest_admin.resolve", return_value={
                "username": ".\\Administrator", "password": "dynamic-secret-from-test"
            }),
        )

    def test_bootstrap_uses_bound_private_credentials_over_qga_stdin(self):
        with patch.multiple("crange", project_dir=Mock(return_value=self.project_dir),
                            json_config=Mock(return_value=self.cfg),
                            output_json=Mock(return_value=self.outputs),
                            templates_for=Mock(return_value=self.approvals)), \
             patch("crange_windows_guest_admin.resolve", return_value={
                 "username": ".\\Administrator", "password": "dynamic-secret-from-test"
             }) as resolve:
            result = self.run_bootstrap()
        resolve.assert_called_once_with(self.ref, project_id=self.project, box_key=self.box_key,
                                        guest_id=self.vm_id, image_id=self.image_id)
        self.assertEqual(result["ipv4"], "10.100.1.12")
        self.assertEqual(result["gateway"], "10.100.1.1")
        self.assertNotIn("password", result)
        calls = self.api.request.call_args_list
        post = next(call for call in calls if call.args[0] == "POST")
        self.assertGreater(calls.index(post), max(i for i, call in enumerate(calls)
                                                   if call.args[0] == "GET" and call.args[1].endswith("/status/current")))
        fields = post.args[2]
        command = fields["command"]
        self.assertEqual(command[:3], [bootstrap.POWERSHELL, "-NoProfile", "-NonInteractive"])
        script = base64.b64decode(command[-1]).decode("utf-16le")
        self.assertNotIn("dynamic-secret-from-test", script)
        payload = json.loads(fields["input-data"])
        self.assertEqual(payload["address"], "10.100.1.12")
        self.assertEqual(payload["gateway"], "10.100.1.1")
        self.assertEqual(payload["dns_servers"], [bootstrap.WINDOWS_DNS])
        self.assertEqual(payload["password"], "dynamic-secret-from-test")
        self.assertEqual(payload["mac"], self.mac)

    def test_wrong_live_owner_tags_prevent_any_guest_exec_or_secret_resolution(self):
        self.config["tags"] = "range-lan;scenario;p100t001;terraform"
        with patch.multiple("crange", project_dir=Mock(return_value=self.project_dir),
                            json_config=Mock(return_value=self.cfg),
                            output_json=Mock(return_value=self.outputs),
                            templates_for=Mock(return_value=self.approvals)), \
             patch("crange_windows_guest_admin.resolve") as resolve:
            with self.assertRaisesRegex(bootstrap.WindowsBootstrapError, "tags or OS"):
                self.run_bootstrap()
        resolve.assert_not_called()
        self.assertFalse(any(call.args[0] == "POST" for call in self.api.request.call_args_list))

    def test_wrong_profile_image_or_out_of_plan_address_fails_before_guest_exec(self):
        with patch.multiple("crange", project_dir=Mock(return_value=self.project_dir),
                            json_config=Mock(return_value=self.cfg),
                            output_json=Mock(return_value=self.outputs),
                            templates_for=Mock(return_value={"windows-server": {**self.approvals["windows-server"],
                                                                                 "image_id": "other-image"}})), \
             patch("crange_windows_guest_admin.resolve") as resolve:
            with self.assertRaisesRegex(bootstrap.WindowsBootstrapError, "approved image identity"):
                self.run_bootstrap()
        resolve.assert_not_called()
        self.assertEqual(self.api.request.call_count, 0)

        self.cfg["scenario_boxes"][self.box_key]["ip_last_octet"] = 101
        with patch.multiple("crange", project_dir=Mock(return_value=self.project_dir),
                            json_config=Mock(return_value=self.cfg),
                            output_json=Mock(return_value=self.outputs),
                            templates_for=Mock(return_value=self.approvals)), \
             patch("crange_windows_guest_admin.resolve") as resolve:
            with self.assertRaisesRegex(bootstrap.WindowsBootstrapError, "host address"):
                self.run_bootstrap()
        resolve.assert_not_called()
        self.assertEqual(self.api.request.call_count, 0)

    def test_script_limits_routes_to_one_adapter_and_does_not_weaken_other_firewall_policy(self):
        script = bootstrap._powershell_script()
        self.assertIn("Get-NetRoute -InterfaceIndex $index", script)
        self.assertIn("Get-NetAdapter -IncludeHidden", script)
        self.assertIn("10.60.0.20", script)
        self.assertIn("Disable-NetFirewallRule", script)
        self.assertIn("Get-NetFirewallRule -DisplayGroup 'Windows Remote Management'", script)
        self.assertNotIn("Set-NetFirewallProfile", script)
        self.assertNotIn("Invoke-Expression", script)
        self.assertNotIn("netsh ", script)

    def test_verified_distributed_replica_can_supply_same_logical_windows_image(self):
        placement=self.outputs['scenario_boxes']['value'][self.box_key]['placement']
        placement.update(template_id=901,template_node='proxmox3')
        replica={'template_id':901,'node':'proxmox3','state':'ready','artifact_sha256':'a'*64}
        image={'kind':'vm','verified':True,'approved_projects':[100],'artifact_sha256':'a'*64,'replicas':[replica]}
        with patch('crange_scheduling.read_settings',return_value=({}, {'images':{self.image_id:image}})):
            target=bootstrap.desired_target(self.project,self.box_key,self.cfg,self.outputs,self.approvals)
            self.assertEqual((target['template_id'],target['template_node']),(901,'proxmox3'))
            replica['artifact_sha256']='b'*64
            with self.assertRaises(bootstrap.WindowsBootstrapError):
                bootstrap.desired_target(self.project,self.box_key,self.cfg,self.outputs,self.approvals)
            replica['artifact_sha256']='a'*64;image['approved_projects']=[101]
            with self.assertRaises(bootstrap.WindowsBootstrapError):
                bootstrap.desired_target(self.project,self.box_key,self.cfg,self.outputs,self.approvals)


if __name__ == "__main__":
    unittest.main()
