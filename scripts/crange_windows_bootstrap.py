"""Fixed QEMU Guest Agent bootstrap for an owned Windows jump VM.

Only trusted worker code calls this module. It has no command/script or
credential-path input; all guest identity and network values come from the
project's approved desired state and applied Terraform output.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import re
import time


CONTROLLER_IP = "10.60.0.20"
WINDOWS_DNS = "10.60.2.1"
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
VMID_PATTERN = re.compile(r"^[0-9]{1,9}$")
MAC_PATTERN = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


class WindowsBootstrapError(ValueError):
    """Fixed Windows bootstrap validation or guest operation failed."""


def _positive_int(value, message, minimum=1, maximum=999999999):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise WindowsBootstrapError(message)
    return value


def _network_spec(project_id, cfg, box):
    if isinstance(box, dict) is False:
        raise WindowsBootstrapError("Desired Windows box declaration is malformed.")
    try:
        team = int(box["team_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WindowsBootstrapError("Desired Windows box has no valid team.") from exc
    configured_teams = {int(item) for item in cfg.get("team_ids", [])}
    if team != 254 and team not in configured_teams:
        raise WindowsBootstrapError("Windows box team is not configured in this project.")
    network_key = box.get("network_key", "")
    if network_key:
        custom = cfg.get("custom_networks", {}).get(network_key)
        if not custom or int(custom.get("owner_team_id", -1)) != team:
            raise WindowsBootstrapError("Windows box custom network is missing or belongs to another team.")
        network_id = _positive_int(custom.get("network_id"), "Invalid Windows project network.", 1, 253)
        vnet = f"p{project_id:03d}c{network_id:03d}"
    else:
        network_id = team
        vnet = f"p{project_id:03d}t{network_id:03d}"
    if not 1 <= network_id <= 254:
        raise WindowsBootstrapError("Windows box network is outside the project address plan.")
    last_octet = _positive_int(box.get("ip_last_octet"), "Invalid Windows box host address.", 2, 100)
    address = ipaddress.IPv4Address(f"10.{project_id}.{network_id}.{last_octet}")
    network = ipaddress.IPv4Network(f"10.{project_id}.{network_id}.0/24")
    gateway = ipaddress.IPv4Address(f"10.{project_id}.{network_id}.1")
    dns = ipaddress.IPv4Address(WINDOWS_DNS)
    return {"team_id": team, "network_id": network_id, "vnet": vnet,
            "network": network, "address": address, "prefix_length": 24,
            "gateway": gateway, "dns_servers": [dns]}


def desired_target(project_id, box_key, cfg, outputs, approvals):
    """Validate desired Windows jump-box identity and derive network settings."""
    from crange_windows_guest_admin import reference

    _positive_int(project_id, "Invalid Windows project.", 1, 255)
    if not isinstance(box_key, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", box_key):
        raise WindowsBootstrapError("Invalid Windows desired box key.")
    boxes = cfg.get("scenario_boxes", {})
    box = boxes.get(box_key)
    if not isinstance(box, dict) or box.get("is_jumpbox") is not True:
        raise WindowsBootstrapError("Choose a declared Windows scenario jump box.")
    template = box.get("template")
    approval = approvals.get(template)
    if not isinstance(approval, dict):
        raise WindowsBootstrapError("Windows box template is not approved for this project.")
    profile = approval.get("profile")
    if (not isinstance(profile, dict) or not isinstance(profile.get("os_type"), str)
            or not profile["os_type"].startswith("win") or profile.get("cloud_init") is not False
            or profile.get("agent_enabled") is not True):
        raise WindowsBootstrapError("Approved template is not a QEMU-agent Windows base image.")
    image_id = approval.get("image_id")
    template_id = approval.get("template_id")
    template_node = approval.get("template_node")
    if not isinstance(image_id, str) or not image_id or isinstance(template_id, bool) or not isinstance(template_id, int):
        raise WindowsBootstrapError("Approved Windows image identity is incomplete.")
    if not isinstance(template_node, str) or not re.fullmatch(r"proxmox[1-6]", template_node):
        raise WindowsBootstrapError("Approved Windows image node is invalid.")
    credential_ref = box.get("windows_admin_credential_ref")
    if credential_ref != reference(project_id, box_key):
        raise WindowsBootstrapError("This Windows box has no matching protected administrator credential reference.")
    network = _network_spec(project_id, cfg, box)
    applied = outputs.get("scenario_boxes", {}).get("value", {}).get(box_key)
    if not isinstance(applied, dict):
        raise WindowsBootstrapError("Windows jump box is not present in applied Terraform state.")
    guest_id = applied.get("id")
    _positive_int(guest_id, "Applied Windows guest ID is invalid.", 100)
    placement = applied.get("placement")
    if not isinstance(placement, dict):
        raise WindowsBootstrapError("Applied Windows guest has no image placement receipt.")
    if placement.get("image_id") != image_id:
        raise WindowsBootstrapError("Applied Windows guest does not match its approved image identity.")
    if placement.get('template_id') != template_id or placement.get('template_node') != template_node:
        from crange_scheduling import read_settings
        image=read_settings()[1].get('images',{}).get(image_id,{})
        matches=[replica for replica in image.get('replicas',[]) if
                 replica.get('template_id')==placement.get('template_id') and
                 replica.get('node')==placement.get('template_node') and replica.get('state')=='ready' and
                 replica.get('artifact_sha256')==image.get('artifact_sha256')]
        if (image.get('verified') is not True or image.get('kind')!='vm' or
                not any(scope in image.get('approved_projects',[]) for scope in ('*',project_id)) or len(matches)!=1):
            raise WindowsBootstrapError('Applied Windows template is not a verified replica of its approved image.')
        template_id,template_node=placement['template_id'],placement['template_node']
    node = placement.get("node_name")
    if not isinstance(node, str) or not re.fullmatch(r"proxmox[1-6]", node):
        raise WindowsBootstrapError("Applied Windows guest has no verified Proxmox node.")
    if applied.get("address") != str(network["address"]):
        raise WindowsBootstrapError("Applied Windows address does not match desired project networking.")
    name = box.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", name):
        raise WindowsBootstrapError("Desired Windows guest name is invalid.")
    return {"project_id": project_id, "box_key": box_key, "guest_id": guest_id,
            "node": node, "image_id": image_id, "template_id": template_id,
            "template_node": template_node, "os_type": profile["os_type"],
            "name": f"{network['vnet']}-{name}", "network": network,
            "credential_ref": credential_ref}


def _owned_live_config(api, target):
    node, vmid = target["node"], target["guest_id"]
    rows = api.request("GET", "/cluster/resources?type=vm")
    matches = [row for row in rows if row.get("type") == "qemu" and str(row.get("vmid")) == str(vmid)]
    if len(matches) != 1 or matches[0].get("node") != node or matches[0].get("name") != target["name"]:
        raise WindowsBootstrapError("Applied Windows VM ownership does not match the live Proxmox resource.")
    config = api.request("GET", f"/nodes/{node}/qemu/{vmid}/config")
    required_tags = {"range-lan", "scenario", f"p{target['project_id']:03d}",
                     target["network"]["vnet"], "terraform"}
    tags = set(str(config.get("tags", "")).split(";"))
    template_flag = config.get("template", 0)
    is_template = template_flag not in (None, 0, "0", False)
    if (config.get("name") != target["name"] or not required_tags.issubset(tags)
            or config.get("ostype") != target["os_type"] or is_template):
        raise WindowsBootstrapError("Live Windows VM tags or OS identity do not match the approved project box.")
    agent = set(str(config.get("agent", "")).split(","))
    if not any(value in ("enabled=1", "1") for value in agent):
        raise WindowsBootstrapError("QEMU Guest Agent is not enabled for this Windows VM.")
    interfaces = [key for key in config if re.fullmatch(r"net[0-9]+", key)]
    if len(interfaces) != 1:
        raise WindowsBootstrapError("Windows bootstrap requires exactly one declared project network interface.")
    nic = config[interfaces[0]]
    fields = dict(part.split("=", 1) for part in str(nic).split(",") if "=" in part)
    mac = fields.get("virtio") or fields.get("e1000") or fields.get("rtl8139") or fields.get("vmxnet3")
    bridge = fields.get("bridge")
    if bridge != target["network"]["vnet"] or not isinstance(mac, str) or not MAC_PATTERN.fullmatch(mac):
        raise WindowsBootstrapError("Windows VM network interface does not match its declared project VNet.")
    status = api.request("GET", f"/nodes/{node}/qemu/{vmid}/status/current")
    if status.get("status") != "running":
        raise WindowsBootstrapError("Start the owned Windows VM before guest-agent network configuration.")
    return config, mac


def _powershell_script():
    # Inputs arrive as JSON on QGA stdin, never as PowerShell command arguments.
    # All host operations are fixed, scoped to the VM's one declared adapter.
    return r"""$ErrorActionPreference='Stop'
$p=([Console]::In.ReadToEnd() | ConvertFrom-Json)
$mac=($p.mac -replace '[:-]','').ToUpperInvariant()
$adapters=@(Get-NetAdapter -IncludeHidden | Where-Object {($_.MacAddress -replace '[:-]','').ToUpperInvariant() -eq $mac})
if($adapters.Count -ne 1){throw 'adapter identity mismatch'}
$a=$adapters[0];$index=[int]$a.ifIndex
$admins=@(Get-LocalUser | Where-Object {$_.SID.Value -match '-500$'})
if($admins.Count -ne 1 -or $admins[0].Name -ne [string]$p.username){throw 'local administrator identity mismatch'}
$secure=ConvertTo-SecureString ([string]$p.password) -AsPlainText -Force
$null=Set-LocalUser -Name $admins[0].Name -Password $secure
$null=Set-NetIPInterface -InterfaceIndex $index -AddressFamily IPv4 -Dhcp Disabled
$oldAddresses=@(Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {$_.IPAddress -ne [string]$p.address -and $_.IPAddress -notlike '169.254.*'})
foreach($old in $oldAddresses){$null=Remove-NetIPAddress -InputObject $old -Confirm:$false -ErrorAction Stop}
$null=Get-NetRoute -InterfaceIndex $index -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
$existingAddress=@(Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {$_.IPAddress -eq [string]$p.address})
if($existingAddress.Count -eq 0){$null=New-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -IPAddress ([string]$p.address) -PrefixLength ([int]$p.prefix_length) -DefaultGateway ([string]$p.gateway) -ErrorAction Stop}
elseif($existingAddress.Count -ne 1 -or $existingAddress[0].PrefixLength -ne [int]$p.prefix_length){throw 'address readback precondition failed'}
$null=New-NetRoute -InterfaceIndex $index -DestinationPrefix '0.0.0.0/0' -NextHop ([string]$p.gateway) -ErrorAction Stop
$null=Set-DnsClientServerAddress -InterfaceIndex $index -ServerAddresses @([string]$p.dns_servers[0])
$null=Set-Service -Name WinRM -StartupType Automatic
$null=Get-NetFirewallRule -DisplayGroup 'Windows Remote Management' -ErrorAction SilentlyContinue | Disable-NetFirewallRule
$existing=@(Get-NetFirewallRule -Name 'CRANGE-WinRM-HTTP' -ErrorAction SilentlyContinue)
if($existing.Count -eq 0){$null=New-NetFirewallRule -Name 'CRANGE-WinRM-HTTP' -DisplayName 'CRANGE WinRM from controller' -DisplayGroup 'Windows Remote Management' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5985 -RemoteAddress '10.60.0.20' -Profile Any}
else{$null=Set-NetFirewallRule -Name 'CRANGE-WinRM-HTTP' -Enabled True -Profile Any;$null=Get-NetFirewallRule -Name 'CRANGE-WinRM-HTTP' | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -RemoteAddress '10.60.0.20'}
$null=Start-Service WinRM
$null=Get-NetFirewallRule -Name 'CRANGE-WinRM-HTTP' | Set-NetFirewallRule -Enabled True -Profile Any
$null=Set-Item -Path WSMan:\localhost\Service\AllowUnencrypted -Value $false
$null=Set-Item -Path WSMan:\localhost\Service\Auth\Basic -Value $false
$listener=@(Get-ChildItem WSMan:\localhost\Listener | Where-Object {$_.Keys -contains 'Transport=HTTP'})
if($listener.Count -eq 0){$null=New-Item -Path WSMan:\localhost\Listener -Transport HTTP -Address * -Force}
$ga=Get-Service QEMU-GA;$rdp=Get-Service TermService;$wrm=Get-Service WinRM
$ips=@(Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 | Where-Object {$_.IPAddress -eq [string]$p.address})
$routes=@(Get-NetRoute -InterfaceIndex $index -DestinationPrefix '0.0.0.0/0' | Where-Object {$_.NextHop -eq [string]$p.gateway})
$dns=@(Get-DnsClientServerAddress -InterfaceIndex $index -AddressFamily IPv4 | ForEach-Object {$_.ServerAddresses} | Where-Object {$_})
$remote=@(Get-NetFirewallRule -Name 'CRANGE-WinRM-HTTP' | Get-NetFirewallAddressFilter | ForEach-Object {$_.RemoteAddress})
if($ips.Count -ne 1 -or $ips[0].PrefixLength -ne [int]$p.prefix_length -or $routes.Count -ne 1 -or $dns -notcontains [string]$p.dns_servers[0] -or $remote -notcontains '10.60.0.20' -or $ga.Status -ne 'Running' -or $rdp.Status -ne 'Running' -or $wrm.Status -ne 'Running'){throw 'Windows bootstrap readback failed'}
[ordered]@{ipv4=$ips[0].IPAddress;prefix_length=$ips[0].PrefixLength;gateway=$routes[0].NextHop;dns_servers=$dns;winrm_running=($wrm.Status -eq 'Running');winrm_allowed_sources=$remote;qemu_agent_running=($ga.Status -eq 'Running');rdp_service_running=($rdp.Status -eq 'Running')} | ConvertTo-Json -Compress"""


def _run_guest_agent(api, target, mac, credentials, timeout):
    script = _powershell_script().encode("utf-16le")
    script_encoded = base64.b64encode(script).decode("ascii")
    payload = {"mac": mac, "address": str(target["network"]["address"]),
               "prefix_length": target["network"]["prefix_length"],
               "gateway": str(target["network"]["gateway"]),
               "dns_servers": [str(item) for item in target["network"]["dns_servers"]],
               "username": credentials["username"], "password": credentials["password"]}
    input_data = json.dumps(payload, separators=(",", ":"))
    command = [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", script_encoded]
    base = f"/nodes/{target['node']}/qemu/{target['guest_id']}/agent"
    response = api.request("POST", base + "/exec", {"command": command, "input-data": input_data})
    pid = response.get("pid") if isinstance(response, dict) else None
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise WindowsBootstrapError("Windows QEMU Guest Agent returned an invalid execution handle.")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = api.request("GET", base + "/exec-status", {"pid": pid})
        if result.get("exited"):
            if result.get("exitcode") != 0:
                raise WindowsBootstrapError("Windows guest-agent bootstrap failed; guest output was withheld.")
            try:
                readback = json.loads(result.get("out-data", ""))
            except (TypeError, ValueError) as exc:
                raise WindowsBootstrapError("Windows guest-agent readback was invalid.") from exc
            expected = {"ipv4": str(target["network"]["address"]),
                        "prefix_length": target["network"]["prefix_length"],
                        "gateway": str(target["network"]["gateway"]),
                        "winrm_running": True, "qemu_agent_running": True,
                        "rdp_service_running": True}
            if (not isinstance(readback, dict) or any(readback.get(key) != value for key, value in expected.items())
                    or str(CONTROLLER_IP) not in readback.get("winrm_allowed_sources", [])
                    or str(WINDOWS_DNS) not in readback.get("dns_servers", [])):
                raise WindowsBootstrapError("Windows guest-agent readback does not match the requested configuration.")
            return {key: readback[key] for key in expected} | {
                "dns_servers": readback["dns_servers"],
                "winrm_allowed_sources": readback["winrm_allowed_sources"],
            }
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise WindowsBootstrapError("Windows guest-agent bootstrap did not finish before its bounded timeout.")


def bootstrap(api, project_id, box_key, *, timeout=180):
    """Bootstrap one applied Windows jump box from trusted project state.

    This operation does not start or stop a VM. It validates ownership and the
    approved Windows image before the first guest-agent exec; caller data never
    supplies a command, script, address, adapter, or credential-store path.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 300:
        raise WindowsBootstrapError("Invalid Windows bootstrap timeout.")
    from crange import project_dir, json_config, output_json, templates_for
    from crange_windows_guest_admin import resolve
    path = project_dir(str(_positive_int(project_id, "Invalid Windows project.", 1, 255)))
    cfg = json_config(path)
    outputs = output_json(path)
    approvals = templates_for(path)
    target = desired_target(project_id, box_key, cfg, outputs, approvals)

    # All provider ownership checks are read-only and complete before credentials
    # are resolved or any QEMU Guest Agent command is submitted.
    config, mac = _owned_live_config(api, target)
    if config != api.request("GET", f"/nodes/{target['node']}/qemu/{target['guest_id']}/config"):
        raise WindowsBootstrapError("Windows guest changed during ownership validation; no guest action was run.")
    status = api.request("GET", f"/nodes/{target['node']}/qemu/{target['guest_id']}/status/current")
    if status.get("status") != "running":
        raise WindowsBootstrapError("Windows guest stopped during ownership validation; no guest action was run.")

    credentials = resolve(target["credential_ref"], project_id=target["project_id"],
                          box_key=target["box_key"], guest_id=target["guest_id"],
                          image_id=target["image_id"])
    return _run_guest_agent(api, target, mac, credentials, timeout)
