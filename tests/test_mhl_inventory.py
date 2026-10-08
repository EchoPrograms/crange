"""Fail-closed selection tests for the Project 100 MHL inventory adapter."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/generate-mhl-inventory.py"
SPEC = importlib.util.spec_from_file_location("generate_mhl_inventory", SCRIPT)
ADAPTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADAPTER)


def fixture_inventory(network_id=254, project=100):
    names = {
        "mhl-db01": ("MHL-DB01", 30),
        "mhl-app01": ("MHL-APP01", 40),
        "mhl-ci01": ("MHL-CI01", 50),
        "mhl-bkp01": ("MHL-BKP01", 60),
    }
    common = {
        "ansible_connection": "ssh",
        "ansible_user": "user",
        "ansible_become": True,
        "ansible_ssh_private_key_file": "/home/crange/.ssh/id_ed25519_range",
        "ansible_python_interpreter": "/usr/bin/python3",
    }
    proxy = 'ssh -W %h:%p root@10.100.0.1 -o ProxyCommand="ssh -W %h:%p root@10.60.2.1"'
    parent_ssh = 'ssh -o ProxyCommand="ssh -W %h:%p root@10.60.2.1"'
    hosts = {}
    scoped = {}
    for key, (hostname, last_octet) in names.items():
        # Matches generate-project-inventory.py's pNNN_ct_<Terraform key>
        # and crange.py's release_scenario_containers <prefix>-<name>-tNNN.
        guest_key = (
            f"t254-{key}" if network_id == 254
            else f"r7-{key}-t{network_id:03d}"
        )
        name = f"p{project:03d}_ct_{guest_key}"
        host = {"ansible_host": f"10.100.{network_id}.{last_octet}"}
        if key == "mhl-app01":
            host["ansible_ssh_common_args"] = proxy
        hosts[name] = host
        scoped[name] = host
    scope = "staging_containers" if network_id == 254 else f"team_{network_id:03d}_containers"
    return {"all": {"vars": {"ansible_ssh_common_args": parent_ssh}, "children": {
        "scenario_containers": {
            "vars": common, "hosts": hosts, "children": {scope: {"hosts": scoped}},
        },
    }}}


class MhlInventoryTests(unittest.TestCase):
    def test_selects_exact_four_guests_and_preserves_ssh_values(self):
        selected = ADAPTER.selected_hosts(fixture_inventory(), 254)
        self.assertEqual(set(selected), {
            "mhl_databases", "mhl_applications", "mhl_build", "mhl_backups",
        })
        app = selected["mhl_applications"]["hosts"]["MHL-APP01"]
        self.assertEqual(app["ansible_host"], "10.100.254.40")
        self.assertEqual(app["ansible_user"], "user")
        self.assertEqual(app["ansible_ssh_private_key_file"], "/home/crange/.ssh/id_ed25519_range")
        self.assertEqual(
            app["ansible_ssh_common_args"],
            'ssh -W %h:%p root@10.100.0.1 -o ProxyCommand="ssh -W %h:%p root@10.60.2.1"',
        )
        db = selected["mhl_databases"]["hosts"]["MHL-DB01"]
        self.assertEqual(db["ansible_ssh_common_args"], 'ssh -o ProxyCommand="ssh -W %h:%p root@10.60.2.1"')

    def test_refuses_missing_deployed_expected_guest(self):
        inventory = fixture_inventory()
        del inventory["all"]["children"]["scenario_containers"]["hosts"]["p100_ct_t254-mhl-ci01"]
        with self.assertRaisesRegex(ADAPTER.InventoryError, "p100_ct_t254-mhl-ci01"):
            ADAPTER.selected_hosts(inventory, 254)

    def test_refuses_wrong_project(self):
        with self.assertRaisesRegex(ADAPTER.InventoryError, "Project 100"):
            ADAPTER.selected_hosts(fixture_inventory(project=3), 254)

    def test_regular_team_requires_explicit_network_and_selects_matching_scope(self):
        selected = ADAPTER.selected_hosts(fixture_inventory(network_id=2), 2)
        db = selected["mhl_databases"]["hosts"]["MHL-DB01"]
        self.assertEqual(db["ansible_host"], "10.100.2.30")
        self.assertEqual(db["mhl_network_id"], 2)
        with self.assertRaises(ADAPTER.InventoryError):
            ADAPTER.selected_hosts(fixture_inventory(), 2)

    def test_empty_current_canonical_inventory_is_refused(self):
        empty = {"all": {"children": {"scenario_containers": {
            "vars": {"ansible_user": "user"}, "hosts": {}, "children": {},
        }}}}
        with self.assertRaisesRegex(ADAPTER.InventoryError, "expected exactly one deployed guest"):
            ADAPTER.selected_hosts(empty, 254)

    def test_cli_emits_json_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "canonical.json"
            output = Path(tmp) / "mhl.json"
            source.write_text(json.dumps(fixture_inventory()))
            import subprocess
            result = subprocess.run(
                ["python3", str(SCRIPT), "--input", str(source), "--output", str(output)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(output.read_text())
            self.assertEqual(len(document["all"]["children"]), 4)


if __name__ == "__main__":
    unittest.main()
