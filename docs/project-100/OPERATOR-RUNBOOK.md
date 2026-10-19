# Meridian Harbor Logistics operator runbook

Private operator material. Give participants the separate
[company packet](COMPANY-PACKET.md), not this document.

## Evidence and current state

[STATUS.md](STATUS.md) records current runtime evidence; [FEATURES.md](FEATURES.md)
tracks dashboard acceptance. [PLAN.md](PLAN.md) defines the complete required
outcome. A successful image capture does not establish a successful release test
or participant deployment. Record job IDs and actual observations for each gate.

Use the authenticated controller dashboard. Automation must submit the same
validated `/api/jobs` requests as its buttons, with the current actor session and
CSRF protection. Do not substitute direct Terraform, guest shell scripts or
hypervisor writes for a missing dashboard feature: implement its fixed reviewed
workflow first. Keep credentials outside Git and job output.

## Release lifecycle

For changes confined to the reviewed service configuration, use Scenario →
Release tools → **Publish service update**. Select the parent release and a new
version. This preserves its immutable image IDs and records the changed recipe;
it requires unchanged staging machine/network definitions and exact source
identities. It does **not** capture manual disk edits. Use the capture workflow
when disk contents or machine definitions change. Fresh release testing and
cleanup must pass before an explicit rollout. Published 0.1.3 is currently
awaiting those gates; deployed 0.1.2 remains unchanged.


1. Select Meridian Harbor Logistics (p100). Edit business machines in Scenario;
   their source environment is Staging/network 254 (`10.100.254.0/24`). Keep
   addresses unique and retain `.1` for the router. Ordinary environments are
   1–5; staging is not network zero.
2. Stage Changes (`stage-changes`). Review the retained plan and its resource
   changes before Deploy (`stage-deploy`). Use the exact reviewed plan ID.
   Deployment includes configuration; investigate failed configuration rather
   than accepting infrastructure creation alone.
3. Run declared health checks and verify the business services. Linux and Windows
   provisioning details are in [linux-provisioning.md](linux-provisioning.md)
   and [windows-configuration.md](windows-configuration.md).
4. Capture an immutable release (`release-capture`). Freeze project definitions
   during capture. The worker stops each selected source, captures its identity,
   exports and hashes its archive, tests a separate isolated clone, cleans that
   probe, then restarts the source. Follow the actual job and provider task.
   Fixed service recipes are preserved by their manifest SHA before capture.
   Fresh-test preparation also preserves a legacy release's recipe if the
   current trusted source still matches. Do not edit old release manifests or
   overwrite recipe archives; an unarchived mismatching recipe requires a new
   configured capture. Replay and rollback select the archived recipe, while
   a new staging capture requires the currently configured recipe.
5. Prepare a fresh release test (`release-test-prepare`). Review/apply its network
   foundation if required. Accept the exact declared LAN on each candidate node
   using `network-verify`; acceptance of another VNet does not qualify this LAN.
   When recreating a deleted test VNet, refresh acceptance on every candidate
   node before staging guests, even if a historical receipt has its old name.
6. Stage Changes, inspect the plan, then deploy the fresh isolated test cohort.
   `release-test-check` must pass service replay, declared health checks and the
   selected startup permutations. An archive checksum alone is insufficient.
7. Explicitly stage release deployment (`release-deploy`) to the chosen
   environments. Review the resulting plans, then apply the exact retained plan
   through `release-apply`. Run `release-check`. Staging edits must never silently
   change deployed participant environments.
8. Remove disposable test definitions using `release-test-cleanup`, review the
   resulting removal plan and apply it. Verify guest, network and reservation
   cleanup. Preserve immutable versions still backing deployed linked clones.

## Required delivery checks

- Five isolated environments, each with six business servers and four assessor
  workstations (two Linux, two Windows): thirty servers and twenty workstations.
- Ten separate participant Guacamole accounts, with grants to their intended
  workstations and no cross-environment access. CRANGE dashboard accounts are
  separate from Guacamole accounts.
- Real authenticated SSH/RDP sessions, correct addresses and power state, and
  ownership-checked power/snapshot actions. Hypervisor console success does not
  replace authenticated desktop acceptance on the participant workstations.
  Business-server admin access may use the hypervisor console. Keep the DC's
  ordinary-user RDP logon restriction; do not grant administrator access merely
  to make a generic desktop test pass.
- DNS, Internet access as declared, controller/Guacamole management access,
  cross-environment denial and no exposure of host management services.
- Actual assessment paths and synthetic data, plus reset and rollback on an
  owned disposable fixture. Verify the resulting service state after rollback.
- In-character participant packet, separate operator notes and complete feature
  evidence. Create reusable agent skills only after full lab acceptance.

## Interrupted operations

Release capture has a bounded twelve-hour controller deadline; ordinary jobs
retain one hour. A worker timeout is not proof that its Proxmox task failed. Inspect the exact
UPID and durable capture/source journals. Do not submit duplicate backups,
clones or restores while provider state is uncertain. Retain partial files and
reservations until exact ownership and outcome are proven.

Capture retries must reuse their existing checkpoint and probe allocation. A
completed probe still needs full verification evidence; never fabricate a
passed receipt. Check both the dashboard job and provider task before restarting
workers or deploying controller code.

For the October 14 storage incident, see
[proxmox5-library-recovery.md](proxmox5-library-recovery.md). The library is CT145
on Proxmox5, management address `10.0.0.249`, with its share mounted at
`/srv/crange-library`; it also has the range access path used by archive hashing.
It is shared infrastructure, not a disposable project guest. The observed
reboot restored service, but the exact kernel-level cause remains unproven.
Never change quorum votes to mask a storage or management stall.

## Placement and maintenance boundaries

Use automatic placement and suggested nodes. Proxmox6 is now enrolled; each
project LAN requires its own published network acceptance before placement. Templates and explicitly stopped guests do not consume CPU/RAM
commitments; their storage remains counted. Actual host memory, reservations,
reserves and unknown-status guests remain accounted for.

Do not shrink existing router disks; the smaller router default applies to
future creation. Preserve unrelated Proxmox workloads. Project automation may
manage OPNsense routes/gateways, not firewall rules. Keep network acceptance and
project state separate and retain older image generations while clones need them.


## Archive capacity and interrupted cache preparation

Before retrying a failed capture, inspect its exact job output and owned capture
journal. Capacity rejection before allocation is different from an interrupted
restore/export. Do not discard retained reservations or submit duplicate copies.

Administrators can open **Templates → Publication & recovery tools → Interrupted
archive restores → Verify restored cache**. This fixed action rechecks the exact
completed archive restore and ownership before verifying a disposable linked
clone and publishing the cache. It rejects active tasks and changed identity,
source, disks, network, pin or reservation. Success reconciles the reservation
and removes the pin only after publication and probe absence are verified.

For central archive headroom, use **Cluster → Maintenance & capacity settings →
Library & storage → Grow archive by 64 GB**. This expands only the fixed CT145
SSD data volume online. Every increment preserves at least 100 GiB and 10% of
the original SSD volume group as free host reserve. A durable pending-growth
journal prevents a retry from blindly issuing another increment. If filesystem
readback fails, inspect the protected journal rather than deleting it. The
fixed helper does not restart NFS or any guest. Growth is refused when the host
reserve cannot be retained; dependency-safe archive retention is then required.

The storage relay is separate from signed network diagnostics. Do not change
the network relay or its trusted source digest just to administer storage.


## Read-only template export failures

If a stopped SATA/IDE immutable template fails native backup with `The device is
not writable: Permission denied`, inspect its exact task and source journal.
An administrator can expand the affected node in Cluster and choose **Prepare
template backups**. This performs the bounded reviewed package repair, not a
full host upgrade. After that job succeeds, retry the same capture version and
selection; its existing identity/transport/copy checkpoints must be retained.
The worker admits the retry only after matching the terminal failure and proving
that the actual installed package is fixed. Never delete the reservation or
change base-disk permissions to force a retry.

## Retrying staging and captured Windows services

Stage Changes automatically retires an expired, unapplied guest preview only
when its declarations still match and no corresponding guest or active provider
task exists. Running applies and retained image-cache allocations are not
released by time alone. Do not edit the reservation database to regain capacity.

For a failed captured Windows verification probe, Templates → Publication &
recovery tools → Interrupted image verification can replace only that failed
disposable linked clone once. Its immutable local template and central archive
remain intact. A failed or uncertain replacement requires reconciliation.

Captured Windows credentials can be qualified WinRM principals such as
`MHL-FS01\Administrator`. Local-account tasks require the bare SAM name. The
worker supplies that per host when replaying archived recipes; do not edit an
immutable release or reset guest passwords to work around that input mismatch.

### Review selected team rollouts

After a passing fresh-clone test and reviewed cleanup, choose **Select release**
to declare the target version for the selected teams. Use **Review rollout**
to prepare missing approved image caches and retain a Terraform plan for only
those recorded releases. Manual scenario boxes, jump boxes, staging, and
unselected releases are excluded. Inspect the actual plan before **Apply
reviewed release**. New declarations do not deploy machines by themselves.

The fixed dashboard payload for review is:

```json
{"operation":"release-stage","project":100,"options":{"teams":[1]}}
```

Review validates current release ownership and checks the resulting Terraform
changes against the selected releases before retaining a plan. Apply repeats
the scope check and rejects a selected-team set differing from its review.
The deployment plan expires after one hour; recreate the review if expired or
inputs change. **Review rollout** is implemented and its live form renders;
the Project 100 production rollout has not yet been exercised.


## Resume an applied company rollout

If infrastructure deployment completed but configuration or health failed,
use **Scenario → Continue configuration** in the rollout table. The global
control selects applied releases still awaiting passing health; a row's
**Continue** selects only that company. No new infrastructure plan is applied.
Recovery verifies all selected live identities and pinned recipes first, then
runs scoped guest configuration and health checks, recording success per company.
Closing the output popup does not cancel the job; reopen it from Jobs.
If preflight reports missing/stopped guests, changed identity or network,
resolve that recorded mismatch before retrying. Do not resubmit a stale
infrastructure apply simply to repeat configuration.

## Portable VM machine types and expired reviews

New guests with a version-pinned newer QEMU machine family use the reviewed
portable 9.2+pve1 baseline only after the selected node reports that exact
capability. This affects the saved deployment plan, not the immutable cache or
live guest settings. Existing guests retain their actual machine type. A missing
baseline fails planning. Review the actual plan before deployment.

Project Overview planning now uses the same guarded expiry sweep as staging.
Only matching actor/project, expired, unapplied guest admissions with unchanged
input identities, no applied guests, and no corresponding live guest/provider
task evidence can be released. Running applies, cache preparations and uncertain
allocations remain reserved. There is no blanket force-release action.

## Release replacement downtime

A reviewed release replacement stops the selected old release guests before
Terraform can create peers with the same addresses. This is an intentional
outage for the selected environments, acknowledged by the reviewed destructive
plan. The worker verifies the old applied endpoint IDs, node, name, pool, project
marker and network; initial deployment has nothing to retire. Staging, assessor
boxes and other teams are outside this stop phase. Old/new node locks cover the
transition. Provider task handles and stop checkpoints are retained under
`.crange-rollout-quiesce/<plan-id>.json`.

A failed or ambiguous stop blocks replacement. Preserve its receipt and inspect
the exact provider task; do not resubmit a guessed stop or start old peers beside
new clones. A fresh reviewed recovery apply can resume from real current state.
After infrastructure, run selected-team configuration and health before serving
users. This path has regression coverage; live upgrade/rollback acceptance is
still pending for Project100.

## Unexpected host loss during a reviewed apply

Check the dashboard job's terminal state and actual provider tasks before any
retry. A failed observer or expired polling request does not prove a provider
operation stopped. Preserve partially created guests and their reservations.
Request a fresh review; do not replay an old saved plan against changed state.
An offline-node ownership or configuration failure must be resolved rather than
ignored. The current API gateway can fail over to a configured member only after
verifying cluster identity and quorum; it never replays uncertain writes.

For the October 19 Proxmox1 outage, five other members retained quorum. Console
inspection is required to establish the host failure cause. Do not lower expected
votes or relocate possibly running guests without established fencing. Current
company health on reachable routers and historical acceptance are recorded
separately in STATUS.md. Workstation apply 8fafed50dda9423b949aea82964f3a13 timed
out with seven Windows guests retained on Proxmox6; no team configuration followed.

## Staging snapshot recovery

For an owned staging box, Guacamole's Snapshots dialog submits fixed snapshot
create/restore/delete jobs. Restoring the tested LXC snapshot left CT124 stopped;
use Start afterward and run that box's declared health checks. Changes made after
the snapshot are discarded. The October19 acceptance used a temporary Linux
account to prove disk rollback, retained the baseline CI account, verified root-key
management access, removed the test snapshot, and returned CI to healthy/running.
See staging-snapshot-acceptance.json. VM rollback and overriding a still-pending
shutdown are separate unaccepted gates.
