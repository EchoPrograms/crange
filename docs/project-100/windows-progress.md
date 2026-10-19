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

## Isolated Windows clone runtime checkpoint — 2026-10-08

Read-only inspection found source VM 115 still stopped. Acceptance VM 8910
(`crange-win-acceptance-a`) is running on proxmox4 with about 5 hours of uptime,
4 GiB RAM, QEMU Guest Agent enabled, and its probe NIC marked link-down. The
clone and start tasks both completed successfully. No Windows capture, OOBE,
or clone worker process was present on proxmox4 at inspection time.

QEMU Guest Agent command execution succeeded once. Windows reported computer
name `MERIDIA-F1MSLM9`, `WORKGROUP`, and `PartOfDomain=false`. The actual clone
MachineGuid is `6e97700b-9619-4ca0-b9f0-f27122819896`; its local built-in
Administrator SID is `S-1-5-21-2584229225-535534933-2584267345-500`. QEMU-GA,
TermService, and WinRM all reported Running with Automatic startup. This is
useful clone identity evidence, but it does not prove uniqueness against a
second clone.

Windows registry indicators reported `OOBEInProgress=1` and `SetupType=2`.
Therefore OOBE completion is not established; the required follow-up read of
Windows Setup state and logs timed out through QEMU Guest Agent and was not
retried. RDP, WinRM reachability, DNS, and application readiness were not
tested. Do not publish or replicate this image on this evidence. Escalate to
the Windows runtime owner to inspect the isolated clone's console/setup state
and decide on a reviewed recovery path. Once OOBE is confirmed complete, the
next publication gate is a fixed Windows-specific probe on this clone and a
fresh independent clone, comparing their actual MachineGuid and local machine
SID while checking desktop edition, workgroup state, QEMU-GA, TermService, and
WinRM readiness.

## Windows OOBE recovery checkpoint — 2026-10-08

Read-only QMP console inspection of owned clone 8910 confirmed Windows was
stuck at `Finalizing your settings`. A bounded graceful PVE reboot completed
and the guest reached the ordinary Windows lock screen. Console login using
the existing private clone-answer ISO credential reached Server Manager and
the actual Desktop Experience. The credential was parsed only in host memory
and entered into the masked console field; its value was not printed.

An Administrator PowerShell console read showed `OOBEInProgress=0`,
`SystemSetupInProgress=0`, and `SetupType=0`. This is observed completion,
without editing Setup registry flags. Source 115 remained stopped throughout.
Windows identity readiness validation now requires all three observed fields
to be integer zero, including when catalog receipts are validated. The 13
focused source identity tests pass, including unfinished/missing/boolean
Setup evidence rejection.

Guest Agent remains a separate acceptance gate. Read-only async diagnostic
PID 4652 before reboot and PID 4604 afterward encountered status RPC timeouts;
operation metadata was retained on P4. Console `Get-Service` initially showed
QEMU-GA Running, but `qm agent 8910 ping` failed. A reviewed service restart
stalled in STOP_PENDING. `sc queryex QEMU-GA` showed process 3052; exact-process
termination reported no running instance while `Get-Process` still listed
that PID. A second graceful Windows reboot was submitted from the console to
clear that service state. Avoid log-heavy guest-exec diagnostics until the
small fixed identity collector succeeds. This checkpoint does not authorize
publication: actual Guest Agent health and a second independent clone with
unique GUID/SID remain required.

The clean second Windows reboot resolved the Guest Agent channel. `qm agent
8910 ping` succeeded and the fixed Windows identity collector (guest PID
4208, exit code 0) passed the strengthened catalog validator. Windows reports
build 26100, `IMAGE_STATE_COMPLETE`, all three Setup flags zero, workgroup
membership, QEMU-GA/TermService/WinRM Running, and RDP allowed only from
`10.60.0.20`. MachineGuid and Administrator SID match the earlier clone
values above. Desktop Experience was observed directly in Server Manager.
The protected P4 result is
`/var/lib/crange-template-builds/windows-recovery-identity8910-result.json`.
Guacamole RDP from `10.60.0.10` is a later deployed-guest policy requirement;
the sealed source policy was not broadened during these checks.

A second independent full clone from stopped sealed source 115 to owned VM
8911 (`crange-win-acceptance-b`) is running under task
`UPID:proxmox4:001A1D4B:1C376D93:6AC7EAFC:qmclone:115:root@pam:`.
Its journal is `/var/lib/crange-template-builds/windows-second-clone8911.json`
and its log is beside that journal. It must remain stopped until its NIC is
set to `crprobe1450,link_down=1` and verified. No second-clone identity or
publication claim is made while this task is in progress. In addition to
13 identity tests, 8 release catalog, 4 distribution workflow, and 4 capture
tests passed for the strengthened readiness check.

Second-clone task completed successfully and the supervisor verified the
frozen source config stayed unchanged. VM 8911 was isolated before its first
boot. With no console clicks or login, it stopped at the Windows `Hi there`
region/language/keyboard page. The small fixed collector was accepted as
PID 3768 but had not exited when first queried; the next status call found
the Guest Agent unavailable. Preserve this operation handle and guest.

This is an unattended-boot blocker. Read-only parsing of the private source
clone-answer ISO confirms `specialize` and `oobeSystem` contain only
`Microsoft-Windows-Shell-Setup`; `Microsoft-Windows-International-Core` is
missing. Therefore the earlier manually advanced clone 8910 cannot prove the
source's unattended setup works. Do not publish source 115 from these checks.
Windows Setup caches answer files and searches higher-priority embedded
answers before removable media, so attaching another ISO alone is not a
verified repair ([Microsoft Setup automation documentation](https://learn.microsoft.com/windows-hardware/manufacture/desktop/windows-setup-automation-overview)).
A corrected never-booted sealed derivative would require a reviewed offline
answer-only preparation plus two fresh independent unattended clone tests.
The original sealed source 115 must remain unchanged.

A bounded repair path was approved: create a new, never-booted owned full
clone 8912 from unchanged sealed source 115, inspect the actual cached and
registry-referenced answer files offline, and alter only the OOBE locale XML
fields. SYSTEM, SAM, SOFTWARE, generalization and licensing values must stay
unchanged; the corrected sealed candidate must remain stopped. Two new
independent clones of that candidate must complete unattended OOBE and pass
identity/readiness checks before publication is considered.

Candidate clone task is
`UPID:proxmox4:001A3B0E:1C385B4E:6AC7ED5D:qmclone:115:root@pam:`;
its root-private journal is
`/var/lib/crange-template-builds/windows-locale-candidate8912.json`.
The supervised read-only inspector waits this exact task and records seal
and answerfile path evidence under
`/var/lib/crange-template-builds/windows-locale-candidate8912/inspection.json`.
Observation timeout must preserve the same task, not start another clone.
The fixed answerfile transformer adds only en-US locale settings and proves
semantic reversion to the original XML while preserving private fields;
its three focused tests pass. No locale edit has been claimed yet.

Candidate 8912 preparation completed without booting it. Actual offline seal
is `GeneralizationState=4`, `CleanupState=2`, and
`IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE`, with an existing success tag. The
initial ImageState comparison was corrected to decode hivexregedit's
`hex(1)` UTF-16 representation; no registry value was changed. The two
existing XML paths were `ProgramData/CRANGE/clone-unattend.xml` and
`Windows/Panther/unattend.xml`; no registry UnattendFile pointer was present.
Only their en-US International-Core locale fields were added. The private
Administrator settings remained semantically identical. SYSTEM, SOFTWARE,
and SAM files stayed byte-for-byte identical to their inspected hashes.
Read-only and write mounts were cleanly unmounted and loops detached.

The candidate remains stopped, never booted, with isolated link-down NIC.
Its frozen normalized configuration hash is
`5e9fdb9337ec631b74100dc1c1f47ffdfb02f8873b32ee6119b49bc7cef27e9d`.
Sanitized proof is in [windows-locale-candidate-seal.json](windows-locale-candidate-seal.json).
The first fresh candidate clone 8913 has task
`UPID:proxmox4:001A58EE:1C39549A:6AC7EFDB:qmclone:8912:root@pam:`.
Its supervisor will validate unchanged stopped sources and isolation before
ordinary first boot; it will not send keys or log in. The second new clone
8914 waits for that copy to complete so disk copies remain sequential.
Publication still requires both independent unattended readiness checks.

The fixed Windows identity collector now also records `InstallationType` and
Automatic startup for QEMU-GA, TermService, and WinRM. Readiness requires
`InstallationType=Server` (Desktop Experience) and all three Automatic flags,
so a Server Core caption or manually started services cannot pass. The
corrected candidate probes will collect those fields; the older clone 8910
record remains historical and does not fabricate the newly required fields.
The 13 identity tests plus 8 catalog, 4 distribution, 4 capture, and 3 locale
transformer tests pass.

For the second corrected probe 8914, P4's local SSD thin pool was measured at
794.3 GiB total, 68.53% data use, and 2.23% metadata use. It can accommodate
one worst-case 64 GiB allocation while retaining more than 20% physical
reserve. The shared reservation ledger currently has no active P4/local-lvm
allocations. The fixed creation helper will recheck live copy tasks, current
thin-pool use, and ledger reservations immediately before allocation; it will
defer if other commitments eliminate that headroom. Candidate 8912 and the
original 115 remain stopped throughout.

Corrected candidate clone 8913 completed ordinary unattended setup and passed
the fixed Guest Agent collector, without any console keys, login, reboot,
or manual recovery. The collector exited 0 as PID 3728. It reports Server
2025 build 26100, `InstallationType=Server`, `IMAGE_STATE_COMPLETE`, all
Setup flags zero, workgroup membership, and QEMU-GA/TermService/WinRM Running
with Automatic startup. Its actual MachineGuid is
`bc7e40b7-630a-4bdd-aed6-6657e4f25cdc`; its machine SID is
`S-1-5-21-3977005292-1299864674-2150539454`.
The source-to-probe copy took about 30 minutes on NFS; it was not restarted.

Second corrected probe 8914 was submitted for sequential copy to P4 `local-lvm`
under `UPID:proxmox4:001AAD9A:1C3C3419:6AC7F736:qmclone:8912:root@pam:`.
Live copy-task, reservation-ledger and local thin-pool checks passed before
allocation: no other local reservations, and more than 20% physical reserve
after a worst-case 64 GiB allocation. Its supervisor is fixed to isolated
normal boot with no interactive keys or login. Sanitized first-proof and
second-task evidence is in
[windows-locale-clone-acceptance.json](windows-locale-clone-acceptance.json).
The sealed source 115 and candidate 8912 remain stopped. Publication still
waits the independent second identity and readiness comparison.

## Independent corrected-clone acceptance — 2026-10-08

The exact 8914 clone task is now terminal `OK`. Its durable journal records
that clone completion, isolation on `crprobe1450` before first boot, unchanged
source configuration, and no manual interventions. Live PVE inspection
confirms candidate 8912 remains stopped with the sealed candidate
configuration, while isolated probe 8914 is running with its NIC link down.
The fixed identity collector's first run was guest PID 5072. Its terminal
response was lost when the host persistence step failed importing
`crange_catalog`; a later status query confirmed that PID no longer existed.
After that was recorded, an explicitly authorized second fixed collector ran
as PID 1372. The poller saved the raw terminal QGA response to a protected P4
file before parsing or validating it. No other guest command, reboot, or clone
was started.

The saved response passed the workspace's `crange_catalog` source identity
validator. Probe 8914 reports Server 2025 Standard Evaluation build 26100,
`InstallationType=Server`, `IMAGE_STATE_COMPLETE`, all three Setup fields
zero, workgroup membership, and QEMU Guest Agent, TermService, and WinRM
running with Automatic startup. Its MachineGuid is
`97165798-6664-4262-83a9-47087ba1658f`; its local Administrator SID is
`S-1-5-21-1988556285-2669735155-2149193717-500`. Probe 8913's GUID and machine
SID differ from 8914's; both probes also have different local Administrator
SIDs. Both are independently unattended-ready without manual intervention.

The first corrected clone 8913 passed the fixed Windows collector without
manual intervention: Server 2025 Standard Evaluation build 26100, Server
installation type, `IMAGE_STATE_COMPLETE`, all three setup flags zero,
workgroup membership, and QEMU Guest Agent, TermService, and WinRM running
with Automatic startup. Its MachineGuid and local Administrator SID are
recorded in the acceptance proof. The candidate source was never booted, so it
has no historical source GUID or machine SID to compare. The existing guarded
publication workflow requires captured source identity before export; that
requirement cannot be satisfied truthfully for this sealed candidate. Keep
publication blocked until a reviewed workflow defines provenance for a
never-booted generalized Windows source.

The paired unattended-clone evidence and collector provenance are recorded in
[windows-locale-probe8914-acceptance.json](windows-locale-probe8914-acceptance.json).

## Deployed business Windows guests — October 12

Historical base-image investigation above predates the current staging guests.
Approved logical base mhl-windows-server-2025-base-p100-v1 was used through the
GUI deployment workflow to create VM128/MHL-DC01 and VM129/MHL-FS01 on P1.
Windows configure 531cf557175744e6b1fea8c22a64fc6a passed authenticated WinRM
and fixed access baseline for both. Credentials remain worker-private.

MHL Windows action 4b10e52d14764df6915af252ceb3fe9a succeeded after schema
repair: forest mhl.test, OUs/users/service accounts/GPO delegation, domain-joined
FS and baseline backup share. Extended action c5c0188fb24945a381bfa8bb2725d857
passed finance/dispatch/operations shares, synthetic exports, privileged scheduled
consumer execution and ACL assertions, and IIS intranet installation/content.
Health d6a553794601401498dbbcc72ee23f50 passed DC DNS/LDAP/SMB and FS SMB/HTTP
checks. Read-only RDP negotiation from actual Guac source 10.60.0.10 passed both
servers; authenticated desktop sessions remain unverified.

The newer 40-GB builder VM123/P5 was converted to a physical template, but its
generic immutable logical publication/clone acceptance is still unfinished.
Domain scenario-snapshot capture must preserve configured service state under
a separate contract; existing workgroup-base identity validation is insufficient
and must not be weakened to falsely approve a domain snapshot.

## Scenario snapshot contract — work in progress

Added a separate service-preserving Windows snapshot validator and fixed
read-only collector. Domain/name/MachineGuid remain stable in an isolated restore;
PVE VM generation must change, and a DC must expose its generation-counter
device plus a changed directory invocation ID. Existing workgroup-base validator
continues rejecting domain snapshots. Three targeted contract tests passed.
This contract is connected to capture, export, catalog validation and isolated
clone verification. Approval is restricted to the snapshot's originating project.
No Windows snapshot has been approved by these tests alone.

Rationale: https://learn.microsoft.com/en-us/windows-server/identity/ad-ds/introduction-to-active-directory-domain-services-ad-ds-virtualization-level-100

Live read-only collector accepted VM129/MHL-FS01: member role3, mhl.test,
completed setup, automatic/running access services, Guac-only RDP and a live
generation-counter device. VM128 also exposes the counter. Its AD invocation
attribute is a byte array, requiring explicit GUID conversion; corrected the
collector rather than relaxing its validator. Runtime clone acceptance is still
required before publication.

After GUID conversion, strict live collection also accepted VM128/MHL-DC01:
role5, domain mhl.test, invocation83a0edbe-2f5e-4c33-be71-676dc8b39a9f,
and a working generation-counter device. Both staged Windows sources now pass
this read-only preflight contract. No restored Windows clone has been tested
under it yet; runtime capture/publication acceptance remains unfinished.

GUI capture job 136d77cb2a6d45469082fd06d131fd8b failed before allocating a
derivative. Its final source restart succeeded in Proxmox but returned the known
non-VirtIO MTU warning, masking the original capture failure. The task adapter now
checks the complete log and accepts only that exact warning on VM start;
unrelated warnings and errors remain failures (six adapter tests passed).
Replacement GUI capture job 13bd2ed4b1c64d2f8f0124f57abd3bbe failed with
`source-probe has insufficient cpu_cores headroom`. No derivative was allocated;
source VM128 returned to running. Fresh discovery reports P1: four physical
cores, two reserved host cores, CPU ratio6, 12-core guest limit and 11 committed
guest cores. Templates are excluded. Full-copy local-lvm commitment also has
negative headroom, despite substantial physical free space; linked commitments
must not be treated as free capacity for a full copy. The ledger additionally
retains a pending file-server placement already deployed as VM129.
Next work must resolve capture placement and reservation reconciliation rather
than repeatedly stopping the source and retrying the same local allocation.
The capture cleanup path also preserves the original error when restart fails.

## Remote capture capacity work

Dashboard reservation reconciliation verified VM129's applied placement and
released its retained allocation (job4a7ba2fa4a9b4995a6390c15f1811310). VM
capture now chooses an eligible node/storage while retaining the live staging
server at its original location. Transport uses an owned stopped source, a
central backup/checksum and a stopped restore in crange-probes; NIC isolation
precedes boot. Full-copy capacity and source CPU compatibility remain gates.

The first remote attempt found no destination using local-lvm. P5 has suitable
CPU/RAM but only ~5 GB of full-copy storage commitment headroom there. Its
larger ssd-lvm is thick LVM; the administrator capture settings action now
audits/enrolls that pool for VM copies without asserting linked-clone support.
Containers retain node-specific approved snapshot-capable thin storage.
Job0cb1f95bcefe4086a804b7cb9b879bcd saved the preferred ssd-lvm setting.
Joba638ffe8176f40fdb990c6807f797561 is the current GUI capture acceptance
attempt; no Windows immutable release is claimed verified yet.

Current live evidence: capture selected proxmox5/ssd-lvm and submitted the
source backup task UPID:proxmox1:00210CA1:1F6FCC34:6ACD7F0A:vzdump:128:crange-iac@pve!terraform:.
The provider task is running and reports disk-transfer progress. Target131 is
reserved but has not yet been claimed restored/verified. No retry should create
a second copy while this handle remains live.

Windows shutdown now selects clean Proxmox shutdown directly, avoiding the
Linux SSH poweroff attempt and ~60-second wait. This was deployed after the
original capture became terminal; its active worker was not restarted.

Remaining rollout gap: the generic Windows initializer only supports workgroup
RID-500/local RDP accounts. A domain snapshot requires a separately gated path
that validates the originating project and captured domain role before changing
access credentials/network settings. Existing base-image checks must stay strict.

## Frozen transport recovery

The original source backup completed in 7m51s (14.90 GB archive) with only the
known e1000 MTU warning. The task adapter previously treated its warning status
as failure. It now requires the exact warning plus an explicit successful-backup
completion log; incomplete or unrelated-warning tasks remain failures.

GUI retry f28a6411da914c11b18d32cc005b4e0f is using the original completed
frozen backup under the same capture receipt/reservation, after confirming the
backup terminal and target131 absent with no active allocation task. No second
source backup was submitted. It verified the archive checksum and started the
stopped restore on P5. Capture identity and independent clone acceptance are
still pending. Tests cover exact recovery and refusal when the copy is present.

Numeric backup transfer progress is now streamed to worker output without raw
provider logs. Additional project/reservation/hash consistency guards are tested
locally and queued for deployment after the current job ends.

## QEMU machine-version compatibility

Restore131/P5 completed, but boot failed because the backup expanded the source
q35 machine to pc-q35-10.1+pve0 while P5 runs QEMU10.0.2. The source DC restarted
successfully. This was a compatibility failure, not a restore/data-transfer failure.

Remote capture now checks the destination's live machine capabilities and uses
the same chipset family at pc-q35-9.2+pve1 (or the corresponding i440fx baseline)
before first boot. It does not alter the live source or upgrade a cluster node.
A narrow recovery gate reuses only the owned stopped isolated copy after proven
successful restore and the exact terminal incompatible-machine start error;
unrelated failures, active tasks and identity-collected copies remain blocked.
Runtime Windows identity/clone acceptance is still required; passing capability
checks alone does not publish an image.

A local Tailscale client stoppage temporarily interrupted observation. Resuming
the existing client restored access; the controller's provider task was inspected
rather than assumed canceled or restarted.

GUI recovery job2ba5917ac05d4ad58f3e841f25e61c15 applied the portable
Q35 baseline to the same owned copy131/P5. Provider qmstart completed with
exitstatus OK. It did not duplicate the source backup or target restore.
Windows snapshot identity/readiness collection remains pending, followed by
conversion/export and an independent disposable clone. This is boot evidence,
not full snapshot acceptance. The prior recovery guard was corrected to match
PVE's unpunctuated error status; a workflow test now covers that exact case.

## GUI capture diagnostics

Added **Inspect retained capture** to Scenario. It resolves the copy from the
project/version/staging key, enforces ownership, pool, stopped status, isolated
NIC and no active task, then starts only that copy for fixed read-only diagnostics
and shuts it down. Tests reject cross-project/source targets and verify cleanup
even when diagnostics fail. No arbitrary guest command is exposed to the GUI.

GUI diagnostic job05e170f550e243a6abe5b77f59e43fda succeeded. Copy131 reports
MHL-DC01/mhl.test/role5; NTDS and ADWS running; DNS StartPending; Ethernet
Disconnected; static10.100.254.10 tentative. Get-ADRootDSE failed with the
ActiveDirectoryCmdlet ArgumentException category. The copy was shut down.
This points to the disconnected network state; snapshot identity is still not
approved, and the original source DC remains running.

Root topology audit of P5 crprobe1450 found no ports but an IPv6 link-local host
address. Do not enable guest link-up yet. Next implement a managed private-bridge
guard that removes host addressing, prevents host/routed traffic, and proves
current isolation before enabling the capture/probe NIC. Preserve all unrelated
interfaces, firewall tables and services.

## Private capture network guard

The fixed guard code is installed on all five nodes. GUI administrator job
2b6b4be9d45d4ca3bf6556d1f709015a prepared P5 crprobe1450 and verified no
host addresses or uplinks, interface IPv6 disabled/routing disabled, and exact
interface-scoped nftables input/forward/output drops. The managed boot service
and interface hook persist those settings. No unrelated interface, firewall
table, OPNsense policy or guest network was changed.

The host relay had been enrolled for P2 only; deployment now includes P1–P5
SSH addresses while preserving its existing guest allowlist. Preparation remains
an administrator-only GUI job. Proof requires the owned capture/probe pool
member and allows only that guest's TAP ports on the otherwise private bridge.

GUI inspection8e06be61cf4141c5a101fbfe7ada13be proved Ethernet Up, preferred
10.100.254.10 and successful AD directory lookup. It shut the copy down and
restored disconnected NIC configuration. Capture and Windows snapshot clone
verification now enable carrier only after a fresh guard proof.
GUI retry391f328f9bfd47a2ab8d52bf8ac321ee is active. Snapshot export and
independent clone acceptance remain unproven.

Tests cover interface-only rules, hook priority/rule drift, host addresses,
uplinks/other guests, relay authentication/extra fields, and diagnostic cleanup.
A bounded retry handles read-only QGA exec startup500 errors; mutating guest
initialization retains its separate command/PID journal.

Latest verified checkpoint: copy131/P5 completed Windows snapshot identity
collection (MHL-DC01, mhl.test, role5, project100 scope) and immutable template
conversion. Its central archive export is progressing; last observed91%.
The export checksum and independent clone proof are not yet accepted, so no
Windows release publication is claimed. Existing GUI job391f328f9bfd47a2ab8d52bf8ac321ee
remains the handle to observe; do not submit another capture while it is active.

Immutable export completed with verified checksum at
crange-library:backup/vzdump-qemu-131-2026_10_12-23_24_05.vma.zst. Initial
independent-probe admission failed; no probe reservation/allocation was retained.
The capture receipt is at `captured`, with immutable template131 and its export
retained. Source128 restarted successfully.

A new recovery path validates that exact template/export/identity checkpoint
and continues probe acceptance without repeating capture or export. GUI retry
0609f23d8ab748ef86a6bfe5fba6cc70 is active and reported checkpoint reuse.
Admission errors now preserve their one-line underlying placement reason.
