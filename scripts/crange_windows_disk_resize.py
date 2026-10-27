"""Build fixed guest-side preparation and verification scripts for Windows shrink.

This module never connects to a guest. A caller must independently establish a
verified full-disk backup before executing the generated preparation script,
then shrink the host virtual disk only after the generated verification script
passes.
"""
from __future__ import annotations

import re
import uuid

GIB = 1024 ** 3
MIB = 1024 ** 2
SECTOR = 512

# Observed Server 2025/Windows 11 GPT disk 0 layout. Supporting an unmeasured
# layout would make recovery relocation ambiguous, so this is deliberately exact.
INITIAL_DISK_BYTES = 64 * GIB
INITIAL_PARTITIONS = (
    (1, 1 * MIB, 260 * MIB, "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"),
    (2, 261 * MIB, 16 * MIB, "e3c9e316-0b5c-4db8-817d-f92df00215ae"),
    (3, 277 * MIB, 67602743296, "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"),
    (4, 67893198848, 824180736, "de94bba4-06d1-4d40-a16a-bfd50179d6ac"),
)
RECOVERY_BYTES = 824180736
RECOVERY_TYPE = "{de94bba4-06d1-4d40-a16a-bfd50179d6ac}"
ROLE_TARGET_GIB = {"server": (30, 34), "jumpbox": (36, 40), "base": (20, 34)}
SERVER_FREE_BYTES = (8 * GIB, 12 * GIB)
JUMPBOX_FREE_BYTES = 16 * GIB
BASE_FREE_BYTES = 8 * GIB


class WindowsDiskResizeError(ValueError):
    """A Windows shrink request is outside this fixed workflow's scope."""


def _validate_request(target_disk_gib: int, role: str, job_id: str) -> tuple[int, str]:
    if type(target_disk_gib) is not int:
        raise WindowsDiskResizeError("target_disk_gib must be an integer")
    if not isinstance(role, str) or role not in ROLE_TARGET_GIB:
        raise WindowsDiskResizeError("role must be 'server', 'jumpbox', or internal 'base'")
    low, high = ROLE_TARGET_GIB[role]
    if not low <= target_disk_gib <= high:
        raise WindowsDiskResizeError(f"{role} target must be between {low} and {high} GiB")
    if not isinstance(job_id, str) or not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}", job_id):
        raise WindowsDiskResizeError("job_id must be a UUID")
    return target_disk_gib * GIB, str(uuid.UUID(job_id))


def plan_guest_layout(target_disk_gib: int, role: str, *, used_bytes: int,
                      supported_min_bytes: int) -> dict[str, int | str]:
    """Validate measured guest space and return exact planned partition bounds."""
    target_bytes, _ = _validate_request(target_disk_gib, role, "00000000-0000-4000-8000-000000000000")
    for value, name in ((used_bytes, "used_bytes"), (supported_min_bytes, "supported_min_bytes")):
        minimum = 1 if name == "supported_min_bytes" else 0
        if type(value) is not int or value < minimum:
            raise WindowsDiskResizeError(f"{name} must be an integer >= {minimum}")
    c_bytes = target_bytes - RECOVERY_BYTES - MIB - 277 * MIB
    free = c_bytes - used_bytes
    if c_bytes < supported_min_bytes:
        raise WindowsDiskResizeError("target C: size is below the measured NTFS supported minimum")
    if free < 0:
        raise WindowsDiskResizeError("measured C: usage exceeds the planned partition")
    if role == "server" and not SERVER_FREE_BYTES[0] <= free <= SERVER_FREE_BYTES[1]:
        raise WindowsDiskResizeError("measured usage does not fit the 8–12 GiB server headroom policy")
    if role == "jumpbox" and free < JUMPBOX_FREE_BYTES:
        raise WindowsDiskResizeError("measured usage does not fit the 16 GiB jumpbox headroom policy")
    if role == "base" and free < BASE_FREE_BYTES:
        raise WindowsDiskResizeError("measured usage does not fit the 8 GiB Windows base headroom policy")
    recovery_offset = target_bytes - MIB - RECOVERY_BYTES
    if recovery_offset < 290455552 + c_bytes or recovery_offset % MIB:
        raise WindowsDiskResizeError("planned recovery partition overlaps C: or is not MiB-aligned")
    return {"target_disk_bytes": target_bytes, "c_offset_bytes": 290455552,
            "c_size_bytes": c_bytes, "recovery_offset_bytes": recovery_offset,
            "recovery_size_bytes": RECOVERY_BYTES, "free_bytes": free}


def _script(target_disk_gib: int, role: str, job_id: str, *, verify: bool,
            verify_phase: str = "pre_host", native_unencrypted: bool = False) -> str:
    target_bytes, canonical_job = _validate_request(target_disk_gib, role, job_id)
    # Target C: ends before the preserved 786 MiB WinRE partition and a 1 MiB
    # GPT tail allowance. The original C offset is 277 MiB.
    c_bytes = target_bytes - RECOVERY_BYTES - MIB - 277 * MIB
    free_bounds = (SERVER_FREE_BYTES if role == "server" else
                   (JUMPBOX_FREE_BYTES, None) if role == "jumpbox" else
                   (BASE_FREE_BYTES, None))
    min_free = free_bounds[0]
    max_free = free_bounds[1]
    script = r'''$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$job = '__JOB__'
$role = '__ROLE__'
$targetBytes = [UInt64]__TARGET_BYTES__
$targetCBytes = [UInt64]__C_BYTES__
$recoveryBytes = [UInt64]824180736
$journalRoot = 'C:\ProgramData\Crange\DiskResize'
$jobRoot = Join-Path $journalRoot $job
$journalPath = Join-Path $jobRoot 'journal.json'
$winreBackup = Join-Path $jobRoot 'Winre.wim'
$nativeUnencrypted = $trueValue
$efiType = '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}'
$msrType = '{e3c9e316-0b5c-4db8-817d-f92df00215ae}'
$basicType = '{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}'
$reType = '{de94bba4-06d1-4d40-a16a-bfd50179d6ac}'
$expectedWinreLocation = '\\?\GLOBALROOT\device\harddisk0\partition4\Recovery\WindowsRE'
$initial = @(
  @{N=1; O=[UInt64]1048576; S=[UInt64]272629760; T=$efiType},
  @{N=2; O=[UInt64]273678336; S=[UInt64]16777216; T=$msrType},
  @{N=3; O=[UInt64]290455552; S=[UInt64]67602743296; T=$basicType},
  @{N=4; O=[UInt64]67893198848; S=[UInt64]824180736; T=$reType}
)
function Save-Journal($state) {
  $doc = [ordered]@{Version=1;Job=$job;Role=$role;TargetBytes=$targetBytes;TargetCBytes=$targetCBytes;State=$state;WinreSha256=$script:winreSha256;RecoveryPartitionGuid=$script:recoveryPartitionGuid}
  $tmp = "$journalPath.tmp"
  [IO.File]::WriteAllText($tmp, ($doc | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
  Move-Item -LiteralPath $tmp -Destination $journalPath -Force | Out-Null
}
function Get-WinrePathFromStatus {
  $raw = & "$env:windir\System32\reagentc.exe" /info 2>&1 | Out-String
  if ($LASTEXITCODE -ne 0 -or $raw -notmatch 'Windows RE status:\s+Enabled' -or $raw -notmatch ('Windows RE location:\s+' + [regex]::Escape($expectedWinreLocation))) { throw 'WinRE must be enabled on the exact recovery partition.' }
  return '\\?\GLOBALROOT\Device\Harddisk0\Partition4\Recovery\WindowsRE\Winre.wim'
}
function Assert-InitialLayout {
  $disk = Get-Disk -Number 0
  if ($disk.PartitionStyle -ne 'GPT' -or $disk.Size -ne [UInt64]68719476736 -or $disk.HealthStatus -ne 'Healthy' -or $disk.IsReadOnly -or $disk.IsOffline) { throw 'Disk 0 is not the expected healthy 64 GiB GPT disk.' }
  $parts = @(Get-Partition -DiskNumber 0 | Sort-Object PartitionNumber)
  if ($parts.Count -ne 4) { throw 'Expected exactly four GPT partitions.' }
  foreach ($want in $initial) {
    $p = $parts | Where-Object PartitionNumber -eq $want.N
    if (@($p).Count -ne 1 -or [UInt64]$p.Offset -ne $want.O -or [UInt64]$p.Size -ne $want.S -or $p.GptType.ToLowerInvariant() -ne $want.T) { throw "Partition $($want.N) differs from the approved layout." }
  }
  $c = Get-Partition -DiskNumber 0 -PartitionNumber 3
  if ($c.DriveLetter -ne 'C') { throw 'Approved partition 3 is not C:.' }
  $v = Get-Volume -DriveLetter C
  if ($v.FileSystem -ne 'NTFS' -or $v.HealthStatus -ne 'Healthy') { throw 'C: must be healthy NTFS.' }
  # Fail closed if the native BitLocker provider is unavailable or ambiguous.
  # Win32_EncryptableVolume conversion state 0 is FullyDecrypted; protection
  # state 0 is Off. Provider/method failures are terminating errors.
  $encClass = Get-CimClass -Namespace 'root/CIMV2/Security/MicrosoftVolumeEncryption' -ClassName Win32_EncryptableVolume -ErrorAction SilentlyContinue
  if ($encClass) {
    $enc = @(Get-CimInstance -Namespace 'root/CIMV2/Security/MicrosoftVolumeEncryption' -ClassName Win32_EncryptableVolume -Filter "DriveLetter='C:'")
    if ($enc.Count -ne 1) { throw 'Native BitLocker status for C: is unavailable or ambiguous.' }
    $conv = Invoke-CimMethod -InputObject $enc[0] -MethodName GetConversionStatus
    $prot = Invoke-CimMethod -InputObject $enc[0] -MethodName GetProtectionStatus
    if ($conv.ReturnValue -ne 0 -or $conv.ConversionStatus -ne 0 -or $prot.ReturnValue -ne 0 -or $prot.ProtectionStatus -ne 0) { throw 'C: BitLocker/device encryption must be fully decrypted with protection off.' }
  } elseif (-not $nativeUnencrypted) { throw 'Native BitLocker provider is unavailable and no compiled-in native unencrypted proof was supplied.' }
  if (Get-PSDrive -Name R -ErrorAction SilentlyContinue) { throw 'Temporary recovery drive letter R: is occupied.' }
  $supported = Get-PartitionSupportedSize -DiskNumber 0 -PartitionNumber 3
  if ($targetCBytes -lt [UInt64]$supported.SizeMin -or $targetCBytes -ge [UInt64]$c.Size) { throw 'Target C: size is outside Windows supported bounds.' }
  $used = [UInt64]$v.Size - [UInt64]$v.SizeRemaining
  if ($used -gt $targetCBytes) { throw 'Measured C: usage exceeds the planned partition.' }
  $freeAfter = $targetCBytes - $used
  if ($role -eq 'server' -and ($freeAfter -lt [UInt64]__MIN_FREE__ -or $freeAfter -gt [UInt64]__MAX_FREE__)) { throw 'Measured C: usage does not fit the server headroom policy.' }
  if ($role -eq 'jumpbox' -and $freeAfter -lt [UInt64]__MIN_FREE__) { throw 'Measured C: usage does not fit the jumpbox headroom policy.' }
  if ($role -eq 'base' -and $freeAfter -lt [UInt64]__MIN_FREE__) { throw 'Measured C: usage does not fit the Windows base headroom policy.' }
  return @{Disk=$disk;C=$c;Volume=$v;Used=$used}
}
function Assert-InitialWinre {
  $source = Get-WinrePathFromStatus
  if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw 'WinRE image is missing from its approved source.' }
  return $source
}
function Assert-Journal($j) {
  if ($j.Version -ne 1 -or $j.Job -ne $job -or $j.Role -ne $role -or [UInt64]$j.TargetBytes -ne $targetBytes -or [UInt64]$j.TargetCBytes -ne $targetCBytes) { throw 'Journal identity differs; preserve it for review.' }
  if ($j.State -notin @('backup_verified','winre_disabled','recovery_removed','c_shrunk','recovery_partition_created','recovery_created','winre_enabled','complete')) { throw 'Unknown journal state; preserve it for review.' }
}
'''
    script = (script.replace("__JOB__", canonical_job).replace("__ROLE__", role)
              .replace("__TARGET_BYTES__", str(target_bytes)).replace("__C_BYTES__", str(c_bytes))
              .replace("$trueValue", "$true" if native_unencrypted else "$false")
              .replace("__MIN_FREE__", str(min_free)).replace("__MAX_FREE__", str(max_free or 0)))
    if verify:
        if verify_phase not in ("pre_host", "post_host"):
            raise WindowsDiskResizeError("verify phase must be 'pre_host' or 'post_host'")
        expected_disk_bytes = INITIAL_DISK_BYTES if verify_phase == "pre_host" else target_bytes
        script = r'''$ErrorActionPreference = 'Stop'
$job = '__JOB__'
$role = '__ROLE__'
$phase = '__PHASE__'
$expectedDiskBytes = [UInt64]__EXPECTED_DISK_BYTES__
$targetBytes = [UInt64]__TARGET_BYTES__
$targetCBytes = [UInt64]__C_BYTES__
$recoveryBytes = [UInt64]824180736
$efiType = '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}'
$msrType = '{e3c9e316-0b5c-4db8-817d-f92df00215ae}'
$basicType = '{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}'
$reType = '{de94bba4-06d1-4d40-a16a-bfd50179d6ac}'
$expectedWinreLocation = '\\?\GLOBALROOT\device\harddisk0\partition4\Recovery\WindowsRE'
'''.replace("__JOB__", canonical_job).replace("__ROLE__", role).replace(
            "__PHASE__", verify_phase).replace("__EXPECTED_DISK_BYTES__", str(expected_disk_bytes)).replace(
            "__TARGET_BYTES__", str(target_bytes)).replace("__C_BYTES__", str(c_bytes))
        script += r'''
$disk = Get-Disk -Number 0
if ($disk.PartitionStyle -ne 'GPT' -or $disk.HealthStatus -ne 'Healthy' -or $disk.IsOffline -or [UInt64]$disk.Size -ne $expectedDiskBytes) { throw 'Disk 0 is not healthy or does not have the capacity expected for this verification phase.' }
$parts = @(Get-Partition -DiskNumber 0 | Sort-Object Offset)
if ($parts.Count -ne 4) { throw 'Expected EFI, MSR, C:, and one recovery partition.' }
if ($parts[0].PartitionNumber -ne 1 -or [UInt64]$parts[0].Offset -ne [UInt64]1048576 -or [UInt64]$parts[0].Size -ne [UInt64]272629760 -or $parts[0].GptType.ToLowerInvariant() -ne $efiType -or $parts[1].PartitionNumber -ne 2 -or [UInt64]$parts[1].Offset -ne [UInt64]273678336 -or [UInt64]$parts[1].Size -ne [UInt64]16777216 -or $parts[1].GptType.ToLowerInvariant() -ne $msrType) { throw 'EFI or MSR partition differs from the approved layout.' }
if ([UInt64]$parts[2].Offset -ne [UInt64]290455552 -or [UInt64]$parts[2].Size -ne $targetCBytes -or $parts[2].DriveLetter -ne 'C') { throw 'C: does not match the planned end.' }
if ($parts[2].GptType.ToLowerInvariant() -ne $basicType) { throw 'C: is not the expected basic-data partition.' }
$r = $parts[3]
if ($r.PartitionNumber -ne 4 -or $r.GptType.ToLowerInvariant() -ne $reType -or [UInt64]$r.Offset -ne ($targetBytes - 1048576 - $recoveryBytes) -or [UInt64]$r.Size -ne $recoveryBytes -or [UInt64]($r.Offset + $r.Size) -gt ($targetBytes - 1048576) -or -not $r.IsHidden -or ($r.NoDefaultDriveLetter -eq $false)) { throw 'Recovery partition geometry or required GPT attributes differ from the target.' }
foreach ($p in $parts) { if ([UInt64]($p.Offset + $p.Size) -gt ($targetBytes - 1048576)) { throw 'A guest partition extends beyond the planned host disk tail.' } }
$status = & "$env:windir\System32\reagentc.exe" /info 2>&1 | Out-String
if ($LASTEXITCODE -ne 0 -or $status -notmatch 'Windows RE status:\s+Enabled' -or $status -notmatch ('Windows RE location:\s+' + [regex]::Escape($expectedWinreLocation))) { throw 'WinRE is not enabled at the relocated recovery partition.' }
$v = Get-Volume -DriveLetter C
$used = [UInt64]$v.Size - [UInt64]$v.SizeRemaining
if ($used -gt $targetCBytes) { throw 'Measured C: usage exceeds its planned partition.' }
$free = $targetCBytes - $used
if ($role -eq 'server' -and ($free -lt [UInt64]__MIN_FREE__ -or $free -gt [UInt64]__MAX_FREE__)) { throw 'Final server free space is outside policy.' }
if ($role -eq 'jumpbox' -and $free -lt [UInt64]__MIN_FREE__) { throw 'Final jumpbox free space is below policy.' }
if ($role -eq 'base' -and $free -lt [UInt64]__MIN_FREE__) { throw 'Final Windows base free space is below policy.' }
[ordered]@{Ok=$true;Job=$job;Role=$role;Phase=$phase;CurrentDiskBytes=[UInt64]$disk.Size;TargetBytes=$targetBytes;COffset=[UInt64]$parts[2].Offset;CBytes=[UInt64]$parts[2].Size;FreeBytes=$free;RecoveryOffset=[UInt64]$r.Offset;RecoveryBytes=[UInt64]$r.Size;WinRE='Enabled'} | ConvertTo-Json -Compress
'''.replace("__MIN_FREE__", str(min_free)).replace("__MAX_FREE__", str(max_free or 0))
        return script

    script += r'''
if (-not (Test-Path -LiteralPath $journalRoot)) { New-Item -ItemType Directory -Path $journalRoot -Force | Out-Null }
if (-not (Test-Path -LiteralPath $journalPath)) {
  $null = Assert-InitialLayout
  $source = Assert-InitialWinre
  if ((Test-Path -LiteralPath $jobRoot) -and @(Get-ChildItem -LiteralPath $jobRoot -Force).Count -ne 0) { throw 'Unjournaled job directory contains files; preserve it.' }
  New-Item -ItemType Directory -Path $jobRoot -Force | Out-Null
  $acl = New-Object Security.AccessControl.DirectorySecurity
  $acl.SetAccessRuleProtection($true,$false)
  foreach ($sid in @('S-1-5-18','S-1-5-32-544')) { $identity = [Security.Principal.SecurityIdentifier]::new($sid); $rule = [Security.AccessControl.FileSystemAccessRule]::new($identity,[Security.AccessControl.FileSystemRights]::FullControl,[Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit',[Security.AccessControl.PropagationFlags]::None,[Security.AccessControl.AccessControlType]::Allow); $acl.AddAccessRule($rule) }
  Set-Acl -LiteralPath $jobRoot -AclObject $acl
  $sourceSha256 = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
  if (-not $sourceSha256) { throw 'Source WinRE image hash is unavailable.' }
  Copy-Item -LiteralPath $source -Destination $winreBackup | Out-Null
  $script:winreSha256 = (Get-FileHash -LiteralPath $winreBackup -Algorithm SHA256).Hash
  if (-not $script:winreSha256 -or $script:winreSha256 -ne $sourceSha256) { throw 'Retained WinRE image differs from its source.' }
  Save-Journal 'backup_verified'
}
if (-not (Test-Path -LiteralPath $journalPath)) { throw 'Job directory exists without its owned journal; preserve it.' }
$j = Get-Content -LiteralPath $journalPath -Raw | ConvertFrom-Json
Assert-Journal $j
$script:winreSha256 = [string]$j.WinreSha256
$script:recoveryPartitionGuid = [string]$j.RecoveryPartitionGuid
if (-not $script:winreSha256 -or -not (Test-Path -LiteralPath $winreBackup -PathType Leaf) -or (Get-FileHash -LiteralPath $winreBackup -Algorithm SHA256).Hash -ne $script:winreSha256) { throw 'Journaled WinRE backup is absent or changed.' }
if ($j.State -eq 'backup_verified') {
  $null = Assert-InitialLayout
  $status = & "$env:windir\System32\reagentc.exe" /info 2>&1 | Out-String
  if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect WinRE state; preserve journal.' }
  if ($status -match 'Windows RE status:\s+Enabled') {
    if ($status -notmatch ('Windows RE location:\s+' + [regex]::Escape($expectedWinreLocation))) { throw 'Enabled WinRE is not on the approved recovery partition.' }
    & "$env:windir\System32\reagentc.exe" /disable | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'reagentc /disable failed; stop for review.' }
  } elseif ($status -notmatch 'Windows RE status:\s+Disabled') { throw 'WinRE has an unknown state; preserve journal.' }
  # A crash after /disable but before this write is reconciled from reagentc.
  Save-Journal 'winre_disabled'; $j.State = 'winre_disabled'
}
if ($j.State -eq 'winre_disabled') {
  $parts = @(Get-Partition -DiskNumber 0 | Sort-Object PartitionNumber)
  if ($parts.Count -eq 4) {
    if ([UInt64]$parts[2].Offset -ne [UInt64]290455552 -or [UInt64]$parts[2].Size -ne [UInt64]67602743296 -or [UInt64]$parts[3].Offset -ne [UInt64]67893198848 -or [UInt64]$parts[3].Size -ne $recoveryBytes -or $parts[3].GptType.ToLowerInvariant() -ne $reType) { throw 'Partition state changed after WinRE disable; preserve journal.' }
    Remove-Partition -DiskNumber 0 -PartitionNumber 4 -Confirm:$false | Out-Null
  } elseif ($parts.Count -ne 3 -or [UInt64]$parts[2].Offset -ne [UInt64]290455552 -or [UInt64]$parts[2].Size -ne [UInt64]67602743296) { throw 'Partition state is neither the known pre-delete nor post-delete state; preserve journal.' }
  # Reconcile only the known result of removal if interrupted before save.
  Save-Journal 'recovery_removed'; $j.State = 'recovery_removed'
}
if ($j.State -eq 'recovery_removed') {
  $c = Get-Partition -DiskNumber 0 -PartitionNumber 3
  if ([UInt64]$c.Size -eq [UInt64]67602743296 -and [UInt64]$c.Offset -eq [UInt64]290455552) {
    Resize-Partition -DiskNumber 0 -PartitionNumber 3 -Size $targetCBytes | Out-Null
  } elseif ([UInt64]$c.Size -ne $targetCBytes -or [UInt64]$c.Offset -ne [UInt64]290455552) { throw 'C: is neither its original nor planned size; preserve journal.' }
  Save-Journal 'c_shrunk'; $j.State = 'c_shrunk'
}
if ($j.State -eq 'c_shrunk') {
  $c = Get-Partition -DiskNumber 0 -PartitionNumber 3
  if ([UInt64]$c.Size -ne $targetCBytes) { throw 'C: size differs from journaled target.' }
  $recoveryOffset = $targetBytes - 1048576 - $recoveryBytes
  $existingRecovery = @(Get-Partition -DiskNumber 0 | Where-Object GptType -eq $reType)
  if ($existingRecovery.Count -ne 0) { throw 'Recovery partition creation is present without its journal identity; preserve it for review.' }
  $r = New-Partition -DiskNumber 0 -Offset $recoveryOffset -Size $recoveryBytes -GptType $reType -DriveLetter R
  if ([UInt64]$r.Offset -ne $recoveryOffset -or [UInt64]$r.Size -ne $recoveryBytes) { throw 'New recovery partition placement differs from target.' }
  if ($r.PartitionNumber -ne 4 -or -not $r.Guid) { throw 'New recovery partition identity is unavailable.' }
  $script:recoveryPartitionGuid = [string]$r.Guid
  Save-Journal 'recovery_partition_created'; $j.State = 'recovery_partition_created'
}
if ($j.State -eq 'recovery_partition_created') {
  $r = Get-Partition -DiskNumber 0 | Where-Object { $_.GptType -eq $reType -and [string]$_.Guid -eq $script:recoveryPartitionGuid }
  if (@($r).Count -ne 1 -or $r.PartitionNumber -ne 4 -or [UInt64]$r.Offset -ne ($targetBytes - 1048576 - $recoveryBytes) -or [UInt64]$r.Size -ne $recoveryBytes -or $r.DriveLetter -ne 'R') { throw 'Journal-owned recovery partition identity or geometry differs.' }
  $rv = Get-Volume -Partition $r -ErrorAction SilentlyContinue
  if (-not $rv) { throw 'Journal-owned recovery volume is not visible; preserve journal.' }
  if ($rv.FileSystem -eq 'RAW' -or ([string]$rv.FileSystem -eq '' -and [string]$rv.FileSystemType -eq 'Unknown' -and [UInt64]$rv.Size -eq 0 -and $rv.HealthStatus -eq 'Healthy')) {
    Format-Volume -Partition $r -FileSystem NTFS -NewFileSystemLabel 'Windows RE tools' -Confirm:$false | Out-Null
  } elseif ($rv.FileSystem -ne 'NTFS' -or $rv.FileSystemLabel -ne 'Windows RE tools' -or $rv.HealthStatus -ne 'Healthy') { throw 'Journal-owned recovery volume has an unexpected filesystem; preserve journal.' }
  New-Item -ItemType Directory -Path 'R:\Recovery\WindowsRE' -Force | Out-Null
  $restored = 'R:\Recovery\WindowsRE\Winre.wim'
  if (-not (Test-Path -LiteralPath $restored -PathType Leaf)) { Copy-Item -LiteralPath $winreBackup -Destination $restored | Out-Null }
  if ((Get-FileHash -LiteralPath $restored -Algorithm SHA256).Hash -ne $script:winreSha256) { throw 'Restored WinRE image hash differs.' }
  Save-Journal 'recovery_created'; $j.State = 'recovery_created'
}
if ($j.State -eq 'recovery_created') {
  $r = Get-Partition -DiskNumber 0 | Where-Object GptType -eq $reType
  if (@($r).Count -ne 1 -or [UInt64]$r.Offset -ne ($targetBytes - 1048576 - $recoveryBytes) -or [UInt64]$r.Size -ne $recoveryBytes) { throw 'Recovery partition differs from planned placement.' }
  & "$env:windir\System32\reagentc.exe" /setreimage /path 'R:\Recovery\WindowsRE' | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'reagentc /setreimage failed; retain journal and backup.' }
  & "$env:windir\System32\reagentc.exe" /enable | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'reagentc /enable failed; retain journal and backup.' }
  $status = & "$env:windir\System32\reagentc.exe" /info 2>&1 | Out-String
  if ($LASTEXITCODE -ne 0 -or $status -notmatch 'Windows RE status:\s+Enabled' -or $status -notmatch ('Windows RE location:\s+' + [regex]::Escape($expectedWinreLocation))) { throw 'WinRE did not enable at the new recovery partition.' }
  Save-Journal 'winre_enabled'; $j.State = 'winre_enabled'
}
if ($j.State -eq 'winre_enabled') {
  $c = Get-Partition -DiskNumber 0 -PartitionNumber 3
  $r = Get-Partition -DiskNumber 0 | Where-Object GptType -eq $reType
  if ([UInt64]$c.Size -ne $targetCBytes -or @($r).Count -ne 1 -or [UInt64]$r.Offset -ne ($targetBytes - 1048576 - $recoveryBytes)) { throw 'Final guest partition geometry is not verified.' }
  if ($r.DriveLetter -eq 'R') { Remove-PartitionAccessPath -DiskNumber 0 -PartitionNumber $r.PartitionNumber -AccessPath 'R:' | Out-Null }
  $dp = Join-Path $jobRoot 'set-recovery-attributes.txt'
  @('select disk 0',"select partition $($r.PartitionNumber)",'gpt attributes=0x8000000000000001','exit') | Set-Content -LiteralPath $dp -Encoding Ascii
  & "$env:windir\System32\diskpart.exe" /s $dp | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Could not set the fixed WinRE GPT attributes; preserve journal.' }
  $r = Get-Partition -DiskNumber 0 -PartitionNumber 4
  if (-not $r.IsHidden -or ($r.NoDefaultDriveLetter -eq $false)) { throw 'WinRE GPT hidden/no-default-letter attributes did not verify.' }
  $status = & "$env:windir\System32\reagentc.exe" /info 2>&1 | Out-String
  if ($LASTEXITCODE -ne 0 -or $status -notmatch 'Windows RE status:\s+Enabled' -or $status -notmatch ('Windows RE location:\s+' + [regex]::Escape($expectedWinreLocation))) { throw 'WinRE changed while recovery attributes were applied.' }
  Save-Journal 'complete'; $j.State = 'complete'
}
if ($j.State -eq 'complete') {
  $c = Get-Partition -DiskNumber 0 -PartitionNumber 3
  $r = Get-Partition -DiskNumber 0 | Where-Object GptType -eq $reType
  $parts = @(Get-Partition -DiskNumber 0 | Sort-Object Offset)
  $v = Get-Volume -DriveLetter C
  $used = [UInt64]$v.Size - [UInt64]$v.SizeRemaining
  if ($used -gt $targetCBytes) { throw 'Measured C: usage exceeds its planned partition.' }
  $free = $targetCBytes - $used
  if ($role -eq 'server' -and ($free -lt [UInt64]__MIN_FREE__ -or $free -gt [UInt64]__MAX_FREE__)) { throw 'Final server free space is outside policy.' }
  if ($role -eq 'jumpbox' -and $free -lt [UInt64]__MIN_FREE__) { throw 'Final jumpbox free space is below policy.' }
  if ($role -eq 'base' -and $free -lt [UInt64]__MIN_FREE__) { throw 'Final Windows base free space is below policy.' }
  $status = & "$env:windir\System32\reagentc.exe" /info 2>&1 | Out-String
  $disk = Get-Disk -Number 0
  if ([UInt64]$disk.Size -ne [UInt64]68719476736) { throw 'Guest disk changed after preparation; use the phase verifier.' }
  if ($parts.Count -ne 4 -or [UInt64]$parts[0].Offset -ne [UInt64]1048576 -or [UInt64]$parts[0].Size -ne [UInt64]272629760 -or $parts[0].GptType.ToLowerInvariant() -ne $efiType -or [UInt64]$parts[1].Offset -ne [UInt64]273678336 -or [UInt64]$parts[1].Size -ne [UInt64]16777216 -or $parts[1].GptType.ToLowerInvariant() -ne $msrType -or [UInt64]$c.Offset -ne [UInt64]290455552 -or [UInt64]$c.Size -ne $targetCBytes -or $c.GptType.ToLowerInvariant() -ne $basicType -or @($r).Count -ne 1 -or [string]$r.Guid -ne $script:recoveryPartitionGuid -or [UInt64]$r.Offset -ne ($targetBytes - 1048576 - $recoveryBytes) -or [UInt64]$r.Size -ne $recoveryBytes -or $r.DriveLetter -or -not $r.IsHidden -or ($r.NoDefaultDriveLetter -eq $false) -or $LASTEXITCODE -ne 0 -or $status -notmatch 'Windows RE status:\s+Enabled' -or $status -notmatch ('Windows RE location:\s+' + [regex]::Escape($expectedWinreLocation))) { throw 'Completed journal no longer matches verified guest state.' }
  [ordered]@{ReadyForHostShrink=$true;Job=$job;Role=$role;CurrentDiskBytes=[UInt64]$disk.Size;TargetDiskBytes=$targetBytes;COffset=[UInt64]$c.Offset;CBytes=[UInt64]$c.Size;CUsedBytes=$used;CFreeBytes=$free;RecoveryOffset=[UInt64]$r.Offset;RecoveryBytes=[UInt64]$r.Size;WinreSha256=$script:winreSha256;WinRE='Enabled'} | ConvertTo-Json -Compress
}
'''
    script = script.replace("__MIN_FREE__", str(min_free)).replace("__MAX_FREE__", str(max_free or 0))
    # The final phase check above intentionally cannot call the original-layout
    # assertion on the reduced layout; remove that no-op assertion line.
    script = script.replace("  $null = Assert-InitialLayout -ErrorAction SilentlyContinue\n", "")
    return script


def build_prepare_script(target_disk_gib: int, role: str, job_id: str,
                         *, native_unencrypted: bool = False) -> str:
    """Return fixed PowerShell to stage the guest partition layout for host shrink.

    The script will reduce C: and relocate WinRE inside the requested target
    capacity. It does not shrink the virtual disk. Require a verified full-disk
    backup outside this interface before running it. ``native_unencrypted`` is
    an internal worker-only proof bit for an isolated probe where a private
    root-owned reader has proved NTFS/OEM bytes for the exact owned VM's disk
    and root partition, identified by VMID and stable disk identity.
    It is never a user/HTTP authorization option; without it, missing native
    BitLocker status blocks the generated script.
    """
    if type(native_unencrypted) is not bool:
        raise WindowsDiskResizeError("native_unencrypted must be a boolean")
    return _script(target_disk_gib, role, job_id, verify=False,
                   native_unencrypted=native_unencrypted)


def build_verify_script(target_disk_gib: int, role: str, job_id: str,
                        phase: str = "pre_host") -> str:
    """Return read-only PowerShell for pre-host or post-host verification."""
    return _script(target_disk_gib, role, job_id, verify=True, verify_phase=phase)
