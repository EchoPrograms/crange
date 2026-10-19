"""Fixed, worker-only initialization of an applied approved Windows VM.

No caller supplied commands, addresses, API paths, or credential paths are
accepted. Identity and network settings are derived from project desired state,
Terraform placement, the approved template profile, and live Proxmox ownership.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import string
import stat
import time

from crange_windows_bootstrap import (CONTROLLER_IP, POWERSHELL, WindowsBootstrapError,
                                     _owned_live_config, desired_target)


def _password():
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    # Guarantee a mixed, strong password while avoiding shell metacharacter
    # interpretation: it is sent only as QGA stdin JSON.
    chars = [secrets.choice(string.ascii_lowercase), secrets.choice(string.ascii_uppercase),
             secrets.choice(string.digits), secrets.choice("!@#$%^&*()-_=+")]
    chars.extend(secrets.choice(alphabet) for _ in range(28))
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _script():
    return r"""$ErrorActionPreference='Stop'
$p=([Console]::In.ReadToEnd() | ConvertFrom-Json)
$deadline=(Get-Date).AddMinutes(8)
do {
  Start-Sleep -Seconds 5
  $state=Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Setup\State' -ErrorAction SilentlyContinue
  $setup=Get-ItemProperty 'HKLM:\SYSTEM\Setup' -ErrorAction SilentlyContinue
  $ready=($state.ImageState -eq 'IMAGE_STATE_COMPLETE' -and [int]$setup.OOBEInProgress -eq 0 -and [int]$setup.SystemSetupInProgress -eq 0 -and [int]$setup.SetupType -eq 0)
} until($ready -or (Get-Date) -ge $deadline)
if(-not $ready){throw 'Windows OOBE did not complete before timeout'}
$mac=($p.mac -replace '[:-]','').ToUpperInvariant()
$a=@(Get-NetAdapter -IncludeHidden | Where-Object {($_.MacAddress -replace '[:-]','').ToUpperInvariant() -eq $mac})
if($a.Count -ne 1){throw 'adapter identity mismatch'}
$index=[int]$a[0].ifIndex
$admins=@(Get-CimInstance Win32_UserAccount | Where-Object {$_.LocalAccount -and $_.SID -match '-500$'})
if($admins.Count -ne 1){throw 'RID-500 administrator identity mismatch'}
$u=$admins[0].Name
if([bool]$p.set_password){$secure=ConvertTo-SecureString ([string]$p.password) -AsPlainText -Force;Set-LocalUser -Name $u -Password $secure}
Enable-LocalUser -Name $u
if([bool]$p.manage_rdp){
  $computer=Get-CimInstance Win32_ComputerSystem
  if($computer.PartOfDomain){throw 'workgroup RDP account initialization refuses a domain-joined guest'}
  $group=Get-LocalGroup -SID 'S-1-5-32-555'
  $rdpUser=Get-LocalUser -Name 'user' -ErrorAction SilentlyContinue
  $member=@();if($rdpUser){$member=@(Get-LocalGroupMember -Group $group.Name -ErrorAction SilentlyContinue | Where-Object {$_.SID.Value -eq $rdpUser.SID.Value})}
  if([bool]$p.set_rdp_password){
    $rdpSecure=ConvertTo-SecureString ([string]$p.rdp_password) -AsPlainText -Force
    if(-not $rdpUser){New-LocalUser -Name 'user' -Password $rdpSecure -PasswordNeverExpires | Out-Null}
    elseif($member.Count -gt 0){Set-LocalUser -Name 'user' -Password $rdpSecure}
    else{throw 'unmanaged local RDP account already exists'}
    $rdpUser=Get-LocalUser -Name 'user'
  } elseif(-not $rdpUser){throw 'bound local RDP account is missing'}
  Enable-LocalUser -Name 'user'
  $member=@(Get-LocalGroupMember -Group $group.Name -ErrorAction SilentlyContinue | Where-Object {$_.SID.Value -eq $rdpUser.SID.Value})
  if($member.Count -eq 0){Add-LocalGroupMember -Group $group.Name -Member $rdpUser}
}
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class CrangeCredentialVerifier {
  [DllImport("advapi32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
  public static extern bool LogonUser(string user, string domain, string password, int type, int provider, out IntPtr token);
  [DllImport("kernel32.dll", SetLastError=true)]
  public static extern bool CloseHandle(IntPtr handle);
}
'@
$adminToken=[IntPtr]::Zero
if(-not [CrangeCredentialVerifier]::LogonUser($u,'.',[string]$p.verify_admin_password,2,0,[ref]$adminToken)){throw 'The bound Windows Administrator credential did not authenticate'}
[void][CrangeCredentialVerifier]::CloseHandle($adminToken)
$rdpToken=[IntPtr]::Zero
if(-not [CrangeCredentialVerifier]::LogonUser('user','.',[string]$p.verify_rdp_password,3,0,[ref]$rdpToken)){throw ('The bound Windows RDP credential did not authenticate; Win32 error '+[Runtime.InteropServices.Marshal]::GetLastWin32Error())}
[void][CrangeCredentialVerifier]::CloseHandle($rdpToken)
Set-NetIPInterface -InterfaceIndex $index -AddressFamily IPv4 -Dhcp Disabled
$existing=@(Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {$_.IPAddress -eq [string]$p.address})
if($existing.Count -eq 0){New-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -IPAddress ([string]$p.address) -PrefixLength 24 -DefaultGateway ([string]$p.gateway) | Out-Null}
elseif($existing.Count -ne 1 -or $existing[0].PrefixLength -ne 24){throw 'static address conflict'}
$routes=@(Get-NetRoute -InterfaceIndex $index -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue)
foreach($r in $routes){if($r.NextHop -ne [string]$p.gateway){Remove-NetRoute -InputObject $r -Confirm:$false}}
if(-not (Get-NetRoute -InterfaceIndex $index -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Where-Object {$_.NextHop -eq [string]$p.gateway})){New-NetRoute -InterfaceIndex $index -DestinationPrefix '0.0.0.0/0' -NextHop ([string]$p.gateway) | Out-Null}
Set-DnsClientServerAddress -InterfaceIndex $index -ServerAddresses @([string]$p.dns)
Set-Service WinRM -StartupType Automatic
Get-NetFirewallRule -DisplayGroup 'Windows Remote Management' -ErrorAction SilentlyContinue | Disable-NetFirewallRule
$f=@(Get-NetFirewallRule -Name 'CRANGE-WinRM-HTTP' -ErrorAction SilentlyContinue)
if($f.Count -eq 0){New-NetFirewallRule -Name 'CRANGE-WinRM-HTTP' -DisplayName 'CRANGE WinRM from controller' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5985 -RemoteAddress ([string]$p.controller) -Profile Any | Out-Null}
else{$f | Set-NetFirewallRule -Enabled True -Profile Any; $f | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -RemoteAddress ([string]$p.controller)}
Set-Item WSMan:\localhost\Service\AllowUnencrypted -Value $false
Set-Item WSMan:\localhost\Service\Auth\Basic -Value $false
if(-not (Get-ChildItem WSMan:\localhost\Listener | Where-Object {$_.Keys -contains 'Transport=HTTP'})){New-Item WSMan:\localhost\Listener -Transport HTTP -Address * -Force | Out-Null}
Start-Service WinRM
# Only after OOBE, desired local accounts, and both private credentials have
# passed authentication, remove answerfiles owned by this fixed base profile.
$ownedAnswerPaths=@('C:\ProgramData\CRANGE\clone-unattend.xml',
  'C:\Windows\Panther\Unattend\Unattend.xml',
  'C:\Windows\Panther\UnattendGC\Unattend.xml',
  'C:\Windows\Panther\unattend.xml',
  'C:\Windows\Panther\UnattendGC\unattend.xml')
foreach($path in $ownedAnswerPaths){
  if(-not (Test-Path -LiteralPath $path)){continue}
  try{[xml]$candidate=[IO.File]::ReadAllText($path)}catch{continue}
  $marker=$candidate.SelectSingleNode("//*[local-name()='ComputerName' and text()='MHL-WINBASE']") -or
    $candidate.SelectSingleNode("//*[local-name()='RegisteredOrganization' and text()='Meridian Harbor Logistics']")
  if($marker){
    (Get-Item -LiteralPath $path).IsReadOnly=$false
    $file=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try{$file.SetLength(0);$file.Flush($true)}finally{$file.Dispose()}
    Remove-Item -LiteralPath $path -Force
  }
}
$winlogon='HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-ItemProperty -Path $winlogon -Name AutoAdminLogon -Value '0' -Type String
Remove-ItemProperty -Path $winlogon -Name DefaultPassword -ErrorAction SilentlyContinue
Remove-ItemProperty -Path $winlogon -Name AutoLogonCount -ErrorAction SilentlyContinue
[ordered]@{ready=$true;account=$u;rdp_account=$(if([bool]$p.manage_rdp){'user'}else{''});ipv4=[string]$p.address;gateway=[string]$p.gateway;dns=[string]$p.dns;winrm=$((Get-Service WinRM).Status -eq 'Running')} | ConvertTo-Json -Compress"""


def _journal_path():
    configured = os.environ.get("CRANGE_WINDOWS_GUEST_INIT_JOURNAL")
    return Path(configured) if configured else Path.home() / ".local/share/crange/windows-guest-init.json"


def _save(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_info = path.parent.lstat()
    if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.geteuid()
            or stat.S_IMODE(parent_info.st_mode) != 0o700):
        raise WindowsBootstrapError("Windows initialization journal directory must be worker-owned mode 0700.")
    if path.is_symlink():
        raise WindowsBootstrapError("Windows initialization journal cannot be a symbolic link.")
    tmp = path.with_name("." + path.name + "." + str(os.getpid()) + "." + secrets.token_hex(6) + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _load(path):
    if not path.exists():
        return {}
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o600:
        raise WindowsBootstrapError("Windows initialization journal must be worker-owned mode 0600.")
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        raise WindowsBootstrapError("Windows initialization journal is unreadable or malformed.") from None


def _journal_lock(path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        os.close(fd)
        raise WindowsBootstrapError("Windows initialization journal lock must be worker-owned mode 0600.")
    return os.fdopen(fd, "r+")


def _wait_for_agent(api, root, timeout):
    deadline = time.monotonic() + min(timeout, 180)
    print('Waiting for the owned Windows guest agent before initialization.', flush=True)
    while time.monotonic() < deadline:
        try:
            info = api.request('GET', root + '/get-osinfo')
        except Exception as exc:
            if getattr(exc.__cause__, 'code', None) == 403:
                raise WindowsBootstrapError('Windows guest-agent read permission is missing.') from None
            time.sleep(min(2, max(0, deadline - time.monotonic())))
            continue
        info = info.get('result', info) if isinstance(info, dict) else {}
        if info.get('id') != 'mswindows':
            raise WindowsBootstrapError('The approved Windows guest reported a different operating system.')
        return
    raise WindowsBootstrapError('Windows guest agent is not ready; retry configuration after the guest finishes booting.')


def _execute(api, target, mac, password=None, *, rdp_password=None,
             verify_admin_password=None, verify_rdp_password=None, set_password=True,
             set_rdp_password=False, timeout=600, journal=None):
    if type(timeout) is not int or not 1 <= timeout <= 900:
        raise WindowsBootstrapError("Invalid Windows initialization timeout.")
    payload = {"mac": mac, "address": str(target["network"]["address"]),
               "gateway": str(target["network"]["gateway"]), "dns": "10.60.2.1",
               "controller": CONTROLLER_IP, "set_password": bool(set_password),
               "manage_rdp": True, "set_rdp_password": bool(set_rdp_password)}
    if set_password:
        if not isinstance(password, str) or len(password) < 16:
            raise WindowsBootstrapError("Windows initialization requires a worker-generated administrator password.")
        payload["password"] = password
    if set_rdp_password:
        if not isinstance(rdp_password, str) or len(rdp_password) < 16:
            raise WindowsBootstrapError("Windows initialization requires a worker-generated RDP password.")
        payload["rdp_password"] = rdp_password
    verify_admin_password = password if verify_admin_password is None else verify_admin_password
    verify_rdp_password = rdp_password if verify_rdp_password is None else verify_rdp_password
    if not isinstance(verify_admin_password, str) or len(verify_admin_password) < 16:
        raise WindowsBootstrapError("Windows initialization requires the private Administrator credential for verification.")
    if not isinstance(verify_rdp_password, str) or len(verify_rdp_password) < 16:
        raise WindowsBootstrapError("Windows initialization requires the private RDP credential for verification.")
    payload["verify_admin_password"] = verify_admin_password
    payload["verify_rdp_password"] = verify_rdp_password
    encoded_script = base64.b64encode(_script().encode("utf-16le")).decode("ascii")
    input_data = json.dumps(payload, separators=(",", ":"))
    root = f"/nodes/{target['node']}/qemu/{target['guest_id']}/agent"
    record = journal['record'] if journal is not None else {}
    if record.get('mac') not in (None, mac):
        raise WindowsBootstrapError('Pending Windows initialization belongs to a different VM network identity.')
    if record.get('state') == 'complete':
        for key in ('pid', 'exitcode', 'raw_terminal_response', 'raw_error_response', 'submission'):
            record.pop(key, None)
        record['state'] = 'pending'
    # A known terminal failure may be retried by a new GUI job with the same
    # private passwords. A missing observation never starts another command.
    if record.get('exitcode') is not None and record.get('exitcode') != 0:
        for key in ('pid', 'exitcode', 'raw_terminal_response', 'submission'):
            record.pop(key, None)
        record['state'] = 'pending'
    pid = record.get('pid')
    submission = {'script_sha256': hashlib.sha256(encoded_script.encode()).hexdigest(),
                  'payload_sha256': hashlib.sha256(input_data.encode()).hexdigest()}
    if pid is not None:
        if type(pid) is not int or pid <= 0:
            raise WindowsBootstrapError('Invalid persisted Windows initialization handle.')
        if record.get('submission') not in (None, submission) and not (record.get('exitcode') == 0 and record.get('raw_terminal_response') is not None):
            raise WindowsBootstrapError('Pending Windows initialization settings changed; inspect the existing task before retrying.')
    else:
        if record.get('state') == 'submitting':
            raise WindowsBootstrapError('Windows initialization submission is uncertain; no duplicate command was sent.')
        _wait_for_agent(api, root, timeout)
        if journal is not None:
            record.update(state='submitting', mac=mac, submission=submission)
            _save(journal['path'], journal['document'])
        response = api.request('POST', root + '/exec', {'command': [POWERSHELL, '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded_script], 'input-data': input_data})
        pid = response.get('pid') if isinstance(response, dict) else None
        if type(pid) is not int or pid <= 0:
            raise WindowsBootstrapError('Windows QEMU Guest Agent returned an invalid initialization handle; no duplicate command will be sent.')
        if journal is not None:
            record.update(pid=pid, state='running')
            _save(journal['path'], journal['document'])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if record.get("exitcode") == 0 and record.get("raw_terminal_response") is not None:
            result = {"exited": True, "exitcode": 0, "out-data": record["raw_terminal_response"]}
        else:
            result = api.request("GET", root + "/exec-status", {"pid": pid})
        if not isinstance(result, dict):
            raise WindowsBootstrapError("Windows guest-agent returned an invalid initialization status.")
        if result.get("exited"):
            raw = result.get("out-data", "")
            if journal is not None:
                journal["record"]["raw_terminal_response"] = raw
                journal["record"]["raw_error_response"] = result.get("err-data", "")
                journal["record"]["exitcode"] = result.get("exitcode")
                journal["record"]["state"] = "terminal"
                _save(journal["path"], journal["document"])
            if result.get("exitcode") != 0:
                raise WindowsBootstrapError("Windows guest initialization failed; guest output was withheld.")
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                raise WindowsBootstrapError("Windows initialization readback was invalid.") from None
            if parsed != {"ready": True, "account": parsed.get("account"), "rdp_account": "user",
                          "ipv4": str(target["network"]["address"]),
                          "gateway": str(target["network"]["gateway"]), "dns": "10.60.2.1", "winrm": True}:
                raise WindowsBootstrapError("Windows initialization readback does not match desired state.")
            return parsed
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise WindowsBootstrapError("Windows guest initialization did not finish before its bounded timeout.")


def _initialize_with_pending(api, target, mac, project_id, box_key, admin_ref, rdp_ref,
                             existing_admin, existing_rdp, timeout, journal_path):
    identity = f"{project_id}:{box_key}:{target['guest_id']}:{target['image_id']}"
    journal_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _journal_lock(journal_path) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = _load(journal_path)
        record = records.get(identity)
        if not isinstance(record, dict):
            record = {"project_id": project_id, "box_key": box_key,
                      "guest_id": target["guest_id"], "image_id": target["image_id"],
                      "state": "pending"}
        elif any(record.get(k) != v for k, v in {"project_id": project_id,
                 "box_key": box_key, "guest_id": target["guest_id"],
                 "image_id": target["image_id"]}.items()):
            raise WindowsBootstrapError("Pending Windows initialization journal identity mismatch.")
        changed = False
        if existing_admin is None and not isinstance(record.get("password"), str):
            record["password"] = _password()
            changed = True
        if existing_rdp is None and not isinstance(record.get("rdp_password"), str):
            record["rdp_password"] = _password()
            other = existing_admin["password"] if existing_admin else record.get("password")
            while record["rdp_password"] == other:
                record["rdp_password"] = _password()
            changed = True
        records[identity] = record
        if changed:
            _save(journal_path, records)
        journal = {"path": journal_path, "document": records, "record": record}
        readback = _execute(
            api, target, mac,
            record["password"] if existing_admin is None else None,
            rdp_password=record["rdp_password"] if existing_rdp is None else None,
            verify_admin_password=record["password"] if existing_admin is None else existing_admin["password"],
            verify_rdp_password=record["rdp_password"] if existing_rdp is None else existing_rdp["password"],
            set_password=existing_admin is None,
            set_rdp_password=existing_rdp is None,
            timeout=timeout, journal=journal)
        from crange_windows_guest_admin import store as store_admin
        from crange_windows_guest_rdp import store as store_rdp
        if existing_admin is None:
            store_admin(project_id, box_key, target["guest_id"], target["image_id"],
                        readback["account"], record["password"])
        if existing_rdp is None:
            store_rdp(project_id, box_key, target["guest_id"], target["image_id"],
                      record["rdp_password"])
        record["state"] = "complete"
        record.pop("password", None)
        record.pop("rdp_password", None)
        record.pop("raw_terminal_response", None)
        record.pop("raw_error_response", None)
        records[identity] = record
        _save(journal_path, records)
    return {"initialized": existing_admin is None or existing_rdp is None,
            "credential_ref": admin_ref, "rdp_credential_ref": rdp_ref}


def initialize(api, project_id, box_key, *, timeout=600):
    """Initialize only the exact live, applied Windows scenario VM."""
    from crange import project_dir, json_config, output_json, templates_for
    from crange_windows_guest_admin import reference, resolve
    from crange_windows_guest_rdp import reference as rdp_reference
    from crange_windows_guest_rdp import resolve as resolve_rdp
    path = project_dir(str(project_id))
    cfg, outputs, approvals = json_config(path), output_json(path), templates_for(path)
    box = cfg.get("scenario_boxes", {}).get(box_key)
    if not isinstance(box, dict):
        raise WindowsBootstrapError("Windows initializer requires a declared scenario VM.")
    # The expected credential reference is worker-derived; users cannot choose it.
    box = dict(box)
    box["windows_admin_credential_ref"] = reference(project_id, box_key)
    # The bootstrap target validator is shared with the legacy jump-box
    # bootstrap. Initialization applies to any declared scenario VM, so satisfy
    # its scope marker only in this in-memory validation copy.
    box["is_jumpbox"] = True
    cfg = dict(cfg); cfg["scenario_boxes"] = dict(cfg.get("scenario_boxes", {})); cfg["scenario_boxes"][box_key] = box
    target = desired_target(project_id, box_key, cfg, outputs, approvals)
    if target["os_type"][:3] != "win":
        raise WindowsBootstrapError("Approved image is not a Windows profile.")
    if target["guest_id"] in (114, 8912, 8913, 8914):
        raise WindowsBootstrapError("Windows initializer refuses the controller and sealed-source verification guests.")
    config, mac = _owned_live_config(api, target)
    resources = api.request("GET", "/cluster/resources?type=vm")
    owned = [row for row in resources if isinstance(row, dict)
             and row.get("type") == "qemu" and str(row.get("vmid")) == str(target["guest_id"])]
    if (len(owned) != 1 or owned[0].get("pool") != "crange-iac"
            or owned[0].get("node") != target["node"]
            or owned[0].get("name") != target["name"]):
        raise WindowsBootstrapError("Windows initializer requires the applied VM in the crange-iac pool.")
    if config != api.request("GET", f"/nodes/{target['node']}/qemu/{target['guest_id']}/config"):
        raise WindowsBootstrapError("Windows guest changed during ownership validation.")
    status = api.request("GET", f"/nodes/{target['node']}/qemu/{target['guest_id']}/status/current")
    if status.get("status") != "running":
        raise WindowsBootstrapError("Start the applied Windows VM before initialization.")
    from crange_windows_snapshot_guest import snapshot_image, managed_source, initialize as initialize_snapshot
    image = snapshot_image(target)
    if image or managed_source(target,box):
        return initialize_snapshot(api,target,mac,config,path=path,cfg=cfg,box=box,approvals=approvals,image=image)
    try:
        existing = resolve(target["credential_ref"], project_id=project_id, box_key=box_key,
                           guest_id=target["guest_id"], image_id=target["image_id"])
    except ValueError:
        existing = None
    rdp_ref = rdp_reference(project_id, box_key, target["guest_id"], target["image_id"])
    try:
        existing_rdp = resolve_rdp(rdp_ref, project_id=project_id, box_key=box_key,
                                   guest_id=target["guest_id"], image_id=target["image_id"])
    except ValueError:
        existing_rdp = None
    return _initialize_with_pending(api, target, mac, project_id, box_key,
                                    target['credential_ref'], rdp_ref, existing,
                                    existing_rdp, timeout, _journal_path())
