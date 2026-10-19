"""Configure captured Windows scenarios without changing their SAM/domain identity.

Worker-only inputs derive from approved images, immutable release lineage and
applied placement. Credentials stay in private stores; only the declared NIC,
DNS and management firewall scope are repaired through fixed QGA commands.
"""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time

from crange_windows_bootstrap import WindowsBootstrapError, POWERSHELL
from crange_windows_guest_initialize import _load, _save, _journal_lock, _wait_for_agent


def credentials_path():
    return Path(os.environ.get('CRANGE_WINDOWS_SNAPSHOT_CREDENTIALS',
        Path.home()/'.local/share/crange/windows-snapshot/image-credentials.json'))


def capture_credentials(project, source_key, guest_id, source_image_id, image_id):
    """Freeze the existing source binding before any capture guest is stopped."""
    from crange_windows_guest_admin import reference, resolve
    from crange_windows_guest_rdp import reference as rdp_reference, resolve as rdp_resolve
    path=credentials_path();path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    scope={'project':project,'source_key':source_key,'source_guest_id':guest_id,
           'source_image_id':source_image_id,'image_id':image_id}
    with _journal_lock(path) as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        records=_load(path)
        if image_id in records:
            if any(records[image_id].get(k)!=v for k,v in scope.items()):
                raise WindowsBootstrapError('Frozen snapshot credentials belong to a different capture source.')
            return
        admin=resolve(reference(project,source_key),project_id=project,box_key=source_key,
                      guest_id=guest_id,image_id=source_image_id)
        rdp=rdp_resolve(rdp_reference(project,source_key,guest_id,source_image_id),project_id=project,
                        box_key=source_key,guest_id=guest_id,image_id=source_image_id)
        records[image_id]={**scope,'admin':admin,'rdp':rdp}
        _save(path,records)


def snapshot_image(target):
    # Controller-generated capture aliases select this separate contract.
    # Generic workgroup bases retain their existing initializer and tests.
    if not target['image_id'].startswith(f"p{target['project_id']:03d}-image-"):
        return None
    from crange_scheduling import read_settings
    from crange_windows_snapshot import validate_captured_identity
    image=read_settings()[1].get('images',{}).get(target['image_id'],{})
    identity=image.get('source_identity',{})
    if identity.get('identity_mode')!='windows-scenario-snapshot-v1':
        return None
    validate_captured_identity(identity)
    if (image.get('verified') is not True or image.get('kind')!='vm' or
            image.get('approved_projects')!=[target['project_id']] or
            identity.get('snapshot_project_id')!=target['project_id']):
        raise WindowsBootstrapError('Windows snapshot is not verified for this project.')
    return image


def release_source(path, cfg, box, target):
    from crange import load_release, templates_for
    if not box.get('release_id') or box.get('release_scope') not in ('test','team'):
        raise WindowsBootstrapError('Snapshot initialization requires explicit immutable release lineage.')
    prefix=f"p{target['project_id']:03d}-"
    if not box['release_id'].startswith(prefix):
        raise WindowsBootstrapError('Windows snapshot release belongs to another project.')
    release=load_release(target['project_id'],box['release_id'][len(prefix):])
    if release.get('project_id')!=target['project_id'] or release.get('release_id')!=box['release_id']:
        raise WindowsBootstrapError('Windows snapshot release project or version differs.')
    matches=[row for row in release['scenario']['boxes']
             if row.get('name')==box.get('release_source_name') and row.get('image')==target['image_id']]
    if len(matches)!=1:
        raise WindowsBootstrapError('Windows snapshot source does not match its release image.')
    source=matches[0]
    original=templates_for(path).get(source.get('source_template'),{})
    source_image_id=source.get('source_image_id') or original.get('image_id')
    if not source_image_id or not source.get('key') or type(source.get('source_vm_id')) is not int:
        raise WindowsBootstrapError('Windows snapshot source credential lineage is incomplete.')
    return source,source_image_id


def image_credentials(path,cfg,box,target,image):
    source,source_image_id=release_source(path,cfg,box,target)
    # Compatibility for captures made before credential freezing was deployed.
    # The fixed guest command authenticates this secret before binding a clone.
    capture_credentials(target['project_id'],source['key'],source['source_vm_id'],source_image_id,target['image_id'])
    record=_load(credentials_path())[target['image_id']]
    return record,source


def managed_source(target,box):
    """Recognize only the recorded fixed MHL domain workflow, never guessed AD."""
    if target['project_id']!=100 or box.get('release_scope') in ('test','team'):
        return False
    from crange_mhl_services import profile_for
    profile=profile_for(int(box.get('network_id',box['team_id']))).get('windows',{})
    binding=profile.get('guests',{}).get(target['box_key'],{})
    return (profile.get('profile')=='project-100-mhl-windows-ad-file-v1' and
            binding.get('guest_id')==target['guest_id'] and binding.get('image_id')==target['image_id'])


def domain_dns(target,cfg,source,identity,approvals):
    if identity['domain_role'] < 3:
        return '10.60.2.1'
    if identity['domain_role']>=4:
        return str(target['network']['address'])
    peers=[]
    for key,box in cfg.get('scenario_boxes',{}).items():
        if (str(box['team_id'])!=str(source['team_id']) or box.get('network_key','')!=source.get('network_key','') or
                box.get('release_id')!=source.get('release_id')):
            continue
        # The fixed staging profile has a known domain controller address.
        if managed_source(target,source) and box.get('name')=='mhl-dc01':
            peers.append(f"10.{target['project_id']}.{source.get('network_id',source['team_id'])}.{box['ip_last_octet']}")
            continue
        approval=approvals.get(box.get('template'),{})
        image_id=approval.get('image_id')
        if not image_id:
            continue
        image=snapshot_image({**target,'image_id':image_id})
        other=image.get('source_identity',{}) if image else {}
        if other.get('domain_role',0)>=4 and other.get('domain_name')==identity['domain_name']:
            peers.append(f"10.{target['project_id']}.{source.get('network_id',source['team_id'])}.{box['ip_last_octet']}")
    if len(set(peers))!=1:
        raise WindowsBootstrapError('Domain member requires one captured domain controller in its declared LAN.')
    return peers[0]


def script():
    return r"""$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
$p=([Console]::In.ReadToEnd()|ConvertFrom-Json)
$cs=Get-CimInstance Win32_ComputerSystem
if($cs.Name -ne $p.computer -or $cs.Domain -ne $p.domain -or [int]$cs.DomainRole -ne [int]$p.role){throw 'snapshot business identity changed'}
$a=@(Get-NetAdapter -IncludeHidden|Where-Object {($_.MacAddress -replace '[:-]','').ToUpperInvariant() -eq ($p.mac -replace '[:-]','').ToUpperInvariant()})
if($a.Count -ne 1){throw 'snapshot adapter identity differs'}
$index=[int]$a[0].ifIndex
# Remove only the captured address or a prior worker-owned address on this NIC.
$marker='C:\ProgramData\CRANGE\snapshot-network.json'
$prior=$null;if(Test-Path -LiteralPath $marker){$prior=Get-Content -LiteralPath $marker -Raw|ConvertFrom-Json}
$allowed=@([string]$p.old_address,[string]$p.address)
$priorIp=$null
if($prior -and $prior.mac -eq $p.mac -and $prior.project -eq $p.project -and $prior.guest_id -eq $p.guest_id -and [Net.IPAddress]::TryParse([string]$prior.address,[ref]$priorIp)){
 $bytes=$priorIp.GetAddressBytes();if($bytes.Length -eq 4 -and $bytes[0] -eq 10 -and $bytes[1] -eq [int]$p.project -and $bytes[3] -ge 2 -and $bytes[3] -le 100){$allowed+=([string]$prior.address)}
}
$unexpected=@(Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -ErrorAction SilentlyContinue|Where-Object {$_.IPAddress -notlike '169.254.*' -and $_.IPAddress -notin $allowed -and $_.PrefixOrigin -ne 'Dhcp'})
if($unexpected.Count){throw 'snapshot NIC has an unmanaged IPv4 address'}
Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -ErrorAction SilentlyContinue|Where-Object {$_.PrefixOrigin -eq 'Dhcp'}|Remove-NetIPAddress -Confirm:$false
Set-NetIPInterface -InterfaceIndex $index -AddressFamily IPv4 -Dhcp Disabled
Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue|Where-Object {$_.IPAddress -in $allowed -and ($_.IPAddress -ne [string]$p.address -or $_.InterfaceIndex -ne $index)}|Remove-NetIPAddress -Confirm:$false
if(-not (Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -ErrorAction SilentlyContinue|Where-Object {$_.IPAddress -eq [string]$p.address})) {New-NetIPAddress -InterfaceIndex $index -IPAddress ([string]$p.address) -PrefixLength 24|Out-Null}
Get-NetRoute -InterfaceIndex $index -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue|Where-Object {$_.NextHop -ne [string]$p.gateway}|Remove-NetRoute -Confirm:$false
if(-not (Get-NetRoute -InterfaceIndex $index -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue|Where-Object {$_.NextHop -eq [string]$p.gateway})){New-NetRoute -InterfaceIndex $index -DestinationPrefix '0.0.0.0/0' -NextHop ([string]$p.gateway)|Out-Null}
Set-DnsClientServerAddress -InterfaceIndex $index -ServerAddresses @([string]$p.dns)
Register-DnsClient
if([int]$p.role -ge 4){& nltest.exe /dsregdns|Out-Null}
# Existing services and accounts are preserved; repair management source scope.
Get-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction Stop|Get-NetFirewallAddressFilter|Set-NetFirewallAddressFilter -RemoteAddress '10.60.0.10'
Get-NetFirewallRule -Name 'CRANGE-WinRM-HTTP' -ErrorAction Stop|Get-NetFirewallAddressFilter|Set-NetFirewallAddressFilter -RemoteAddress '10.60.0.20'
New-Item -ItemType Directory -Path 'C:\ProgramData\CRANGE' -Force|Out-Null
@{mac=$p.mac;address=$p.address;project=$p.project;guest_id=$p.guest_id}|ConvertTo-Json|Set-Content -LiteralPath $marker
Add-Type -TypeDefinition @'
using System;using System.Runtime.InteropServices;
public static class SnapshotLogon { [DllImport("advapi32.dll",SetLastError=true,CharSet=CharSet.Unicode)] public static extern bool LogonUser(string u,string d,string p,int t,int provider,out IntPtr token); [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr token); }
'@
$t=[IntPtr]::Zero
if([int]$p.role -ge 4){$logonUser=([string]$p.admin_username)+'@'+([string]$p.domain);$logonDomain=$null}else{$logonUser=[string]$p.admin_username;$logonDomain=[string]$p.admin_domain}
if(-not [SnapshotLogon]::LogonUser($logonUser,$logonDomain,[string]$p.admin_password,3,0,[ref]$t)){throw 'snapshot management credential did not authenticate'}
[void][SnapshotLogon]::CloseHandle($t)
$deadline=(Get-Date).AddSeconds(20)
do{$ip=@(Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4|Where-Object {$_.IPAddress -eq [string]$p.address});if($ip.Count -eq 1 -and [string]$ip[0].AddressState -eq 'Preferred'){break};Start-Sleep -Seconds 1}while((Get-Date) -lt $deadline)
$dns=@((Get-DnsClientServerAddress -InterfaceIndex $index -AddressFamily IPv4).ServerAddresses)
$route=@(Get-NetRoute -InterfaceIndex $index -DestinationPrefix '0.0.0.0/0'|Where-Object {$_.NextHop -eq [string]$p.gateway})
if($ip.Count -ne 1 -or $ip[0].PrefixLength -ne 24 -or [string]$ip[0].AddressState -ne 'Preferred' -or $dns.Count -ne 1 -or $dns[0] -ne [string]$p.dns -or $route.Count -ne 1){throw 'snapshot network readback differs'}
[ordered]@{ready=$true;ipv4=[string]$p.address;gateway=[string]$p.gateway;dns=[string]$p.dns;computer=$cs.Name;domain=$cs.Domain;role=[int]$cs.DomainRole}|ConvertTo-Json -Compress
"""


def execute(api,target,payload,*,timeout=600):
    path=Path(os.environ.get('CRANGE_WINDOWS_SNAPSHOT_INIT_JOURNAL',
        Path.home()/'.local/share/crange/windows-snapshot/network-jobs.json'))
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    root=f"/nodes/{target['node']}/qemu/{target['guest_id']}/agent"
    _wait_for_agent(api,root,timeout)
    command=[POWERSHELL,'-NoProfile','-NonInteractive','-EncodedCommand',base64.b64encode(script().encode('utf-16le')).decode()]
    body=json.dumps(payload,separators=(',',':'))
    key=f"{target['project_id']}:{target['box_key']}:{target['guest_id']}:{target['image_id']}"
    digest=hashlib.sha256(json.dumps([command,body],separators=(',',':')).encode()).hexdigest()
    with _journal_lock(path) as lock:
        fcntl.flock(lock,fcntl.LOCK_EX);records=_load(path);record=records.get(key)
        if record and record.get('state')=='complete':record=None
        if record and record.get('digest')!=digest:
            if record.get('state')!='complete':
                raise WindowsBootstrapError('Snapshot network request changed while its previous command is unresolved.')
            record=None
        if record is None:
            record={'digest':digest,'state':'submitting'};records[key]=record;_save(path,records)
            try:response=api.request('POST',root+'/exec',{'command':command,'input-data':body})
            except Exception as error:raise WindowsBootstrapError('Snapshot command submission is uncertain; no duplicate command was sent.') from None
            pid=response.get('pid') if isinstance(response,dict) else None
            if type(pid) is not int or pid<=0:
                raise WindowsBootstrapError('Snapshot command has no valid handle; retain its journal.')
            record.update(pid=pid,state='running');_save(path,records)
        elif record.get('state')=='submitting' or type(record.get('pid')) is not int:
            raise WindowsBootstrapError('Snapshot command submission is uncertain; retain its journal.')
        deadline=time.monotonic()+timeout
        while True:
            result=record.get('terminal') or api.request('GET',root+'/exec-status',{'pid':record['pid']})
            if result.get('exited'):
                # Persist before parsing; errors/credentials are never printed.
                record['terminal']={'exited':True,'exitcode':result.get('exitcode'),'out-data':result.get('out-data','')};_save(path,records)
                if result.get('exitcode')!=0:
                    raise WindowsBootstrapError('Snapshot network configuration failed; guest output was withheld.')
                try:parsed=json.loads(result['out-data'])
                except (ValueError,KeyError):raise WindowsBootstrapError('Snapshot network readback was invalid.') from None
                expected={k:payload[k] for k in ('ipv4','gateway','dns','computer','domain','role') if k in payload}
                expected['ipv4']=payload['address'];expected['ready']=True
                if parsed!=expected:
                    raise WindowsBootstrapError('Snapshot network readback differs from its declared identity.')
                record['state']='complete';_save(path,records);return parsed
            if time.monotonic()>=deadline:
                raise WindowsBootstrapError('Snapshot command remains running; retry will poll the same handle.')
            time.sleep(1)


def initialize(api,target,mac,config,*,path,cfg,box,approvals,image=None):
    from crange_windows_snapshot import collect,verify_clone,WindowsSnapshotError
    from crange_distribution import _fixed_guest_read,DistributionError
    from crange_windows_guest_admin import reference,resolve,store
    from crange_windows_guest_rdp import reference as rr,resolve as rd,store as rs
    read=lambda command:_fixed_guest_read(api,target['node'],'qemu',target['guest_id'],command,None,120)
    _wait_for_agent(api,f"/nodes/{target['node']}/qemu/{target['guest_id']}/agent",180)
    deadline=time.monotonic()+180
    waiting=False
    while True:
        try:
            identity=collect(read,config.get('vmgenid'),target['project_id']);break
        except (WindowsSnapshotError,DistributionError):
            if time.monotonic()>=deadline:
                raise WindowsBootstrapError('Captured Windows services did not reach verified readiness.') from None
            if not waiting:
                print('Waiting for captured Windows domain/services to become ready.',flush=True);waiting=True
            time.sleep(3)
    if image:
        verify_clone(image['source_identity'],identity)
        frozen,source=image_credentials(path,cfg,box,target,image)
        admin,rdp=frozen['admin'],frozen['rdp']
        old_address=f"10.{target['project_id']}.{source.get('network_id',source['team_id'])}.{source['ip_last_octet']}"
    else:
        admin=resolve(reference(target['project_id'],target['box_key']),project_id=target['project_id'],box_key=target['box_key'],guest_id=target['guest_id'],image_id=target['image_id'])
        rdp=rd(rr(target['project_id'],target['box_key'],target['guest_id'],target['image_id']),project_id=target['project_id'],box_key=target['box_key'],guest_id=target['guest_id'],image_id=target['image_id'])
        old_address=str(target['network']['address'])
    dns=domain_dns(target,cfg,box,identity,approvals)
    account=admin['username']
    domain=identity['domain_name'] if identity['domain_role']>=4 else identity['computer_name']
    if '@' in account:account,domain=account.split('@',1)
    elif '\\' in account:domain,account=account.split('\\',1)
    payload={'project':target['project_id'],'guest_id':target['guest_id'],'mac':mac,'old_address':old_address,'address':str(target['network']['address']),
             'gateway':str(target['network']['gateway']),'dns':dns,'computer':identity['computer_name'],
             'domain':identity['domain_name'],'role':identity['domain_role'],'admin_username':account,
             'admin_domain':domain,'admin_password':admin['password']}
    execute(api,target,payload)
    admin_login=f"{account}@{identity['domain_name']}" if identity['domain_role']>=4 else f'{domain}\\{account}'
    if image:
        store(target['project_id'],target['box_key'],target['guest_id'],target['image_id'],admin_login,admin['password'])
        rs(target['project_id'],target['box_key'],target['guest_id'],target['image_id'],rdp['password'],show_credentials=rdp.get('show_credentials',False))
    return {'initialized':True,'snapshot':True,'admin_login':admin_login,
            'credential_ref':target['credential_ref'],
            'rdp_credential_ref':rr(target['project_id'],target['box_key'],target['guest_id'],target['image_id'])}
