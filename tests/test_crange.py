import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "crange.py"
spec = importlib.util.spec_from_file_location("crange", SCRIPT)
crange = importlib.util.module_from_spec(spec)
spec.loader.exec_module(crange)


class CrangeConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_envs = crange.ENVS
        self.old_root = crange.ROOT
        crange.ROOT = Path(self.tmp.name) / "repo"
        crange.ROOT.mkdir()
        crange.ENVS = Path(self.tmp.name) / "envs"
        crange.ENVS.mkdir()
        self.project = crange.ENVS / "project-003"
        self.project.mkdir()
        (self.project / "template-profile.json").write_text(json.dumps({
            "bios": "seabios", "machine": None, "scsi_hardware": "virtio-scsi-single", "os_type": "l26"}))
        template = crange.ENVS / "project-template"
        template.mkdir()
        (template / "main.tf").write_text("terraform {}\n")
        (template / "template-profile.json").write_text("{}\n")
        (template / "approved-scenario-templates.json").write_text(
            json.dumps({"approved-base": {"template_id": 112, "template_node": "proxmox4"}})
        )
        (template / "approved-container-templates.json").write_text(
            json.dumps({"approved-ct-base": {"template_id": 120, "template_node": "proxmox4"}})
        )
        (self.project / "approved-scenario-templates.json").write_text(
            json.dumps({"approved-base": {"template_id": 112, "template_node": "proxmox4"}})
        )
        (self.project / "approved-container-templates.json").write_text(
            json.dumps({"approved-ct-base": {"template_id": 120, "template_node": "proxmox4"}})
        )
        (self.project / "terraform.tfvars").write_text('''project_id = 3
transit_address = "10.60.2.32/24"
team_ids = ["1"]
jump_boxes = {
  "team1_jump01" = {
    team_id = "1"
    jump_number = 1
    node_name = "proxmox4"
  }
}
''')

    def tearDown(self):
        crange.ENVS = self.old_envs
        crange.ROOT = self.old_root

    def test_import_keeps_existing_jump_box(self):
        cfg = crange.json_config(self.project)
        self.assertEqual(cfg["jump_boxes"]["team1_jump01"]["jump_number"], 1)
        self.assertEqual(cfg["jump_boxes"]["team1_jump01"]["node_name"], "proxmox4")

    def test_box_add_preserves_existing_declarations(self):
        cfg = crange.json_config(self.project)
        cfg["jump_boxes"]["team1_jump01"]["full_clone"] = False
        crange.save_config(self.project, cfg)
        args = SimpleNamespace(project="3", name="t001-j02", team=1, node="proxmox4", cores=2, memory=4096, full_clone=None)
        crange.mutate_box(args, True)
        result = json.loads((self.project / "crange.auto.tfvars.json").read_text())
        self.assertIn("team1_jump01", result["jump_boxes"])
        self.assertIn("t001-j02", result["jump_boxes"])
        self.assertTrue(result["jump_boxes"]["t001-j02"]["full_clone"])
        self.assertFalse(result["jump_boxes"]["team1_jump01"]["full_clone"])

    def test_custom_network_adds_router_and_places_jump_box(self):
        net_args = SimpleNamespace(project="3", name="corp", team=1, network_id=100, node="proxmox4")
        crange.mutate_custom_network(net_args, True)
        cfg = crange.json_config(self.project)
        self.assertEqual(cfg["custom_networks"]["corp"]["uplink_last_octet"], 100)
        jump_args = SimpleNamespace(project="3", name="corp-j01", team=1, network="corp", node="proxmox4",
                                    cores=2, memory=4096, full_clone=None)
        crange.mutate_box(jump_args, True)
        cfg = crange.json_config(self.project)
        self.assertEqual(cfg["jump_boxes"]["corp-j01"]["network_id"], 100)
        with self.assertRaisesRegex(crange.CrangeError, "still has jump boxes"):
            crange.mutate_custom_network(SimpleNamespace(project="3", name="corp"), False)
        crange.mutate_box(SimpleNamespace(project="3", name="corp-j01"), False)
        crange.mutate_custom_network(SimpleNamespace(project="3", name="corp"), False)
        self.assertFalse(crange.json_config(self.project).get("custom_networks"))

    def test_custom_staging_network_accepts_team_zero_jump(self):
        crange.mutate_custom_network(SimpleNamespace(project="3", name="isolated", team=0, network_id=100, node="proxmox4"), True)
        args = SimpleNamespace(project="3", name="isolated-j01", team=0, network="isolated", node="proxmox4",
                               cores=2, memory=4096, full_clone=None)
        crange.mutate_box(args, True)
        self.assertEqual(crange.json_config(self.project)["jump_boxes"]["isolated-j01"]["team_id"], "254")

    def test_team_zero_jump_uses_staging_subnet(self):
        args = SimpleNamespace(project="3", name="staging-j01", team=0, network="", node="proxmox4",
                               cores=2, memory=4096, full_clone=None)
        crange.mutate_box(args, True)
        box = crange.json_config(self.project)["jump_boxes"]["staging-j01"]
        self.assertEqual((box["team_id"], box["network_id"], box["network_key"]), ("254", 254, ""))

    def test_release_override_parser_and_container_validation(self):
        args = crange.make_parser().parse_args(["release-deploy", "3", "1.0", "--teams", "1", "--team-override", "1:web:cores=4"])
        self.assertEqual(args.team_override, ["1:web:cores=4"])
        cfg = crange.json_config(self.project)
        cfg["scenario_containers"] = {"t001-dns": {
            "team_id": "1", "name": "dns", "ip_last_octet": 20,
            "template": "approved-ct-base", "node_name": "proxmox4",
            "cores": 1, "memory_mb": 1024, "started": True,
        }}
        crange.validate_container_config(self.project, cfg)
        cfg["scenario_containers"]["t001-dns"]["ip_last_octet"] = 11
        with self.assertRaisesRegex(crange.CrangeError, "Duplicate static address"):
            crange.validate_container_config(self.project, cfg)

    def test_custom_container_attachment_and_address_uniqueness(self):
        crange.mutate_custom_network(SimpleNamespace(project="3", name="corp", team=1, network_id=100, node="proxmox4"), True)
        args=SimpleNamespace(project="3", name="svc", team=1, ip=22, template="approved-ct-base",
                             node="proxmox4", network="corp", cores=1, memory=1024,
                             health='[{"type":"tcp","port":80}]')
        crange.cmd_container_add(args)
        box=crange.json_config(self.project)["scenario_containers"]["t001-svc"]
        self.assertEqual(box["network_id"],100)
        args.name="duplicate";args.ip=22
        with self.assertRaisesRegex(crange.CrangeError,"Duplicate static address"):
            crange.cmd_container_add(args)

    def test_release_capture_records_startup_permutations(self):
        cfg = crange.json_config(self.project)
        cfg["scenario_boxes"] = {
            "t254-web": {"team_id": "254", "name": "web", "ip_last_octet": 20,
                         "template": "approved-base", "node_name": "proxmox4", "cores": 2,
                         "memory_mb": 4096, "full_clone": False, "started": True,
                         "health_checks": [{"type": "tcp", "port": 22}]},
            "t254-db": {"team_id": "254", "name": "db", "ip_last_octet": 21,
                        "template": "approved-base", "node_name": "proxmox4", "cores": 2,
                        "memory_mb": 4096, "full_clone": False, "started": True,
                        "health_checks": [{"type": "tcp", "port": 5432}]},
        }
        crange.save_config(self.project, cfg)
        class API:
            def vm(self, node, vmid): return {"name": "p003t254-" + ("web" if vmid == 115 else "db"), "template": 0}
            def status(self, node, vmid): return "running"
            def shutdown(self, *args): pass
            def capture_image(self, node, vmid, name, desc): return 200 + vmid
            def start(self, *args): pass
        outputs={"scenario_boxes":{"value":{
            "t254-web":{"id":115,"address":"10.3.254.20"},
            "t254-db":{"id":116,"address":"10.3.254.21"}}}}
        with patch.object(crange, "output_json", return_value=outputs), patch.object(crange,"check_endpoint"), patch.object(crange,"ProxmoxAPI",return_value=API()), patch.object(crange,"run",return_value=SimpleNamespace(stdout="rev\n")):
            crange.cmd_release_capture(SimpleNamespace(project="3",version="3.0",boot_order=["web,db","db,web"]))
        release = crange.load_release(3, "3.0")
        self.assertEqual(release["scenario"]["boot_order_permutations"], [["web", "db"], ["db", "web"]])

    def test_release_clones_keep_guacamole_protocol_and_default_legacy_to_ssh(self):
        vm = {"name": "desktop", "ip_last_octet": 20, "template": "approved-base",
              "node_name": "proxmox4", "cores": 2, "memory_mb": 2048,
              "full_clone": False, "started": True, "health_checks": [{"type": "ping"}]}
        ct = dict(vm, template="approved-ct-base")
        for protocol in (None, 'ssh', 'rdp'):
            with self.subTest(protocol=protocol):
                for item in (vm, ct):
                    if protocol is not None:
                        item['guac_protocol'] = protocol
                    else:
                        item.pop('guac_protocol', None)
                release = {"release_id": "p003-1.0", "scenario": {
                    "boxes": [vm], "containers": [ct], "templates": {
                        "approved-base": {"template_id": 112, "template_node": "proxmox4"},
                        "approved-ct-base": {"template_id": 120, "template_node": "proxmox4"}}}}
                boxes = crange.release_scenario_boxes(self.project, release, "1", "release-1")
                containers = crange.release_scenario_containers(self.project, release, "1", "release-1")
                self.assertEqual(next(iter(boxes.values()))['guac_protocol'], protocol or 'ssh')
                self.assertEqual(next(iter(containers.values()))['guac_protocol'], protocol or 'ssh')

    def test_scenario_add_checks_team_template_and_ip(self):
        args = SimpleNamespace(
            project="3", name="web", team=0, ip=20, template="approved-base",
            node="proxmox4", cores=2, memory=4096, full_clone=None,
            health='[{"type":"http","port":80,"path":"/health","status":200}]'
        )
        crange.cmd_scenario_add(args)
        result = json.loads((self.project / "crange.auto.tfvars.json").read_text())
        self.assertEqual(result["scenario_boxes"]["t254-web"]["team_id"], "254")
        self.assertEqual(result["scenario_boxes"]["t254-web"]["ip_last_octet"], 20)
        self.assertTrue(result["scenario_boxes"]["t254-web"]["full_clone"])
        args.name = "other"
        with self.assertRaisesRegex(crange.CrangeError, "already assigned"):
            crange.cmd_scenario_add(args)

    def test_scenario_address_cannot_collide_with_jump_box(self):
        args = SimpleNamespace(project="3", name="web", team=1, ip=11, template="approved-base",
                               node="proxmox4", cores=2, memory=4096, full_clone=None,
                               health='[{"type":"tcp","port":22}]')
        with self.assertRaisesRegex(crange.CrangeError, "already assigned"):
            crange.cmd_scenario_add(args)

    def test_removed_team_cannot_have_scenario_boxes(self):
        cfg = crange.json_config(self.project)
        cfg["jump_boxes"] = {}
        cfg["scenario_boxes"] = {"t001-web": {"team_id": "1"}}
        crange.save_config(self.project, cfg)
        with self.assertRaisesRegex(crange.CrangeError, "scenario VMs"):
            crange.mutate_team(SimpleNamespace(project="3", team=1), False)

    def test_bad_health_check_is_rejected(self):
        with self.assertRaisesRegex(crange.CrangeError, "tcp.*http"):
            crange.validate_health_checks([{"type": "shell", "command": "anything"}])

    def write_release(self):
        release = {
            "schema_version": 2,
            "release_id": "p003-1.0.0",
            "version": "1.0.0",
            "project_id": 3,
            "scenario": {"templates": {"p003-image-test": {"template_id": 200, "template_node": "proxmox4"}}, "boxes": [{
                "key": "t254-web", "team_id": "254", "name": "web",
                "ip_last_octet": 20, "template": "p003-image-test", "source_template": "approved-base", "source_vm_id": 115, "node_name": "proxmox4",
                "cores": 2, "memory_mb": 4096, "full_clone": False, "started": True,
                "health_checks": [{"type": "tcp", "port": 80, "timeout": 10}]
            }]}
        }
        release["integrity_sha256"] = crange.hashlib.sha256(
            json.dumps(release, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        approvals = json.loads((self.project / "approved-scenario-templates.json").read_text())
        approvals.update(release["scenario"]["templates"])
        (self.project / "approved-scenario-templates.json").write_text(json.dumps(approvals))
        dest = crange.manifest_path(3, "1.0.0")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(release, indent=2) + "\n")
        return dest

    def test_release_integrity_detects_tampering(self):
        path = self.write_release()
        self.assertEqual(crange.load_release(3, "1.0.0")["release_id"], "p003-1.0.0")
        path.write_text(path.read_text().replace("p003-image-test", "different-image"))
        with self.assertRaisesRegex(crange.CrangeError, "integrity check"):
            crange.load_release(3, "1.0.0")

    def test_release_rejects_moved_template_alias(self):
        self.write_release()
        (self.project / "approved-scenario-templates.json").write_text(
            json.dumps({"p003-image-test": {"template_id": 999, "template_node": "proxmox4"}})
        )
        release = crange.load_release(3, "1.0.0")
        with self.assertRaisesRegex(crange.CrangeError, "pinned release reference"):
            crange.release_scenario_boxes(self.project, release, "1", "r1")

    def test_test_clone_lifecycle_uses_temporary_state(self):
        self.write_release()
        args = SimpleNamespace(project="3", version="1.0.0")
        with patch.object(crange, "verify_release_images"):
            crange.cmd_release_test_prepare(args)
        cfg = json.loads((self.project / "crange.auto.tfvars.json").read_text())
        test_key = next(iter(cfg["scenario_boxes"]))
        self.assertEqual(cfg["scenario_boxes"][test_key]["team_id"], "254")
        self.assertEqual(cfg["scenario_boxes"][test_key]["ip_last_octet"], 90)
        crange.cmd_release_test_cleanup(args)
        cfg = json.loads((self.project / "crange.auto.tfvars.json").read_text())
        self.assertFalse(cfg["scenario_boxes"])
        with patch.object(crange, "verify_release_images"):
            with self.assertRaisesRegex(crange.CrangeError, "no recorded passing"):
                crange.cmd_release_deploy(SimpleNamespace(project="3", version="1.0.0", teams=[1]))

    def test_capture_freezes_guest_disk_and_pins_image(self):
        cfg = crange.json_config(self.project)
        cfg["scenario_boxes"] = {"t254-web": {
            "team_id": "254", "name": "web", "ip_last_octet": 20,
            "template": "approved-base", "node_name": "proxmox4", "cores": 2,
            "memory_mb": 4096, "full_clone": False, "started": True,
            "health_checks": [{"type": "tcp", "port": 22, "timeout": 10}]
        }}
        crange.save_config(self.project, cfg)
        class FakeAPI:
            def __init__(self):
                self.events = []
                self.state = "running"
            def vm(self, node, vmid):
                return {"name": "p003t254-web", "template": 0}
            def status(self, node, vmid):
                return self.state
            def shutdown(self, node, vmid, address):
                self.events.append("shutdown")
                self.state = "stopped"
            def capture_image(self, node, vmid, name, description):
                self.events.append("capture")
                assert self.state == "stopped"
                return 200
            def start(self, node, vmid):
                self.events.append("start")
                self.state = "running"
        api = FakeAPI()
        outputs = {"scenario_boxes": {"value": {"t254-web": {"id": 115, "address": "10.3.254.20"}}}}
        with patch.object(crange, "output_json", return_value=outputs), \
             patch.object(crange, "check_endpoint"), \
             patch.object(crange, "ProxmoxAPI", return_value=api), \
             patch.object(crange, "run", return_value=SimpleNamespace(stdout="revision\n")):
            crange.cmd_release_capture(SimpleNamespace(project="3", version="2.0.0"))
        release = crange.load_release(3, "2.0.0")
        box = release["scenario"]["boxes"][0]
        alias = box["template"]
        self.assertEqual(api.events, ["shutdown", "capture", "start"])
        self.assertEqual(box["source_template"], "approved-base")
        self.assertEqual(box["source_vm_id"], 115)
        self.assertEqual(release["scenario"]["templates"][alias]["template_id"], 200)
        self.assertEqual(json.loads((self.project / "approved-scenario-templates.json").read_text())[alias]["template_id"], 200)
        with self.assertRaisesRegex(crange.CrangeError, "already exists"):
            crange.cmd_release_capture(SimpleNamespace(project="3", version="2.0.0"))

    def test_project_init_copies_scenario_template_allowlist(self):
        crange.cmd_project_init(SimpleNamespace(project="5", transit="10.60.2.50/24"))
        created = crange.ENVS / "project-005"
        self.assertTrue((created / "approved-scenario-templates.json").is_file())
        cfg = json.loads((created / "crange.auto.tfvars.json").read_text())
        self.assertEqual(cfg["project_id"], 5)

    def test_nonstandard_environment_is_rejected(self):
        old = crange.ENVS / "project-002"
        old.mkdir()
        (old / "main.tf").write_text('variable "project_id" { default = 2 }\n')
        with self.assertRaisesRegex(crange.CrangeError, "no standardized"):
            crange.json_config(old)
        self.assertFalse((old / "crange.auto.tfvars.json").exists())


if __name__ == "__main__":
    unittest.main()
