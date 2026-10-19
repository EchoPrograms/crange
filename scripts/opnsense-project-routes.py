#!/usr/bin/env python3
"""Idempotently ensure the project's OPNsense gateway and static route."""
import base64
import ipaddress
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request


BASE = os.environ.get("OPNSENSE_URL", "https://10.60.2.1").rstrip("/")
KEY = os.environ.get("key") or os.environ.get("OPNSENSE_API_KEY")
SECRET = os.environ.get("secret") or os.environ.get("OPNSENSE_API_SECRET")
if not KEY or not SECRET:
    raise SystemExit("Source ~/.config/crange/opnsense-api.txt first; key and secret are required.")

if os.environ.get("OPNSENSE_INSECURE", "0").lower() in ("1", "true", "yes"):
    TLS = ssl._create_unverified_context()
else:
    ca = os.environ.get("OPNSENSE_CA_BUNDLE")
    TLS = ssl.create_default_context(cafile=ca or None)

AUTH = base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()


def api(method, path, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    headers = {"Authorization": f"Basic {AUTH}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        BASE + path,
        data=body,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, context=TLS, timeout=20) as response:
            result = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:1000]
        raise RuntimeError(f"OPNsense API {method} {path} returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OPNsense API connection failed for {path}: {exc.reason}") from exc
    if isinstance(result, dict) and result.get("errorMessage"):
        raise RuntimeError(f"OPNsense API rejected {path}: {result['errorMessage']}")
    if isinstance(result, dict) and "result" in result and result["result"] not in ("saved", "ok", "deleted"):
        raise RuntimeError(f"OPNsense API did not save {path}: {result}")
    return result


def selected_choice(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for name, option in value.items():
            if isinstance(option, dict) and str(option.get("selected", "0")) == "1":
                return name
    return None


def occupied_transit_addresses():
    """Return transit-side addresses already present in OPNsense gateways/routes."""
    occupied = set()
    gateway_doc = api("GET", "/api/routing/settings/get")
    gateways = gateway_doc.get("gateways", {}).get("gateway_item", {})
    values = gateways.values() if isinstance(gateways, dict) else gateways
    names = {}
    transit = ipaddress.ip_network("10.60.2.0/24")
    for item in values:
        if not isinstance(item, dict):
            continue
        name, raw = item.get("name"), item.get("gateway")
        try:
            address = ipaddress.ip_address(str(raw).split("%", 1)[0])
        except ValueError:
            continue
        if address in transit:
            occupied.add(str(address))
            if name:
                names[name] = str(address)

    route_doc = api("GET", "/api/routes/routes/get")
    routes = route_doc.get("route", {}).get("route", {})
    values = routes.values() if isinstance(routes, dict) else routes
    for item in values:
        if not isinstance(item, dict):
            continue
        gateway = selected_choice(item.get("gateway"))
        raw = names.get(gateway, gateway)
        try:
            address = ipaddress.ip_address(str(raw).split("%", 1)[0])
        except ValueError:
            continue
        if address in transit:
            occupied.add(str(address))
    return sorted(occupied, key=ipaddress.ip_address)


def ensure(spec):
    pid = int(spec["project_id"])
    expected_network = f"10.{pid}.0.0/16"
    network = str(ipaddress.ip_network(spec["network"], strict=True))
    gateway_ip = str(ipaddress.ip_address(spec["gateway_address"]))
    if network != expected_network:
        raise RuntimeError(f"Refusing unexpected project route {network}; expected {expected_network}")
    if ipaddress.ip_address(gateway_ip) not in ipaddress.ip_network("10.60.2.0/24"):
        raise RuntimeError(f"Gateway {gateway_ip} is not on the shared transit subnet")
    if gateway_ip == "10.60.2.1":
        raise RuntimeError("Refusing OPNsense itself as the project-router gateway")

    gname = spec["gateway_name"]
    interface = spec["interface"]
    gateway_doc = api("GET", "/api/routing/settings/get")
    existing_gateways = gateway_doc.get("gateways", {}).get("gateway_item", {})
    gateway_uuid = None
    for uuid, item in existing_gateways.items():
        if item.get("name") == gname:
            gateway_uuid = uuid
            gw_ip = item.get("gateway")
            gw_if = selected_choice(item.get("interface"))
            if gw_ip != gateway_ip or gw_if != interface:
                raise RuntimeError(
                    f"Gateway name {gname} already exists as {gw_if}/{gw_ip}; "
                    f"expected {interface}/{gateway_ip}. Resolve the conflict in OPNsense first."
                )
            break
        if item.get("gateway") == gateway_ip:
            raise RuntimeError(
                f"Gateway IP {gateway_ip} already belongs to OPNsense gateway {item.get('name')}; "
                "refusing to create a duplicate."
            )

    # Check route conflicts before creating a gateway so refusal leaves no orphan.
    route_doc = api("GET", "/api/routes/routes/get")
    routes = route_doc.get("route", {}).get("route", {})
    route_items = routes.items() if isinstance(routes, dict) else enumerate(routes)
    matches = [(uuid, item) for uuid, item in route_items if item.get("network") == network]
    if len(matches) > 1:
        raise RuntimeError(f"Found multiple routes for {network}; refusing ambiguous changes.")
    if matches:
        current = matches[0][1]
        if selected_choice(current.get("gateway")) != gname and current.get("descr") != f"CRANGE managed project p{pid:03d}":
            raise RuntimeError(f"Route {network} is unmanaged or belongs to another project; refusing to overwrite it.")
    changed = False
    if gateway_uuid is None:
        payload = {"gateway_item": {
            "disabled": "0",
            "name": gname,
            "descr": f"CRANGE managed project p{pid:03d}",
            "interface": interface,
            "ipprotocol": "inet",
            "gateway": gateway_ip,
            "defaultgw": "0",
            "fargw": "0",
            "monitor_disable": "1",
            "monitor_noroute": "0",
            "monitor_killstates": "0",
            "monitor_killstates_priority": "0",
            "monitor": "",
            "force_down": "0",
            "nosync": "0",
            "priority": "255",
            "weight": "1",
        }}
        api("POST", "/api/routing/settings/add_gateway", payload)
        changed = True
        api("POST", "/api/routing/settings/reconfigure", {})
        gateway_doc = api("GET", "/api/routing/settings/get")
        existing_gateways = gateway_doc.get("gateways", {}).get("gateway_item", {})
        if not any(item.get("name") == gname and item.get("gateway") == gateway_ip
                   and selected_choice(item.get("interface")) == interface
                   for item in existing_gateways.values()):
            raise RuntimeError(f"OPNsense did not return the newly created gateway {gname}")
        print(f"Created OPNsense gateway {gname} -> {gateway_ip} on {interface}")
    else:
        print(f"Gateway {gname} already matches ({gateway_ip} on {interface})")

    # Gateway choices are populated through configd and may lag a new gateway.
    # Confirm its availability before a route write instead of submitting a
    # value the route model still considers invalid.
    if not matches or selected_choice(matches[0][1].get("gateway")) != gname:
        deadline = time.monotonic() + 30
        while True:
            choices = api("GET", "/api/routes/routes/getroute").get("route", {}).get("gateway", {})
            if isinstance(choices, dict) and gname in choices:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Gateway {gname} is saved but not yet available to the route API; retry route synchronization.")
            time.sleep(1)

    route_payload = {"route": {
        "network": network,
        "gateway": gname,
        "descr": f"CRANGE managed project p{pid:03d}",
        "enabled": "1",
    }}
    if not matches:
        api("POST", "/api/routes/routes/addroute", route_payload)
        changed = True
        print(f"Created OPNsense route {network} via {gname}")
    else:
        uuid, current = matches[0]
        current_gateway = selected_choice(current.get("gateway"))
        if current_gateway == gname:
            print(f"Route {network} already points to {gname}")
        elif str(current.get("descr", "")).startswith("CRANGE managed project "):
            api("POST", f"/api/routes/routes/setroute/{uuid}", route_payload)
            changed = True
            print(f"Updated managed OPNsense route {network} to use {gname}")
        else:
            raise RuntimeError(
                f"Route {network} already exists via {current_gateway} with description "
                f"{current.get('descr')!r}; refusing to overwrite an unmanaged route."
            )
    if changed:
        api("POST", "/api/routes/routes/reconfigure", {})
        print("OPNsense routing configuration applied")
    else:
        print("OPNsense routing already matches; no changes applied")



def remove_project_route(project_id, allow_unmanaged=False, dry_run=False):
    pid = int(project_id)
    if pid < 1 or pid > 255 or pid in (60, 61):
        raise RuntimeError("Invalid project ID")
    network = f"10.{pid}.0.0/16"
    gateway_name = f"P{pid:03d}"
    route_doc = api("GET", "/api/routes/routes/get")
    routes = route_doc.get("route", {}).get("route", {})
    route_items = routes.items() if isinstance(routes, dict) else enumerate(routes)
    matches = [(uuid, item) for uuid, item in route_items if item.get("network") == network]
    if not matches:
        print(f"No OPNsense route exists for {network}")
        return
    if len(matches) != 1:
        raise RuntimeError(f"Found {len(matches)} routes for {network}; refusing ambiguous cleanup")
    uuid, item = matches[0]
    current_gateway = selected_choice(item.get("gateway"))
    if current_gateway != gateway_name:
        raise RuntimeError(f"Route {network} points to {current_gateway}, expected {gateway_name}; refusing cleanup")
    if not allow_unmanaged and not str(item.get("descr", "")).startswith("CRANGE managed project "):
        raise RuntimeError(f"Route {network} is not marked CRANGE-managed; pass explicit allow_unmanaged only for reviewed legacy cleanup")
    other_refs = [
        route.get("network") for route in (routes.values() if isinstance(routes, dict) else routes)
        if route.get("network") != network and selected_choice(route.get("gateway")) == gateway_name
    ]
    if other_refs:
        raise RuntimeError(f"Gateway {gateway_name} is also used by other routes: {', '.join(map(str, other_refs))}")
    print(f"Removing OPNsense static route {network} via {gateway_name} (description {item.get('descr')!r})")
    if dry_run:
        return
    api("POST", f"/api/routes/routes/delroute/{uuid}")
    api("POST", "/api/routes/routes/reconfigure", {})
    after = api("GET", "/api/routes/routes/get").get("route", {}).get("route", {})
    after_routes = after.values() if isinstance(after, dict) else after
    if any(route.get("network") == network for route in after_routes):
        raise RuntimeError(f"OPNsense still reports a route for {network} after deletion")
    print(f"Removed and reconfigured OPNsense route {network}")

def retire_project_network(project_id, transit_address, interface, dry_run=False):
    """Remove only this exact automation-owned static route and gateway.

    API contract: https://docs.opnsense.org/development/api/core/routing.html
    Firewall policy is never modified by this workflow.
    """
    pid = int(project_id)
    if not 1 <= pid <= 255 or pid in (60, 61):
        raise RuntimeError('Invalid project ID')
    gateway_name = f'P{pid:03d}'
    address = str(ipaddress.ip_interface(transit_address).ip)
    if ipaddress.ip_address(address) not in ipaddress.ip_network('10.60.2.0/24') or address == '10.60.2.1':
        raise RuntimeError('Project gateway address is outside the shared transit network')
    gateways = api('GET', '/api/routing/settings/get').get('gateways', {}).get('gateway_item', {})
    matches = [(key, item) for key, item in gateways.items() if item.get('name') == gateway_name]
    if len(matches) > 1:
        raise RuntimeError('Project gateway ownership is ambiguous')
    if matches:
        key, item = matches[0]
        if (item.get('descr') != f'CRANGE managed project p{pid:03d}' or item.get('gateway') != address or
                selected_choice(item.get('interface')) != interface or
                any(str(item.get(flag, '0')) not in ('0','', 'None') for flag in ('upstream','defaultgw'))):
            routes = api('GET', '/api/routes/routes/get').get('route', {}).get('route', {})
            values = routes.values() if isinstance(routes, dict) else routes
            # A reused project number may collide with a historical manually
            # configured gateway. If this project's route was never installed,
            # that different-address, unmanaged gateway is outside its lifecycle.
            if (item.get('descr') != f'CRANGE managed project p{pid:03d}' and
                    item.get('gateway') != address and
                    not any(r.get('network') == f'10.{pid}.0.0/16' for r in values)):
                print(f'No current project route/gateway exists for p{pid:03d}; preserving unrelated legacy gateway {gateway_name}')
                return
            raise RuntimeError(f'Gateway {gateway_name} does not match this project’s managed address/interface; refusing to delete an unrelated gateway')
        # OPNsense 26.7 del_gateway validates interface, gateway-group and
        # static-route references atomically. Do not require broader group
        # administration privileges for the narrowly scoped route worker.
    remove_project_route(pid, dry_run=dry_run)
    if dry_run:
        print(f"Reviewed managed route/gateway cleanup for p{pid:03d}; no changes made")
        return
    if matches:
        key, _ = matches[0]
        api('POST', f'/api/routing/settings/del_gateway/{key}')
        api('POST', '/api/routing/settings/reconfigure', {})
        after = api('GET', '/api/routing/settings/get').get('gateways', {}).get('gateway_item', {})
        if any(row.get('name') == gateway_name for row in after.values()):
            raise RuntimeError('Project gateway remains after deletion')
        print(f'Removed managed OPNsense gateway {gateway_name}')


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "occupied-transit":
        print(json.dumps(occupied_transit_addresses()))
    elif len(sys.argv) >= 2 and sys.argv[1] == "ensure" and len(sys.argv) == 2:
        ensure(json.load(sys.stdin))
    elif len(sys.argv) in (5,6) and sys.argv[1] == "retire":
        retire_project_network(sys.argv[2], sys.argv[3], sys.argv[4], dry_run="--dry-run" in sys.argv[5:])
    elif len(sys.argv) >= 3 and sys.argv[1] == "remove":
        pid = int(sys.argv[2])
        allow = "--allow-unmanaged" in sys.argv[3:]
        dry = "--dry-run" in sys.argv[3:]
        remove_project_route(pid, allow_unmanaged=allow, dry_run=dry)
    else:
        raise SystemExit("Usage: opnsense-project-routes.py occupied-transit | terraform output -json opnsense_route | opnsense-project-routes.py ensure | opnsense-project-routes.py remove PROJECT_ID [--allow-unmanaged] [--dry-run]")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError) as exc:
        print(f"crange: OPNsense routing: {exc}", file=sys.stderr)
        raise SystemExit(2)
