# Project 100 Windows platform status

Project 100 uses the fictional Meridian Harbor Logistics (MHL) domain
`mhl.test`. The Windows scope is domain controllers, file servers, and Windows
Server Desktop Experience jumpboxes on isolated project networks. VM 104 on
proxmox4 is a stopped, unmanaged `home-lan` VM and remains untouched.

## Verified media and installer

The Server 2025 Evaluation x64 ISO was obtained through Microsoft's official
`WinServ2025iso-enus` redirect:

* [Windows Server 2025 Evaluation download](https://www.microsoft.com/en-us/evalcenter/download-windows-server-2025)
* File: `26100.32230.260111-0550.lt_release_svc_refresh_SERVER_EVAL_x64FRE_en-us.iso`
* Size: 8,152,356,864 bytes
* SHA256: `7b052573ba7894c9924e3e87ba732ccd354d18cb75a883efa9b900ea125bfd51`

The shared library and both media mirrors each contain this exact size and
SHA256. The official VirtIO ISO is also hash-verified on the shared library
and mirrors. The Server Evaluation has a 180-day evaluation term and must
retain the activation limitations in administrator-facing documentation.

The fixed admin builder created VMID 115 on proxmox4 with a 64 GiB SATA disk,
OVMF, the official ISO, VirtIO drivers, and QEMU Guest Agent enabled. Its
answerfile seed ISO is local-only and contains a temporary administrator
password; it must never be promoted to the shared library or mirrors. Setup
initially completed without the Guest Agent because the v4 answerfile seed
contained the XML but not the fixed setup script. A local-only, script-only
ISO was then built from the reviewed repository script, with both its ISO9660
short name and Joliet long name verified against SHA256
`4e26e64b1b3adea4a97695650d198ebdcfe5da54e59eba3f15aa53996761b77b`. The
script was run from the attached setup ISO without reinstalling Windows.
`C:\ProgramData\CRANGE\base-setup.log` records successful completion. The v5
private answer seed now includes both the answer file and setup script. Its
RunSynchronous command searches optical drive letters for the fixed script and
stays below Windows Setup's 259-character path limit. The v5 seed remains local
to proxmox4 because its answer file contains the temporary administrator
password.

The first installer attempt failed with a specific unattended Setup schema
error: WinPE `setuperr.log` identified
`Microsoft-Windows-Deployment/RunSynchronous/RunSynchronousCommand[Order=1]/Path`
as invalid, HRESULT `0x80220005`. The decoded Path was 340 characters; the
Microsoft schema limit is 259. After shortening it, Setup still rejected the
second answer file. Its log said `Found an unexpected tag 'ms0:component' while
processing components` (`0x8022000b`). The ISO's raw XML showed that the
unattend namespace had been serialized as `ns0:` prefixes, despite the XML
parser resolving the namespaces correctly. The fixed serializer now emits
the default unattend namespace and `wcm` prefix. A v4 private seed was created
with the original temporary password preserved, and its raw ISO contents,
answerfile hash, and 79-character Setup path were verified. Both failed
installer runs are retained as controller-side metadata and their exact owned
VMs were cancelled. VMID 115 now reports Windows Server 2025 Standard
Evaluation build 26100, computer `MHL-WINBASE`, and workgroup membership.
QEMU Guest Agent, TermService, and WinRM are Running with Automatic startup;
the VirtIO Serial Driver reports OK. RDP firewall rules allow only
`10.60.0.20`, and WinRM is likewise restricted by the fixed setup script. The
actual source MachineGuid and local Administrator SID were observed through
QEMU Agent before generalization, but their values were not saved in a durable
comparison record. VMID 115 remains unpublished: it has not
yet been generalized, frozen as a template, exported, or tested through an
isolated clone.

## Project 100 configuration

Staging addresses are DC `.10`, file server `.20`, Linux PostgreSQL `.30`,
Linux app `.40`, Linux CI `.50`, and Linux backup `.60` on `10.100.254.0/24`.
Each team uses the same host octets on its own isolated `10.100.T.0/24` LAN;
the Windows jumpbox is `.12`. Each released team has an independent `mhl.test`
forest and its own DNS and credentials.

The Ansible drafts configure domain controllers, file servers, and Windows
jumpboxes. They model two non-administrative service accounts, an employee-
writable backup staging share, and a `Warehouse Deployment` GPO mistakenly
linked at the domain root with edit permission delegated to
`WarehouseDeployers`. Guest-local administrator passwords are rotated from
unique inventory-host-keyed vault entries before domain membership. These
changes are code-only and have not yet been run against Windows guests. They do
not grant Domain Admin or weaken SMB signing.

## Remaining acceptance

Windows image publication is blocked until the actual guest reports healthy
Windows Server 2025 Desktop Experience, QEMU Guest Agent, and workgroup state,
and a fixed Windows-specific capture/probe proves unique MachineGuid and local
machine SID on an isolated clone. Linux `machine-id` and SSH-key evidence is
not applicable to Windows. The source has since been generalized; publication, replication, and Project 100
use still require actual fresh-clone identity and boot checks.

The Windows jumpbox requirement does not specify Windows 11. The approved
profile is Windows Server Desktop Experience and will be recorded as Windows
Server. The generic `26100.1_MULTI_X64_EN-US.ISO` is not used because its
official source and checksum are unknown.

The fixed local checks pass: the two Project 100 Windows playbooks and sample
inventory parse as YAML; the host-scope regex accepts `10.100.0.0/24` through
`10.100.5.0/24` and `10.100.254.0/24` while rejecting other project ranges;
and 15 focused source-identity/playbook tests pass. Ansible is not installed in
the workspace, so this does not replace controller-side syntax or live Windows
acceptance.

## Sealed-source checkpoint — 2026-10-08

The Windows agent reported offline, read-only SYSTEM hive values
`GeneralizationState=4` and `CleanupState=2`. Setup logged creation of
`Sysprep_succeeded.tag` followed by shutdown. The SOFTWARE hive no longer
contains MachineGuid. EFI BCD export warnings (`c000000d`) remain in the error
log; successful fresh-clone boot has not yet been proved. No offline SAM SID
has been established.

VM 115 is stopped in `crange-probes`, retaining its unique build marker plus
`crange-image;mhl-windows-sealed` tags. Final configuration has 4 GiB RAM,
SATA0-only boot and no IDE media. The pre-transition configuration fingerprint
was not persisted; the final seal fingerprint must be recorded honestly rather
than reconstructing historical evidence. Keep this source stopped until the
controlled publication/clone workflow resumes. Two independent OOBE clones
must establish distinct actual identities and working services.

The final normalized PVE configuration fingerprint is
`a884aea74b225fdb0f4e754ceb6d39932b482c87d3639c74dfa5ebfe4c5f3aab`.
P4's PVE storage-status endpoint timed out in a bounded 8-second check. A
follow-up confirmed VM 115's source volume is `/dev/pve/vm-115-disk-1` on
`local-lvm`; the existing `crange-library` mount is NFSv4 from
`10.0.0.249:/crange-library`, mounted read-write, with 278,178 available
1-MiB blocks (about 271.7 GiB). No `pvesm` scan was active during that check.
The NFS filesystem is reachable, but the PVE storage status call remains
unresolved; no full clone was started. The root-owned offline seal record is
summarized in [windows-source-seal.json](windows-source-seal.json).
