# Project 100 changes and fixes

## 2026-10-20 — outage-safe project review and core router storage

- Extended changed-resource review to ordinary infrastructure plans. The local
  comparison only selects targets; the saved plan still refreshes selected
  resources and checks untouched-state fingerprints before/after applying.
- Retained existing VM boot order so an updated base profile does not reboot
  healthy guests during an unrelated deployment. A live dashboard review now
  proposes exactly the 13 missing workstation creates and no existing updates.
- Added an operator-only, journaled core-router storage installer. It checks the
  exact thin pool, physical headroom and quorum, refuses to format an existing
  device, and never replays an interrupted installation. Proxmox4's new store
  passed shared read/write checks on every online node. Production HA enrollment
  and Terraform node adoption still require separate acceptance.
- Verified all 18 release servers in Teams 2/3/4 through the normal health job.
  The remaining workstation deployment is running; full lab readiness is not
  yet established.

## 2026-10-20 — align Guacamole account-menu rows

- Removed inherited native button margins from Preferences and Logout so their
  hit areas, highlights and text align with Home and Settings.
- Verified both desktop and phone geometry with native Guacamole CSS and kept
  existing menu keyboard/navigation behavior. The fix is deployed at
  `range.brogano.dev`; the public CSS includes the corrected rule.

## 2026-10-20 — consistent sign-in and Guacamole navigation

- Shared Control/Access sign-in geometry, type, fields, errors and responsive
  controls through `gui/login.css`; retained separate branding and added links
  in both directions. Native Guacamole credentials/continuation remain native.
- Moved Preferences into the account dropdown and separated the controller link
  at the far right. Settings back navigation now uses tab-local Projects/server
  context, including all-server and individual-project views.
- Corrected History search/input sizing and mobile wrapping; made native search,
  sort and action artwork visible against dark surfaces. Search/download retain
  their native handlers.
- Changed all repository references and deployed public URL defaults to
  `https://range.brogano.dev/`. Internal Guacamole connector addressing is unchanged.
- Deployed and checked the 27-entry Guacamole package. Browser checks passed for
  native sign-in/errors, dropdown Preferences, back navigation and logout. Full
  Python suite: 1,093 tests, one skipped; three Guacamole JS suites passed.

## 2026-10-20 — scoped configuration and outage acceptance

- Deployed post-apply private inventory filtering, SSH identity refresh scope
  and reuse of the filtered inventory for Windows initialization. Explicit
  Configure team remains a complete team operation. Added tests for nested
  host removal, preserved routing variables, temporary-file permissions and
  missing-host rejection.
- Live staging review exposed a stale-inventory gate before any allocation.
  Deployed guest-disk storage filtering and four concurrent read-only guest
  configuration requests; provider mutations remain sequential. The next
  dashboard API review succeeded and proposed only the disposable staging CT.
  Deployment, router-local ping and controller SSH passed. Reviewed cleanup
  deleted only the probe and revoked its managed Guacamole SSH permission.
  All 50 prior guest states stayed identical; the temporary manager and sessions
  were removed. See redundancy-staging-deployment-acceptance.json.
- Added tests/fixes for preserving unchanged guests' exact image/storage
  metadata and retaining unavailable snapshot-cache journals/reservations while
  allowing planning on surviving nodes. Those fixes and scoped deletion access
  cleanup are deployed. Full suite: 1,093 tests, one skipped.
- Verified the actual mounted Guacamole extension matches the current build
  entry-for-entry and the public JS serves the custom Settings UI/controller
  link. An unused host path was not mistaken for the running Docker mount.

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

## October 18–19 — workstation deployment and release-transition guard

- Fixed incompatible QEMU machine inheritance for isolated probes and reviewed
  new-VM deployment overrides, preserving existing guest machine types. Windows
  cache recovery c6dfdbc9e340494c93d0fc8481c6283b passed readiness/identity and
  cleanup; no second archive restore ran.
- Project Overview planning now invokes guarded expired guest review cleanup;
  dedicated jump-box names are derived correctly. Live plan preparation
  released one confirmed unused expired review.
- Reviewed corrected plan 5613c70c92cea61d9bf5675253ed618d: 20 linked workstation
  creates, ten empty-CD-ROM boot-order updates, no replacements/deletes.
  Apply 8fafed50dda9423b949aea82964f3a13 is running. Provider boot-order changes
  rebooted company Windows guests; repeat their health checks after completion.
- Added a release-transition stop phase under the existing reviewed Apply
  workflow: verify all retiring old release guests and their exact endpoints,
  lock both old/new nodes, journal stop task handles, stop the old guests, and
  verify they remain stopped before creating peers with reused IPs. Uncertain
  submissions/tasks block Terraform rather than being replayed. This code has
  eight focused tests including saved-apply ordering/failure, plus existing
  review/recovery regressions. Actual upgrade/rollback acceptance remains open.
- Fixed staging CI artifact credentials to authenticate a real unprivileged
  account, and published immutable service update 0.1.3 while preserving the
  parent and six image references. Fresh testing/rollout remains pending.

- Participant authentication acceptance now covers all ten native Guacamole
  password logins, revoked test sessions, absent self UPDATE permissions and a
  sampled self-password-change request returning HTTP403 without changing its
  effective credential. Workstation sessions/grants remain a separate gate.
- Source now grants apply and stage-deploy the existing bounded 12-hour batch
  budget; interactive actions retain one hour. The 26-job suite passes. Do not
  restart the current worker mid-apply; activate this change when it is idle.

## October 19 — partial apply and unexpected host outage

- Recorded the workstation apply timeout and seven retained Windows guests on
  Proxmox6; configuration and desktop acceptance remain pending. Fixed both
  Terraform apply and ordinary apply-worker budgets, without replaying the old plan.
- Deployed verified same-cluster API gateway failover, with actual read acceptance
  through Proxmox4 while Proxmox1 is unavailable. Uncertain writes are not retried.
- Reservation reconciliation stopped on inaccessible Proxmox1 router CT120; no
  reservation release or quorum override was forced. Root cause is unknown.

- Post-outage exact dashboard release-check passed for Teams 2–4 (18 business
  servers). Teams 1 and 5 still depend on unreachable Proxmox1 routers.
- Native Guacamole admin console connections to Team 2 Linux and Windows guests
  passed through the Proxmox4 API gateway; native sessions survived console close
  and reload. No additional guest network services were opened.

## October 19 — Guacamole account lifecycle acceptance

- Recovered controller access after the local session workspace was replaced.
- Verified the actual browser account lifecycle and a serialized native lock
  check. Completed lock job b74ceab50e1b4eada8e43b079214d4d3 rejected login HTTP403;
  unlock job 9bf753ded5244b7d940b75eae6f3b572 accepted a six-character password.
- Deleted the isolated audit account through dashboard API job
  21ab73557cdf41bea6dbb739d5e614d2 and confirmed native/directory absence.
- Kept the existing Guacamole 1.6 disabled flag payload: the earlier overlapping
  lock/login/unlock experiment did not establish a product defect.

## October 19 — staging snapshots and power through Guacamole

- Used actual Guacamole extension HTTP payloads as configured native management
  account crange-sync to create, restore and delete one staging-only LXC snapshot,
  and to force-stop/start the same CT124. No company deployment was changed.
- Proved filesystem rollback by creating a temporary account after snapshot,
  confirming its presence, and confirming its absence after restore. Preserved
  warehouse-bot and controller root-key access; final ping/two HTTP checks passed.
- Removed the disposable snapshot and returned CI to running. Clarified the native
  identity scope in earlier console evidence; no literal guacadmin password login
  was established by that harness. QEMU/override acceptance remains pending.


## 2026-10-19 — Interface maintenance

Deployed the controller/Guacamole contrast and layout audit, project/server cards,
grouped preferences and floating remote-session controls. Applied square corners,
neutral status/alert treatments and self-hosted IBM Plex Mono. Live search, session
network mapping, native advanced controls and mobile layout were checked.
No guest deployment, power or network configuration was changed. Evidence:
[interface-polish-acceptance.json](interface-polish-acceptance.json); design rules:
[interface-design.md](../interface-design.md).


- 2026-10-19 — Controller/Guacamole UI follow-up: compact Plex Sans typography,
  semantic colors on neutral surfaces, project selector in the sidebar, corrected
  remote-access favicon, project creation/name settings and project user context.
  Browser writes were tested with an isolated API fixture; no lab guests, release
  contents or network policy changed.


- 2026-10-19 — Guacamole follow-up: corrected connection-icon vertical alignment,
  larger server text, heavier symbols, neutral icons on white Connect buttons,
  and Linux/Windows/BSD indicators from recorded OS metadata. Native verification
  no longer aborts when Proxmox1 is offline: it skips offline nodes and individual
  failed guest reads, bounds interactive reads, and uses cluster-reported power.
  The authenticated Guacamole-to-controller inventory returned HTTP 200 with 36
  permitted connections, 33 matched to reachable guests. No guest power, disk or
  network settings were changed.


## October 19 — project retention and access interface

- Added manager deletion from Project Settings, retained-project admin restore,
  and dependency-checked permanent removal. Saved definitions/releases survive
  ordinary deletion; restoring a project requires redeploying its infrastructure.
- Added queue/lifecycle fences, exact Terraform ownership checks, managed route
  and gateway cleanup, and archived administrator job history on permanent removal.
- Guacamole displays the saved company project name with `(p100)`. CRANGE admin
  launch preserves native Guacamole login instead of impersonating the dashboard
  username. Thickened dashboard icons and changed force-stop indication to red.
- Fixed floating Guacamole management menu positioning and prioritized selected
  server power checks. Verified current authorized sample box states read as running.
- Validation uses temporary projects and intercepted browser requests. No live
  Project 100 resources were deleted or reset. Proxmox1 remains unavailable and
  blocks complete release-image dependency proof for permanent cleanup.

### Project lifecycle and session controls (2026-10-19)

- Project managers can delete deployed infrastructure from Settings while retaining definitions and immutable releases. Administrators can restore a deleted project for redeployment or purge its private artifacts. The administrator project list exposes Delete; Deleted projects offers Restore and Permanently delete after teardown succeeds. Both destructive actions require the exact project identifier.
- A reused project number no longer makes teardown fail on an unrelated old OPNsense gateway when the new project has no aggregate route. The legacy gateway is preserved. Routing cleanup is preflighted before infrastructure destruction; firewall policy is unchanged.
- Guacamole headings use saved project names. Dashboard administrators open native Guacamole authentication, preserving the separate guacadmin account. Floating server menus have viewport anchoring and complete power controls.
- Interactive power and node status use short-lived, identity-checked cluster observations instead of per-guest Terraform subprocesses or old capacity snapshots. Offline or unverified hosts cannot be displayed as online from stale capacity data. Capacity age remains visible.
- CRANGE Users is a compact table with a plus button and a focused account editor. Project access, per-project roles, account status and deletion remain available.
- The connection network map includes authorized machines in the current team. Each browser/account/team can retain host aliases, notes and manually entered custom hosts. Managed addresses refresh from the authorized inventory; managed hosts cannot be deleted or have their IP overridden. These edits never submit a controller job.
- The session toolbar can be dragged by its grip or moved with arrow keys, hidden and reopened. Position and visibility are retained locally. Native clipboard, display, advanced controls and power authorization are preserved.
- Lifecycle destructive operations are covered by mocked safety tests and live routing dry-runs; verification does not delete a live project. Private-image purge remains blocked where dependency evidence cannot be collected from an offline node.

- Deletion and permanent deletion now persist `waiting-for-node` jobs when required hosts are offline. These jobs retain their project fence and appear as active jobs, but release the execution lane. The worker checks reconnection every 15 seconds, then requeues the same job and rechecks authorization and exact ownership before proceeding. Unrelated offline hosts do not block projects whose guests are on available nodes. Private-image deletion waits for all dependency evidence.
- Templates returns saved builds immediately and refreshes live availability once in a shared background collector with short interactive API timeouts. The tab polls only while collecting and preserves open dialogs. Cached display hints never authorize publication or guest mutations.
- The connection title opens Guacamole’s native connection selector, with checkboxes for multiple connections displayed together. Existing authentication, permission filtering, connection grouping and tiling remain native.

- Preferences now has a Back action; leaving a connection returns to its active project instead of the project directory. The Access icon is a simplified inverse CR monogram, with SVG and touch-icon assets updated together.


## Guacamole settings and portal consistency — 2026-10-19

- Matched portal and Settings headers to CRANGE Control's typography, spacing,
  square controls and neutral palette; retained the larger readable portal text.
- Added a plain CRANGE Control link at the top right, opening the controller in a
  separate tab without leaving the current access portal.
- Replaced the inconsistent native account dropdown with an aligned popover and
  reorganized Settings into a compact navigation rail and readable working area.
  Native permissions, settings actions, filtering and editing routes are retained.
- Pinned project card network/server counts to the bottom for consistent alignment.
- Verified desktop/narrow browser layouts, menu keyboard/outside dismissal and
  project return; Guacamole UI/workspace/auth regression checks passed.


## Node-loss resilience — 2026-10-19

- Restricted new workloads on Proxmox1 to router containers; Proxmox2 remains
  unrestricted. Explicit pins, installer admission, cache target planning and
  release-capture destinations honor the role. Existing placements are retained.
- Fixed automatic router bootstrap requiring offline unused candidates to be
  available. Exact pins still fail when their target is unavailable.
- Added 26-case placement and quorum models plus conflicting-policy rejection.
  The full Python suite passed 1,079 tests (one skipped), including subsequent
  scoped-apply checks.
- Installed configuration version 23 with two votes for Proxmox4 and one for
  each other node. Verified configuration and live quorum on all five online
  members without restarting services or inducing a host failure. Scoped
  Terraform work during outages and fenced router HA remain pending; these
  checks must not be presented as complete lab failover acceptance.

- Fixed the offline capacity reader denying unrelated services on an unavailable
  node. Added a root-owned inventory scope with quorum, membership, offline
  identity, tag and digest checks; output contains limits rather than raw config.
  Historical VM snapshot sections are excluded from active-limit accounting.

- Live validation exposed repeated LXC mount settings in an unrelated service.
  The sanitized reader now ignores non-capacity fields while continuing to
  reject duplicate CPU/RAM/disk limits. The 31 relay and 31 scheduling tests pass.

- Cache planning now uses published image sizing without contacting an offline
  source template. A live discovery/admission check selected Proxmox6/local-hdd
  for a new router; no guest or reservation was created. Discovery took 112s.
- Deployed reviewed-plan guest scoping for begin/finish/reconciliation, retained
  state fingerprints, and explicit deletion-node checks. Removed broad guest
  dependencies on every team router while retaining SDN and Ansible ordering.
  Tests verify an unrelated offline router is untouched, while affected-node
  outages, unreviewed reservations and unrelated state drift remain errors.
  Terraform validation and formatting passed.
- The first live review avoided unrelated team routers but still tried every
  staging guest, failing on the three staging guests on Proxmox1. A local-only
  comparison now selects changed targets before a separate normally refreshed
  provider plan. The final review succeeded in 106.6s, with no resource changes
  and all 50 existing guest states untouched. No guest creation/apply was done.
  Post-deploy configuration scoping, creation retries and fenced HA remain open.
- Pinned provider source inspection confirms container node changes force
  replacement, so native router HA needs verified Terraform state adoption as
  well as shared surviving storage. No router migration/HA enrollment was made.
