#!/usr/bin/env python3
"""Generate an Ansible JSON inventory from an applied Terraform environment."""
import argparse
import json
from pathlib import Path
import subprocess

KEY = "/home/crange/.ssh/id_ed25519_range"
PROJECT_ROUTER = "p{project:03d}_router"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--terraform-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    proc = subprocess.run(
        ["terraform", "output", "-json"], cwd=args.terraform_dir,
        check=True, text=True, capture_output=True
    )
    outputs = json.loads(proc.stdout)
    def val(name):
        return outputs[name]["value"]

    project_id = int(val("project_id"))
    routers = val("routers")
    jumps = val("jump_boxes")
    scenario_hosts = []
    for output_name in ("scenario_boxes", "scenario_containers"):
        for box in (val(output_name) or {}).values():
            if box.get("address") and box.get("guac_protocol", "ssh") == "ssh":
                scenario_hosts.append({"address": box["address"],
                                       "network_id": str(box.get("network_id", box.get("team_id")))})
    custom_networks = routers.get("custom_networks", {})
    project_router_ip = routers["project"]["address"]
    project_network = f"10.{project_id}.0.0/16"
    project_downstream = f"10.{project_id}.0.1"

    project_routes = [
        {"network": item["lan"], "gateway": item["address"]}
        for item in routers["teams"].values()
    ]
    staging = routers.get("staging")
    if staging:
        project_routes.append({"network": staging["lan"], "gateway": staging["address"]})
    for net in custom_networks.values():
        owner = str(net["owner_team_id"])
        parent = staging if owner == "254" else routers["teams"][owner]
        project_routes.append({"network": net["lan"], "gateway": parent["address"]})

    groups = {
        "routers": {
            "vars": {
                "ansible_connection": "ssh",
                "ansible_user": "root",
                "ansible_become": False,
                "ansible_ssh_private_key_file": KEY,
                "ansible_python_interpreter": "/usr/bin/python3",
            },
            "children": {
                "project_routers": {
                    "hosts": {
                        PROJECT_ROUTER.format(project=project_id): {
                            "ansible_host": project_router_ip,
                            "router_forward_source": project_network,
                            "router_team_network": project_network,
                            "router_downstream_interface": "eth1",
                        "router_routes": project_routes,
                        # The project router admits Guacamole SSH into the
                        # project aggregate. Child routers restrict it to the
                        # declared scenario endpoints below.
                        "router_guac_ssh_upstream": True,
                        }
                    }
                },
                "team_routers": {"hosts": {}},
                "staging_routers": {"hosts": {}},
                "custom_routers": {"hosts": {}},
            },
        },
        "jumpboxes": {
            "vars": {
                "ansible_connection": "ssh",
                "ansible_user": "user",
                "ansible_become": True,
                "ansible_ssh_private_key_file": KEY,
                "ansible_python_interpreter": "/usr/bin/python3",
            },
            "hosts": {},
            "children": {},
        },
    }

    def old_custom_routes(container_id):
        path = Path("/var/lib/crange/custom-routes") / f"{project_id:03d}-{container_id}.json"
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return []

    common_ssh = (
        "-o IdentitiesOnly=yes -o StrictHostKeyChecking=no "
        "-o ControlMaster=no -o ControlPersist=no -o ControlPath=none "
        "-o ServerAliveInterval=5 -o ServerAliveCountMax=3 -o ConnectTimeout=10"
    )
    proxy = (
        f'ssh {common_ssh} -i {KEY} -W %h:%p root@{project_router_ip}'
    )
    # Read each router's recorded subnet policy so removed project-owned rules can be deleted.
    for team_id, team in sorted(routers["teams"].items(), key=lambda pair: int(pair[0])):
        team_number = int(team_id)
        host = f"p{project_id:03d}t{team_number:03d}_router"
        lan = team["lan"]
        team_gateway = f"10.{project_id}.{team_number}.1"
        groups["routers"]["children"]["team_routers"]["hosts"][host] = {
            "ansible_host": team["address"],
            "ansible_ssh_common_args": f'{common_ssh} -o ProxyCommand="{proxy}"',
            "router_project_gateway": project_downstream,
            "router_forward_source": lan,
            "router_team_network": lan,
            "router_downstream_interface": "eth1",
            "router_dhcp_gateway": team_gateway,
            "router_dhcp_start": f"10.{project_id}.{team_number}.101",
            "router_dhcp_end": f"10.{project_id}.{team_number}.254",
            "router_custom_routes": [
                {"network": net["lan"], "gateway": net["address"]}
                for net in custom_networks.values()
                if str(net["owner_team_id"]) == str(team_number)
            ],
            "router_managed_custom_sources": old_custom_routes(team["id"]),
            "router_forward_sources": [lan] + [
                net["lan"] for net in custom_networks.values()
                if str(net["owner_team_id"]) == str(team_number)
            ],
            "router_routes": [
                {"network": net["lan"], "gateway": net["address"]}
                for net in custom_networks.values()
                if str(net["owner_team_id"]) == str(team_number)
            ],
            "router_guac_ssh_targets": [
                box["address"] for box in scenario_hosts
                if box["network_id"] == str(team_number) or any(
                    str(net["network_id"]) == box["network_id"] and str(net["owner_team_id"]) == str(team_number)
                    for net in custom_networks.values()
                )
            ],
        }

    if staging:
        host = f"p{project_id:03d}t254_router"
        lan = staging["lan"]
        gateway = f"10.{project_id}.254.1"
        groups["routers"]["children"]["staging_routers"]["hosts"][host] = {
            "ansible_host": staging["address"],
            "ansible_ssh_common_args": f'{common_ssh} -o ProxyCommand="{proxy}"',
            "router_project_gateway": project_downstream,
            "router_forward_source": lan,
            "router_team_network": lan,
            "router_downstream_interface": "eth1",
            "router_dhcp_gateway": gateway,
            "router_dhcp_start": f"10.{project_id}.254.101",
            "router_dhcp_end": f"10.{project_id}.254.254",
            "router_custom_routes": [
                {"network": net["lan"], "gateway": net["address"]}
                for net in custom_networks.values()
                if str(net["owner_team_id"]) == "254"
            ],
            "router_managed_custom_sources": old_custom_routes(staging["id"]),
            "router_forward_sources": [lan] + [
                net["lan"] for net in custom_networks.values()
                if str(net["owner_team_id"]) == "254"
            ],
            "router_routes": [
                {"network": net["lan"], "gateway": net["address"]}
                for net in custom_networks.values()
                if str(net["owner_team_id"]) == "254"
            ],
            "router_guac_ssh_targets": [
                box["address"] for box in scenario_hosts
                if box["network_id"] == "254" or any(
                    str(net["network_id"]) == box["network_id"] and str(net["owner_team_id"]) == "254"
                    for net in custom_networks.values()
                )
            ],
        }

    for key, net in sorted(custom_networks.items()):
        owner = str(net["owner_team_id"])
        host = f"p{project_id:03d}c{int(net['network_id']):03d}_router"
        parent_gateway = f"10.{project_id}.{owner}.1"
        groups["routers"]["children"]["custom_routers"]["hosts"][host] = {
            "ansible_host": net["address"],
            "router_owner_team_id": owner,
            "ansible_ssh_common_args": f'{common_ssh} -o ProxyCommand="{proxy}"',
            "router_project_gateway": project_downstream,
            "router_forward_source": net["lan"],
            "router_forward_sources": [net["lan"]],
            "router_team_network": net["lan"],
            "router_downstream_interface": "eth1",
            "router_dhcp_gateway": f"10.{project_id}.{net['network_id']}.1",
            "router_dhcp_start": f"10.{project_id}.{net['network_id']}.101",
            "router_dhcp_end": f"10.{project_id}.{net['network_id']}.254",
            "router_routes": [],
            "router_custom_routes": [],
            "router_guac_ssh_targets": [
                box["address"] for box in scenario_hosts
                if box["network_id"] == str(net["network_id"])
            ],
        }

    for key, jump in jumps.items():
        address = jump["address"]
        network_vnet = jump.get("network_vnet", f"p{project_id:03d}t{int(jump['team_id']):03d}")
        jump_number = int(jump.get("jump_number", int(address.split(".")[3]) - 10))
        name = f"{network_vnet}_jump{jump_number:02d}"
        groups["jumpboxes"]["hosts"][name] = {"ansible_host": address}
        scope = "staging_jumpboxes" if int(jump["team_id"]) == 254 else f"team_{int(jump['team_id']):03d}_jumpboxes"
        groups["jumpboxes"]["children"].setdefault(scope, {"hosts": {}})["hosts"][name] = {"ansible_host": address}

    document = {"all": {"children": groups}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2) + "\n")
    args.output.chmod(0o640)
    router_count = len(routers["teams"]) + 1 + int(bool(staging)) + len(custom_networks)
    print(f"Wrote {args.output} with {router_count} routers and {len(jumps)} jump boxes")


if __name__ == "__main__":
    main()
