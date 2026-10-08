"""Project 100's pending full scenario and participant assignments."""
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DRAFT = json.loads((ROOT / "docs/project-100/scenario-draft.json").read_text())


class ScenarioDraftTests(unittest.TestCase):
    def test_company_hosts_use_reserved_staging_slots(self):
        hosts = {item["key"]: item for item in DRAFT["service_hosts"]}
        self.assertEqual(set(hosts), {"mhl-dc01", "mhl-file01", "mhl-db01", "mhl-app01", "mhl-ci01", "mhl-bkp01"})
        self.assertEqual({key: hosts[key]["ip_last_octet"] for key in hosts}, {
            "mhl-dc01": 10, "mhl-file01": 20, "mhl-db01": 30,
            "mhl-app01": 40, "mhl-ci01": 50, "mhl-bkp01": 60,
        })
        self.assertEqual(DRAFT["team_networks"]["staging"]["cidr"], "10.100.254.0/24")

    def test_every_team_has_two_linux_and_two_windows_boxes(self):
        boxes = DRAFT["participant_boxes"]
        self.assertEqual(len(boxes), 20)
        for team in range(1, 6):
            group = [box for box in boxes if box["team_id"] == team]
            self.assertEqual(len(group), 4)
            self.assertEqual({(box["os"], box["ip_last_octet"]) for box in group}, {
                ("Linux Parrot", 11), ("Windows Server 2025", 12),
                ("Linux Parrot", 13), ("Windows Server 2025", 14),
            })
            linux = [box for box in group if box["os"] == "Linux Parrot"]
            windows = [box for box in group if box["os"].startswith("Windows")]
            self.assertTrue(all(box["services"] == ["ssh", "rdp"] for box in linux))
            self.assertTrue(all(box["services"] == ["rdp"] for box in windows))

    def test_ten_users_only_receive_their_own_team_pair(self):
        users = DRAFT["assessor_users"]
        boxes = {box["key"]: box for box in DRAFT["participant_boxes"]}
        self.assertEqual(len(users), 10)
        self.assertEqual(len({user["username"] for user in users}), 10)
        for user in users:
            self.assertEqual(len(user["assigned_boxes"]), 2)
            for key in user["assigned_boxes"]:
                self.assertEqual(boxes[key]["team_id"], user["team_id"])
                self.assertEqual(boxes[key]["assessor"], int(user["username"][-2:]))

    def test_windows_image_is_pending_and_debian_containers_are_declared(self):
        windows = [box for box in DRAFT["participant_boxes"] if box["os"].startswith("Windows")]
        windows_hosts = [host for host in DRAFT["service_hosts"] if host["os"].startswith("Windows")]
        self.assertTrue(all(box["image_id"] is None and box["status"] == "pending_image_publication" for box in windows))
        self.assertTrue(all(host["required_image"] is None for host in windows_hosts))
        linux_services = [host for host in DRAFT["service_hosts"] if host["os"] == "Debian 12 LXC"]
        self.assertTrue(all(host["status"] == "declared_not_applied" for host in linux_services))
        self.assertTrue(all(host["template_alias"] == "mhl-debian12-stock" for host in linux_services))
        self.assertTrue(all(not ("password" in key.lower()) for key in DRAFT))

    def test_ansible_example_inventory_matches_participant_addresses(self):
        inventory = (ROOT / "ansible/project-100-participant-inventory.example.yml").read_text()
        for box in DRAFT["participant_boxes"]:
            address = f"10.100.{box['team_id']}.{box['ip_last_octet']}"
            self.assertEqual(inventory.count(address), 1)


if __name__ == "__main__":
    unittest.main()
