# Project 100 changes and fixes

## Starting state

The preceding platform work published and clone-verified Parrot 7.3, prepared a
local replica on Proxmox 1, and enabled multiple SSH/RDP/VNC protocol selection.
Project 1's non-applying plan still demonstrated an exact network-verification
gate. That evidence does not establish Project 100 network or release readiness.

## Current work

- Established the Meridian Harbor Logistics scenario and five-team/two-assessor
  requirements, with separate company and assessment workstation systems.
- Started independent authoritative Windows-image and Project 100 platform
  inspections. No existing unowned Windows guest is approved for modification.
- Created the acceptance matrix; all lab-specific checks remain pending until
  supported by actual runtime evidence.
- Initialized live Project 100, allocated transit `10.60.2.2/24`, and declared
  Teams 1–5 plus staging. No guest deployment was implied by initialization.
- A reviewed bootstrap attempt exposed a single-node admission limit; proving
  additional container-provider nodes and balancing router foundations is next.
- Downloaded official Windows Server 2025 Evaluation media. Large multipart
  uploads disconnected without leaving an installed ISO; a checksum-verified,
  fixed-destination atomic import is being prepared rather than repeating uploads.

Agents append concise dated entries with changed files, jobs/results and any
remaining blockers. Do not place secrets or fabricated verification here.

## 2026-10-08 — capacity and image status

- Configured absolute CPU reserves: two host cores each on Proxmox 1 and 5,
  zero on the other nodes. Existing overcommit ratios and RAM/disk reserves
  remain intact. The live policy revision is `mhl-cluster-cpu-reserve-20261008`.
- Verified all eighteen actual templates contribute zero CPU/RAM commitments;
  dashboard rows now separate configured image size from committed resources.
  Active installer guests remain actual workloads until converted to templates.
- Fixed archive observation to use checksum manifests and Proxmox storage
  content, rather than a nonexistent guest VMID. Uncached locations are labelled
  “Not cached”; deleted provenance locations remain explicit stale records.
- Corrected Parrot source template 109's recorded storage to `crange-library`,
  preserving its independently prepared local replica 111.
- Capacity/bootstrap checks passed 49 tests, image snapshot checks passed five
  tests, and the refreshed live snapshot confirms the requested reserves and
  zero template commitments. No Project 100 deployment is claimed by these fixes.

## Project 100 foundation and Windows installation

- Verified and applied a project-owned foundation plan: seventeen additions,
  no changes or deletions. Seven routers are placed across accepted nodes.
  Bookkeeping and router/network acceptance remain distinct from guest readiness.
- Fixed bootstrap revalidation forwarding and a Proxmox task-list query mismatch;
  handled confirmed missing-guest inventory races without ignoring unknown errors.
- Windows unattended installation failed on a 340-character command exceeding
  Microsoft's 259-character limit, then on ElementTree's serialized namespace
  prefixes. Both defects received targeted regression coverage.
- The answerfile ISO omitted the setup script. A hash-verified script-only ISO
  repaired the installed, owned VM without another OS reinstall. The builder
  must include and verify both files in future seed media.
- Windows build VM 115 now reports Server 2025 Standard Evaluation in a workgroup,
  with QEMU Guest Agent, RDP, WinRM and its serial driver healthy. It is not yet a
  generalized, clone-verified published Windows image.

## Windows and Linux scenario implementation

- Windows scenario playbooks committed as `73cbf82`: independent-team AD
  configuration, per-guest local Administrator rotation, employee/service
  identities and an overbroad Warehouse Deployment GPO delegation/link.
  Fifteen focused offline tests passed. The sealed source evidence records
  cleared MachineGuid and successful Sysprep shutdown alongside EFI warnings;
  fresh-clone boot and identity acceptance remain unverified.
- Linux roles committed as `1f5981c`: PostgreSQL shipment records, customer
  portal/report endpoints, synthetic CI artifacts and backup exports for
  staging and teams 1–5. Ten focused offline tests passed, including encoded
  query handling and service health failures. Fake database tests do not prove
  actual PostgreSQL permissions, deployment or cross-team isolation.
- Integration review found hard-coded PostgreSQL version paths and missing
  runtime SELECT permission on seeded tables. Corrections are in progress;
  review also requires a PostgreSQL Decimal response regression.
- Proxmox 2 is offline, affecting the project router and two team routers.
  NFS storage status was confirmed active after allowing its measured
  approximately 10.6-second response instead of expiring checks at 8 seconds.
- Windows clone launch is pending deployment of tested capture/identity code
  to the controller; no clone job was reported running at this checkpoint.

## Resumed deployment checkpoint

- Linux staging desired state committed as `2b69501`: ten Linux jumpboxes
  use `.11/.13`, reserving `.12/.14` for Windows participants; four Team 0
  business containers use the verified Debian 12 alias. Twenty focused checks
  passed. No scenario guests were applied.
- Windows source 115 remains stopped. A synchronous clone command timeout left
  partial VM 8910; the terminal failed task, absent worker and owned volumes
  were verified before guarded cleanup. The controller's asynchronous request
  then received VM.Clone denial without creating a guest.
- The authorized host-admin fallback launched a persistent clone process,
  PID 1465086, tracked by Proxmox task
  `UPID:proxmox4:00165B00:1C16E38A:6AC797AF:qmclone:115:root@pam:`.
  The latest agent observation reports the same task running at 17 percent.
  This is copy progress, not proof of Windows boot or image publication.
- Acceptance will measure identity/services on the isolated first OOBE clone,
  generalize it, then compare a fresh second clone's actual GUID/SID. Historical
  source identity is not reconstructed.

## Guacamole visibility repair — 2026-10-08

- Live investigation found ten native Guacamole connections, with administrator
  permissions, hidden by the custom homepage because the CRANGE applied access
  indexes contain no connections. Project 100 has ten Linux jumpboxes and four
  business containers declared; these are not yet deployed scenario guests.
- The homepage now merges the authenticated Guacamole connection tree with
  CRANGE-managed rows. Native connections show their permitted Connect links
  and addresses, group protocol suffixes into one row, and appear under Other
  connections. Native access does not imply verified CRANGE guest ownership;
  power and snapshot actions require that ownership mapping.
- Built and deployed the extension on VM 110. The served app.js contains the
  change; authenticated Guacamole login and its ten-connection tree succeeded.
  A focused JavaScript harness checked grouped services, address loading and
  absence of power requests for untracked connections. Guest reachability was
  not proved by those checks.
- Empty project launch tickets now explain that boxes must first be deployed,
  rather than directing the user to reapply an empty access manifest.
