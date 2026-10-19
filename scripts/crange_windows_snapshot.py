"""Separate service-preserving Windows snapshot identity contract.

Base-image generalization retains its existing workgroup/unique-SID contract.
These snapshots preserve the configured SAM/domain; VM generation and DC
invocation identifiers, not rewritten business identity, prove a new instance.
"""
import json
import re
import uuid

class WindowsSnapshotError(ValueError):pass

def guid(value):
    try:return str(uuid.UUID(str(value)))==str(value).lower() and uuid.UUID(str(value)).int!=0
    except (ValueError,TypeError,AttributeError):return False

def validate(identity):
    if not isinstance(identity,dict) or identity.get('identity_mode')!='windows-scenario-snapshot-v1':
        raise WindowsSnapshotError('Explicit Windows scenario snapshot evidence required.')
    role=identity.get('domain_role')
    if (type(role) is not int or role not in (2,3,4,5)
            or identity.get('part_of_domain') is not (role>=3)
            or not guid(identity.get('machine_guid')) or not guid(identity.get('vm_generation_id'))
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,14}',str(identity.get('computer_name','')))
            or not str(identity.get('os_caption','')).startswith('Microsoft Windows ')
            or identity.get('image_state')!='IMAGE_STATE_COMPLETE'
            or any(type(identity.get(k)) is not int or identity[k]!=0 for k in ('oobe_in_progress','system_setup_in_progress','setup_type'))
            or any(identity.get(k) is not True for k in ('qemu_guest_agent','qemu_guest_agent_automatic','term_service_running','term_service_automatic','winrm_service_running','winrm_service_automatic'))
            or identity.get('rdp_allowed_sources')!=['10.60.0.10']):
        raise WindowsSnapshotError('Windows snapshot identity/readiness or approved RDP source is invalid.')
    if role>=3 and not re.fullmatch(r'[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+',str(identity.get('domain_name',''))):
        raise WindowsSnapshotError('Domain snapshot requires its actual DNS domain.')
    if role>=4 and (not guid(identity.get('directory_invocation_id')) or identity.get('generation_counter_present') is not True):
        raise WindowsSnapshotError('DC snapshot requires live invocation identity and generation-counter support.')
    return identity

def verify_clone(source,clone):
    validate(source);validate(clone)
    preserved=('computer_name','domain_name','domain_role','machine_guid','os_caption')
    if any(source.get(k)!=clone.get(k) for k in preserved):
        raise WindowsSnapshotError('Restored Windows business identity differs from the captured scenario.')
    if clone['vm_generation_id']==source['vm_generation_id']:
        raise WindowsSnapshotError('Windows snapshot clone retained the source VM generation ID.')
    if source['domain_role']>=4 and clone['directory_invocation_id']==source['directory_invocation_id']:
        raise WindowsSnapshotError('Restored DC did not reset its directory invocation ID.')
    return {'passed':True,'instance_generation_unique':True,'service_identity_preserved':True}


def collect(read, vm_generation_id, project_id=None):
    """Fixed read-only guest collector; generation ID comes from PVE config."""
    script=r"""$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
$os=Get-CimInstance Win32_OperatingSystem;$cs=Get-CimInstance Win32_ComputerSystem
$setup=Get-ItemProperty 'HKLM:\SYSTEM\Setup'
$ga=Get-Service QEMU-GA;$term=Get-Service TermService;$wrm=Get-Service WinRM
$rdp=@(Get-NetFirewallRule -DisplayGroup 'Remote Desktop' -Enabled True|Get-NetFirewallAddressFilter|Select-Object -ExpandProperty RemoteAddress -Unique)
$inv=$null
if([int]$cs.DomainRole -ge 4){
 Add-Type -AssemblyName System.DirectoryServices.Protocols
 $ldap=[System.DirectoryServices.Protocols.LdapConnection]::new('127.0.0.1')
 try {
  $ldap.AuthType=[System.DirectoryServices.Protocols.AuthType]::Negotiate
  $ldap.Timeout=[TimeSpan]::FromSeconds(10)
  $ldap.SessionOptions.ProtocolVersion=3
  $ldap.SessionOptions.Signing=$true;$ldap.SessionOptions.Sealing=$true
  $ldap.Bind()
  $rootRequest=[System.DirectoryServices.Protocols.SearchRequest]::new('', '(objectClass=*)', [System.DirectoryServices.Protocols.SearchScope]::Base, [string[]]@('dsServiceName'))
  $root=[System.DirectoryServices.Protocols.SearchResponse]$ldap.SendRequest($rootRequest)
  $dn=[string]$root.Entries[0].Attributes['dsServiceName'][0]
  $ntdsRequest=[System.DirectoryServices.Protocols.SearchRequest]::new($dn, '(objectClass=*)', [System.DirectoryServices.Protocols.SearchScope]::Base, [string[]]@('invocationId'))
  $ntds=[System.DirectoryServices.Protocols.SearchResponse]$ldap.SendRequest($ntdsRequest)
  $inv=([Guid]::new([byte[]]$ntds.Entries[0].Attributes['invocationId'][0])).ToString()
 } finally {$ldap.Dispose()}
}
$counter=@(Get-CimInstance Win32_PnPEntity -Filter "Name LIKE '%Generation Counter%' AND Status='OK'")
[ordered]@{computer_name=$cs.Name;domain_name=$cs.Domain;domain_role=[int]$cs.DomainRole;part_of_domain=[bool]$cs.PartOfDomain;machine_guid=(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography').MachineGuid;os_caption=$os.Caption;image_state=(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Setup\State').ImageState;oobe_in_progress=[int]$setup.OOBEInProgress;system_setup_in_progress=[int]$setup.SystemSetupInProgress;setup_type=[int]$setup.SetupType;qemu_guest_agent=($ga.Status -eq 'Running');qemu_guest_agent_automatic=($ga.StartType -eq 'Automatic');term_service_running=($term.Status -eq 'Running');term_service_automatic=($term.StartType -eq 'Automatic');winrm_service_running=($wrm.Status -eq 'Running');winrm_service_automatic=($wrm.StartType -eq 'Automatic');rdp_allowed_sources=$rdp;directory_invocation_id=$inv;generation_counter_present=($counter.Count -gt 0)}|ConvertTo-Json -Compress"""
    raw=read([r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe','-NoProfile','-NonInteractive','-Command',script])
    try:identity=json.loads(raw)
    except (ValueError,TypeError):raise WindowsSnapshotError('Windows snapshot readback is not JSON.') from None
    identity.update(identity_mode='windows-scenario-snapshot-v1',os_family='windows',vm_generation_id=vm_generation_id)
    if project_id is not None: identity["snapshot_project_id"]=project_id
    return validate(identity)


def validate_captured_identity(identity):
    if isinstance(identity,dict) and identity.get('identity_mode')=='windows-scenario-snapshot-v1':
        project=identity.get('snapshot_project_id')
        if type(project) is not int or not 1<=project<=255 or project in (60,61):
            raise WindowsSnapshotError('Windows scenario snapshot requires its project approval scope.')
        return validate(identity)
    from crange_catalog import validate_source_identity
    return validate_source_identity(identity)
