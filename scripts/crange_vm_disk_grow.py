"""Fixed QGA disk growth for explicitly sized scenario VMs.

The worker validates desired/applied/native ownership before reading a guest.
Guest commands are embedded collectors or fixed scripts; user supplied strings
are never interpreted as commands. Unsupported layouts fail closed.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import time
from pathlib import Path


class DiskGrowError(ValueError):
    """A selected VM cannot be safely inspected or grown."""


def linux_inspector():
    return ["/usr/bin/python3", "-c", r'''import json,subprocess
def run(*a):return subprocess.run(a,check=True,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE).stdout.strip()
root=run('findmnt','-n','-o','SOURCE,FSTYPE','/')
source,fs=root.split(None,1)
ls=json.loads(run('lsblk','-b','-J','-O'))['blockdevices']
def flatten(rows):
 for x in rows:
  yield x
  yield from flatten(x.get('children') or [])
allx=list(flatten(ls)); node=next((x for x in allx if x.get('path')==source),None)
if node is None:raise SystemExit('root device unresolved')
disk=next((x for x in allx if x.get('name')==node.get('pkname')),None)
if disk is None:raise SystemExit('root parent unresolved')
# GPT partition numbers need not follow physical order (Debian uses 1/14/15).
parts=sorted([x for x in disk.get('children') or [] if x.get('type')=='part'],key=lambda x:int(x.get('start') or 0))
row={'os':'linux','disk':disk.get('path'),'disk_bytes':int(disk.get('size') or 0),'table':str(disk.get('pttype') or '').lower(),'root':source,'filesystem':fs,'partition':int(node.get('partn') or 0),'partition_bytes':int(node.get('size') or 0),'partition_start':int(node.get('start') or 0),'partition_count':len(parts),'root_is_last':bool(parts and parts[-1].get('path')==source),'mount_options':run('findmnt','-n','-o','OPTIONS','/')}
if fs in ('ext4','xfs','btrfs'):
 row['free_bytes']=int(run('df','-B1','--output=avail','/').splitlines()[-1]);row['filesystem_bytes']=int(run('df','-B1','--output=size','/').splitlines()[-1])
else:raise SystemExit('unsupported root filesystem')
print(json.dumps(row,separators=(',',':')))''']


def windows_inspector():
    ps = r'''$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
$d=@(Get-Disk|Where-Object {$_.IsBoot -and $_.IsSystem});if($d.Count-ne 1){throw 'boot disk ambiguity'}
$disk=$d[0];$p=@(Get-Partition -DiskNumber $disk.Number|Sort-Object Offset)
$c=@($p|Where-Object {$_.DriveLetter -eq 'C'});if($c.Count-ne 1){throw 'C partition ambiguity'}
$tail=@($p|Where-Object {$_.Offset -gt $c[0].Offset}|Select-Object -Last 1)
$w=Get-CimInstance Win32_OperatingSystem
$vol=Get-Volume -DriveLetter C;$free=$vol.SizeRemaining;$cfs=$vol.FileSystem
if($vol.HealthStatus -ne 'Healthy'){throw 'C volume health is not healthy'}
$encrypted=$true;$encryptionEvidence=''
$encClass=Get-CimClass -Namespace 'root/CIMV2/Security/MicrosoftVolumeEncryption' -ClassName Win32_EncryptableVolume -ErrorAction SilentlyContinue
if($encClass){
 $enc=@(Get-CimInstance -Namespace 'root/CIMV2/Security/MicrosoftVolumeEncryption' -ClassName Win32_EncryptableVolume -Filter "DriveLetter='C:'")
 if($enc.Count-ne 1){throw 'C encryption state is ambiguous'}
 $conv=Invoke-CimMethod -InputObject $enc[0] -MethodName GetConversionStatus
 $prot=Invoke-CimMethod -InputObject $enc[0] -MethodName GetProtectionStatus
 if($conv.ReturnValue-ne 0 -or $conv.ConversionStatus-ne 0 -or $prot.ReturnValue-ne 0 -or $prot.ProtectionStatus-ne 0){throw 'C must be fully decrypted with protection off'}
 $encrypted=$false;$encryptionEvidence='native-conversion-status'
}else{
 $service=Get-Service -Name BDESVC -ErrorAction SilentlyContinue
 if($service){throw 'Encryption provider unavailable while the BitLocker service is installed'}
 $device='\\.\PhysicalDrive'+$disk.Number
 $raw=[IO.FileStream]::new($device,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::ReadWrite)
 try{[void]$raw.Seek([int64]$c[0].Offset,[IO.SeekOrigin]::Begin);$sector=New-Object byte[] 512;if($raw.Read($sector,0,512)-ne 512){throw 'Incomplete native partition header'}}finally{$raw.Dispose()}
 if([Text.Encoding]::ASCII.GetString($sector,3,8)-ne 'NTFS    ' -or $sector[510]-ne 85 -or $sector[511]-ne 170){throw 'Native physical C header does not prove unencrypted NTFS'}
 $encrypted=$false;$encryptionEvidence='native-ntfs-header-without-encryption-provider-or-service'
}
$reagent=reagentc /info|Out-String;if($LASTEXITCODE -ne 0){throw 'WinRE status unavailable'}
$enabled=$reagent -match 'Windows RE status:\s+Enabled';$pathLine=($reagent -split "`n"|Where-Object {$_ -match 'Windows RE location'}|Select-Object -First 1)
$location='';if($pathLine){$location=($pathLine -split ':',2)[1].Trim()};$match=[regex]::Match($location,'harddisk\d+\\partition(\d+)','IgnoreCase');$locationPartition=$(if($match.Success){[int]$match.Groups[1].Value}else{$null})
$journal='C:\ProgramData\CRANGE\vm-disk-grow.json';$root=Split-Path $journal
if(Test-Path -LiteralPath $root){$i=Get-Item -Force -LiteralPath $root;if($i.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'growth journal directory is redirected'}}
$growth=$null;if(Test-Path -LiteralPath $journal){$i=Get-Item -Force -LiteralPath $journal;if($i.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'growth journal is redirected'};try{$growth=Get-Content -Raw -LiteralPath $journal|ConvertFrom-Json}catch{throw 'guest growth journal is invalid'}}
$backup='C:\ProgramData\CRANGE\winre.wim';$backupHash='';if(Test-Path -LiteralPath $backup){$i=Get-Item -Force -LiteralPath $backup;if($i.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'WinRE backup is redirected'};$backupHash=(Get-FileHash -Algorithm SHA256 -LiteralPath $backup).Hash}
$rvol=Get-Volume -DriveLetter R -ErrorAction SilentlyContinue;$rpart=$(if($rvol){Get-Partition -DriveLetter R -ErrorAction SilentlyContinue}else{$null})
$rAvailable=(!$rvol) -and (!(Test-Path 'R:\'))
[ordered]@{os='windows';disk_number=$disk.Number;disk_bytes=[int64]$disk.Size;partition_style=[string]$disk.PartitionStyle;partitions=@($p|%{$v=Get-Volume -Partition $_ -ErrorAction SilentlyContinue;[ordered]@{number=$_.PartitionNumber;type=[string]$_.GptType;offset=[int64]$_.Offset;size=[int64]$_.Size;letter=[string]$_.DriveLetter;filesystem=[string]$v.FileSystem}});c_number=$c[0].PartitionNumber;c_offset=[int64]$c[0].Offset;c_bytes=[int64]$c[0].Size;filesystem_bytes=[int64]$vol.Size;c_fs=[string]$cfs;c_health=[string]$vol.HealthStatus;encrypted=[bool]$encrypted;encryption_evidence=$encryptionEvidence;tail_number=$(if($tail.Count){$tail[0].PartitionNumber}else{$null});tail_type=$(if($tail.Count){[string]$tail[0].GptType}else{''});tail_size=$(if($tail.Count){[int64]$tail[0].Size}else{0});winre_enabled=[bool]$enabled;winre_location_partition=$locationPartition;drive_r_available=[bool]$rAvailable;r_partition=$(if($rpart){$rpart.PartitionNumber}else{$null});r_type=$(if($rpart){[string]$rpart.GptType}else{''});r_size=$(if($rpart){[int64]$rpart.Size}else{0});r_label=$(if($rvol){$rvol.FileSystemLabel}else{''});growth_journal=$growth;winre_backup_sha256=$backupHash;free_bytes=[int64]$free;windows_directory=[string]$w.WindowsDirectory}|ConvertTo-Json -Compress -Depth 6'''
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
            base64.b64encode(ps.encode("utf-16le")).decode("ascii")]


def validate_linux_layout(m):
    if (not isinstance(m, dict) or m.get('os') != 'linux' or m.get('table') != 'gpt'
            or m.get('filesystem') not in {'ext4','xfs','btrfs'}
            or m.get('root_is_last') is not True or type(m.get('partition')) is not int or m['partition']<1
            or type(m.get('partition_count')) is not int or m['partition_count']<1
            or type(m.get('disk_bytes')) is not int or type(m.get('partition_bytes')) is not int
            or type(m.get('free_bytes')) is not int or 'ro' in m.get('mount_options','').split(',')):
        raise DiskGrowError('Linux root layout is unsupported or ambiguous.')
    if not re.fullmatch(r'/dev/(?:sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme[0-9]+n[0-9]+)(?:p?[0-9]+)?',str(m.get('root',''))):
        raise DiskGrowError('Linux root device identity is unsupported.')
    if m.get('partition_bytes',0)<=0 or m.get('disk_bytes',0)<=m.get('partition_bytes',0):
        raise DiskGrowError('Linux disk and partition capacities are inconsistent.')
    return dict(m)


def validate_windows_layout(m):
    if not isinstance(m,dict) or m.get('os')!='windows' or m.get('partition_style')!='GPT' or m.get('c_fs')!='NTFS':
        raise DiskGrowError('Windows system disk must use GPT and NTFS C:.')
    rows=m.get('partitions')
    if not isinstance(rows,list) or len(rows) not in (3,4):raise DiskGrowError('Windows partition layout is unsupported.')
    ordered=sorted(rows,key=lambda p:p.get('offset',-1))
    # EFI, MSR, C:, optional standard WinRE at the end.
    if ordered[0].get('type','').lower()!='{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}' or ordered[1].get('type','').lower()!='{e3c9e316-0b5c-4db8-817d-f92df00215ae}' or ordered[2].get('letter')!='C':
        raise DiskGrowError('Windows system disk is not the approved EFI/MSR/C layout.')
    if len(ordered)==4 and (ordered[3].get('type','').lower()!='{de94bba4-06d1-4d40-a16a-bfd50179d6ac}' or ordered[3].get('filesystem')!='NTFS'):
        raise DiskGrowError('Windows tail partition is not standard WinRE.')
    if m.get('c_number')!=ordered[2].get('number') or (len(ordered)==4 and m.get('tail_number')!=ordered[3].get('number')):
        raise DiskGrowError('Windows partition identity is inconsistent.')
    journal=m.get('growth_journal') or {}
    phases={'initial','backed-up','recovery-removed','c-grown','creating-recovery','recovery-created','recovery-restored','complete'}
    active=bool(journal) and journal.get('phase') in phases-{'initial','complete'}
    if journal and (journal.get('phase') not in phases or journal.get('disk')!=m.get('disk_number')
            or journal.get('c_number')!=m.get('c_number') or journal.get('c_offset')!=m.get('c_offset')
            or type(journal.get('tail_bytes')) is not int or type(journal.get('was_enabled')) is not bool):
        raise DiskGrowError('Windows guest growth journal identity is invalid.')
    if journal and (type(journal.get('old_c')) is not int or type(journal.get('c_target')) is not int
            or type(journal.get('target')) is not int or journal.get('target')<m.get('disk_bytes',0)):
        raise DiskGrowError('Windows guest growth journal capacities are invalid.')
    if journal:
        expected=((journal['target']-m['c_offset']-journal['tail_bytes']-1024**2)//(1024**2))*(1024**2)
        if expected<=0 or journal['c_target']!=expected:
            raise DiskGrowError('Windows guest growth journal partition target is inconsistent.')
    if journal:
        phase=journal.get('phase')
        allowed_sizes=(journal.get('old_c'),journal.get('c_target')) if phase in ('initial','backed-up','recovery-removed') else (journal.get('c_target'),)
        if m.get('c_bytes') not in allowed_sizes:
            raise DiskGrowError('Windows C: capacity differs from its guest recovery checkpoint.')
    if active and journal.get('tail_bytes',0)>0:
        if (not re.fullmatch(r'[A-Fa-f0-9]{64}',str(journal.get('winre_sha256','')))
                or m.get('winre_backup_sha256')!=journal.get('winre_sha256')):
            raise DiskGrowError('Windows interrupted WinRE operation has no matching verified backup.')
    recovery_letter_owned=(bool(journal) and m.get('r_type')=='{de94bba4-06d1-4d40-a16a-bfd50179d6ac}'
                           and m.get('r_size')==journal.get('tail_bytes')
                           and ((active and m.get('r_partition')==journal.get('new_tail_number')
                                 and m.get('r_label') in ('','Windows RE tools'))
                                or (active and journal.get('phase')=='backed-up'
                                    and m.get('r_partition')==journal.get('tail_number'))
                                or (active and journal.get('phase')=='creating-recovery'
                                    and m.get('r_label') in ('','Windows RE tools'))
                                or (journal.get('phase')=='initial' and m.get('r_partition')==journal.get('tail_number'))))
    if (m.get('c_health')!='Healthy' or m.get('encrypted') is not False
            or type(m.get('winre_enabled')) is not bool
            or (m.get('drive_r_available') is not True and not recovery_letter_owned)):
        raise DiskGrowError('Windows C: health, encryption, WinRE state, or recovery drive availability is unsupported.')
    if len(ordered)==4 and (m.get('tail_size')!=ordered[3].get('size')
            or (m.get('winre_enabled') is True and m.get('winre_location_partition')!=ordered[3].get('number') and not active)):
        raise DiskGrowError('Windows recovery image is not bound to the measured WinRE partition.')
    if len(ordered)==4 and not 500*1024**2<=int(m.get('tail_size') or 0)<=2*1024**3:
        raise DiskGrowError('Windows recovery partition size is outside the supported range.')
    if (m.get('c_bytes')!=ordered[2].get('size') or m.get('c_offset')!=ordered[2].get('offset')
            or type(m.get('disk_bytes')) is not int or type(m.get('filesystem_bytes')) is not int
            or not 0 <= m['c_bytes'] - m['filesystem_bytes'] <= 2*1024**2):
        raise DiskGrowError('Windows measured system disk capacities are inconsistent.')
    if len(ordered)==3 and active and journal.get('tail_bytes',0)>0:
        if str(journal.get('tail_type','')).lower()!='{de94bba4-06d1-4d40-a16a-bfd50179d6ac}' or not 500*1024**2<=journal['tail_bytes']<=2*1024**3:
            raise DiskGrowError('Windows recovery resume journal has an unsupported partition identity.')
    elif len(ordered)==3 and (m.get('winre_enabled') is True or m.get('winre_location_partition') is not None):
        raise DiskGrowError('Windows has an enabled or configured WinRE location without a recovery partition.')
    if (ordered[0].get('size')!=260*1024**2 or ordered[1].get('size')!=16*1024**2
            or ordered[2].get('offset')!=ordered[1].get('offset',0)+ordered[1].get('size',0)
            or (len(ordered)==4 and ordered[3].get('offset')!=ordered[2].get('offset',0)+ordered[2].get('size',0))):
        raise DiskGrowError('Windows partition sizes or adjacency differ from the approved standard layout.')
    return dict(m)


def linux_already_grown(measurement, target_gb):
    target=target_gb*1024**3
    return (measurement.get('disk_bytes')==target
            and measurement.get('partition_bytes',0)>=target-2*1024**3
            and measurement.get('filesystem_bytes',0)>=target-2*1024**3
            and measurement.get('free_bytes',0)>0)


def windows_already_grown(measurement, target_gb):
    target=target_gb*1024**3
    journal=measurement.get('growth_journal') or {}
    if journal and (journal.get('phase')!='complete'
            or (journal.get('tail_bytes',0)>0 and len(measurement.get('partitions',[]))!=4)):return False
    tail_bytes=int(measurement.get('tail_size') or 0)
    expected_c=((target-int(measurement.get('c_offset',0))-tail_bytes-1024**2)//(1024**2))*(1024**2)
    return (measurement.get('disk_bytes')==target
            and 0 <= expected_c-measurement.get('c_bytes',0) <= 2*1024**2
            and measurement.get('filesystem_bytes',0) >= measurement.get('c_bytes',0)-2*1024**2
            and measurement.get('free_bytes',0)>=1024**3)


def system_disk_slot(native, approved_order=None, expected_interface=None):
    """Resolve the one boot disk from live Proxmox config, never by interface guess."""
    disk_keys={key for key in native if re.fullmatch(r'(?:scsi|sata|virtio|ide)[0-9]+',key)
               and 'size=' in str(native[key])}
    ordered=[part for part in native_boot_order(native) if part in disk_keys]
    candidates=set(ordered)
    if len(candidates)!=1:raise DiskGrowError('Live Proxmox boot order does not identify exactly one system disk.')
    slot=next(iter(candidates))
    if expected_interface is not None and slot!=expected_interface:
        raise DiskGrowError('Live system disk differs from its approved immutable image profile.')
    if approved_order is not None:
        if not isinstance(approved_order,list):raise DiskGrowError('Approved image boot order is invalid.')
        approved=[part for part in approved_order if part in disk_keys]
        if len(set(approved))!=1 or approved[0]!=slot:
            raise DiskGrowError('Live boot disk differs from the approved template boot profile.')
    return slot


def native_boot_order(native):
    bootdisk=native.get('bootdisk');boot=native.get('boot','')
    if isinstance(boot,dict):boot=boot.get('order','')
    order=str(boot).partition('order=')[2] if 'order=' in str(boot) else str(boot)
    result=[part for part in re.split(r'[;, ]+',order) if re.fullmatch(r'(?:scsi|sata|virtio|ide)[0-9]+',part)]
    if not result and isinstance(bootdisk,str):result=[bootdisk]
    return result


def windows_grow_script(measurement, target_gb):
    """Build resumable PowerShell for the one measured Windows system disk."""
    m=validate_windows_layout(measurement)
    target=target_gb*1024**3
    if type(target_gb)is not int or target<m['disk_bytes']:raise DiskGrowError('Windows disk exceeds its declared capacity.')
    journal=m.get('growth_journal') or {}
    c_offset=int(m['c_offset']);old_c=int(journal.get('old_c',m['c_bytes']))
    tail_num=journal.get('tail_number',m.get('tail_number'))
    tail_size=int(journal.get('tail_bytes',m.get('tail_size') or 0));tail_type=journal.get('tail_type',m.get('tail_type',''))
    was_enabled=journal.get('was_enabled',m.get('winre_enabled',False))
    reserve=1024**2
    c_target=((target-c_offset-tail_size-reserve)//reserve)*reserve
    if c_target<=old_c:raise DiskGrowError('Windows target cannot preserve C:, the recovery partition, and GPT tail reserve.')
    r_owned=(journal and m.get('r_partition')==journal.get('new_tail_number')
             and m.get('r_label')=='Windows RE tools')
    if not m.get('drive_r_available') and not r_owned:raise DiskGrowError('Windows recovery drive R: is unavailable.')
    if tail_num is None and m.get('winre_enabled'):raise DiskGrowError('Enabled WinRE has no measured recovery partition.')
    state='''$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
$diskNo=__DISK__; $target=[int64]__TARGET__; $cNo=__CNO__; $cOffset=[int64]__COFF__; $oldC=[int64]__OLDC__; $cTarget=[int64]__CTARGET__; $tailNo=__TAILNO__; $tailBytes=[int64]__TAILSIZE__; $tailType='__TAILTYPE__'; $wasEnabled=__ENABLED__
$d=Get-Disk -Number $diskNo;if($d.PartitionStyle -ne 'GPT' -or $d.Size -ne $target){throw 'disk identity or target capacity changed'}
$c=Get-Partition -DiskNumber $diskNo -PartitionNumber $cNo;if($c.Offset -ne $cOffset){throw 'C partition offset changed'}
$journal='C:\\ProgramData\\CRANGE\\vm-disk-grow.json';$backup='C:\\ProgramData\\CRANGE\\winre.wim';$root=Split-Path $journal
if(Test-Path -LiteralPath $root){$i=Get-Item -Force -LiteralPath $root;if($i.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'growth journal directory is redirected'}}
New-Item -ItemType Directory -Force $root|Out-Null
foreach($path in @($journal,$backup,$journal+'.tmp')){if(Test-Path -LiteralPath $path){$i=Get-Item -Force -LiteralPath $path;if($i.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'growth journal or WinRE backup is redirected'}}}
function Save-State($phase,$extra=@{}){$script:st.phase=$phase;foreach($k in $extra.Keys){$script:st[$k]=$extra[$k]};$tmp=$journal+'.tmp';$script:st|ConvertTo-Json -Compress|Set-Content -LiteralPath $tmp -Encoding UTF8;Move-Item -LiteralPath $tmp -Destination $journal -Force}
if(Test-Path -LiteralPath $journal){try{$j=Get-Content -Raw -LiteralPath $journal|ConvertFrom-Json;$st=@{};$j.PSObject.Properties|%{$st[$_.Name]=$_.Value}}catch{throw 'growth journal is unreadable'};if($st.disk -ne $diskNo -or $st.target -ne $target -or $st.c_number -ne $cNo -or $st.c_offset -ne $cOffset -or $st.old_c -ne $oldC -or $st.c_target -ne $cTarget -or $st.tail_number -ne $tailNo -or $st.tail_bytes -ne $tailBytes -or $st.tail_type -ne $tailType){throw 'growth journal identity differs'}}
else{$st=@{disk=$diskNo;target=$target;c_number=$cNo;c_offset=$cOffset;old_c=$oldC;c_target=$cTarget;tail_number=$tailNo;tail_bytes=$tailBytes;tail_type=$tailType;was_enabled=$wasEnabled;phase='initial'};$script:st=$st;Save-State 'initial'}
if($st.phase -in @('initial','backed-up','recovery-removed')){if($c.Size -ne $oldC -and $c.Size -ne $cTarget){throw 'C partition has an unknown size before recovery removal'}}
elseif($c.Size -ne $cTarget){throw 'C partition differs from the journaled grown size'}
if($tailBytes -gt 0 -and $st.phase -eq 'initial'){
 $r=Get-Partition -DiskNumber $diskNo -PartitionNumber $tailNo;if($r.GptType -ne $tailType -or $r.Size -ne $tailBytes){throw 'original WinRE partition identity changed'}
 $assigned=Get-Partition -DriveLetter R -ErrorAction SilentlyContinue
 if($assigned -and ($assigned.DiskNumber -ne $diskNo -or $assigned.PartitionNumber -ne $tailNo)){throw 'recovery drive R: became occupied'}
 if(-not $script:st){$script:st=$st};if(-not $assigned){Add-PartitionAccessPath -DiskNumber $diskNo -PartitionNumber $tailNo -DriveLetter R}
 $src='R:\\Recovery\\WindowsRE\\Winre.wim';if(-not(Test-Path -LiteralPath $src)){throw 'WinRE image missing'}
 $hash=(Get-FileHash -Algorithm SHA256 -LiteralPath $src).Hash;Copy-Item -LiteralPath $src -Destination $backup -Force;if((Get-FileHash -Algorithm SHA256 -LiteralPath $backup).Hash -ne $hash){throw 'WinRE backup hash mismatch'}
 Save-State 'backed-up' @{winre_sha256=$hash}
}
if(-not $script:st){$script:st=$st}
if($tailBytes -gt 0 -and $st.phase -eq 'backed-up'){
 if(-not(Test-Path -LiteralPath $backup) -or (Get-FileHash -Algorithm SHA256 -LiteralPath $backup).Hash -ne $st.winre_sha256){throw 'verified WinRE backup is unavailable'}
 $r=@(Get-Partition -DiskNumber $diskNo|Where-Object {$_.GptType -eq $tailType})
 if($r.Count -eq 1 -and $r[0].PartitionNumber -eq $tailNo -and $r[0].Size -eq $tailBytes){
  if(Get-Volume -DriveLetter R -ErrorAction SilentlyContinue){Remove-PartitionAccessPath -DiskNumber $diskNo -PartitionNumber $tailNo -DriveLetter R}
  if($wasEnabled){reagentc /disable|Out-Null;if($LASTEXITCODE -ne 0){throw 'WinRE disable failed'}}
  Remove-Partition -DiskNumber $diskNo -PartitionNumber $tailNo -Confirm:$false
 }elseif($r.Count -ne 0){throw 'unexpected recovery partition while resuming'}
 Save-State 'recovery-removed'
}
if($tailBytes -eq 0 -and $st.phase -eq 'initial'){Save-State 'recovery-removed'}
if($st.phase -eq 'recovery-removed'){
 $c=Get-Partition -DiskNumber $diskNo -PartitionNumber $cNo
 if($c.Size -eq $oldC){Resize-Partition -DiskNumber $diskNo -PartitionNumber $cNo -Size $cTarget}
 elseif($c.Size -ne $cTarget){throw 'C partition is in an unknown interrupted size state'}
 $c=Get-Partition -DiskNumber $diskNo -PartitionNumber $cNo;if($c.Size -ne $cTarget){throw 'C capacity differs from exact target'}
 Save-State 'c-grown'
}
if($tailBytes -gt 0 -and $st.phase -in @('c-grown','creating-recovery')){
 $r=@(Get-Partition -DiskNumber $diskNo|Where-Object {$_.GptType -eq $tailType})
 if($r.Count -eq 0){if($st.phase -ne 'creating-recovery'){Save-State 'creating-recovery'};$r=@(New-Partition -DiskNumber $diskNo -Size $tailBytes -GptType $tailType -DriveLetter R)}
 elseif($r.Count -ne 1 -or $r[0].Size -ne $tailBytes -or $r[0].Offset -lt ($cOffset+$cTarget)){throw 'unexpected recovery partition while resuming'}
 $script:st.new_tail_number=$r[0].PartitionNumber;Save-State 'recovery-created'
}
if($tailBytes -gt 0 -and $st.phase -in @('creating-recovery','recovery-created')){
 $r=Get-Partition -DiskNumber $diskNo -PartitionNumber ([int]$st.new_tail_number);if($r.GptType -ne $tailType -or $r.Size -ne $tailBytes){throw 'created recovery partition identity changed'}
 $v=Get-Volume -Partition $r -ErrorAction SilentlyContinue
 if(-not $v){Format-Volume -Partition $r -FileSystem NTFS -NewFileSystemLabel 'Windows RE tools' -Confirm:$false|Out-Null}
 elseif($v.FileSystem -ne 'NTFS' -or $v.FileSystemLabel -ne 'Windows RE tools'){throw 'recovery partition has unexpected formatted data'}
 $assigned=Get-Partition -DriveLetter R -ErrorAction SilentlyContinue
 if($assigned -and ($assigned.DiskNumber -ne $diskNo -or $assigned.PartitionNumber -ne $r.PartitionNumber)){throw 'recovery drive R: resolves to an unexpected partition'}
 if(-not $assigned){if(Test-Path 'R:\'){throw 'recovery drive R: is occupied'};Add-PartitionAccessPath -DiskNumber $diskNo -PartitionNumber $r.PartitionNumber -DriveLetter R}
 $dir='R:\\Recovery\\WindowsRE';New-Item -ItemType Directory -Force $dir|Out-Null;Copy-Item -LiteralPath $backup -Destination "$dir\\Winre.wim" -Force
 if((Get-FileHash -Algorithm SHA256 "$dir\\Winre.wim").Hash -ne $st.winre_sha256){throw 'restored WinRE hash mismatch'}
 $diskpart=@("select disk $diskNo","select partition $($r.PartitionNumber)",'gpt attributes=0x8000000000000001','exit') -join "`r`n";$diskpart|diskpart.exe|Out-Null;if($LASTEXITCODE -ne 0){throw 'WinRE GPT attributes could not be set'}
 Set-Partition -DiskNumber $diskNo -PartitionNumber $r.PartitionNumber -IsHidden $true -NoDefaultDriveLetter $true
 reagentc /setreimage /path $dir /target C:\\Windows|Out-Null;if($LASTEXITCODE -ne 0){throw 'WinRE registration failed'}
 if($wasEnabled){reagentc /enable|Out-Null;if($LASTEXITCODE -ne 0){throw 'WinRE enable failed'}}
 if(Get-Volume -DriveLetter R -ErrorAction SilentlyContinue){Remove-PartitionAccessPath -DiskNumber $diskNo -PartitionNumber $r.PartitionNumber -DriveLetter R}
 Save-State 'recovery-restored'
}
$c=Get-Partition -DiskNumber $diskNo -PartitionNumber $cNo;$free=(Get-Volume -DriveLetter C).SizeRemaining
if($c.Size -ne $cTarget -or $free -lt 1GB){throw 'C filesystem capacity verification failed'}
if($tailBytes -gt 0){$r=Get-Partition -DiskNumber $diskNo -PartitionNumber ([int]$st.new_tail_number);$attrs=Get-Partition -DiskNumber $diskNo -PartitionNumber $r.PartitionNumber;if($r.GptType -ne $tailType -or $r.Size -ne $tailBytes -or -not $attrs.IsHidden -or -not $attrs.NoDefaultDriveLetter){throw 'WinRE partition identity or GPT attributes differ'};$info=reagentc /info|Out-String;if($LASTEXITCODE -ne 0 -or (($info -match 'Windows RE status:\\s+Enabled') -ne $wasEnabled)){throw 'WinRE enabled state differs'};$loc=([regex]::Match($info,'harddisk\\d+\\\\partition(\\d+)','IgnoreCase'));if($wasEnabled -and (-not $loc.Success -or [int]$loc.Groups[1].Value -ne $r.PartitionNumber)){throw 'WinRE location identity differs'}}
Save-State 'complete'
[ordered]@{ready=$true;disk_bytes=$d.Size;c_bytes=$c.Size;free_bytes=$free;winre_enabled=$wasEnabled}|ConvertTo-Json -Compress'''
    replacements={'__DISK__':str(int(m['disk_number'])),'__TARGET__':str(target),'__CNO__':str(int(m['c_number'])),'__COFF__':str(c_offset),'__OLDC__':str(old_c),'__CTARGET__':str(c_target),'__TAILNO__':('$null' if tail_num is None else str(int(tail_num))),'__TAILSIZE__':str(tail_size),'__TAILTYPE__':tail_type,'__ENABLED__':('$true' if was_enabled else '$false')}
    for key,value in replacements.items():state=state.replace(key,value)
    return state


def _selected_targets(path,cfg,resource_keys=None,team=None,api=None):
    from crange import ProxmoxAPI, output_json, templates_for
    if api is None:api=ProxmoxAPI()
    project=int(cfg.get('project_id') or path.name.rsplit('-',1)[-1])
    outputs=output_json(path).get('scenario_boxes',{}).get('value',{})
    try:applied=json.loads((path/'crange-applied-placements.json').read_text())
    except FileNotFoundError:applied={}
    approved=templates_for(path)
    selected=set(resource_keys) if resource_keys is not None else {'vm:'+k for k,v in cfg.get('scenario_boxes',{}).items() if v.get('disk_gb') is not None}
    rows=api.request('GET','/cluster/resources?type=vm')
    result=[]
    for rk in sorted(selected):
        if not isinstance(rk,str) or not rk.startswith('vm:'):continue
        key=rk[3:];box=cfg.get('scenario_boxes',{}).get(key)
        if not box or box.get('disk_gb') is None:continue
        if team is not None and str(box.get('team_id'))!=str(team):
            if resource_keys is None:continue
            raise DiskGrowError('Selected VM belongs to another team.')
        live=outputs.get(key);receipt=applied.get(rk)
        placement=live.get('placement',{}) if isinstance(live,dict) else {}
        vmid=live.get('id') if isinstance(live,dict) else None;node=placement.get('node_name')
        if live is None and receipt is None:
            # A newly declared VM has no pre-apply guest to inspect. Its first
            # inspection happens after Terraform creates the applied clone.
            continue
        if (type(vmid)is not int or not isinstance(receipt,dict) or receipt.get('guest_id')!=vmid or receipt.get('node_name')!=node or receipt.get('declared_template')!=box.get('template') or receipt.get('image_id')!=placement.get('image_id') or receipt.get('template_id')!=placement.get('template_id') or receipt.get('template_node')!=placement.get('template_node')):
            raise DiskGrowError('Selected VM does not have a matching applied placement receipt.')
        network_key=box.get('network_key')
        if network_key:
            network=cfg.get('custom_networks',{}).get(network_key)
            if not isinstance(network,dict):raise DiskGrowError('Selected VM references an undeclared custom network.')
            vnet=f'p{project:03d}c{int(network.get("network_id",-1)):03d}'
        else:vnet=f'p{project:03d}t{int(live.get("network_id",box.get("team_id"))):03d}'
        name=f'{vnet}-{box.get("name")}'
        native_rows=[r for r in rows if str(r.get('vmid'))==str(vmid)]
        if len(native_rows)!=1 or any(native_rows[0].get(k)!=v for k,v in {'type':'qemu','pool':'crange-iac','node':node,'name':name,'status':'running'}.items()):raise DiskGrowError('Selected VM live Proxmox identity differs from desired ownership.')
        native=api.request('GET',f'/nodes/{node}/qemu/{vmid}/config')
        tags=set(str(native.get('tags','')).split(';'))
        if native.get('name')!=name or f'p{project:03d}' not in tags or vnet not in tags or native.get('template',0):raise DiskGrowError('Selected VM live Proxmox tags or configuration are not owned by this project.')
        if type(box.get('disk_gb')) is not int or not 8<=box['disk_gb']<=1024:raise DiskGrowError('Selected VM disk_gb is invalid.')
        approved_template=approved.get(box.get('template'),{})
        profile=approved_template.get('profile',{}) if isinstance(approved_template,dict) else {}
        boot_order=profile.get('boot_order') if isinstance(profile,dict) else None
        template_id=placement.get('template_id');template_node=placement.get('template_node')
        if (not isinstance(approved_template,dict) or approved_template.get('template_id')!=template_id
                or approved_template.get('template_node')!=template_node or type(template_id)is not int
                or not re.fullmatch(r'proxmox[1-6]',str(template_node))):
            raise DiskGrowError('Selected VM template differs from its approved immutable image reference.')
        template_native=api.request('GET',f'/nodes/{template_node}/qemu/{template_id}/config')
        if not template_native.get('template'):
            raise DiskGrowError('Approved scenario source is not a live immutable Proxmox template.')
        if not isinstance(boot_order,list):boot_order=native_boot_order(template_native)
        if not isinstance(boot_order,list) or not boot_order:raise DiskGrowError('Selected VM has no approved immutable boot-disk profile.')
        image_disk=profile.get('system_disk')
        expected_interface=image_disk.get('interface') if isinstance(image_disk,dict) else profile.get('system_disk_interface')
        result.append({'key':key,'resource_key':rk,'project_id':project,'guest_id':vmid,'node':node,'name':name,'target_gb':box['disk_gb'],'native':native,'box':box,'image_id':placement.get('image_id'),'approved_boot_order':boot_order,'expected_system_disk':expected_interface})
    return api,result


def _journal_path():
    return Path(os.environ.get('CRANGE_VM_DISK_GROW_JOURNAL',
        Path.home()/'.local/share/crange/vm-disk-grow/jobs.json'))


def _journal_save(path, records):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if path.parent.stat().st_uid!=os.geteuid() or stat.S_IMODE(path.parent.stat().st_mode)!=0o700:
        raise DiskGrowError('Disk growth journal directory must be worker-owned mode 0700.')
    fd,name=tempfile.mkstemp(prefix='.jobs-',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as stream:
            json.dump(records,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path);os.chmod(path,0o600)
    finally:
        try:os.unlink(name)
        except FileNotFoundError:pass


def _qga(api,target,command,timeout=300,*,journal=True,resumable=False):
    root=f"/nodes/{target['node']}/qemu/{target['guest_id']}/agent"
    if not journal:
        deadline=time.monotonic()+timeout
        for attempt in range(3):
            try:response=api.request('POST',root+'/exec',{'command':command})
            except Exception:raise DiskGrowError('Read-only guest measurement submission failed.') from None
            pid=response.get('pid') if isinstance(response,dict) else None
            if type(pid)is not int or pid<=0:raise DiskGrowError('Guest measurement returned no valid handle.')
            while time.monotonic()<deadline:
                try:state=api.request('GET',root+'/exec-status',{'pid':pid})
                except Exception as exc:
                    # Windows OOBE can restart QGA after it accepted a collector.
                    # Only a confirmed lost read-only handle permits a new exec.
                    cause=exc.__cause__;lost=False
                    if getattr(cause,'code',None)==500:
                        try:detail=cause.read(8192).decode('utf-8',errors='replace')
                        except (AttributeError,OSError):detail=''
                        lost=bool(re.search(r'PID\s+(?:[0-9]+|lld)\s+does not exist',detail,re.I))
                    if not lost or attempt==2:raise
                    time.sleep(min(5,max(0,deadline-time.monotonic())))
                    break
                if state.get('exited'):
                    if state.get('exitcode')!=0:raise DiskGrowError('Fixed guest measurement failed; guest output withheld.')
                    return str(state.get('out-data',''))
                time.sleep(1)
            else:break
            if time.monotonic()>=deadline:break
        raise DiskGrowError('Guest measurement timed out.')
    digest=hashlib.sha256(json.dumps(command,separators=(',',':')).encode()).hexdigest()
    key=f"{target['project_id']}:{target['resource_key']}:{target['guest_id']}:{digest}"
    path=_journal_path();path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    lockpath=path.with_suffix(path.suffix+'.lock')
    fd=os.open(lockpath,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        os.fchmod(fd,0o600);fcntl.flock(fd,fcntl.LOCK_EX)
        if path.is_symlink():raise DiskGrowError('Disk growth journal cannot be a symbolic link.')
        if path.exists():
            st=path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.geteuid() or stat.S_IMODE(st.st_mode)!=0o600:raise DiskGrowError('Disk growth journal must be worker-owned mode 0600.')
            try:records=json.loads(path.read_text())
            except (OSError,ValueError):raise DiskGrowError('Disk growth journal is malformed.') from None
        else:records={}
        record=records.get(key);retry_failed=False
        if record and record.get('digest')!=digest:raise DiskGrowError('Guest disk operation identity changed while a prior operation is unresolved.')
        if record and record.get('state')=='complete':return record.get('output','')
        if record and record.get('state')=='submitting':
            raise DiskGrowError('Guest disk operation submission is unresolved; inspect its journal before retrying.')
        if record and record.get('state')=='failed':
            if not resumable or int(record.get('attempts',1))>=3:
                raise DiskGrowError('Guest disk operation failed; inspect its guest journal before retrying.')
            record['attempts']=int(record.get('attempts',1))+1
            record['state']='submitting';record.pop('pid',None);_journal_save(path,records);retry_failed=True
        if not record or retry_failed:
            record={'digest':digest,'state':'submitting','attempts':1};records[key]=record;_journal_save(path,records)
            try:response=api.request('POST',root+'/exec',{'command':command})
            except Exception:raise DiskGrowError('Guest command submission is uncertain; it will not be resent.') from None
            pid=response.get('pid') if isinstance(response,dict) else None
            if type(pid)is not int or pid<=0:raise DiskGrowError('Guest agent command returned no valid handle; retain its journal.')
            record.update(pid=pid,state='running');_journal_save(path,records)
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            state=api.request('GET',root+'/exec-status',{'pid':record['pid']})
            if state.get('exited'):
                output=str(state.get('out-data',''));record.update(state='complete' if state.get('exitcode')==0 else 'failed',exitcode=state.get('exitcode'),output=output);_journal_save(path,records)
                if state.get('exitcode')!=0:raise DiskGrowError('Fixed guest disk operation failed; guest output withheld.')
                return output
            time.sleep(1)
        raise DiskGrowError('Guest disk operation is still running; retry will poll its existing handle.')
    finally:
        fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)


def _measure(api,target,refresh=False):
    native=target['native'];os_type=str(native.get('ostype','')).lower()
    if refresh and os_type.startswith('win'):
        _qga(api,target,['powershell.exe','-NoProfile','-NonInteractive','-Command','Update-HostStorageCache'],180,journal=False)
    command=windows_inspector() if os_type.startswith('win') else linux_inspector()
    try:measurement=json.loads(_qga(api,target,command,180,journal=False))
    except (ValueError,KeyError):raise DiskGrowError('Guest disk layout measurement was invalid.') from None
    try:
        from crange_proxmox_disk import size_bytes
        slot=system_disk_slot(native,target.get('approved_boot_order'),target.get('expected_system_disk'))
        host_bytes=size_bytes(native.get(slot,''))
    except ValueError:raise DiskGrowError('Selected VM system disk capacity cannot be verified.') from None
    if measurement.get('disk_bytes')!=host_bytes:raise DiskGrowError('Guest system disk size differs from live Proxmox configuration.')
    target['system_disk_slot']=slot
    return measurement


def preflight_selected(path,cfg,resource_keys=None,team=None,api=None):
    """Read and validate selected explicit-disk VM layouts before infrastructure apply."""
    api,targets=_selected_targets(path,cfg,resource_keys,team,api);result=[]
    for target in targets:
        m=_measure(api,target)
        if m.get('disk_bytes',0)>target['target_gb']*1024**3:
            raise DiskGrowError('Selected guest disk exceeds its declared target; preflight refuses to shrink.')
        if m.get('os')=='windows':validate_windows_layout(m)
        else:validate_linux_layout(m)
        result.append({'resource_key':target['resource_key'],'guest_id':target['guest_id'],'layout_sha256':hashlib.sha256(json.dumps(m,sort_keys=True).encode()).hexdigest(),'current_disk_bytes':m.get('disk_bytes')})
    return result


def initialize_selected(path,cfg,resource_keys=None,team=None,api=None):
    """Grow explicit-disk scenario VM filesystems after apply and verify readback."""
    api,targets=_selected_targets(path,cfg,resource_keys,team,api);result=[]
    for target in targets:
        m=_measure(api,target,refresh=True)
        target_gb=target['target_gb']
        if m.get('os')=='windows':
            validate_windows_layout(m)
            if m['disk_bytes']!=target_gb*1024**3:raise DiskGrowError('Windows host disk has not reached the exact declared capacity.')
            if not windows_already_grown(m,target_gb):
                # Windows growth is a fixed guest script; store only nonsecret layout identity.
                script=windows_grow_script(m,target_gb)
                encoded=base64.b64encode(script.encode('utf-16le')).decode('ascii')
                # Script is sent through QGA's command-line script mode. Payload comes from the measured layout.
                _qga(api,target,['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',encoded],600,resumable=True)
            verify=_measure(api,target);validate_windows_layout(verify)
            if not windows_already_grown(verify,target_gb):raise DiskGrowError('Windows disk and C: filesystem did not reach the declared target.')
            result.append({'resource_key':target['resource_key'],'guest_id':target['guest_id'],'disk_gb':target_gb,'filesystem_free_bytes':verify['free_bytes']})
        else:
            m=validate_linux_layout(m)
            # Cloud-init growpart/resizefs is the only Linux operation supported
            # here; require an approved initialized cloud image and verify root.
            if m['disk_bytes']<target_gb*1024**3:
                raise DiskGrowError('Linux host disk has not reached the declared target before guest expansion.')
            if m['disk_bytes']>target_gb*1024**3:raise DiskGrowError('Linux disk already exceeds its declared size; refusing shrink or ambiguous resize.')
            if not linux_already_grown(m,target_gb):
                command=['/usr/bin/cloud-init','single','--name','growpart','--frequency','always']
                _qga(api,target,command,300,resumable=True)
                _qga(api,target,['/usr/bin/cloud-init','single','--name','resizefs','--frequency','always'],300,resumable=True)
            verify=validate_linux_layout(_measure(api,target))
            if not linux_already_grown(verify,target_gb):raise DiskGrowError('Linux root partition/filesystem did not grow to the declared target.')
            result.append({'resource_key':target['resource_key'],'guest_id':target['guest_id'],'disk_gb':target_gb,'filesystem_free_bytes':verify['free_bytes']})
    return result
