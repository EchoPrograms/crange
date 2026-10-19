# Project 100 dashboard acceptance

Mark an item complete only with an actual job ID, runtime observation, or
browser check. Avoid destructive exercises on unrelated resources.

| Area | Required exercise | Evidence/status |
| --- | --- | --- |
| Projects | Create, automatic transit allocation, rename, selection | Partial: live P100 created, five teams declared, transit 10.60.2.2; authenticated project-settings API renamed it to Meridian Harbor Logistics; Chromium login and project selection displayed the new name |
| Templates | Shared ISO/mirror selection, Windows installer, approved images | Partial: official Windows media imported, failed owned builds cancelled, corrected installer reached managed OS; sealed Windows publication job `3a07826c1c5a47b4b5476ef031656911` succeeded; fresh unattended build/browser acceptance remains pending |
| Cluster | Capacity in GiB, commitments, node distribution, suggested nodes | Partial: fixed admin template-backup preparation on Proxmox6 succeeded through the live node UI (node-template-backup-preparation-acceptance.json); CPU reserves and zero template commitments verified; stopped guests now excluded from CPU/RAM commitments; refresh `d56feaac1e5d41fab417dbf203983df3` succeeded; P5 thin storage `2782f84fbebe4020bf5a3c5f9038c505` and CT cache proof `d8482e3fdb6c4c7ab7a340391484ab13` passed; Cluster browser checks passed for six nodes, GB columns, distribution and 43 stopped/template rows with zero compute commitment (dashboard-cluster-browser-acceptance.json); broader automatic-placement acceptance remains pending |
| Staging | Declare/edit/remove VM, container and custom network | Partial: six original business systems remain deployed. Release0.1.2 fresh-clone acceptance passed both startup orders and final checks (45 total), then reviewed cleanup removed only its disposable cohort; release-0.1.2-fresh-clone-acceptance.json and release-0.1.2-test-cleanup-acceptance.json. |
| Planning | Foundation review/apply, staging review, retained plan identity | Partial: bootstrap and isolated test foundations were reviewed/applied; exact cleanup plan a3d8017843d83cfd3f40d256222003e5 inspected before apply. New selected-release review excludes unrelated staging/jump-box declarations; five-team review 6c40a1f6d0624071a6df99bd9669ae9d produced an independently inspected 30-create linked-clone plan with no other changes; actual browser apply created all 30 servers, then hit a null-ID inventory bug. Configuration-only dashboard recovery 7ec764af704a4db4a7e44f5ac8d4a202 succeeded for all five companies, 75 declared health checks passed. |
| Deployment | Apply and configuration together, refresh after completion | Partial: test cohort Stage Deploy `e7c646b8aacd45db9b153fc4eb234ee3` completed infrastructure, scoped configuration, authenticated Windows management and Guac refresh; five-company recovery succeeded with all 30 servers and 75 checks; participant workstation deployment remains pending |
| Health | Default ping and explicit SSH/RDP/HTTP checks, edit checks | Partial: release0.1.2 passed all 45 fresh-clone health checks (two 15-check boot orders plus 15 final checks). All five deployed companies also passed 15 checks each (75 total), independently recorded; workstation checks remain pending. |
| Teams | Five teams, Team 0, box status and team-scoped management | Partial: Chromium displayed six project networks (Team 0 plus Teams 1–5) desktop/mobile; all five company server deployments now configured and healthy |
| Jump boxes | Custom Windows/Linux profiles, names, placement, protocols | Partial: ten Linux jump boxes and ten custom Windows VM jump boxes are declared, two of each per team. Windows jobs used the actual custom-jumpboxes API, approved Desktop Experience image, RDP, static .12/.14 and automatic placement; workstation-declarations-acceptance.json. Deployment/desktops pending. |
| Users | Bulk create, default setup, naming, passwords, box assignments | Partial: ten team-scoped users created via authenticated API; jobs `66505afd239a461eabe351778e507e52`, `34ad9a994efe42afa39d0ad04bf8b901`, `782f16bd8b324942b0c4661520b0e7a9`, `0a1da8113a204ce1a47826624e989b4e`, `f028a1613cca44208bf4f8005a6e28a7` succeeded; Guac API confirmed all ten; Chromium showed all ten desktop in natural team/user order; self-service password reset remains disabled; actual browser saves assigned each account one Linux and one custom Windows workstation, 20 unique declared boxes total, with zero broad team grants. All ten sync jobs succeeded; effective access waits for deployment (participant-workstation-grants-browser-acceptance.json) |
| Account management | Rename, reset permission, set password, lock/unlock, delete fixture | Partial: API fixture create/rename/password change/reset toggle/lock/unlock/delete verified with jobs `fbd6a3079cf547e9948e67c29530fb18`, `1146ed7e7dab43c8ae7cc32e393e8ee4`, `09aa81362f12401681658f55a15f2930`, `2734debc2a9f4226a12a09c533a393e7`, `92a1aa9f55bf4a26a2c2fa96ea81eabe`, `419a3f04d1234b2e9a2f9bbf742fe0ef`; new password authenticated, old rejected, fixture deleted remotely and locally; actual browser create/duplicate-error/rename/lock/unlock plus serialized native lock, six-character password login, and complete fixture deletion now accepted (guacamole-user-lifecycle-acceptance.json) |
| CRANGE accounts | Separate dashboard accounts and project authorization | API acceptance passed: integer project access, manager role, project visibility, clear duplicate rejection, foreign-project and admin denial, fixture deletion and session revocation; evidence in dashboard-account-acceptance.json. Rendered form acceptance also passed: actual create payload [100], saved access, invalid-input and duplicate errors, and deletion; dashboard-users-browser-acceptance.json |
| Guest accounts | Create/delete, password, sudo grant/revoke on owned fixture | Linux acceptance passed on staging container t254-mhl-app01: actual browser forms, old password rejected after change, new password verified, real password-authenticated sudo root command succeeded then denied after revoke. Fixture account, home and managed policy deleted; controller root access retained. Fixed automatic-node resolution and missing sudo prerequisite. See guest-account-acceptance.json. Windows/BSD account administration remains outside this Linux-only workflow. |
| Guacamole | Team/project grouping, IPs, SSH/RDP connections and admin access | Partial: 36 real servers show IP/power after actual browser dashboard-launch and reload acceptance; and admin hypervisor VNC consoles connected to both Windows VMs and all four Linux containers. Native session survives console launch/reload; hidden input artifacts fixed. Evidence: guacamole-ticket-session-acceptance.json, guacamole-console-coverage-acceptance.json. Company Linux console also connected under the configured native management account and preserved its session (guacamole-company-console-acceptance.json). All ten participant accounts exist, but workstation grants and real participant desktops remain pending. Service-container SSH entries have an unresolved default-account mismatch. |
| Power/snapshots | Start, shutdown, stop override, restart, create/revert/delete fixture snapshot | Partial: actual Guacamole management endpoint accepted LXC snapshot create/restore/delete, start and force stop on staging CI CT124/P2. Rollback removed a post-snapshot account; original CI account/root-key access remained. Final ping and two HTTP checks passed; test snapshot removed. See staging-snapshot-acceptance.json. QEMU, restart/shutdown and pending-shutdown override remain unaccepted. |
| Jobs | Visible task/status, modal, scrolling, notices, stop and recovery | Browser acceptance passed for page size, next-page navigation, legible name/status and expanded output surviving a completed refresh re-render; dashboard-jobs-browser-acceptance.json. Running task/spinner and persistence after reload also passed (dashboard-running-job-browser-acceptance.json). Centered popup launch, close-without-cancel, queued Stop task, cancellation notice and live-output scroll persistence also passed (dashboard-job-dialog-browser-acceptance.json and dashboard-job-scroll-browser-acceptance.json). Running-task stop and success/failure notices still require acceptance |
| Releases | Capture, fresh-clone test, selected/all-team rollout, upgrade/rollback | Partial: complete six-image release0.1.2 captured and fresh-clone accepted; service recipes replayed unchanged, both boot orders passed and disposable test cleanup verified. Version0.1.2 declared for all five teams through job32634bfa69ea43079fa83bd8892808b0. All 30 company servers created with independently verified linked backing; dashboard recovery 7ec764af704a4db4a7e44f5ac8d4a202 passed all five company health matrices; upgrade/rollback pending. |
| Isolation | Cross-team denial, controller/Guac management path, no host exposure | Pending |
| Cleanup | Disposable probes/users/releases removed; necessary bases retained | Partial: release0.1.2 test guests/router removed by reviewed plan and provider verification; originals/caches retained and Guacamole refreshed to six connections. New caches reuse freed IDs, so historical ID absence is not deletion authority. Remaining lab-wide cleanup pending. |

Features that require additional implementation remain pending rather than
being claimed as exercised. The final audit must cover all twenty participant
jump boxes and every deployed team's business systems.


### Compact GUI acceptance — 2026-10-16

The refactored controller layout is deployed. Authenticated browser checks
rendered all ten live views without page overflow or JavaScript errors, verified
actual project selection, six-node disclosure persistence across immediate
refresh, row menus, visible running-job status/task indicator, and a 390px
scenario layout. No mutation requests were made beyond login. Evidence:
[compact GUI acceptance](dashboard-compact-gui-acceptance.json). This visual
acceptance does not establish completion of the pending lab deployment and
workflow checks listed above.


October 16 additional GUI acceptance: Remote access is a monitor icon in the
visible row controls for Scenario VMs/containers, team VMs/containers and jump
boxes. The existing configuration dialog opens without submitting a mutation.
See `dashboard-remote-access-icon-acceptance.json`. This does not mark participant
connection grants or desktop logins complete.

### October18 cache-storage controls

Live Chromium checked both approved P6 pools, disabled preparation for an
already verified cache on its selected pool, enabled another eligible pool, and
preserved selection across rerender. Existing source templates with no linked
verification no longer masquerade as prepared caches. Actual clicks queued APP
and BKP preparation jobs664fe5e41eb1462ca5cbd97b8d300f46 and
3223f4038e884b13912e5a664b45455d. No JavaScript errors. See
[cache storage UI acceptance](cache-storage-ui-acceptance.json).

### October 18 service-update publication and workstation cache recovery

Publish service update is implemented and its actual browser job published
0.1.3 while preserving all six image references, the parent manifest and the
Windows recipe. Seven release-recipe tests cover identity/definition changes,
publication collisions, retry and scope. Staging CI actual artifact-token login
passed through the fixed role. Missing applied image metadata now completes
only exact matching guest identities; targeted inventory regressions pass.

Windows cache restore recovery now admits the exact incompatible-machine
failure followed by a confirmed stop, retaining the same probe and reservation.
The isolated probe uses the capability-checked portable machine baseline.
Nineteen archive recovery tests and 51 distribution tests pass; runtime
verification job c6dfdbc9e340494c93d0fc8481c6283b was running at the checkpoint.
This does not yet establish workstation deployment or desktop access.


Independent read-only HTTP checks now passed in all five deployed 0.1.2 company
environments: database health, the intended cross-customer shipment read, the
bounded report-query behavior (one normal row versus three broadened rows),
and the published artifact identity/token presence. Evidence records no token
values. Authentication binding is proved in staging only until 0.1.3 rollout.
See [five-company application checks](five-company-application-acceptance.json).

Windows cache runtime recovery is now accepted: job
c6dfdbc9e340494c93d0fc8481c6283b succeeded, the original probe passed independent
identity/readiness, deletion was confirmed and the reservation/pin released.
See windows-workstation-cache-acceptance.json. Workstation deployment remains
pending. The QEMU compatibility regression is fixed for isolated cache probes;
no live guest or immutable parent machine setting was changed.

New-VM plan overrides now use the same capability-checked portable machine
baseline as verification; existing machine settings remain frozen from applied
state. Ten archive/media tests, 29 scheduling tests, 21 admin-template tests,
and 19 archive-recovery tests pass. Expired guest review cleanup is now invoked
from Project Overview planning and understands dedicated jump-box provider
names; four cleanup tests pass and actual job
314c45617e0c4da1a067154adb437fdf released one expired unused review before planning.
No workstation apply is claimed from these checks.

### October 19 — reviewed release replacement ordering

The existing release-apply workflow now quiesces exact retiring release guests
before fresh clones can reuse their IPs. Eight tests cover selected ownership,
all-guests validation before stops, recorded-task replay protection, old/new
node locks, real saved-apply ordering and failed-stop prevention of Terraform.
Review and configuration-only recovery regression suites pass. This is tested
implementation, not yet live upgrade/rollback acceptance.

Read-only preflight against actual Team 1 Terraform resources and current
provider identities passed for its two VMs and four containers. The deletion
entries were simulated only to validate parsing/ownership; no stop or upgrade
was submitted. Evidence: release-retirement-readonly-preflight.json. This caught
and corrected the provider CT hostname field location (initialization.hostname)
before a live release replacement.

### October 19 — participant native authentication

All ten actual participant logins authenticated with their protected stored
passwords to native PostgreSQL-backed Guacamole, and all test sessions were
revoked. Their effective native user permissions lack self UPDATE. A sampled
same-value self-password change request was denied HTTP403, leaving the
credential unchanged. Evidence: participant-native-login-acceptance.json and
participant-self-password-policy-acceptance.json. Two-box visibility, cross-team
denial and actual Linux/Windows desktop sessions remain pending deployment.

### October 19 — outage recovery checkpoint

Verified same-cluster API failover is deployed and actual controller reads now
use Proxmox4. Eight transport regression tests passed, including identity/quorum
gates and no replay of uncertain writes. Console gateway selection is updated;
the idle broker restarted at 02:17:07 UTC. New console-session acceptance is pending.
Workstation apply timed out after creating seven Windows guests on Proxmox6.
Both apply timeout layers are fixed; one command-budget and eight release-retirement
tests passed. No completed workstation deployment is claimed. Proxmox1 is offline,
reservation reconciliation and a fresh plan failed safely, and its root cause
requires console/host logs. Historical health acceptance predates this outage.

Post-outage actual dashboard release-check job
9fbc017370b148e1b503a6104861ce0f succeeded for all 18 company servers in
Teams 2–4; Teams 1 and 5 remain untested during their router-host outage.
The native Guacamole admin connected to a Team 2 Linux hypervisor console
through the verified Proxmox4 gateway and survived console close/reload without
a visible error. See release-0.1.2-outage-health-acceptance.json and
guacamole-failover-console-acceptance.json. A separate dashboard-launch
observation rendered all 36 rows and IPs, but recorded one transient HTTP403
and unresolved power observations; it is not full power/status acceptance.

Console acceptance identity clarification: the browser harness authenticated
using the protected configured native management account. It did not record
the exact username. Current configuration identifies that account as
crange-sync. Those console checks establish native management-role access,
not a password-login test of the literal guacadmin account. New snapshot and
power acceptance records name their actual native actor explicitly.
