#!/usr/bin/env python3
"""Select the deployed Project 100 MHL Linux guests from canonical inventory."""

import argparse
import copy
import json
from pathlib import Path


GUESTS = {
    "mhl-db01": ("mhl_databases", "MHL-DB01", 30),
    "mhl-app01": ("mhl_applications", "MHL-APP01", 40),
    "mhl-ci01": ("mhl_build", "MHL-CI01", 50),
    "mhl-bkp01": ("mhl_backups", "MHL-BKP01", 60),
}
ALLOWED_NETWORKS = (1, 2, 3, 4, 5, 254)


class InventoryError(ValueError):
    """Raised when canonical inventory cannot safely identify the target guests."""


def selected_hosts(document, network_id):
    """Return the four selected canonical guest records, or fail closed."""
    try:
        children = document["all"]["children"]
        containers = children["scenario_containers"]
        hosts = containers["hosts"]
        scopes = containers["children"]
    except (KeyError, TypeError) as exc:
        raise InventoryError("canonical inventory has no scenario_containers group") from exc

    prefix = "p100_ct_"
    if not any(name.startswith(prefix) for name in hosts):
        projects = sorted({name.split("_ct_", 1)[0] for name in hosts if "_ct_" in name})
        if projects:
            raise InventoryError("canonical inventory contains no Project 100 containers")

    scope_name = "staging_containers" if network_id == 254 else f"team_{network_id:03d}_containers"
    scope = scopes.get(scope_name, {})
    scoped_hosts = scope.get("hosts", {})
    all_vars = document["all"].get("vars", {})
    scope_vars = scope.get("vars", {})
    result = {}
    for key, (group, hostname, last_octet) in GUESTS.items():
        expected_key = f"t254-{key}" if network_id == 254 else None
        marker = key.lower().split("-")
        candidates = []
        for canonical_name, host_value in scoped_hosts.items():
            if not canonical_name.startswith(prefix):
                continue
            guest_key = canonical_name[len(prefix):]
            normalized_key = guest_key.lower().split("-")
            if expected_key is not None:
                name_matches = guest_key == expected_key
            else:
                # release_scenario_containers() builds keys as
                # <release-prefix>-<source-name>-tNNN.
                name_matches = (
                    len(normalized_key) >= len(marker) + 2
                    and any(normalized_key[i:i + len(marker)] == marker
                            for i in range(len(normalized_key) - len(marker) + 1))
                    and normalized_key[-1] == f"t{network_id:03d}"
                )
            if name_matches:
                candidates.append((canonical_name, host_value))
        if len(candidates) != 1:
            wanted = f"{prefix}{expected_key}" if expected_key else f"release key containing {'-'.join(marker)} and ending -t{network_id:03d}"
            raise InventoryError(
                f"expected exactly one deployed guest matching {wanted} in {scope_name}; inventory is incomplete"
            )
        canonical_name, scoped_value = candidates[0]
        source = hosts.get(canonical_name)
        if source is None:
            raise InventoryError(f"canonical host {canonical_name} is missing from scenario_containers")
        merged = copy.deepcopy(all_vars)
        merged.update(copy.deepcopy(containers.get("vars", {})))
        merged.update(copy.deepcopy(scope_vars))
        merged.update(copy.deepcopy(scoped_value))
        merged.update(copy.deepcopy(source))
        expected_address = f"10.100.{network_id}.{last_octet}"
        if merged.get("ansible_host") != expected_address:
            raise InventoryError(
                f"{canonical_name} has unexpected address; expected {expected_address}"
            )
        merged.update({
            "mhl_network_id": network_id,
            "mhl_guest_hostname": hostname,
        })
        result[group] = {"hosts": {hostname: merged}}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path,
        default=Path("ansible/inventory/generated/project-100.json"),
        help="canonical output of generate-project-inventory.py",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--network-id", type=int, choices=ALLOWED_NETWORKS, default=254,
        help="Project 100 network to select (defaults to Team 0 staging, 254)",
    )
    args = parser.parse_args()

    try:
        source = json.loads(args.input.read_text())
        groups = selected_hosts(source, args.network_id)
    except (OSError, json.JSONDecodeError, InventoryError) as exc:
        parser.exit(2, f"inventory selection refused: {exc}\n")

    project_vars = {
        "mhl_domain": "mhl.test",
        "mhl_db_ip": f"10.100.{args.network_id}.30",
        "mhl_app_ip": f"10.100.{args.network_id}.40",
        "mhl_customer_records": [
            {"id": 4101, "name": "Northwind Cold Storage"},
            {"id": 4102, "name": "Cedar Freight Cooperative"},
        ],
    }
    output = {"all": {"vars": project_vars, "children": groups}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    args.output.chmod(0o640)
    print(f"Wrote {args.output} for Project 100 network {args.network_id} (4 guests)")


if __name__ == "__main__":
    main()
