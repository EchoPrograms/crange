import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch
import importlib.util

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate-project-inventory.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("generate_project_inventory", SCRIPT)
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)


class WindowsInventoryGroupsTests(unittest.TestCase):
    def test_empty_applied_outputs_return_actionable_cli_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            stderr = io.StringIO()
            args = ["generate-project-inventory.py", "--terraform-dir", str(project),
                    "--output", str(project / "inventory.json")]
            with patch.object(sys, "argv", args), \
                    patch.object(inventory.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "{}", "")), \
                    redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    inventory.main()
            self.assertEqual(caught.exception.code, 2)
            self.assertIn("Apply the project infrastructure first.", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def assert_profile_groups(self, include_pending=False, missing_identity=False):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "crange.auto.tfvars.json").write_text(json.dumps({"scenario_boxes": {
                "linux-jump": {"template": "parrot", "is_jumpbox": True},
                "windows-jump": {"template": "server", "is_jumpbox": True},
                "windows-app": {"template": "server", "is_jumpbox": False},
            }}))
            (project / "approved-scenario-templates.json").write_text(json.dumps({
                "parrot": {"profile": {"os_type": "l26", "cloud_init": True}},
                "server": {"profile": {"os_type": "win11", "cloud_init": False}},
            }))
            output = project / "inventory.json"
            outputs = {
                "project_id": {"value": 100},
                "routers": {"value": {"project": {"address": "10.60.2.40"}, "teams": {}, "custom_networks": {}}},
                "jump_boxes": {"value": {}},
                "scenario_containers": {"value": {}},
                "scenario_boxes": {"value": {
                    key: {"address": f"10.100.254.{ip}", "team_id": 254, "network_id": 254,
                          "id": 3000 + ip, "placement": {"template_id": 200, "template_node": "proxmox4",
                                                               "image_id": "mhl-windows-image-v1", "node_name": "proxmox2"}}
                    for key, ip in (("linux-jump", 11), ("windows-jump", 12), ("windows-app", 13))
                }},
            }
            if include_pending:
                desired=json.loads((project / "crange.auto.tfvars.json").read_text())
                for key,source,address in [('pending-windows','windows-jump','10.100.254.14'),('pending-linux','linux-jump','10.100.254.15')]:
                    desired['scenario_boxes'][key]=dict(desired['scenario_boxes'][source])
                    outputs['scenario_boxes']['value'][key]={**outputs['scenario_boxes']['value'][source],'id':None,'address':address}
                (project / "crange.auto.tfvars.json").write_text(json.dumps(desired))
            if missing_identity:
                saved={'guest_id':3012,'declared_template':'server','image_id':'mhl-windows-image-v1',
                       'node_name':'proxmox2','template_id':200,'template_node':'proxmox4'}
                (project/'crange-applied-placements.json').write_text(json.dumps({'vm:windows-jump':saved}))
                outputs['scenario_boxes']['value']['windows-jump']['placement']['image_id']=''
                outputs['scenario_boxes']['value']['windows-jump']['placement']['node_name']=None
            args = ["generate-project-inventory.py", "--terraform-dir", str(project), "--output", str(output)]
            with patch.object(sys, "argv", args), patch.object(inventory.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(outputs), "")):
                inventory.main()

            groups = json.loads(output.read_text())["all"]["children"]
            self.assertEqual(set(groups["scenario_vms"]["hosts"]), {"p100_vm_linux-jump"})
            targets = groups["routers"]["children"]["project_routers"]["hosts"]["p100_router"]["router_controller_winrm_targets"]
            self.assertEqual(set(targets), {"10.100.254.12", "10.100.254.13"})
            windows = groups["windows_guests"]
            self.assertEqual(windows["vars"]["ansible_connection"], "winrm")
            self.assertEqual(windows["vars"]["ansible_port"], 5985)
            self.assertEqual(windows["vars"]["ansible_winrm_message_encryption"], "always")
            jump_hosts = windows["children"]["windows_jumpboxes"]["children"]["staging_windows"]["hosts"]
            app_hosts = windows["children"]["windows_vms"]["children"]["staging_windows"]["hosts"]
            self.assertEqual(set(jump_hosts), {"p100_vm_windows-jump"})
            self.assertEqual(set(app_hosts), {"p100_vm_windows-app"})
            win_host = windows["hosts"]["p100_vm_windows-jump"]
            self.assertTrue(win_host["crange_windows_jumpbox"])
            self.assertEqual(win_host["crange_guest_id"], 3012)
            self.assertEqual(win_host["crange_project_id"], 100)
            self.assertEqual(win_host["crange_box_key"], "windows-jump")
            self.assertEqual(win_host["crange_image_id"], "mhl-windows-image-v1")
            self.assertEqual(win_host["crange_node_name"], "proxmox2")
            self.assertNotIn("ansible_password", windows["vars"])

    def test_approved_profile_splits_linux_and_windows_jump_vms(self):
        self.assert_profile_groups()

    def test_null_id_outputs_do_not_create_inventory_or_firewall_targets(self):
        self.assert_profile_groups(include_pending=True)

    def test_targeted_plan_missing_metadata_uses_the_same_applied_guest(self):
        self.assert_profile_groups(missing_identity=True)

    def test_legacy_profile_defaults_to_linux_but_unmanaged_os_is_not_linux(self):
        self.assertEqual(inventory.scenario_vm_os_type("legacy-parrot", {"legacy-parrot": {}}), "l26")
        self.assertEqual(inventory.scenario_vm_os_type("approved-bsd", {"approved-bsd": {"profile": {"os_type": "other"}}}), "other")


if __name__ == "__main__":
    unittest.main()
