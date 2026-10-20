# Project 100 — current status

Updated October 19, 2026 (UTC). Historical diagnostics and job evidence are in
[platform-progress.md](platform-progress.md).

## October 19 — redundancy implementation in progress

The owner clarified that only Proxmox1 should be limited to routers; Proxmox2
remains available for normal workloads. Admission now enforces that role across
new scenario/jump-box placement, installers and capture destinations. Automatic
router bootstrap can ignore an offline unused candidate instead of requiring
all candidate nodes to be online. All 26 modeled zero-to-three non-core failure
sets pass new router/scenario placement checks with available images/capacity.
The full Python suite ran 1,077 cases successfully, with one skipped.
The protected offline capacity reader now supports unrelated services as well as
lab guests, withholding raw configuration and checking quorum and exact offline
identity. This removes the project-specific allowlist from capacity discovery.
Cache planning now uses published image sizing without requiring the original
template host to be online. A live read-only admission check discovered the
remaining nodes and selected Proxmox6/local-hdd for a new router; it created no
guests or reservations. Discovery took 112 seconds, so its latency still needs
attention. See [admission acceptance](redundancy-admission-acceptance.json).

The reviewed quorum design gives Proxmox4 two votes and all other nodes one:
seven expected votes, quorum four. Corosync accepted the candidate configuration.
The change is live at configuration version 23 and verified on all five online
members; no host networking or node power state was changed. See
[quorum acceptance](redundancy-quorum-acceptance.json). This does not establish
router HA or end-to-end creation during outages. Scoped Terraform admission and
completion are now deployed with unchanged-team-state verification and fewer
router dependencies; live outage review is in progress. Safe creation retries
and fenced router runtime recovery remain unfinished. See
[redundancy implementation status](../redundancy.md).

## October 19 — staging snapshot and power acceptance

The staging CI container (CT124 on Proxmox2, 10.100.254.50) passed actual
Guacamole management-endpoint snapshot creation, rollback, deletion, force stop
and start. A temporary Linux account created after the snapshot was present
before rollback and absent afterward; the baseline warehouse-bot account and
controller root-key access remained intact. Restore left the container stopped;
Start returned it to service. The disposable snapshot was deleted. Final GUI
container-health job ef7d36cdf1384fb49b3086583c721c72 passed ping and both HTTP
checks. Guacamole's stopped-state response matched the actual provider state.
The actual native management actor was crange-sync. See
[staging snapshot acceptance](staging-snapshot-acceptance.json).

This establishes LXC endpoint behavior, not QEMU snapshot or pending-shutdown
override acceptance. Full workstation deployment and the remaining five-company
acceptance gates still depend on recovering Proxmox1.

## October 19 — Guacamole account management acceptance

Actual browser actions created the isolated audit account, displayed a duplicate
username error, renamed it, and submitted lock/unlock jobs. A later isolated
lock test waited for its completed job and verified Guacamole's disabled flag
and a rejected native login (HTTP403). Unlock with a six-character password
completed and authenticated natively (HTTP200), with zero connection grants.
The account was then deleted through the same dashboard API, and deletion was
confirmed in both directories. Temporary native test sessions were revoked.
See [account lifecycle acceptance](guacamole-user-lifecycle-acceptance.json).
An earlier overlapping lock/login/unlock observation was inconclusive; no
speculative change to the existing Guacamole 1.6 lock payload was made.

The cluster still reports five of six members with quorum; Proxmox1 remains
absent. Workstation deployment, full company acceptance and agent skills remain
pending as detailed below.

## Latest checkpoint — October 19: partial workstation apply; Proxmox1 outage

Workstation apply `8fafed50dda9423b949aea82964f3a13` failed at the Terraform
subprocess's 1800-second timeout. Direct provider inspection confirmed seven
new Windows workstations (VMs 220–226) running on Proxmox6, no new Linux
workstations, and no active provider tasks. Their guest configuration and desktop
access are not yet accepted. Preserve these guests; do not replay the stale plan.
Both the worker and Terraform apply budgets now allow a bounded 12-hour batch;
the worker was restarted while idle to activate the change.

Proxmox1 unexpectedly became unreachable over LAN and Tailscale. The owner
confirmed this was not planned maintenance. Tailscale diagnostic ping also
failed. The remaining five nodes retain quorum. The reviewed workstation plan
contained guest changes on Proxmox6, no host network changes or node reboots.
Root cause remains unproven until console/recovered system logs are available.
During that outage investigation, no quorum overrides, host network changes, or forced guest migrations were made.

The protected controller API gateway now verifies cluster identity and quorum
before failing over to another configured cluster member. Actual reads succeeded
through Proxmox4; writes are never automatically replayed after uncertain delivery.
See [API failover evidence](cluster-api-failover-acceptance.json).
Reservation reconciliation job `cea6ffa333b8453f86d04d3552f32a47` failed safely
when Proxmox1 could not provide router CT120's configuration (HTTP595).
Reservations remain retained. Fresh plan job `23aa0528748547e5a72c329c4138049c`
also stopped at the protected offline-config snapshot gate. No apply followed.
See [outage observations](proxmox1-outage-2026-10-19.json).

The old post-apply observer exited without submitting team configuration jobs.
Boot-order updates rebooted existing Windows company guests on Proxmox6, so
repeat company health and access acceptance after recovery. Release 0.1.3 still
requires fresh testing and explicit rollout. Full lab acceptance and agent skills
remain pending.

Post-outage dashboard API health job `9fbc017370b148e1b503a6104861ce0f`
succeeded for Teams 2–4, checking all 18 deployed company servers and their
declared checks. Teams 1 and 5 were not retested because their routers reside
on Proxmox1. See release-0.1.2-outage-health-acceptance.json. Native Guacamole
admin connected to Team 2's Linux hypervisor console through the recovered API
path and retained the native session after reload, without opening guest ports.
The Windows domain-controller hypervisor console also connected through the same
path and retained the native session. See guacamole-failover-console-acceptance.json
and guacamole-failover-windows-console-acceptance.json.

## Earlier workstation preparation — historical observations

Recovery job `c6dfdbc9e340494c93d0fc8481c6283b` succeeded. The same Windows
probe passed independent identity/readiness checks on the supported portable
machine baseline, was stopped and deleted, and its admission and archive pin
were released. The immutable cache parent 219 remains stopped on Proxmox6 /
local-hdd. Exactly one archive restore exists in its provider history; no new
restore was submitted. See [Windows workstation cache acceptance](windows-workstation-cache-acceptance.json).

The first workstation plan proposed 20 creates and ten in-place changes that
only remove an empty CD-ROM from existing company Windows boot orders. It was
not applied: deeper inspection found new Windows guests still requested QEMU
10.1. New-VM planning now checks and supplies the portable machine baseline;
existing guest machine settings are frozen from actual state. The immutable
source image is unchanged.

Plan job `288c58fec9c244e797b2a9cd983934e0` failed at the outstanding-review
limit. The guarded expired-guest-review sweep now also runs for Project Overview
plans and derives dedicated jump-box names correctly. Plan job
`314c45617e0c4da1a067154adb437fdf` released the expired unused review after
negative guest/provider-task checks, then caught an override bug: Parrot's
intentional null machine type cannot use coalesce(null, null). The corrected
conditional preserves inheritance. Controller Terraform validation passed;
its pre-existing undeclared router_placement warning remains. Corrected plan
job `2f5dc4801fc94360a1529a9cda19f2ab` succeeded.
Corrected plan `5613c70c92cea61d9bf5675253ed618d` passed independent inspection:
20 linked creates (ten Linux, ten Windows), all Windows machine types set to
pc-q35-9.2+pve1, no destroy/replacement and no existing disk/CPU/RAM/NIC/machine
changes. Ten empty-CD-ROM boot-order removals remain. Exact dashboard apply
payload submitted job `8fafed50dda9423b949aea82964f3a13`; it later failed as described above.
See workstation-plan-acceptance.json and workstation-apply-api-acceptance.json.

Provider observation confirmed a Windows server reboot during its boot-order
update, followed by running state. This is an interruption even though no disk,
CPU, RAM or machine type changed. Repeat company health after apply/configuration.
The first new Windows workstation creation completed as VM220; the old cache
probe's retired ID was reused, so historical probe absence is not current
permission to delete VM220.

All ten participant native Guacamole password logins passed; temporary sessions
were revoked. All ten lack self-password UPDATE permission, and a sampled native
password-change request was denied HTTP403. See participant-native-login-acceptance.json
and participant-self-password-policy-acceptance.json. Workstation desktops and
cross-team isolation remain unaccepted.

## Previous checkpoint — 2026-10-18 19:34 UTC

All five release 0.1.2 company environments previously passed their 75 declared
health checks; all 30 deployed business servers have independent running-state
and linked-backing evidence. Twenty participant workstations remain declared,
not deployed. Ten accounts each have two intended workstation grants; actual
participant desktop access remains unaccepted.

The CI artifact credential now has a real authentication consumer. Staging
configuration job `d32e4ecab18245388242191f51e88cad` passed: the fixed role reads
the token from the published HTTP artifact, authenticates locally as the
unprivileged warehouse-bot account, and checks its UID/groups without recording
the secret. A missing-image-metadata inventory failure was fixed by filling only
empty fields from exact matching applied guest/template identities; explicit
conflicts and reallocated guests are preserved or rejected.

New dashboard Release tools → Publish service update published release 0.1.3
through job `a892893ef6d74c9ea2bd149c1cf794b3`. It reuses the six retained 0.1.2
images and updates only the fixed service recipe. Parent manifest and Windows
recipe remain unchanged. Fresh 0.1.3 testing and rollout are still pending;
0.1.2 company deployments have not silently changed. See
[publication evidence](release-0.1.3-service-update-acceptance.json).

The Windows workstation base restore completed successfully into stopped cache
219 on Proxmox6/local-hdd. Its existing disposable probe 220 failed because the
archive pins QEMU 10.1 and this node has 10.0.2. Capability inspection confirmed
the portable pc-q35-9.2+pve1 baseline. A guarded recovery fix is deployed; it
reuses only the exact owned stopped probe after checking the failed start and
successful stop, archive pin, reservation, disks and ownership. Recovery job
`c6dfdbc9e340494c93d0fc8481c6283b` was observed running actual verification.
No additional restore was submitted and no live guest or immutable parent was
changed. Completion and probe cleanup must still be checked. The failed RAID
cache capacitor remains a physical repair requirement; no unsafe write-cache
override was enabled.


Independent read-only HTTP checks now passed in all five deployed 0.1.2 company
environments: database health, the intended cross-customer shipment read, the
bounded report-query behavior (one normal row versus three broadened rows),
and the published artifact identity/token presence. Evidence records no token
values. Authentication binding is proved in staging only until 0.1.3 rollout.
See [five-company application checks](five-company-application-acceptance.json).

Remaining: workstation deployment/access, fresh 0.1.3 test and rollout,
assessment and isolation proofs, operational exercises, final cleanup and
handover. Agent lab-authoring skills come after full lab acceptance.

## Previous checkpoint — October 18, 18:35 UTC

**All five company server environments are configured and healthy.** Dashboard
recovery job `7ec764af704a4db4a7e44f5ac8d4a202` succeeded. All five recorded
release health states are `passed` with no failures, covering 15 declared checks
per company (75 total). Independent direct guest-status reads confirm all 30
company servers running. Cluster summary rows currently report `unknown`; fresh
per-guest status and configuration were used instead. P6 pvestatd is active,
and quorum is healthy with all six expected votes. See
[five-company health acceptance](release-0.1.2-five-team-health-acceptance.json).

Guacamole has 36 applied real server connections. Actual dashboard launch now
renders all 36 rows, all company IPs and running states, has a nonempty project
selector and survives reload without errors. Large launch grants now use URL
fragments and authentication POSTs, avoiding Tomcat's header limit. Native
Guac admin also connected to a company Linux hypervisor console and retained
its native session through console close/reload. No new guest services were
opened. See [browser evidence](guacamole-company-server-browser-acceptance.json)
and [company console](guacamole-company-console-acceptance.json).

All ten participant accounts now have exactly two intended workstation grants:
one Linux dedicated jump box and one custom Windows VM, with no broad team
grants, unchanged passwords, enabled accounts and self-service password reset
disabled. All ten saves used the actual Users page and all ten synchronization
jobs succeeded. A count bug omitted custom VM/container jump boxes from the
user list; it now uses the same classification as user details, and actual
browser checks confirm all ten rows show two assigned boxes. These are desired
grants only; effective workstation connections and desktops await deployment.
See [participant grant acceptance](participant-workstation-grants-browser-acceptance.json).

Guest account controls now have actual browser and live-guest acceptance on a
staging Debian container outside the affected HDD pool: create, password change
(old password rejected/new accepted), password-authenticated sudo root command,
revocation denying that same command, and deletion of account/home/managed
policy. The disposable user is removed. Two reproduced bugs were fixed:
automatic placement preferences are now resolved against the verified live
guest, and Grant sudo installs the missing package/policy directory on minimal
images. Ten backend regression tests pass; all successful account actions
retained controller root-key access. See
[guest-account acceptance](guest-account-acceptance.json).

The 20 participant workstations are still declared, not deployed. Parrot's
existing P6 cache169 is verified and its disposable probe deleted. Windows
base preparation `f7f7d42b839e4fc892c0569690a7c71d` hit the 1,800-second
controller wait after reading the complete archive. Its exact provider restore
for template219 is **still running** under journal
`cache-9d9d22dc04d24e349d31f75244fad7ad`; do not submit another restore or treat
the dashboard timeout as a stopped task. Once the exact task finishes OK, use
Cluster's protected cache-restore recovery to verify and publish it.

Read-only HPE diagnostics now identify a hardware fault: the Smart Array P420i
reports **Cache Status: Permanently Disabled** because its backup capacitor
failed to charge, and **Battery/Capacitor Status: Failed (Replace Batteries)**.
Both logical drives have caching disabled; all eight physical drives report OK
and the logical drives report no unrecoverable media errors. This explains the
severe parity-array write penalty observed during flush. Replace the failed
controller cache backup capacitor/power module, then verify protected cache
health. No unsafe write-cache override or controller/array configuration change
was made. The vendor utility was extracted temporarily for a read-only `show`
command and removed, without installing a package. Evidence:
[storage cache fault](proxmox6-storage-cache-fault.json).

A fresh controller-to-application SSH acceptance attempt timed out before
returning its HTTP proofs. Do not confuse previously passed service health with
current responsiveness during the storage flush. The deeper assessment and
workstation acceptance gates remain open.

Five in-character scope registers are drafted from actual endpoints, without
credentials or team terminology. Keep them draft until workstation access,
service assessment and isolation acceptance. Next: finish the Windows cache,
deploy 20 workstations, grant each assessor its two boxes, prove actual desktops
and isolation, finish assessment/reset/rollback exercises, then finalize docs
and create lab-authoring skills. Overall completion is approximately 75%; this
is an estimate, not delivery acceptance.

## Previous checkpoint — October 18, configuration recovery

Release 0.1.2 is captured. All six local release caches are independently
verified across Proxmox1, Proxmox3 and Proxmox5; disposable probes are deleted,
cache reservations released and archive pins removed. Fresh P1/P3/P5 checks of
the current isolated test LAN passed all six gates and cleanup.

The six-server test cohort is created and running across P1/P3/P5; deployment
configuration succeeded. All six original staging servers remain running.
The initial guest plan expired during the disconnected session. Stage Changes now retires an expired,
unused guest review only after unchanged-input, absent-guest and inactive-task
checks. Review job `627bb7431aac4962aa9c0d75cf9f6425` released the old six-server
reservation and produced exact plan `80cc7d6e19a2dc5e6a57e2d9d56bba71`.
Deployment job `9ef82c396c894cf4b35b4b08d46bd59a` created all six linked clones,
completed configuration and synchronized 12 Guacamole connections. Running cache reservations were
preserved. This change passes 59 targeted tests.

Acceptance attempt `6d2f8aaf4f6f4f258828963089e738ef` passed all six
archived service recipes. Its first boot order passed 13 of 15 health checks;
the two Windows ping checks failed, so the job was stopped through the Jobs API
before claiming acceptance. AD DNS records already pointed to the correct test
addresses. The platform baseline now permits ICMP echo only from each guest's
own router, and Windows checks share one bounded 600-second startup budget per
guest (Linux: 180 seconds). This does not open new TCP/UDP services.

Acceptance job `23d112ca76ae4908acb24c0b4aedf9c7` succeeded. All six
service recipes, both 15-check boot orders, and the final 15-check matrix passed
(45 checks total). The controller recorded a passing result for `p100-0.1.2` at
`releases/project-100/test-results/0.1.2-20261018T024238Z.json`; its SHA-256 was
independently read and the per-guest check multiplicities verified. See
[fresh-clone acceptance](release-0.1.2-fresh-clone-acceptance.json).

Reviewed cleanup job `2373c915e564463888c0938251e3aa2c` succeeded.
Provider verification confirms test guests 184–189 and router 177 are absent,
the six original staging servers remain running, and caches 178–183 remain
templates on their expected nodes. Guacamole has six applied server connections.
See [cleanup acceptance](release-0.1.2-test-cleanup-acceptance.json).

Release declaration `32634bfa69ea43079fa83bd8892808b0` succeeded for teams
1–5. Review job `d2f277eee7794f6ab2363523f79638b2` prepared and verified the
DB and CI caches on Proxmox6/local-hdd, then failed its 30-minute wait for the
file-server archive restore. The exact provider task subsequently finished OK;
its data-read phase took 179 seconds but total restore/template conversion took
18,683 seconds. It left stopped template185/P6. No duplicate restore was run.

Dashboard recovery job `2e82ff28df9a4ca296dddae912f93112` succeeded: the
existing FS cache is now independently verified and published, its reservation
released and archive pin removed. DB/CI/FS caches are ready on P6 (templates
177/184/185). See [P6 cache acceptance](release-0.1.2-proxmox6-cache-acceptance.json).
Kernel warnings, selected LVM/userspace logs and clock checks did not identify
the root cause of the long post-restore delay. The HDD thin pool was only 1.38%
full (metadata 0.59%); no space exhaustion is indicated.

The DC cache preparation on P6/local-lvm succeeded in 1,042 seconds overall
(job `c42ecbfa46904dce9deedeb0593c4d17`, template 186). Runtime diagnostics caught
VMA's blocked `fdatasync` thread waiting for block-device writeback after 100%
archive reading; the finalization wait must not be assumed to be template
conversion alone. P6/local-lvm maps to the non-rotational sda drive; local-hdd
maps to rotational sdb. Current writeback drained to zero; no unsafe cache or
storage settings were changed. The HDD performance cause is not fully established.

Ten custom Windows workstation jobs succeeded through the custom-jumpboxes
API. All twenty workstations are now declared (ten Linux and ten Windows),
with .11/.13 Linux and .12/.14 Windows per team. They are not deployed.
See [workstation declarations](workstation-declarations-acceptance.json).

All six release caches are now verified on P6 (DC on local-lvm; the other five
on local-hdd). Cache preparation now reuses an eligible existing pool on a node
before copying that image to another pool on the same node. If the cached pool
cannot fit the guest, normal safe fallback and cross-node balancing remain.
Three new regressions and 42 existing cache-staging/scheduling tests pass.

Selected-release review `6c40a1f6d0624071a6df99bd9669ae9d` succeeded with zero
new cache fills. Protected plan `0d1d2fecd2c86b84ca3dab24b5fcf4c2` was inspected:
exactly 30 linked-clone creates, six company servers per team, all on P6; no
updates, replacements or deletions. It excludes originals and the twenty
workstation declarations. Actual Chromium Apply reviewed release submission
started job `83754c92f74c44d3b6fc6096806ccb25`. All 30 servers were created and independently observed running; actual root-volume origins confirm linked backing. The job subsequently failed in configuration, not infrastructure creation.
The centered job output opened and was closed without cancelling the job.
Team configuration and health acceptance are still pending.

The configuration failure was caused by targeted Terraform outputs retaining
undeployed Windows workstation entries with `id: null`. Inventory generation
and Guacamole now exclude guests without an allocated numeric identity; real
identity mismatches still fail closed. Guacamole refresh
`b9dac5d4da124840bab762590aa38e93` succeeded with exactly 36 applied server
connections (six staging plus 30 company servers), excluding all 20 undeployed
workstations. Participant accounts still have no workstation grants.

Added **Continue configuration** to the rollout table. It verifies all selected
applied guest identities, ownership, networks and pinned service recipes before
configuring existing machines, without Terraform apply. Health acceptance is
checkpointed per company. Seven recovery tests pass; the previous checkpoint
also passed 94 targeted inventory, credential, job, scope and CLI tests.
Actual Chromium submitted this action for teams 1–5 as job
`4c1bf68bab4d4fcabad30a5e94206f06`; preflight passed, but Windows route
cleanup exposed an Ansible strict-boolean conditional bug on a blank saved
line. The condition now uses explicit boolean expressions. Actual installed
Ansible passed three cases (blank skipped, current retained, obsolete selected)
and the playbook syntax check. Retry `7ec764af704a4db4a7e44f5ac8d4a202` was
submitted through the dashboard and is running; it is not yet an acceptance result. See
[configuration recovery browser evidence](release-0.1.2-five-team-resume-browser-acceptance.json),
[infrastructure identities](release-0.1.2-five-team-infrastructure-acceptance.json)
and [linked-volume origins](release-0.1.2-five-team-linked-backing-acceptance.json).

Actual dashboard-to-Guacamole browser acceptance now covers all 36 real server
rows, all 30 company IP addresses, running power state, a nonempty project
selector, no error banner and successful reload. The first live check exposed
Tomcat's 8 KiB request-header limit: the encrypted grant set was in the launch
URL's ordinary query. Launch and console tickets now use client-side route
fragments, so Guacamole receives credentials in its authentication POST body.
The change passed 17 Python tests and both JavaScript session tests. The ticket
broker was restarted only after confirming no active broker connections;
configuration worker remained running. See
[Guacamole company browser acceptance](guacamole-company-server-browser-acceptance.json).

Five draft in-character scope registers now list the actual deployed targets,
web URLs, internal DNS reference and workstation allocation policy. They
contain no credentials and remain marked draft until service/workstation and
isolation acceptance. See [scope handover](scope-registers/README.md).

The existing Parrot P6 restore (cache169) has an exact successful provider
restore task. Dashboard recovery `36db335306804229bb028baec32ac3a3` succeeded: its existing restore was verified with a disposable clone, which
was deleted, and its reservation released. No additional archive restore ran. Windows workstation
base preparation `f7f7d42b839e4fc892c0569690a7c71d` is queued behind company
configuration; the earlier queued job `56cdd1735d964caea7381fead177f623` was
cancelled before starting solely to change that order. Read-only placement
preview confirms these two P6 caches are the remaining image preparations for
20 workstations and includes their full guest growth in capacity checks.

Five participant deployments, deployment of the 20 declared assessor workstations, Guacamole
grants/desktops, isolation and reset/rollback acceptance remain. Agent
lab-building skills come after delivery acceptance. Overall completion is
roughly 70%, an estimate rather than an acceptance result.

Evidence: [six caches](release-0.1.2-cache-acceptance.json),
[P3 network](release-0.1.2-test-network-proxmox3-acceptance.json),
[P5 network](release-0.1.2-test-network-proxmox5-acceptance.json). Earlier entries
below are historical; this checkpoint supersedes their running-job statuses.

## Placement accounting

Powered-off VMs and containers no longer consume CPU/RAM commitments for
automatic placement or project quotas. Their disks still count. Running and
unknown-status guests, pending boot reservations, actual host memory use and
host reserves remain accounted for. No live guest was resized or stopped.

## Verified staging

- Four Linux containers provide database, application, CI and backup services.
  Seven declared Linux checks have passed.
- Windows VM128 (MHL-DC01) and VM129 (MHL-FS01) run on Proxmox1.
  The mhl.test forest, company accounts, warehouse GPO delegation, domain
  membership, business shares, scheduled dispatch consumer and intranet exist.
- AD DNS/LDAP/SMB and file-server SMB/HTTP checks have passed.
- Guacamole admin console access is deployed for both Windows VMs. Live tests
  used the native Guacamole management endpoint, encrypted JSON authentication,
  and Guacamole HTTP tunnels; both returned screen and rendering instructions.
  This is hypervisor console access: no guest listening ports were added.
  Authenticated guest RDP acceptance now passes for FS01, including an active
  Windows desktop session. DC01 authenticates but denies desktop logon with
  Windows status `0xc000015b` (missing logon right); this is a
  diagnostic finding rather than a participant desktop requirement. Admin
  hypervisor console access remains the server management path. Evidence: [native RDP acceptance](staging-rdp-acceptance.json).
- Company packet Markdown, HTML and PDF are in character, revision 0.2.

## Current dashboard and remote access

October 16 dashboard-launch follow-up: the actual **Open Guacamole** link and
reload now pass for dashboard `admin`, which has no separate native Guacamole
account. Ticket sessions no longer attempt that optional database verification;
native logins keep their verification. See
`guacamole-dashboard-launch-acceptance.json`.

October 16 follow-up: fixed temporary console logins replacing the browser's
normal Guacamole session. Ticket tokens now stay in their own tab; database
protocol links remain available across authenticated contexts. Hidden keyboard
and clipboard textareas no longer inherit visible field sizes. Admin console
access now includes LXC containers as well as VMs, through the existing Proxmox
VNC relay; no guest ports or firewall rules were changed. Live browser acceptance
and console coverage are recorded in `guacamole-ticket-session-acceptance.json`
and `guacamole-console-coverage-acceptance.json`.


Compact controller and Guacamole interfaces are deployed. Live Guacamole checks
show exactly six staging servers with declared IPs, and the DC power state
matches its authenticated status response and host observation during capture.
Native password login and dashboard launch/reload both pass; the launch-auth
cleanup remains installed. This verifies management access and interface
behavior, not the pending participant desktop/grant acceptance.
[Live interface evidence](guacamole-compact-interface-acceptance.json).

## Accepted immutable captures

- APP acceptance release 0.0.1: CT130/P2, project-scoped image
  p100-image-2f571d3a89d3. Central archive checksum, fresh linked-clone backing,
  unique identity, readiness and cleanup passed.
- DC acceptance release 0.0.2: VM131/P5, project-scoped image
  p100-image-52230deabb41. Preserved domain-controller identity, verified central
  archive, independent clone verification and cleanup passed. Dashboard job
  0609f23d8ab748ef86a6bfe5fba6cc70 succeeded and restarted staging VM128.
- Complete release 0.1.0: DC VM132/P5, FS VM133/P5, APP CT134/P2,
  BKP CT135/P2, CI CT136/P2 and DB CT137/P3. All six immutable images passed
  independent clone identity, readiness, backing and cleanup checks. Dashboard
  capture job `06d1a19051ab4b96858d09b2a64764ff` succeeded and published
  `p100-0.1.0`. Original staging guests were restarted.
- Capture acceptance is complete; fresh full-release service replay, deployment
  to participant environments and delivery acceptance remain outstanding.

## Current task

Proxmox6 enrollment and HDD integration are complete. All six Project100 LAN
checks passed; VM and CT linked-clone caches are verified on local-lvm and the
new 4,016 GiB local-hdd thin pool. Read-only automatic placement selected the HDD
pool for both guest types; temporary reservations were released. See
[enrollment evidence](proxmox6-enrollment.md).

Release **0.1.1 is captured and published**. Job
`d716569816bd4fb5a02772dcfcf43a17` succeeded with all six images, verified
fresh clones and cleanup. Its manifest SHA-256 is
`71c52f41e49e7ccdde49e43ffad300295511209f7cfe1d3a2b152bf8d184f1ea`.
Live inventory confirms all six original staging servers are running.
[Capture and recipe evidence](release-0.1.1-capture-acceptance.json).

The recipe-preservation change is deployed after 75 focused and regression
tests passed. New captures preserve hash-checked copies of fixed service recipes;
fresh-test preparation preserves legacy recipes while their source still
matches. Release testing and rollout use each release's recipe fingerprint,
allowing older releases to remain reproducible after playbook updates. Capture
still rejects staging configured with an outdated recipe. Both 0.1.1 recipe
archives were verified against their manifest hashes (13 business files and two
Windows playbooks). Domain-controller RDP policy remains unchanged; participant
desktop acceptance targets jump boxes.

Authenticated dashboard job `a3372f8b809248e49f2a5f456bd103be` declared two
VMs, four containers and one isolated test LAN for 0.1.1. Stage Changes
`c3f358d849414bf0b33cb5738df661fe` produced reviewed foundation plan
`d9943ca7aab046a23934cb5b2a9740d5`; deploy/configure job
`2f72d73dbfd249e2b0d5c4fc164e91e8` succeeded. Fresh `p100c100` checks are
queued/running for all six nodes before test guest placement.
The first custom-LAN probes exposed a recreated-router SSH identity bug. It is
fixed and deployed; strict SSH to both parent/custom routers now passes. All
three uncertain probe cleanups reconciled with guests/rules/state absent and
reservations released. The fresh Proxmox1 check
`00412548038c4dd39057a85ac89d606d` passed all six checks, cleaned up, and
published acceptance. Fresh checks also passed and published on Proxmox2,
Proxmox3 and Proxmox5; all four confirmed complete cleanup. Proxmox4 was rejected
before allocation because it lacks an approved ready Linux probe template.
Proxmox6 was rejected before reservation by the cross-VXLAN guard because it
hosts this custom LAN's router; this does not invalidate its previously passed
standard team/staging LAN receipts. No acceptance was fabricated for those two.
[Exact test LAN evidence](release-0.1.1-test-network-acceptance.json).
Stage Changes job `0541ce4bfa8940dc997dc3a0348c5692` was interrupted before
test guest deployment. Its VM162 cache restore subsequently completed OK, but
dashboard recovery `609d1dcbced24ccbb72ec633535ef1a3` subsequently verified
and published template 162 on Proxmox5, deleted its disposable probe and released
the reservation/pin. [Recovery evidence](cache-restore-recovery-acceptance.json).
The obsolete
0.1.1 test cohort declarations were removed through dashboard cleanup; review
`784d3a9dc93ed0f0fef807f390252f60` removes only its disposable router/VNet
and updates SDN bookkeeping. Dashboard deploy `ed8f52ccf40e4613bbcfe7775908a7a3`
succeeded: both were removed and Team 0 configuration plus Guacamole refresh
completed. All six original
staging servers were verified running after the deletion.

Staging application checks prove the intended cross-customer read and report
query weaknesses. Fixed the CI symlink ownership bug through the reviewed role;
business configuration `6f388022951145fc88f279cd87ad817f` succeeded, and its
artifact now returns HTTP 200. [Read-only evidence](staging-application-acceptance.json).
Staging therefore has a newer business recipe than immutable 0.1.1; after its
obsolete fresh test cohort is cleaned, capture a new complete release.
The four Linux roles now accept worker-verified isolated test scope (21 focused
tests passed). Staging business configuration succeeded with the corrected
recipe, and CI now has a declared health gate for the actual artifact URL. Do not
silently alter 0.1.1's images, manifest or archived playbooks.

Release **0.1.2 is captured and published**. Job
`a096e2912cca4949bd13b794c8852f07` completed all six immutable captures.
Manifest SHA-256:
`c6ed914f71ead90a6bfa63419edb8cef8710d9bd519fa1cd5cef5fab53dadfcd`.
Its isolated fresh-test router/network is deployed. Exact P1 and P5 network
checks passed all six gates and cleanup. DB, CI and file-server local caches
are verified; the domain-controller cache is undergoing retained-probe recovery.
The six fresh test servers have not yet been deployed.
[Capture evidence](release-0.1.2-capture-acceptance.json).

Fresh whole-release service/boot-order acceptance has not yet passed.

Overall lab delivery remains approximately 65% complete. Full fresh-release
service/boot-order testing, five participant deployments, 20 workstations,
Guacamole desktop/grant checks, isolation and reset/rollback acceptance remain.
The infrastructure enrollment is complete; the lab is not yet delivery-ready.

The completed 0.1.0 trial below is historical: its six test guests and custom
router were removed through the reviewed cleanup deployment. They are not
currentlydeployed. Original staging guests were preserved. Recreate a fresh
isolated test cohort from 0.1.1 after capture succeeds.

The reviewed network-foundation plan `b06ace3e8802ab89b198d3a239e6ed9d`
created custom router CT138 on Proxmox2 with a 4 GB boot disk, plus its VNet.
Scoped recovery `510aa8bde41749cf86f6d251a48becee` verified the applied
identity and configured routes, firewall, DHCP and IPv6 isolation without
reapplying Terraform. The initial SSH failure was a guest logind namespace
failure; read-only inspection confirmed Proxmox2's static IP was still present.

Exact network acceptance passed on Proxmox5 (`6242e52a936f46b89fb6d1bea3c2ed62`)
and Proxmox1 (`ce23ff2351b849968d5c33651960ebd4`). Both passed controller,
DNS, Guacamole, internet, isolation and VXLAN MTU checks. Disposable probe VMs,
parent/custom-router temporary permissions and reservations were cleaned up;
network receipts were automatically published. Other nodes still require this
exact LAN's acceptance before they can host its guests.

Cache preparation now accounts for serial probe and guest capacity peaks.
An administrator dashboard job reduced Proxmox1's host RAM reserve to 2 GB or
12.5% (whichever is larger); CPU/RAM/disk commitment ratios were preserved.
The next Stage Changes attempt failed safely on Proxmox5 disk headroom.

The dashboard's reviewed storage workflow has now created a dedicated 512 GB
thin pool (`crange-thin`) in Proxmox5's previously unused HDD VG. Apply job
`2782f84fbebe4020bf5a3c5f9038c505` succeeded. Native snapshot COW and deletion
of both disposable 64 MB proof volumes passed. The controller and token have
their existing datastore role on this new storage only. Automatic placement
policy approves it; the SSD-backed library, existing disks and commitment limits
were preserved. Nine storage safety/recovery tests passed.

Stage Changes job `3871bdceaf1a4982841b3cce6f361ff6` confirmed the new storage
is eligible, then stopped before cache transfer or guest creation: its second
Windows clone exceeded Proxmox5's observed-memory admission headroom after queued
guests were included. Comparison with the unchanged ledger then found preview
was subtracting nominal queued RAM twice under the 2:1 policy. This accounting
bug is fixed and regression-tested: preview and ledger agree on the same budget
and both reject excess demand. Stage Changes is being retried with unchanged
host reserves and ratios. Fresh full-release service replay and startup/health
testing remain outstanding.
Cache-worker, catalog, cache lifecycle and physical container-backing checks now
accept the explicitly verified local thin pool. Service replay for the exact
six-guest test cohort is implemented and unit checked; live acceptance is pending.
A failed pre-allocation cache admission retained a running reservation with no
journal. Automatic recovery now checks it under the same worker locks and
preserves every journal-backed/published operation. Stage Changes job
`0fc5de177abc4d8eaaf4d7bca5134881` released the exact old reservation and
started DC cache restore VM139 on Proxmox5/crange-thin. The owned qmrestore task
completed successfully, and linked disposable probe VM140 was created. The
Stage Changes job then failed on an older UUID-only Windows ownership check.
Compact probe markers are now bound to the exact journal job and VMID, with the
root relay verifying that hidden job description and pool membership. Existing
Recovery job `58a4c82ce38d4cf98944df237d6e97d8` verified the existing Windows
snapshot probe, deleted VM140, published DC cache VM139 on crange-thin and
released its reservation. No second restore or clone was submitted. The source-IP
and declared-versus-applied image checks for service replay are alsodeployed.
The next container cache exposed missing container restore/linked-clone capability
receipts: the existing policy only records VM paths. An administrator Cluster
control now runs isolated container acceptance, with fixed image/node/storage
inputs and inferred project ownership. It publishes capabilities only after real
restore, physical linked backing, unique identity/readiness and owned-probe
cleanup. Live container acceptance is the active step. The initial preflight failures
exposed an obsolete provider flag and a flat-policy bridge lookup; both are
corrected. Cluster refresh `d56feaac1e5d41fab417dbf203983df3` succeeded. Acceptance
job `acf9964420384556883d2bc2b3ea335a` restored CT140 successfully, then
failed because the archive already restored a template and the worker attempted
template conversion again. That redundant conversion is fixed. Dashboard job
`c8ab0c6d7a0c432ebcda7cb0f6da5d96` verified the exact retained CT140,
its completed restore task, immutable archive pin and reservation before resuming
clone verification. It created linked probe CT141, then exposed the cache
verifier’s obsolete static relay allowlist. The deployed relay and verifier now
bind compact markers to exact VMIDs and hidden job descriptions, retain source
pool and disconnected-network checks, and reject foreign ownership. Job
`d8482e3fdb6c4c7ab7a340391484ab13` succeeded: resumed CT141 without a
second restore or clone, verified physical linked backing and independent
identity/readiness, deleted the probe, and published cache CT140. Container
restore/linked-clone capabilities are now backed by the live receipt on
Proxmox5/crange-thin. Stage Changes job
`6720b03cc29f46bb982e62d24edddee6` completed all four cache fills: backup,
Windows file server, CI and database passed identity/readiness and cleanup.
Terraform then rejected semantic-version dots in generated release-test keys.
The VM/CT placement-key validator is corrected in Project100 and the future
project template; real Terraform tests accept versioned keys and reject invalid
router/path keys. Retry `935165c1362f4f928790c31f3daca3c9` reuses existing
caches and succeeded. Saved plan `b8cdc6dfba690dc2fbeeebe6d31a46f3` was
inspected through actual Terraform JSON and digest verification: exactly six
creates, zero updates/deletes/replacements, no staging blockers. Five guests
use P5/crange-thin; CI uses P1/local-lvm. Dashboard Stage Deploy job
`e7c646b8aacd45db9b153fc4eb234ee3` succeeded with destruction/replacement
disabled: all six fresh guests were created and configured. Windows VMs151/152
run on P5; authenticated Windows management and source-restricted RDP/WinRM
configuration passed. Guacamole refreshed 12 declared connections; participant
grants remain pending. Full-release service replay and startup/health check job
`506b53c54b8941e28aa4e14e1cb35564` failed before service replay because
the test used declaration node `auto` as a Proxmox host. Release test and deployed
release health checks now resolve physical nodes fromthe current cluster
VMID/type inventory, rejecting missing/ambiguous guests and retaining identity
checks. Fifteen rollout regressions passed. Retry
`7955ae0db3e047bb96ce423b3f49e88d` passed guest resolution and reached
Windows service replay, then failed its safety preflight before guest writes.
The domain playbook only permits network IDs 1–5/254 and therefore rejects
isolated test LAN100. Next: allow only worker-validated test scopes, configure
the corrected recipe in staging and capture a new release. The old release
manifest and its hashed service recipe remain immutable. The corrected guard is
deployed: worker-selected test scopes receive a typed flag and the playbook
requires the exact scoped DC address. Ordinary calls cannot select arbitrary
custom networks. Eleven service-selector and three playbook tests passed.
Dashboard staging recipe update `8c595c9cf9654b2bb440588a10637648` succeeded.
Six real Ansible preflight acceptance/rejection cases passed. Capture requires
cleanup of the previous disposable cohort: desired-state cleanup
`e41c5844bfb94b8297cf83b768e5e907` succeeded; cleanup plan job
`697d7b3c416f4f75a8c54a2cac4589d6` succeeded. Plan
`60d1d7d7bd269c529c55586fc56d6c28` was inspected: six disposable guests,
test router138 and VNet p100c100 removed, plus the SDN apply trigger/revision;
original staging guests and ordinary routers unchanged. Cleanup deployment
`2bf7fde9b84b42d58336dd725c17052a` succeeded. A live cluster read confirmed
CT138 and all six disposable guest IDs absent; all original staging servers
124–129 remain running. Dashboard capture `42ebeb87cd234afdbf62352d6a853fc2` failed before
allocation: the shared archive had 137.8 GiB free, enough for a full 70.4 GiB
backup but not its additional 20% reserve. The source DC was restarted. Shared
backup archives now reserve 10% with a 20 GiB minimum, still requiring space
for the whole uncompressed image before admission. Host and guest-disk reserves
are unchanged; twelve headroom/capture tests passed. Retry
`b040f2d9687f4dd283133080e9f11834` captured the transport archive and
restored the DC copy, then failed on guest-agent collection. See the recovery
section below for the active retry of release 0.1.1. Independent clone verification, archive checksum and cleanup remain
required for every captured image. No fresh-release
service pass or participant rollout is claimed.

Historical troubleshooting evidence is retained in platform-progress.md.

## Fixes verified in this continuation

Guacamole power/console actions resolve the applied guest ID against current
cluster resources, rather than treating declared node `auto` as a hostname.
They verify guest name, template flag, declared subnet and attached project
network before operation. This also handles a guest moved between nodes.
Resolver, console and bridge unit checks passed. Capture-network ordering
checks verify disconnected ownership is established before host proof and
failed proof cannot enable carrier. Native admin console launches
and real screen transport passed for both deployed Windows VMs.

## Remaining acceptance work

- Complete fresh-release service replay and health/startup testing.
- Validate the deployed ownership-checked clone-networking/credential path for
  service-preserving Windows domain snapshots. It derives IP/DNS, preserves
  accounts, freezes source credentials privately, authenticates management
  credentials and journals one QGA PID. Workgroup initialization remains separate.
  Verified distributed Windows image replicas are accepted by logical identity;
  foreign, unverified or mismatched replicas are rejected.
- Explicitly deploy the tested release to five isolated environments.
- Deploy two Linux and two Windows assessor workstations per environment;
  apply account grants and verify desktop access, isolation and reset/rollback.
- Verify assessment paths and finish private operator documentation.
- After full acceptance, create reusable agent skills for the verified GUI
  design/build/release/deployment/recovery/cleanup workflows, without secrets.

Future routers created from stock OS archives default to 4 GB. Existing disks
are pinned from Terraform state; CPU/RAM edits cannot shrink them. Captured
router filesystems retain their source allocation. No live router was resized.
The measured project router uses 683 MB of its 8 GB thin disk.

The lab is not yet ready for delivery. No subagents are active for this work.

## Dashboard readiness reload

During release 0.1.1 capture, authenticated `/api/projects` returned no template
readiness. Broker logs showed the pre-thin-pool catalog validator still loaded
(`local cache requires owned local-lvm generation`). Reloading only
crange-guac-broker restored ready status for Windows Desktop Experience and
Parrot bases;the capture worker PID was unchanged. Source deployment now
reloads the broker with workers/web when no jobs are active, so imported Python
validators cannot remain stale after subsequent deployments.

## Capture recovery after interruption

Capture `b040f2d9687f4dd283133080e9f11834` completed the DC archive checksum
and restore to VM138/P5, then failed during a guest-agent identity command.
The source DC was restarted. Dashboard inspection
`ad2ed63286934acba9dede03dbf6833a` succeeded: healthy mhl.test directory,
NTDS/QGA/WinRM/RDP running and clean boot/shutdown; DNS was still starting.
The retained copy was stopped after inspection.

Recovery now verifies exact ownership, isolation, completed provider/diagnostic
tasks and clean inspection evidence, then reruns identity collection on the
retained copy. It does not approve an image from diagnostic evidence alone.
Windows cold-boot collection gets a bounded 600-second wait. Nineteen recovery,
transport and inspection tests passed. Dashboard retry
`257829dfc547469ab459daa997a7ebc5` reused VM138 successfully, collected its
identity, converted it to a template and began final immutable export. A status
poll returned HTTP596; the job failed but the exact provider backup was confirmed
still running. GET595/596 and task observation now retry within their deadlines;
mutating requests never retry. Pending-export recovery verifies the template,
identity, reservation and completed provider history before attaching to the
existing export, then hashes and publishes it. Thirty-three identity/export/
transport/API/checkpoint tests passed. Dashboard retry
`abee9567f5ab49dcb9189f25e68b4dc1` is active; no duplicate transport, restore
or final backup is submitted.

## Shared library pressure and recovery

Pending-export retry abee9567f5ab49dcb9189f25e68b4dc1 stopped at its strict
checkpoint check; no backup was resubmitted. Provider reads subsequently
stalled. Proxmox5 remained reachable with its 62-day uptime, about 10 GiB
available RAM and no CPU pressure, but near 99% I/O pressure and load 143.
Kernel diagnostics showed blocked NFS-Ganesha threads, and CT145 had accumulated
millions of memory-high/reclaim events under its 768 MiB/no-swap limit.

The verified library address is 10.0.0.249, as recorded in ops/media-library;
the older 10.60.0.30 note was stale. Dashboard job
7f1bd118a7344a6db32ede910036f60b increased only this verified container online
to 2 GiB RAM, 2 GiB swap and 2 cores. Live cgroup limits match; no reboot occurred.
Pressure remained high, so a graceful NFS-only recovery action was added. The
share also backs a Proxmox Datacenter Manager disk; no VM, container or host
reboot/force-stop is authorized by this recovery action.

Job 601c655570bb43c29e050a4ee72f1725 did not complete service recovery.
NFS remains deactivating/stop-sigkill with Result=timeout and a pending root
journal. No duplicate restart was sent. A Proxmox5 maintenance reboot may be
required; it needs authorization because OpenMediaVault VM103 would be affected.
The concrete impact and recovery plan are in proxmox5-library-recovery.md. Export task outcome
and archive checksum still require confirmation before capture can continue.


### Approved maintenance: clean-shutdown prerequisite failed

The user approved the Proxmox5 maintenance plan. Read-only checks confirmed
OpenMediaVault VM103 is still running but guest SSH and QMP are unresponsive.
A single bounded graceful shutdown request with force-stop disabled did not
stop it. No host reboot occurred; the original boot ID remains unchanged.
The recovery plan explicitly requires reassessment at this point. Separate
force-reboot authorization is pending, as OpenMediaVault may have pending
disk writes. Release 0.1.1 capture remains paused; its existing VM138 and
exact provider export task are retained, without duplicate backup submission.

Capture checkpoint diagnostics now identify failed checks (such as stopped
state or export guest identity) without exposing configuration or credentials.
All existing rejection conditions remain enforced. Six focused tests passed,
including rejection of running guests and mismatched export VMIDs. The reviewed
single-module change was deployed with no queued/running dashboard jobs;
controller web, workers and Guacamole broker returned active. No capture retry
or backup submission was made while storage recovery remains pending.


### Storage recovery and capture resumption

P5 completed the externally requested reboot. New boot ID
`3474395a-c405-4d19-a0bd-f8ed01116732`; all five votes present, NFS and
local storage active, OMV103 and routers116/117 running. NFS-Ganesha is active
and host memory pressure is zero. VM138 remains the stopped identity-verified
immutable capture. Old export is confirmed stopped with `unexpected status`;
its unregistered 8.5 GB `.vma.dat` and temporary directory are retained.

The existing dashboard capture workflow now reconciles this specific failure:
root relay verifies exact failed UPID, preboot start time, stopped immutable
guest, owned tags/pool, unchanged config and no active backup for that guest
before clearing only its stale backup lock. Failed export journal is archived
with recovery evidence; partial files are preserved. Successful or uncertain
tasks, registered archives, changed/running/foreign guests are rejected.
Seventeen source/export, thirty relay, three recovery (multiple denial cases),
four library and one capture-checkpoint tests passed. Relay source/pin
`de1cdd303342fa5f91ada49799cb67993716febdf70924ba32339f3bc1beb6e3`.
Authenticated dashboard release-capture job
`8f23b4e7fb8b46e6a9a3668506320630` resumes 0.1.1 from VM138, not a new
source restore. Final acceptance remains pending; no release success claimed.

Live recovery succeeded: old backup task is terminal failed; its partial files
and failed journal are preserved. New export
`UPID:proxmox5:00012932:000C00D9:6AD01F15:vzdump:138:crange-iac@pve!terraform:`
finished `OK`, producing the 16,706,368,427-byte archive
`crange-library:backup/vzdump-qemu-138-2026_10_14-20_32_31.vma.zst`.
The source journal records exported/verified/complete_capture and a real
checksum. Capture receipt is now captured. Fresh full-copy acceptance VM20000
is being created by the confirmed running task
`UPID:proxmox5:00013C23:000CA6E5:6AD020BE:qmclone:138:crange-iac@pve!terraform:`;
last native log observed 12.8 GiB/64 GiB (20.02%). This is active disk-copy
work, not an unstarted job. Release capture remains running; fresh clone
acceptance, the other five captures and full release testing are unfinished.

Prepared (not yet deployed while capture is active): clone-copy progress in
Proxmox task observation, using only bounded numeric transfer lines. Twelve
API observation tests pass, including clone progress/privacy, optional log
failures, exact-task retry and no mutation replay. Deploy after active jobs
finish so the running worker is not restarted. Source: scripts/crange.py;
regressions: tests/test_proxmox_api_retry.py.

The exact clone task remains running and has advanced to 27.5 GiB/64 GiB
(43.04%). Additional prepared recovery change (not deployed during capture):
resume_exported_capture now selects source-probe-resume when the exact probe
journal already exists, and verifies completed probe evidence without allocating
another clone. Existing reservation/task/ownership checks remain in the resume
implementation. Expanded checkpoint regression and five transport-retirement
tests pass. Pending source deployment batch: scripts/crange.py (progress) and
scripts/crange_capture.py (retained-clone resume). Do not restart worker until
its active job is terminal; a provider timeout alone is not task completion.

DC 0.1.1 fresh-clone acceptance passed live. VM20000 was created by the exact
clone task, which finished `OK`; its verification record is source-probed and
passed/identity_unique. Evidence preserves MHL-DC01/mhl.test/domain-role 5,
service identity, Windows snapshot identity mode, QGA, automatic/running
WinRM and RDP and VM Generation ID checks. Clone start, stop and deletion
tasks are all confirmed terminal. The full capture job remains active while
verified transfer-backup retirement finishes; the other five captures and
full release test/participant rollout are not yet accepted.

DC capture finalization completed: verified transfer backup retired by exact
imgdel task, immutable replacement archive retained, staging VM128 restarted.
The same running dashboard job has advanced to stopping/capturing file-server
VM129. No new release-capture job was submitted and no additional DC copy was
created. Current task: file-server capture, followed by the four Linux captures
and full 0.1.1 release testing.

Prepared worker fix: release-capture receives a bounded twelve-hour deadline
instead of the generic one-hour limit. Other ordinary jobs keep one hour and
network workflows retain their existing two hours. Twenty-four worker tests
pass, including actual queued capture vs ordinary-job dispatch. This does not
change the deadline already loaded by running job 8f23b4e7; deploy after it is
terminal, and reconcile exact provider tasks before retry if it times out.
Pending deployment now includes scripts/crange_jobs.py, scripts/crange.py and
scripts/crange_capture.py. Operator workflow is consolidated in
[OPERATOR-RUNBOOK.md](OPERATOR-RUNBOOK.md), with acceptance gates explicitly
distinguished from remaining work.

Job 8f23b4e7fb8b46e6a9a3668506320630 reached the old one-hour controller
deadline while restoring FS capture VM151. Provider restore task
`UPID:proxmox5:0001A186:0010867A:6AD02AA9:qmrestore:151:crange-iac@pve!terraform:`
continues independently (77% observed); it was not failed, cancelled or
resubmitted. Transport backup is verified and its task confirmed terminal.
The reviewed three-module batch is now deployed: twelve-hour release-capture
deadline, numeric clone progress and retained-clone checkpoint routing. All
controller web/worker/network-worker/broker services are active. Dashboard
machine-state job b3492a9ea9bc4c899a329c0891c5bc69 restarted original FS VM129
successfully after its frozen transport backup completed. Wait for exact restore
completion before resuming the same release; reuse VM151, not another restore.

Browser acceptance evidence now covers Users creation with integer project
access, saved assignments, invalid-input and duplicate errors, and deletion;
Jobs page size/navigation, legible task/status and expanded output surviving a
confirmed re-render; active task spinner and persistence after reload; and a
valid nonblank project selection. API account acceptance separately verifies
manager scope, denial of cluster/foreign-project access and deletion/session
revocation. Temporary accounts were removed. Evidence files are the
`dashboard-*-acceptance.json` files in this directory; untested feature gates
remain marked pending in FEATURES.md.

FS recovery is confirmed live: retained VM151 identity is MHL-FS01, mhl.test,
domain role 3, windows-ready, with QGA/WinRM/RDP running. Its exact shutdown
and qmtemplate tasks finished OK; immutable export is progressing (4% observed).
The original transport backup and restore task IDs were reused, with no second
source backup or restored allocation. Fresh-clone acceptance for FS and the four
Linux captures still remain. Current running job: f3a5c66716154d95b30c5503ca10b380.

Additional browser acceptance passed using an owned queued inventory fixture
(job 1b8816730b1840c9bc7a1c2581e5fe73): clicking Generate inventory opened the
centered output dialog; closing it left the job queued; clicking Stop task
cancelled it and displayed a notice. Capture remained running. A separate
read-only invocation of the existing dialog renderer for the real capture job
verified that a real output update preserves reader scroll and keeps the popup
open; closing it does not cancel capture. This renderer test is not claimed as
a separate UI launch button test; the clicked inventory action covers launch.
No running provider task was stopped during these checks.

Cluster browser audit discovered a per-guest display discrepancy: node placement
already excluded stopped guests, but snapshot guest rows zeroed templates only.
The display builder now excludes explicitly stopped guests too, preserving
configured resources, disk commitments and conservative running/unknown/paused
accounting. Five cluster tests passed. Deployed atomically without service
restart; active capture worker PID unchanged. After scheduled collection, browser
acceptance passed for all five nodes, GB capacity/commitment columns, guest
distribution and 41 non-compute guest rows.

Capture f3a5c66716154d95b30c5503ca10b380 exported FS successfully, then failed
safely at source-probe reservation because its preferred SSD datastore lacked
full-copy disk headroom. No probe allocation or second source copy was made.
Automatic local full-VM probe storage fallback now runs each candidate through
the same atomic ledger checks. Shared/unapproved stores, linked copies, CPU/RAM
pressure, quotas and duplicate allocations remain rejected. Three real-ledger
fallback tests (multiple denial cases), 27 placement tests, 17 identity/export
tests and the expanded capture-checkpoint test passed. Deployed two-module fix
then submitted 9401509de97b4c5cb92b9308889a8ecd through authenticated /api/jobs.
It reused DC and FS checkpoints without a new source backup, restore or archive.

## October 17: reusable base templates and capture checkpoint

Release **0.1.2 capture succeeded** as dashboard job
`a096e2912cca4949bd13b794c8852f07`: all six immutable images passed
fresh-clone source verification, transfer backups were retired after verification,
and the original staging sources were restarted. Complete-release fresh-cohort
acceptance is a separate gate: `release-test-prepare` job
`7b77d103fc0248f3b4644e8619b0b1fb` is queued; no whole-lab completion is claimed.

Creation selectors now exclude captured release images, remove node-readiness
suffixes, collapse equivalent aliases, and distinguish VM/container labels.
Release operations and existing guests retain their historical references.
The maintained-base admin dialog is deployed and its actual browser controls
pass without JavaScript errors. The broker also required a refresh because it
serves Templates metadata independently of the web process.

Five fixed Linux-base publication jobs are queued/running through the same
authenticated dashboard API as the GUI. They capture isolated derivatives,
verify archives and independent clones, then prepare local caches and link
versioned aliases into projects and future-project defaults. Old live/template
references are preserved. Tests cover rejection of wrong/live sources, incomplete
clone evidence, container approval isolation, and source-only P4 admission;
131 relevant tests passed. Original templates are never booted or changed.

**All-node readiness remains incomplete.** Parrot preparation passed on P5;
P2/P3 correctly rejected its current 6000 MB probe, and a timed-out P6 restore
actually finished successfully. Exact recovery is queued, without another copy.
P4 remains excluded from global placement pending verified enrollment.
Older-node Windows compatibility also requires real clone acceptance.
See `base-template-distribution-acceptance.json` for job IDs and browser evidence.

### Follow-up deployment and remaining acceptance (October 17)

The worker's old task API response uses `job_id`, not `id`; the deployment
follow-up was repaired and its queued job IDs are retained in the acceptance
JSON. The original worker was refreshed only after becoming idle. Subsequent
code deployments left the active worker and its child processes running.

Three independent backend issues were reproduced and corrected:

- Remote restores of base templates preserve the immutable template flag.
  Publication version 3 restores a separate, never-booted transport template,
  full-clones its writable derivative, verifies separate owned disks, and
  deletes only that temporary transport template after confirmed clone success.
  Capacity reserves both copies. Original bases remain stopped and unchanged.
  P4's measured capacity rejected local builds; the private source policy keeps
  P4 drained for allocation and uses admitted remote capacity, preferring the
  approved P6 HDD/P5 thin pools for build copies. It never fabricates P4
  Terraform/container/network enrollment or snapshot evidence.
- PVE container package 6.0.9 rejects Debian `13.6`. Fixed node support jobs
  installed only the reviewed qemu-server 9.0.18 / pve-container 6.0.10 fixes
  on P2/P3/P6 as needed. All unrelated package versions were checked unchanged;
  no guests rebooted. P5's IPv6 download failed; an IPv4-only download retry is
  queued. A wider 6.0.18 transaction that would remove pve-manager was rejected.
- Cache recovery treated a read-only shared ISO as a writable disk. It now
  permits only read-only ISOs in the approved shared media stores; writable
  disks and generated cloud-init volumes still require exact local VMID
  ownership. The same retained P6 restore has a corrected verification job;
  no second restore was submitted.

Cache preparation can now test a 2 GB / at most 2-core Linux verification
clone instead of demanding the original Parrot template's 6 GB. The smaller
profile is proposed only: no catalog evidence is asserted until the live clone
passes readiness, independent identity and cleanup. Failures retain their
resources for exact reconciliation. Normal deployed guest sizes are unchanged.
The P2 retry is executing this path; live acceptance is still pending.

Current verification: 233 relevant tests pass. The actual Templates dialog
passes its browser check with five fixed Linux bases and no JavaScript errors;
VM/container creation selectors exclude release-image aliases. The admin
published-image approval dialog now supports verified captured containers too.

Release 0.1.2 fresh-test preparation succeeded through the dashboard API:
two VMs, four containers and one isolated network are declared. Staging job
`4c93e63692124615b1dcf92c2795080e` is queued. A reviewed apply, service replay,
health checks, both boot orders, cleanup, five environment rollouts, participant
workstation sessions/grants and rollback remain outstanding. No full-lab or
all-node-template completion is claimed. Older failed source copies and archives are retained for reviewed cleanup.
PVE removed the failed Debian v1 restore and later reused ID170 for the Ubuntu
v1 restored template; recovery of the old Debian receipt must never delete
that different owned allocation. New transport templates use a separately
tracked ID band and full-copy ownership checks.

### October17 follow-up: publication remains gated

The selector and maintained-base dialog browser checks pass. The 236-test
regression group passes; an additional 19 identity-capture tests pass after
adding bounded collector names, exit codes and safe readiness states to errors.
Guest diagnostic output is not copied into job errors.

DebianVM v3 produced the independent runnable derivative175 on P6 and removed
its temporary transport template21000. Its identity collector failed before
publication. The original3015/P4 remains stopped and unchanged. An exact-owned
read-only diagnostic is retained separately; no new derivative or publication
version was created to bypass this failure. UbuntuVM v3 is still transferring.
P6 observed I/O pressure exceeded85%; repeated restores would worsen contention.

Parrot P2's smaller probe failed because the inherited4128MB balloon minimum
exceeded2048MB RAM. The fix sets balloon0 on the disposable probe only; exact
probe174 recovery is queued. P3 has no running guests or pending reservations,
but its default4096MB host reserve prevents this probe; a controlled2048MB
reserve update and retry are queued. No smaller-profile acceptance is claimed.

All-node base distribution is incomplete: P4 is absent from the enrolled-node,
approved-storage and network-attestation policy. Missing publication IDs also
remain for the five Linux bases and a legacy Windows alias. Existing references
remain intact; they are not relabelled as verified to conceal the missing proof.

New maintained-base allocations now prefer eligible SSD-backed nodes and use
P6 local-lvm rather than its HDD when P6 is selected. Existing reservations
retain their exact recorded storage; this change does not relocate or rebuild
an existing guest. Base cache warming also prefers P6 local-lvm for new copies.
The HDD remains in the approved storage pool.

The exact175 read-only diagnostic could not reach QGA under the HDD queue;
its graceful shutdown failed. A separate, guarded receipt confirmed the owned
isolated guest and terminal shutdown failure before stopping only175. It is
now stopped. This does not establish the original collector's failed command
or approve its image. P4's fresh provider observation shows16GiB available
RAM and roughly228GiB free local thin storage, but provider free space alone
is not enrollment, commitment headroom, or network acceptance evidence.

The final related regression group passes256 tests after the collector and SSD-preference changes. Both creation selectors pass a fresh browser check with no JavaScript errors.

### Lab acceptance queue priority

Release0.1.2 passes its manifest integrity check and contains two captured VMs
and four captured containers. A fresh provider read confirms all six staging
originals running. Fresh test definitions exist in one isolated test network;
deployment and test acceptance remain pending.

Seven unstarted maintenance jobs were cancelled through the dashboard Jobs API
so the fresh-test staging review follows the already-running Ubuntu build and
P5 compatibility preparation. Their exact original operation/options are kept
privately at`/home/crange/.local/share/crange/deferred-template-maintenance.json`
for re-queueing after the lab's reviewed test deployment. This defers three
Linux-container publications, P3 reserve tuning and the P2/P3/P6 Parrot retries;
none of their retained cache allocations were deleted or restarted. Do not
report those original cancelled job IDs as queued.

### October17: fresh0.1.2 foundation and network acceptance

Dashboard staging review`8dc016424e1449b3b88b10ff84f4b934` produced plan
`07aae37d33a4cf6f8abcef3721e9ca69`. The only guest create was the new test
custom router; the other changes were its VNet, SDN apply marker and revision.
Apply/configure job`fda540c4122b4eceb54a5376ad9b8203` succeeded. The router
uses P6 local-lvm and a4GB disk. Ordinary team routers and existing guests
were outside this configuration scope.

A P6 network check was rejected before probe creation because the custom
router is on P6: verification must traverse VXLAN from another node. The new
P1 check`a39605a9e41d4480a83a8067cc9b3783` passed all six checks, confirmed
probe/rule/state/reservation cleanup and published its exact placement scope.
Receipt SHA256:`1a07f519280cfe9f593022c7b25767b5e324be3dbdc509bda04d119b65c46a06`.
The GUI now excludes the custom router's applied node from probe candidates;
a real browser check passes. Fixed the custom-network node lookup to use its
actual`router:custom:` placement key.

Staging job`27e99a818a0644b5968873899465e9fe` is preparing six release caches,
starting with the DB image on P5/crange-thin. Its exact restore for CT178 has
provider status stopped/OK; subsequent cache proof and guest deployment are
still pending. No task was restarted due to observation delay.

The idle diagnostic of the stopped Debian base derivative established the
publication failure: only systemd-networkd-wait-online.service failed under
intentional NIC disconnection; machine-id and SSH public key are present.
Added a guarded status reset only for that exact failed unit on an owned,
running, disconnected disposable QEMU guest. Services are not masked/disabled;
normal readiness and independent identity still must pass. Other failures
remain blocking. Recovery verifies the exact frozen source, reservation,
terminal transport/boot tasks, stopped ownership, pool, private NICs and own
disks before reusing the existing derivative. No second restore/clone occurs.
New independent base probes prefer approved SSD storage and record that
selection separately from the immutable source's storage. Live base-publication
retries are deferred until the reviewed lab test deployment.

The combined lab/template regression group passes347 tests. The readiness
recovery is deployed, but live all-node base publication remains unverified.

The stage-changes worker timeout is extended to12hours for future runs because
one action can prepare multiple Windows/CT caches. Ordinary single jobs keep
one hour. The targeted25-test Jobs suite passes, including both time limits.
Worker source is deployed, but PID885869 is still executing the current job
with its previously loaded one-hour limit. Refresh the worker only when it is
idle; then recover exact retained cache generations if the current job times
out. Do not cancel or repeat a live provider restore to refresh this setting.

Cache acceptance now confirms DB template178 and CI template179 on
P5/crange-thin. File-server template180 has completed restore; disposable
probe181 started successfully and is undergoing Windows identity checks.
VMIDs can be reused after confirmed cleanup: CI's historical probe180 was
removed before180 became the FS template. Never delete a historical probe ID
without checking its current ownership and generation.

### October 17: retained DC probe recovery

DB178, CI179 and FS180 are verified ready caches on Proxmox5/crange-thin.
Staging job `27e99a818a0644b5968873899465e9fe` then failed during DC
identity collection; cache181 and its disposable probe182 remain retained.
The read-only DC collector now uses bounded local LDAP instead of ADWS for
the directory invocation ID. Its 33 focused tests passed.

Retry `62b15818bb2e462d8ce22eda43dccd72` could not reach the guest agent
within the three-minute boot window. Windows disposable restores now have a
bounded ten-minute boot/collector budget; Linux retains three minutes and
explicit timeout overrides remain honored. The change passes 72 focused tests
and is deployed. Current staging job `81aded18f9af438f8b6798206e29fb5e`
reuses the exact cache181/probe182; no restore or clone is resubmitted.
Identity, directory-generation and cleanup requirements are unchanged.

The ordinary worker was refreshed only while idle (PID918746), so the twelve-hour
stage-changes limit is now active. P5 test-network job
`a01fef153da44d658666046496af509a` passed all six checks and full cleanup;
receipt SHA256 `b95d95503cf15da89e6a9d134901329eeb9817631a9c987c8eb888013040584a`.
P1's earlier successful receipt remains valid. No full fresh-release test,
participant deployment or all-node base-publication completion is claimed.

### October 17: one-time disposable-probe replacement

Staging retry `81aded18f9af438f8b6798206e29fb5e` failed after the bounded
ten-minute Windows boot window. The console reached the lock screen but its
QEMU guest agent remained unavailable; a console login attempt stalled before
the desktop became usable. The original DC128 guest-agent ping succeeds.
Do not extend this timeout again or claim its cache verified.

Added Templates → Interrupted image verification → Replace failed verification
probe, backed by admin-only job `template-cache-snapshot-retry`. It verifies the
exact stopped owned probe, immutable archive pin, running capacity reservation
and terminal provider task history. It deletes only the failed disposable
probe, confirms absence and recreates one linked clone using that same reserved
VMID; the cache template and archive remain unchanged. One immutable replacement
attempt is allowed per cache journal. Provider tasks and previous evidence are
retained; a partial replacement remains blocked for reconciliation.

The related 128 focused/regression tests passed. API, worker and GUI are deployed;
services were refreshed while the ordinary queue was idle (worker PID920672).
Dashboard recovery job `daacb1072e464bb497ba543a369c9c94` confirmed deletion,
linked clone and start for probe182 from cache181. Fresh identity acceptance is
still pending. No full restore was repeated.

### October 17: DC recovery accepted; remaining staging resumed

Recovery job `daacb1072e464bb497ba543a369c9c94` succeeded. Its fresh clone
passed the complete Windows identity/readiness check after 166.6 seconds of
collector execution: company identity preserved, VM generation unique, directory
invocation reset, generation counter present, expected RDP
source restriction retained. Probe182 deletion completed OK. Cache181 is ready;
its capacity reservation is released and archive pin removed.

DB178, CI179, FS180 and DC181 acceptance is recorded in
[release-0.1.2-cache-acceptance.json](release-0.1.2-cache-acceptance.json).
Stage Changes `8e0bc45f56854a79a9e976395a9d0edb` is now running to prepare
the remaining application/backup caches and generate a reviewed test-guest
plan. No test guests are applied yet. The broker was refreshed with no active
console/request threads; the full live recovery-row browser check remains inconclusive (the row was
inside collapsed maintenance tools and later disappeared after acceptance).
A separate actual-browser component render passed with a fixture, without
JavaScript errors or submitting a job. The fixed admin operation was exercised
successfully through the authenticated dashboard API.
