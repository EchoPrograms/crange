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
