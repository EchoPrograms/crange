"""Windows configure resolves exact applied identity and keeps passwords private."""

import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import scripts.crange as crange


def windows_inventory(network_id=254):
    return {"all": {"children": {"windows_guests": {
        "vars": {"ansible_connection": "winrm"},
        "hosts": {"p100_vm_mhl-dc01": {
            "ansible_host": f"10.100.{network_id}.10",
            "crange_project_id": 100,
            "crange_box_key": "mhl-dc01",
            "crange_guest_id": 8910,
            "crange_image_id": "mhl-windows-image-v1",
            "crange_template": "mhl-windows-v1",
            "crange_template_id": 115,
            "crange_template_node": "proxmox4",
            "crange_guest_network_id": str(network_id),
        }},
    }}}}


def applied_placements():
    return {"vm:mhl-dc01": {
        "guest_id": 8910,
        "image_id": "mhl-windows-image-v1",
        "declared_template": "mhl-windows-v1",
        "template_id": 115,
        "template_node": "proxmox4",
        "node_name": "proxmox4",
    }}


class WindowsConfigureReadinessTests(unittest.TestCase):
    def test_staging_windows_configure_initializes_only_selected_team(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory_path = root / 'ansible/inventory/generated/project-100.json'
            inventory_path.parent.mkdir(parents=True)
            project = root / 'project-100'
            project.mkdir()
            document = windows_inventory()
            host = dict(document['all']['children']['windows_guests']['hosts']['p100_vm_mhl-dc01'])
            host.update(crange_box_key='live', crange_guest_id=8911, crange_guest_network_id='1', ansible_host='10.100.1.10')
            document['all']['children']['windows_guests']['hosts']['p100_vm_live'] = host
            placements = applied_placements()
            placements['vm:live'] = {**placements['vm:mhl-dc01'], 'guest_id':8911}
            (project / 'crange-applied-placements.json').write_text(json.dumps(placements))
            def generate(*args, **kwargs):
                inventory_path.write_text(json.dumps(document))
            with patch.object(crange, 'ROOT', root), \
                 patch.object(crange, 'project_dir', return_value=project), \
                 patch.object(crange, 'json_config', return_value={'scenario_boxes': {'mhl-dc01': {'team_id':'254'}, 'live': {'team_id':'1'}}}), \
                 patch.object(crange, 'run', side_effect=generate), \
                 patch.object(crange, 'ProxmoxAPI', return_value=object()), \
                 patch('crange_windows_guest_initialize.initialize') as initialize, \
                 patch('crange_windows_guest_admin.resolve', return_value={'username':'Administrator','password':'test-only-secret-value'}), \
                 patch.object(crange, '_run_windows_guest_baseline') as baseline:
                crange.cmd_configure(SimpleNamespace(project=100, target='windows', yes=True, team_scope=254))
            initialize.assert_called_once_with(__import__('unittest.mock', fromlist=['ANY']).ANY, 100, 'mhl-dc01')
            self.assertEqual(set(baseline.call_args.args[0]['all']['children']['windows_guests']['hosts']), {'p100_vm_mhl-dc01'})

    def test_management_routes_do_not_configure_other_team_routers(self):
        inventory = {'all': {'children': {'routers': {'children': {
            'project_routers': {'hosts': {'p100_router': {}}},
            'team_routers': {'hosts': {'p100t001_router': {}, 'p100t002_router': {}}},
            'staging_routers': {'hosts': {'p100t254_router': {}}},
            'custom_routers': {'hosts': {'stage_custom': {'router_owner_team_id':254}, 'live_custom': {'router_owner_team_id':1}}}
        }}}}}
        with patch.object(crange, 'ansible') as ansible:
            crange._run_windows_management_routes(SimpleNamespace(project=100), inventory, 254)
        self.assertEqual([call.args[2] for call in ansible.call_args_list], [['p100_router'], ['p100t254_router'], ['stage_custom']])

    def test_global_windows_configure_accepts_non_mhl_team_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "crange-applied-placements.json").write_text(json.dumps(applied_placements()))
            with patch("crange_windows_guest_admin.resolve", return_value={
                "username": "Administrator", "password": "test-only-secret-value",
            }) as resolve:
                result = crange._windows_configure_credentials(100, windows_inventory(101), path)
            self.assertIn("p100_vm_mhl-dc01", result)
            resolve.assert_called_once()

    def test_credentials_resolve_only_for_exact_applied_guest_and_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "crange-applied-placements.json").write_text(json.dumps(applied_placements()))
            expected = {"username": "Administrator", "password": "test-only-secret-value"}
            with patch("crange_windows_guest_admin.resolve", return_value=expected) as resolve:
                credentials = crange._windows_configure_credentials(100, windows_inventory(), path)
            self.assertEqual(credentials, {"p100_vm_mhl-dc01": expected})
            resolve.assert_called_once_with(
                {"project_id": 100, "box_key": "mhl-dc01"},
                project_id=100, box_key="mhl-dc01", guest_id=8910,
                image_id="mhl-windows-image-v1",
            )

    def test_guest_or_image_mismatch_fails_before_credential_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            placements = applied_placements()
            placements["vm:mhl-dc01"]["image_id"] = "stale-image"
            (path / "crange-applied-placements.json").write_text(json.dumps(placements))
            with patch("crange_windows_guest_admin.resolve") as resolve:
                with self.assertRaisesRegex(crange.CrangeError, "does not match generated guest/image"):
                    crange._windows_configure_credentials(100, windows_inventory(), path)
            resolve.assert_not_called()

    def test_missing_private_credential_fails_without_exposing_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "crange-applied-placements.json").write_text(json.dumps(applied_placements()))
            with patch("crange_windows_guest_admin.resolve", side_effect=ValueError("missing")):
                with self.assertRaises(crange.CrangeError) as caught:
                    crange._windows_configure_credentials(100, windows_inventory(), path)
            self.assertNotIn("password", str(caught.exception).lower())
            self.assertIn("worker-private Windows credential", str(caught.exception))

    def test_plaintext_password_in_canonical_inventory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            inventory = windows_inventory()
            inventory["all"]["children"]["windows_guests"]["children"] = {
                "windows_vms": {"vars": {"ansible_password": "must-not-be-here"}}
            }
            with patch("crange_windows_guest_admin.resolve") as resolve:
                with self.assertRaisesRegex(crange.CrangeError, "worker-private guest credential store"):
                    crange._windows_configure_credentials(100, inventory, path)
            resolve.assert_not_called()

    def test_baseline_uses_temporary_private_inventory_and_removes_it(self):
        inventory = windows_inventory()
        credential = {"username": "Administrator", "password": "test-only-secret-value"}
        observations = {}

        def inspect_run(command, **kwargs):
            inventory_path = Path(command[command.index("-i") + 1])
            observations["mode"] = inventory_path.stat().st_mode & 0o777
            observations["document"] = json.loads(inventory_path.read_text())
            observations["command"] = command

        with patch.object(crange, "run", side_effect=inspect_run):
            crange._run_windows_guest_baseline(inventory, {"p100_vm_mhl-dc01": credential})
        self.assertEqual(observations["mode"], 0o600)
        self.assertEqual(observations["document"]["all"]["children"]["windows_guests"]["hosts"]["p100_vm_mhl-dc01"]["ansible_password"], credential["password"])
        self.assertNotIn(credential["password"], " ".join(observations["command"]))
        self.assertFalse(Path(observations["command"][observations["command"].index("-i") + 1]).exists())

    def test_boxes_preflights_missing_applied_binding_before_route_sync(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory_path = root / "ansible/inventory/generated/project-100.json"
            inventory_path.parent.mkdir(parents=True)

            def generate(command, **kwargs):
                inventory_path.write_text(json.dumps(windows_inventory()))

            with patch.object(crange, "ROOT", root), \
                    patch.object(crange, "project_dir", return_value=root), \
                    patch.object(crange, "run", side_effect=generate), \
                    patch.object(crange, "cmd_route_sync") as route_sync:
                with self.assertRaisesRegex(crange.CrangeError, "crange-applied-placements.json"):
                    crange.cmd_configure(SimpleNamespace(project=100, target="boxes", yes=True))
            route_sync.assert_not_called()

    def test_windows_target_runs_only_fixed_baseline_after_bound_identity_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory_path = root / "ansible/inventory/generated/project-100.json"
            inventory_path.parent.mkdir(parents=True)
            project = root / "terraform/environments/project-100"
            project.mkdir(parents=True)
            (project / "crange-applied-placements.json").write_text(json.dumps(applied_placements()))
            observed = {}

            def run_command(command, **kwargs):
                if command[0] == "ansible-playbook":
                    private_inventory = Path(command[command.index("-i") + 1])
                    observed["private_mode"] = private_inventory.stat().st_mode & 0o777
                    observed["private_inventory"] = json.loads(private_inventory.read_text())
                    observed["playbook"] = Path(command[command.index("--limit") - 1]).name
                    return
                inventory_path.write_text(json.dumps(windows_inventory()))

            with patch.object(crange, "ROOT", root), \
                    patch.object(crange, "project_dir", return_value=project), \
                    patch.object(crange, "run", side_effect=run_command), \
                    patch("crange_windows_guest_admin.resolve", return_value={
                        "username": "Administrator", "password": "test-only-secret-value",
                    }), \
                    patch("crange_windows_guest_initialize.initialize", return_value={"initialized": False}), \
                    patch.object(crange, "ProxmoxAPI", return_value=object()), \
                    patch.object(crange, "cmd_route_sync") as route_sync, \
                    patch.object(crange, "ansible") as generic_ansible:
                crange.cmd_configure(SimpleNamespace(project=100, target="windows", yes=True))

            route_sync.assert_not_called()
            generic_ansible.assert_not_called()
            self.assertEqual(observed["playbook"], "crange-guests.yml")
            self.assertEqual(observed["private_mode"], 0o600)
            self.assertFalse(observed["private_inventory"]["all"]["children"]["windows_guests"]["vars"].get("ansible_password"))
            self.assertEqual(
                observed["private_inventory"]["all"]["children"]["windows_guests"]["hosts"]["p100_vm_mhl-dc01"]["ansible_password"],
                "test-only-secret-value",
            )

    def test_all_is_not_blocked_when_no_windows_guest_is_in_canonical_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory_path = root / "ansible/inventory/generated/project-100.json"
            inventory_path.parent.mkdir(parents=True)

            def generate(command, **kwargs):
                inventory_path.write_text(json.dumps({"all": {"children": {"windows_guests": {
                    "hosts": {},
                }}}}))

            with patch.object(crange, "ROOT", root), \
                    patch.object(crange, "project_dir", return_value=root), \
                    patch.object(crange, "run", side_effect=generate), \
                    patch.object(crange, "cmd_route_sync") as route_sync, \
                    patch.object(crange, "ansible") as ansible:
                crange.cmd_configure(SimpleNamespace(project=100, target="all", yes=True))
            route_sync.assert_called_once()
            self.assertTrue(ansible.called)

    def test_windows_target_reports_missing_declared_guests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory_path = root / "ansible/inventory/generated/project-100.json"
            inventory_path.parent.mkdir(parents=True)

            def generate(command, **kwargs):
                inventory_path.write_text(json.dumps({"all": {"children": {"windows_guests": {
                    "hosts": {},
                }}}}))

            with patch.object(crange, "ROOT", root), \
                    patch.object(crange, "project_dir", return_value=root), \
                    patch.object(crange, "run", side_effect=generate):
                with self.assertRaisesRegex(crange.CrangeError, "No declared Windows guests"):
                    crange.cmd_configure(SimpleNamespace(project=100, target="windows", yes=True))


if __name__ == "__main__":
    unittest.main()
