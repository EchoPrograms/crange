# Project 100 platform progress

This file records observed Project 100 infrastructure state. A deployed
resource is not considered accepted until its scoped network checks pass.

## 2026-10-08 — router foundation

- Applied the reviewed router-only Terraform plan under policy revision
  `mhl-cluster-cpu-reserve-20261008`: 17 resources added, 0 changed, 0
  destroyed. Terraform state contains 17 instances and the placement record
  maps seven routers to their reviewed nodes:
  - Project router CT 113 on Proxmox 2 (`10.60.2.2`, `10.100.0.1`).
  - Staging router CT 116 on Proxmox 5 (`10.100.0.7`).
  - Team routers CT 120, 118, 117, 119, 121 on Proxmox 1, 2, 5, 2, 1
    (`10.100.0.2`–`.6`).
- Proxmox gateway P100 is `10.60.2.2` on `opt2`; the OPNsense static route
  `10.100.0.0/16` via P100 is installed. No OPNsense firewall changes were
  made.
- Persistent router routes were applied on the project router, all five team
  routers, and staging. They cover `10.100.1.0/24` through
  `10.100.5.0/24` and `10.100.254.0/24` via their declared router transit
  addresses.
- SSH sessions to Debian 12 unprivileged router containers stalled in the
  optional PAM `pam_systemd` profile because `systemd-logind` fails with
  `226/NAMESPACE` in this container environment. Disabled only the Debian
  `systemd` PAM profile with `pam-auth-update` on these seven Project 100
  routers; the Unix authentication profile and `/var/lib/pam/seen` were
  unchanged. The affected `common-session` hash changed from
  `c43a99cba44390edf1fe48e777e7ca6bfdee49fbbfa14260d32bd4b3b5b771e4` to
  `93b668185836f7b2df9bc9a1ad80f9a89c7b1580e0a59d21514b5145cbf0bcc7`.
- Project-router Ansible installed UFW, the declared sysctls, DNS/SSH access,
  and forwarding rules. Earlier Ansible output reported UFW installation on
  team routers 1–4, but a later direct inspection found UFW inactive on the
  reachable P1/P5 sample and absent on two of those containers. That evidence
  does not establish that UFW caused the SSH failures. The live bootstrap
  record is `configuration_failed`; staging and exact-VNet acceptance remain
  incomplete.
- A direct IPv4 TCP check from team router CT 117 to Debian mirror address
  `151.101.18.132:443` returned `No route to host`; its DNS A lookup succeeded.
  Package installation/egress requires further diagnosis. No OPNsense
  firewall rule has been added.

The controller Ansible launcher now supplies the installed `C.UTF-8` locale;
the focused test `tests.test_ansible_locale` passes. Exact per-VNet acceptance,
router isolation checks, Linux company servers, and participant jump boxes are
still outstanding.

## Access checkpoint — 2026-10-08

A bounded read-only follow-up from Proxmox 4 to `root@10.0.0.102` failed
with SSH exit 255, `No route to host`, before the requested CT 113 firewall
inspection could execute. This is a host-management path failure, distinct
from guest UFW diagnosis; it does not prove that guest policy caused it.
No firewall changes followed that failed host-management check.

## Cluster outage confirmed — 2026-10-08

A separate Proxmox 1 management SSH attempt to `10.0.0.102` also returned
`No route to host`. The authoritative Proxmox `/nodes` API then reported
Proxmox 2 **offline**, while nodes 1, 3, 4 and 5 remained online.
`/cluster/resources` reported CTs 113, 118 and 119 on Proxmox 2 with unknown
status. The immediate project-router outage is therefore a node availability
problem; earlier temporal correlation with UFW does not establish its cause.
Do not recreate or migrate these unknown-state guests without recovering
authoritative node/guest state. P1 CTs 120/121 and P5 CTs 116/117 are running;
read-only inspection found their expected routes and forwarding enabled, with
the same `common-session` hash as the earlier PAM adjustment. UFW was inactive
on sampled CT120/CT117 and missing on CT121/CT116. Current network probe
failures therefore remain unattributed. No OPNsense changes were made.

Reviewed Linux business roles are present in `ansible/linux` and
`ansible/roles/mhl_*`: PostgreSQL-backed shipment data, a small portal, a
synthetic warehouse build artifact, and a synthetic dispatch backup export.
The example inventory declares `.30/.40/.50/.60` host slots for Team 0 and
Teams 1–5. These roles are not deployed; they require protected
`mhl_db_runtime_password` and `mhl_ci_synthetic_token` variables and accepted
team-network connectivity first. The schema grants the runtime role read
access to the seed table while retaining the intended `CREATEDB` overprivilege.
PostgreSQL configuration discovery uses the installed Debian cluster version
rather than assuming a major version. A local PostgreSQL server/client is not
installed in the development workspace, so the role has not been exercised
against a live local database.

The fixed Project 100 CLI has declared ten Linux assessor jump boxes, two per
team, from the ready Parrot logical image. Linux addresses are `.11` and `.13`
so the paired Windows boxes can use `.12` and `.14`. Guacamole services are
SSH+RDP for Linux and RDP for Windows. No plan or apply was run. The complete
20-box/10-user assignment and six staging business systems are recorded in
[`scenario-draft.json`](scenario-draft.json). Four Debian 12 staging containers
are now declared against the verified project alias; the Windows image remains
unpublished, and none of these guests has been planned or applied. The separate
participant inventory example covers all five teams and both operating
systems.

## Staging desired-state update — 2026-10-08

The signed Debian 12.12-1 archive is linked into Project 100 as the fixed
`mhl-debian12-stock` template alias. The link records SHA256
`ff5c55cba730fc1e93bc7de3e0ea4aecb05c692094009cfcf2999973a56f15e5`, the
official SHA512 aplinfo checksum, size, and independent provider/content
acceptance receipts on P1, P3, and P5. No claim is made for offline P2. The
fixed CLI has declared Team 0 containers `mhl-db01`, `mhl-app01`, `mhl-ci01`,
and `mhl-bkp01` at `.30/.40/.50/.60`; each uses one core and 512 MiB to 1 GiB.
The desired-state checks currently use ping while the service roles await
network acceptance and deployment. No plan or apply has been run.

The assignment draft now marks these four containers as declared, not
deployed. Windows Server images and associated participant boxes remain
pending immutable publication. The host-management path to Proxmox 2 remains
unavailable, so router/network acceptance and service deployment are blocked;
no guest on that node has been recreated or migrated.

## Participant connection scope — 2026-10-08

All ten Project 100 participant accounts were updated through the authenticated
dashboard API with an explicit own-Linux-jumpbox assignment and no default team
scope. For each team, user 01 maps to `jumpbox:tTTT-j01` and user 02 maps to
`jumpbox:tTTT-j03`; the existing home-team and user-number fields remain.
All ten `guac-users` jobs succeeded and the desired record reports `scoped=true`.
Windows `j02`/`j04` connection keys are pending Windows guest declaration.
There are currently zero applied connections; live participant grants have not
been tested.

## Offline capacity accounting — 2026-10-08

Project 100 `stage-changes` initially stopped because `/cluster/resources`
reported Proxmox 2 offline and CTs 113, 118, and 119 with `status=unknown` and
null `maxmem`, `maxcpu`, and `maxdisk`. The scheduler's old offline branch
required those fields and could not proceed. Read-only inspection of the
authoritative PVE cluster configuration on Proxmox 4 found each CT configured
with 1 core, 512 MiB RAM, and an 8 GiB root filesystem. No guest was recreated
or migrated.

Controller API config reads for CTs 113, 118, and 119 returned HTTP 500 through
both P2 and P4 endpoints. The existing root relay on P4 now accepts one fixed
read-only `offline-config-snapshot` operation for exact allowlisted guest IDs,
types, and required project/image tags. Its protected allowlist is
`/etc/crange-ct-read-relay.json` (root-owned mode 0600). The relay checks current
cluster membership, requires P2 to remain offline, verifies ownership tags,
and confirms both membership and pmxcfs config bytes stayed unchanged during
the read. It returns only configured RAM bytes, CPU cores, disk bytes, template
status, config digest, and capture time. The controller worker accepts only a
fresh snapshot matching its discovered guest row. The original probe identity
commands retain their prior restrictions.

An authenticated worker read returned CT113/118/119 at 512 MiB, 1 core, and
8 GiB each; VM147 (`crange-image` cache) was identified as a template with a
20 GiB configured disk, so only that disk is charged to quotas. Offline guests
remain ineligible for placement. Focused relay and scheduler tests pass (36
tests).

The desired `mhl-debian12-stock` template resolves to approved catalog image
`debian-12-router-v1` (8 GiB disk). Its Debian 12 stock archive replicas are
already marked ready on P1, P3, and P5 with the verified SHA256 above, so this
failure is not a missing image/cache. The actual next gate is network
eligibility: the scenario guest requires `p100t254`, and P1/P3/P5 currently
have no verified VNets in their admission records. P2 is offline and P4 is
excluded by policy (and lacks CT acceptance).

Cache planning previously returned an empty rejection map because it skipped
nodes before storage/capacity evaluation. It now reports those node exclusions
and handles a null optional image-feature list as no additional feature
requirement. A real retry reports `required-networks-not-verified:p100t254` on
P1/P3/P5; P2 is separately reported offline, and P4 policy-excluded. No
storage/capacity candidate was evaluated (`storage_or_capacity_failures` is
empty), so no cache preparation was attempted. No reviewed plan was produced
and no apply ran. No offline guest was recreated or migrated, the Windows
source was left unchanged, and no OPNsense changes were made. P100 team-0
network acceptance remains blocked while P2's project router is unavailable.

## P2 management-path check — 2026-10-08

An independent check from Proxmox 1 to Proxmox 2 management address
`10.0.0.102` found ping returning `Destination Host Unreachable` from
`10.0.0.101`, SSH on TCP 22 timing out, and the `vmbr0` neighbor entry remaining
`INCOMPLETE`. This confirms a layer-2 management-path failure while P2 is
offline. The fixed config relay can still read shared pmxcfs metadata from P4;
it does not restore P2 router service or establish network acceptance. No
firewall changes followed these checks.

## Participant Guacamole accounts — 2026-10-08

The existing authenticated bulk-user workflow created two enabled accounts for
each team. Logins are `p100-t001u01/02` through `p100-t005u01/02`; each
account is scoped to its own team. All five `guac-enroll` jobs succeeded
(`66505afd239a461eabe351778e507e52`, `34ad9a994efe42afa39d0ad04bf8b901`,
`782f16bd8b324942b0c4661520b0e7a9`, `0a1da8113a204ce1a47826624e989b4e`,
`f028a1613cca44208bf4f8005a6e28a7`). The Guacamole API confirms all ten
accounts exist, have no connection grants, and lack self-service password
`UPDATE`. Their generated credentials are kept only in
`/home/crange/.config/crange/project-100-assessor-guac.json` (mode 0600); no
passwords are in the repository. The draft maps each assessor to its two
intended Linux/Windows boxes, with connection grants pending guest deployment.

The first authenticated API attempt exposed a project-directory permission
issue. Project 100 is now mode 0710 so the dashboard's `crange` primary group
can traverse it to read the 0640 desired-state file, without directory listing
access. The `project-init` workflow now creates future project directories
with the same mode; focused tests cover both directory and config-file modes.

A disposable Team 1 account exercised the account lifecycle through the
authenticated API and fixed Guacamole jobs: create
`fbd6a3079cf547e9948e67c29530fb18`, rename plus password change
`1146ed7e7dab43c8ae7cc32e393e8ee4`, password-reset disable
`09aa81362f12401681658f55a15f2930`, lock `2734debc2a9f4226a12a09c533a393e7`,
unlock `92a1aa9f55bf4a26a2c2fa96ea81eabe`, and delete
`419a3f04d1234b2e9a2f9bbf742fe0ef`. The renamed password authenticated; the
old password was rejected. Guacamole confirmed the reset permission and lock
state changes, and both local and remote fixture records were absent after
delete. Temporary fixture credentials were removed after cleanup.

A read-only Chromium check confirmed login, Project 100 selection/name, and the
Overview, Projects, Teams, Scenario, Users, and Jobs pages. Teams showed six
network entries; Users showed all ten accounts. Teams, Scenario, Users, and Jobs
were rendered at 390px with no page-level horizontal overflow. Screenshots are
under `/tmp/project100-ui-evidence/`; browser navigation does not substitute for
the pending guest connection and rollout checks.

## P2 recovery and Project100 network gate — 2026-10-08

The live Proxmox `/nodes` API now reports P2 online. `/cluster/resources`
reports CT113, CT118, and CT119 running with configured maxima of 1 CPU,
512 MiB RAM, and 8 GiB disk each. The existing Project100 router placement
record remains unchanged: project/t2/t4 on P2, staging/t3 on P5, and t1/t5
on P1. No P2 host interface, address, or gateway was written by this work.

The fixed `bootstrap-configure 100` workflow completed and returned the
bootstrap record to `awaiting_network_acceptance`. The authenticated
`stage-changes 100` retry still stops at exact VNet admission:
`required-networks-not-verified:p100t254` on P1/P2/P3/P5. P4 remains excluded
by placement policy and lacks container acceptance. No storage/capacity
candidate was evaluated; approved image and cache readiness are not the
blocker. No Project100 Terraform guest plan was produced, so there are no
Project100 additions, replacements, or deletions to review yet.

A separate, root-owned disposable network-probe plan is prepared for root
review. It contains exactly one create: VM30007 on P1, pool `crange-probes`,
full clone from the approved ready Alpine VM149 replica on P1/local-lvm, 1 core,
512 MiB RAM, 20 GiB disk, one MTU-1450 NIC on `p100t254`, and static
`10.100.254.98/24` via `10.100.254.1`. The address is absent from live PVE
configs and Project100 declarations; VMID30007 is absent from cluster config.
The plan is bound to job
`network-2cd5a04ee71322720aa0138143f016ac`, and its create-plan JSON SHA256 is
`627210efdb22ead92ac88c3237d706b6162c87c36c192f3fad85da2139874093` (binary
plan SHA256 `35ca780a7db9b096fd47aa4ec2bb301dc263738620f3c4559ae925c4e5f10792`).
The fixed adapter validated the exact create boundary and reported
`applied=false`. Root review is still required before running the disposable
probe. Existing network checks cover controller SSH/22 and Guacamole SSH/22;
they do not attest Guacamole RDP/3389, so no RDP path acceptance is claimed.
No firewall, OPNsense, Windows source, or P2 host-network change was made.

Root approved and initiated the one scoped disposable probe run. The fixed
preflight passed for the exact P1/P100 staging tuple, but create failed with
Proxmox HTTP 403 before the six acceptance checks began. The clone left a
stopped VM30007 in pool `crange-probes`; it retains the approved source image's
tags and disconnected source NIC/IP configuration, so it is not an accepted
probe. The protected journal remains `cleanup_uncertain`; its create-plan JSON
hash is unchanged. The exact owned temporary router rules are absent and the
project inputs are unchanged. No network receipt or P1 staging attestation was
issued.

The fixed cleanup reconciler was extended with a narrow partial-clone proof
and tested. It requires the 403 journal, the reviewed create plan, the sole
matching Terraform state resource, the approved source configuration, and the
stopped disconnected live clone before permitting cleanup planning. The proof
matches this VM, but fresh deletion-plan generation also fails with sanitized
Proxmox HTTP 403. No delete plan was generated and no raw delete was attempted.
The VM and reservation remain for reconciliation after the controller's
Proxmox identity is given a fixed, reviewed cleanup capability. Acceptance
checks, including the separate Guacamole RDP/3389 check, remain pending.

Read-only P4 ACL review and P1 API logs isolated the missing privilege: the
create request `PUT /nodes/proxmox1/qemu/30007/config` returned 403 while
removing the cloned `ide2` cloud-init device; the installed QEMU API maps that
operation to `VM.Config.Disk`. The later destroy-plan refresh request
`GET /nodes/proxmox1/storage/local-lvm/content/local-lvm:vm-30007-disk-0` also
returned 403; Proxmox's volume access check requires `VM.Config.Disk` on the
owning VM in addition to datastore audit, which is already present for
`local-lvm`. Root approved a narrowly scoped ACL delta. `CrangeProbeWorker`
retained its existing privileges and gained only `VM.Config.CPU`,
`VM.Config.Disk`, `VM.Config.Memory`, and `VM.Config.Cloudinit`; its only
assignments remain the user and token at `/pool/crange-probes`. A separate
`CrangeProbeCleanup` role contains exactly `VM.Allocate` and `VM.Config.Disk`,
assigned temporarily to the user and token only at `/vms/30007`. Protected
before/after ACL snapshots are root-owned mode 0600 on PVE4 and outside Git.

After deploying the reconciler's redacted failure-category journaling, one
guarded retry still stopped before deleting VM30007. The protected journal
records `destroy_failure_cause=valueerror_detail_redacted`; the exact one-delete
saved plan remains bound to its reviewed manifest digest. Read-only PVE
inventory still shows VM30007 stopped in `crange-probes`; both temporary
`/vms/30007` ACL assignments remain because cleanup has not completed. No
network checks ran and no attestation was issued. The guarded local validation
failure must be identified before another reconcile attempt.

The exact local gate was the reconciler's immediate pre-apply ownership
recheck: it changes the protected journal state to `reconcile_destroy_pending`
before calling `partial_create_probe` again, while that helper admitted only
`cleanup_uncertain`. The helper now accepts that one fixed cleanup transition
state while continuing to require the original 403 cause and exact plan/state/
guest identity. A root-owned protected-fixture integration test exercises the
initial proof, transition, repeated proof, and saved delete-plan inspection.
All 28 adapter tests passed on CT114. The guarded reconcile then succeeded:
VM30007 is absent, Terraform state is empty, temporary router rules are absent,
and the reservation is released. PVE confirms no remaining `/vms/30007` ACL
assignments. The journal is `failed_cleaned`. This was cleanup only; the six
network checks were not run, no attestation was issued, and Guacamole
RDP/3389 acceptance remains pending.

A fresh exact-scope P1 staging probe job was prepared as
`network-6593f7de8e524610ab03e393f7e5c73c`, VMID30008, `p100t254`,
`10.100.254.98/24`. Its protected manifest and Terraform input match the
reviewed tuple: one full clone from approved Alpine149 on P1/local-lvm, 1 CPU,
512 MiB, 20 GiB, and one MTU-1450 NIC. No VM, Terraform state, or reservation
was created. The read-only Terraform plan failed with Proxmox HTTP 403 before
a saved plan could be reviewed; the CLI yielded no request path, and observed
P1 log 403s were unrelated QGA reads. No create/apply, acceptance checks, or
attestation occurred. Exact privilege attribution remains pending; do not
retry or widen ACLs until the failing endpoint is identified.
# Job history navigation

The Jobs view now pages completed job history with 10, 25, 50, or 100 records
per page (25 by default), ordered newest first. The API returns the authorized
terminal-history count and page metadata, while all authorized queued and
running jobs remain available on every page for live task indicators and
notifications. Project filtering is applied by the API and retains the same
project authorization checks as job access.

## Resumed staging isolation — 2026-10-09

Stage Changes now previews cache/capacity using a staging projection while
retaining the complete project desired state. Fixed Terraform targets include
new and removed staging guests and custom networks; the actual plan is still
checked for non-staging changes before retention and again before apply.
Existing team placements remain frozen, and undeployed team guests do not
require staging capacity reservations or media resolution.

Dashboard job `1987dad02697485aba84b7ce2e78a7bf` reached placement of all four
staging Linux containers without being blocked by the ten pending team
jumpboxes. It failed before apply because Terraform rejected the GUI's `auto`
container node value. The template and live P100 validation were corrected to
accept automatic node suggestions for containers and custom networks;
controller `terraform validate` then passed. Retried Stage Changes job
`e1e0480e8ccd41308986a831ac8167e2` is pending final review. No deployment success
or business-service health is claimed by this entry.

### Linux staging completed through dashboard workflows — 2026-10-10

The initial archive-backed apply created the four staging containers, then
failed configuration because the stock OS archives had no `user` account or
controller root key. The container module now installs the controller public
key when creating an approved stock archive. Inventory uses root only when
that archive source is present in the applied placement. Captured template
management-user propagation remains to be checked during release testing.

Recreated guests also exposed stale SSH host keys. The configure workflow now
refreshes only newly applied guest addresses after validating live project,
network and guest identity. The container play obtains regenerated public keys
through the authenticated connection before restarting SSH and saves them on
the controller. Unprivileged Debian services required the fixed `nesting`
feature; a reviewed in-place staging update and explicit dashboard stop/start
jobs activated it. A subsequent SSH check found `systemd-logind` active and
completed in 0.31 seconds. Automatic-node power actions now resolve the actual
applied placement instead of sending `auto` to Proxmox.

| Staging system | Address | Node | CT |
| --- | --- | --- | --- |
| MHL-APP01 | 10.100.254.40 | proxmox2 | 125 |
| MHL-DB01 | 10.100.254.30 | proxmox1 | 127 |
| MHL-CI01 | 10.100.254.50 | proxmox2 | 124 |
| MHL-BKP01 | 10.100.254.60 | proxmox2 | 126 |

Dashboard configure job `4de9a02e911e475eb7438e43567d566e` succeeded for all
four guests. Business-service job `14489c4105cd40ffabe05b17debf767e` installed
PostgreSQL and the seeded database, the shipment portal, nginx CI artifacts,
and backup reports. Its prerequisites now include sudo for PostgreSQL's
service account. The complete MHL role bundle is deployed, and its role path
is explicit. Worker-owned service credentials live outside Git under
`~/.config/crange/secrets/project-100`, with private directory/file modes.

Container edits were submitted through the same job API as the GUI. Health
job `6355d5bed4064f5ebc0d26ddcca88efa` passed all seven checks: four pings,
portal HTTP 200 on port 8080 `/healthz` (including database authentication),
CI HTTP 200 on port 80, and PostgreSQL TCP 5432. Stage Changes job
`07fc5fbfa06e4670b805fda262586e41` then retained a plan with no infrastructure
changes. Deploy to Staging job `a0eef89fdbb540a9bef1b53d7394d1f7` applied that
review and completed configuration plus Guacamole access refresh successfully.
No ordinary team resources were changed.

Independent network job `bc631aae816f40b7a725178e8eac2585` passed all six
checks for `proxmox2/p100t001`, cleaned its probe/rule/state/reservation, and
published receipt `3aaddde14f9fba06ee64de8cc39957b8cd1c697535b062a20e2ed1391a32823a`.
This acceptance is scoped to that tuple, not the entire cluster.

This is completion of the Linux staging milestone, not the full lab. Windows
DC/file-server guests, participant jumpboxes, immutable complete-release
capture/test and five-team rollout remain unverified and unfinished. The ten
participant accounts still have no deployed jumpboxes; four managed staging
connections do not prove interactive Guacamole credentials work.

## Windows staging progress and node outage — 2026-10-10

The previous goal turn made verified progress: the Linux staging workflow and
seven health checks completed. This continuation created and initialized a
Windows staging candidate through dashboard jobs, and exposed an external
node outage. Full lab completion is still unproven.

The administrator capacity job `d11bd1c539a446888dff06aa18e5ae5f` set the
existing bounded CPU ceiling to 6:1 while preserving RAM 2:1, linked virtual
disk 2:1, host reserves, storage approvals and allowed nodes. Template
identifiers now have their own bounded validator instead of using the 24
character guest-hostname validator. The actual approved Windows reference is
`mhl-windows-server-2025-base-p100-v1`.

Declaration `f346fbc2346943f5b789e1f2ca31472c` and Stage Changes
`7134feea48df41d89d0484ebccc03a4c` selected a verified linked clone from
P1 template122. Plan `b60ad650b54ad449749f5246f485842d` created only
`t254-mhl-dc01`. Deploy `22d3c309849843bb9d4ba939fbafc628` created VM128 on P1
but failed its initial agent call during startup. Team configuration now
includes only that team's applied Windows hosts. Controller WinRM rules are
limited to actual declared Windows endpoints and the controller source IP;
only the selected team's routers and the shared project route are operated on.
No OPNsense firewall rule was changed.

Windows initialization now waits for a Windows agent, journals submission
intent before mutation, persists and resumes accepted PIDs, retains private
terminal diagnostics, and refuses uncertain duplicate submissions. The Proxmox
API's input-data field is now raw JSON, matching the PowerShell reader; the
previous extra base64 encoding caused a real guest-side failure. Credential
verification uses the documented network logon mode, separate from eventual
RDP verification. Windows bootstrap uses the same corrected transport.

Configure job `8b7c6dd1d42749e8bcfb4a3162fde846` completed VM128's local
initialization before failing at the unavailable project router. Its private
journal records `complete`, PID3788, exitcode0. A fresh agent interface read
confirmed `10.100.254.10`; management/RDP credential bindings were stored
privately. This is a Windows candidate, not an AD domain controller yet. The
file-server declaration `146412065b654f62b0a62e97e91ae8c6` created desired
`t254-mhl-file01` at .20, but no file-server VM was deployed.

During this work PVE reported Proxmox2 offline. LAN SSH, Tailscale SSH both
through P1 and from the operator workspace, and Tailscale ping all timed out.
The node's Tailscale peer was listed online, which did not prove reachability.
Project router CT113 and Linux APP/CI/BKP guests reside on this unavailable
node. Therefore their prior successful health checks are historical evidence,
not proof they are currently reachable. The node was not fenced or replaced,
and no host management address was modified. Physical/network recovery input
was requested from the operator. Routed WinRM/RDP and further deployment must
be rechecked after recovery.

Administrator publish job `65e9107574eb45d09dfea5faa98fedd6` converted sealed
builder `fce642d72e594756ae45a84158042266`, VM123/P5, to a physical template.
This removes its CPU/RAM commitment as a template, but does not prove immutable
publication or independent clone readiness. Its approved physical alias must
not be treated as a verified automatically distributable image.

The final targeted Windows/job suites passed 46 tests. Remaining work includes
Proxmox2 recovery, file-server deployment, AD/business configuration,
interactive Guacamole verification, generic immutable template publication,
complete-release capture/test, participant jumpboxes and the five-team rollout.

### Release service replay and captured container access — 2026-10-10

Proxmox2 remained offline on fresh cluster checks during the next continuation.
Independent source work found that `service_profiles` were captured in release
manifests but never applied by `release-apply`. New deployments now validate
recorded guest-recipe hashes before Terraform apply, configure only selected
teams, replay their known MHL business/Windows profiles, and then check health.
The recipe digest covers fixed guest playbooks and business role source files;
it contains no secret values. A missing/changed recipe requires staging service
configuration and a new capture. Historical profiles without the digest were
not silently upgraded or represented as newly verified.

Captured container references now retain their approved management account in
the pinned profile. Inventory and MHL configuration accept root management for
captured archive-derived guests only when the approved reference says so;
a host's self-declared login does not grant that provenance. Existing references
without this metadata retain their legacy user default.

The release, topology, capture-worker and MHL suites passed 36 tests for these
changes. The changes were deployed to the controller; actual team rollout is
still unverified because the project router and staging sources on P2 are
unreachable. Further release audit found that primary-LAN test clones currently
share staging with remapped addresses, while applications can retain old service
addresses. Isolated primary-network testing and recipe replay into its test
scope still need implementation and runtime verification. Generic immutable
publication of the new Windows physical template is also still unfinished.

### Primary release-test network isolation — 2026-10-10

The next continuation changed test preparation so machines originally on the
primary staging LAN receive a dedicated, release-owned test network. Their
original host octets are preserved inside that separate subnet; custom source
networks still receive independent copies. Production team attachment remains
unchanged. Allocation is stable on reuse, protects occupied/applied IDs and
uplinks, and cleanup remains ownership checked. This prevents a test clone from
silently reaching an original staging database through its old address.

The allocation/topology and CLI suites passed 33 tests. The declaration changes
were deployed, but no fake release was created to claim GUI runtime verification.
Service reconfiguration in the isolated test scope and custom-network bootstrap/
acceptance still need implementation and real tests after the router path is
available. The complete lab goal remains unfinished.

### Recovery and staging continuation — October 10

P2 is reachable again. Console operator confirms IP loss during the first
outage; host kernel records repeated e1000e hardware transmit hangs. A local
address-recovery guard with repeated-failure graceful reboot escalation and
a reversible TSO/GSO mitigation was deployed (see network audit).

All lab mutations in this continuation use authenticated dashboard POST
/api/jobs with the same operation/options payloads as UI controls. Staging
configure job 5ef7cf49d34847dbb1deeaab9d255e61 reached both routers but failed
because APP/CI/BKP containers remained stopped after the P2 reboot (onboot=0).
APP start 6b6c7e7f63524ad8bdeebad2fc49739e and CI start
e7a5823a18474821b44b7670eaf15ad2 succeeded. BKP start
a3f587a3dfeb4ef380e8c3d15df59ca6 was running at this checkpoint.

Stage review 5731740d430f47298087c746da219765 succeeded, plan
89ae2e09ff2bd3e04e39ba7fd8e5563a: one FS Windows VM create on P1,
three stopped CT starts and DC boot-order correction; no destroys or ordinary
team changes. Reviewed deployment 2bd09e6fc7214e48912c724bb1593e50 is queued
after starts; its success and subsequent service health are not yet claimed.

Checkpoint update: all three CT start jobs succeeded. Reviewed staging deploy
2bd09e6fc7214e48912c724bb1593e50 is running; Terraform created FS VM129 on
P1 and is applying the DC VM128 boot-order correction. Post-deploy configuration
is not yet complete. Linux business-service replay was queued after deployment
through the existing Team 0 GUI operation to refresh recipe-hash provenance.

Next checkpoint: first apply created VM129 but failed CT updates with generic
compatibility refusal. Linux service replay eadf624ac001423e87244fda09bb2258
succeeded for all four business CTs. Compatibility validation messages now
reach jobs and journal without logging request bodies/secrets; 9 tests passed,
source deployed and service restarted. No compatibility checks were bypassed.

Windows configure 977887c338aa454e859c0ea78058fe9b safely failed because new
FS placement metadata was not finalized after the partial apply. Fresh plan
0ab6e3f5546b4880a5a43d220c924b96 succeeded: no creates/deletes, only removal
of empty ide2 from VM129 boot order. Reviewed plan 50ff3e56e728077b94d3ca5e0ab98408
is being applied/configured through f7dc8c23e679422291ad90d32e5bd522. Observe
this existing job; do not restart solely for an observation timeout. VM128
and129 running was confirmed; VM128 QGA ping was not ready immediately
after restart. Further Windows initialization/domain services, health, capture,
fresh-clone test, participant boxes and five-team rollout remain unverified.

### Windows management runtime acceptance — October 12 continuation

Observed existing configure job 531cf557175744e6b1fea8c22a64fc6a terminal
succeeded. Both VM128 and VM129 passed authenticated wait_for_connection and
fixed Windows management baseline. Guacamole refreshed six business-server
connections. Earlier initial WinRM blank error was resolved by bounded
readiness and 60-second connection timeout, not by disabling encryption or
loosening source restrictions. Linux health job a5be6c8836cd4f47992afdcb4111f875
passed all seven declared ping/HTTP/PostgreSQL checks.

Started Team 0 GUI action mhl-windows-configure as
bf41bbe8250b489d9d55e1e443e935d9. Domain/file-service success is not yet
claimed; observe this handle before proceeding to capture/release acceptance.

AD workflow bf41bbe8250b489d9d55e1e443e935d9 failed before mutation because
Windows canonical inventory omitted crange_node_name required by the fixed
MHL applied-node validator. Generator now emits the actual Terraform placement
node, covered by Windows inventory test using a clone node different from its
source-template node. Inventory and MHL suites passed 11 tests; generator
deployed. Retry f3951f8ed8024804a97713c868e4cc36 passed that gate and is running
project-100-domain.yml (live Ansible PIDs630533/630543 observed).

Locally improved MHL wrapper prints fixed phase messages and combines Ansible
stdout/stderr before secret redaction on failures. Eight service tests passed.
This wrapper change is not yet deployed; wait for existing job terminal state.

### AD promotion and schema repair — October 12

Job f3951f8ed8024804a97713c868e4cc36 is terminal failed after successfully
renaming/rebooting MHL-DC01, installing AD/DNS, promoting the MHL forest, and
creating five OUs and MHL-Employees. Failure is the current microsoft.ad.user
groups argument schema (dictionary required, list supplied). All three user
tasks now use groups.add. Retry detects exact domain role/name/domain before
configuration and skips local-user rotation on a promoted MHL controller;
unexpected domain roles/forests remain refused. Fixed double separators in
member file-share paths and principals. MHL tests passed eight cases; both
playbooks pass installed-controller syntax checks using C.UTF-8 locale.
Deployed playbooks and fixed wrapper phase/combined-redacted-error messages.
New GUI mhl-windows-configure retry was submitted; promotion is proven, complete
user/GPO/file-service setup and immutable release acceptance are not yet proven.

### Complete staging Windows business services — October 12

4b10e52d14764df6915af252ceb3fe9a succeeded: MHL forest, service identities,
warehouse policy delegation, employee users, FS domain membership and backup
share. Expanded fixed role c5c0188fb24945a381bfa8bb2725d857 also succeeded: Dispatch/Finance/Operations shares, matching synthetic shipment/finance data,
readable recovery export, employee-writable operations consumer, privileged
5-minute dispatch export schedule, and legacy IIS intranet. The role exercised
the scheduled consumer and verified its SYSTEM SID, three exported rows and
actual employee Modify ACL. All mutations used GUI job payloads.

First stronger health job ab1c7df2d4fb43f0b12294492d138d81 passed DNS/LDAP/SMB
and IIS but incorrectly probed RDP from the router, which Windows intentionally
denies. Read-only RDP negotiation from Guac VM110's actual 10.60.0.10 address
passed for both Windows servers. Corrected router-origin checks were submitted
through Edit VM followed by Validate; full authenticated desktop sessions remain
to be verified separately.

Release audit gaps: current release-capture explicitly gates CTs, despite four
Linux CTs in this lab. Windows source identity validator permits only workgroup
bases with controller-only RDP source, so promoted domain snapshots need a
separate reviewed scenario-capture contract. Do not substitute stock archives
or select only Windows guests to claim a complete lab release. Isolated release
test recipe replay and 20 participant boxes/five-team rollout remain unfinished.

Corrected Windows business health job d6a553794601401498dbbcc72ee23f50
succeeded: DC ping/DNS TCP53/LDAP389/SMB445; FS ping/SMB445/IIS HTTP200.
RDP negotiation was separately verified from Guacamole as documented above.
Fixed ordinary-team Windows role lookup to use release_source_name rather
than impossible hyphen-token matching; regression covers shortened release
keys, actual team addressing, and rejection of an unrelated source name.
Nine MHL tests passed and helper deployed while the job queue was idle.
Complete immutable release capture/test/rollout remains unfinished.

### Container capture worker foundation — October 12

Added a journaled capture_container worker using existing managed-storage
capacity accounting, full source clone, disconnected probe-pool isolation,
fixed Linux identity reads, clean shutdown/template conversion, verified archive
export and independent source-probe gates. Policy now distinguishes VM QGA
requirements from Linux CT readiness rather than fabricating an agent flag.
Tests cover reserve-before-clone/isolate-before-boot ordering, rejection of
external mounts before allocation, shutdown after identity-read failure, and
independent-probe failure preventing verified state. Four CT and five existing
VM capture worker tests passed.

This foundation remains local and is not yet wired to GUI release capture:
the root identity reader currently accepts only static acceptance VMIDs, and
new container clones need verified machine-ID preparation before independent
clone acceptance. Keep the existing CT capture gate until those are implemented
and runtime tested. Domain Windows scenario-snapshot support remains a separate
required gap; existing base-publication workgroup identity policy must remain
strict. No release or image was falsely published in this continuation.

### GUI container capture integration — October 12

Capture now offers a staging-machine selector (all selected by default), backed
by distinct-key validation and existing CLI --machine scope. Added platform-admin
Cluster capture defaults control; job 36176259a49b49e0ad9b34c9cf7891ab audited
existing thin-LVM and shared archive storage and saved crprobe1450/local-lvm/
crange-library defaults for the four currently enrolled policy nodes. This did
not enable the capacity-excluded P4 node.

Deployed journaled CT capture, fixed owned-image identity reads, ownership-bound
clone-only identity reset, and physical linked-backing inspection. Root relay
requires authenticated worker UID, approved node address, exact capture/probe
marker, probe-pool membership, disconnected unprivileged guest, and no external
mounts/devices. It accepts no caller-supplied commands or paths. Forwarded reader
program now handles python -c without __file__; regression executes that program.

First GUI capture 3ff02b2b3a4e46ea9f01db01da6acd18 failed before guest mutation
on missing capture settings. Next a019770490124162a9fcf3aeb8918731 failed before
allocation because reservation omitted kind=container; source restart is guarded
by the capture finally block. Fixed and tested the explicit kind contract.
Retry 6ac737bf0ffe4a91bbfa72e8bd69aecc is running for selected APP CT125 as
acceptance release 0.0.1; protected receipt capture-p100-2f571d3a89d3 allocated
CT130/P2. This partial acceptance capture is not the complete lab release.
All full-release requirements, including Windows domain snapshot capture, remain
in scope. Continue observing this handle instead of blindly restarting it.

Company packet revision 0.2 is now fully in character in Markdown, HTML and PDF.
Removed team/project/range mechanics, simulation notices and fictional-contact
labels. PDF text was checked for those out-of-character terms. Platform guidance
and actual acceptance status stay in operator docs.

Capture reached CT130 identity preparation and successful template conversion;
LXC conversion returns null rather than UPID. Added verified-config completion
and exact recovery without re-cloning/re-converting. Original CT125 is running.
Resume 62e3dd8deb264c73971b55812edfa9d5 failed export on missing VM.Backup;
added reproducible admin deployment script ops/proxmox/capture-backup-permission.py
granting only that privilege on /pool/crange-probes to the range user/token.
Archive retry retains failed task history and requires terminal permission
failure plus absence of any new archive for the source, never an observation
timeout. Current GUI capture handle 88602ca487814e9ba7b8675dc08ad922 is running.

Archive export/checksum is verified at crange-library:backup/vzdump-lxc-130-2026_10_12-16_12_27.tar.zst. Fixed exact ledger reconciliation API and probe
CLI argument contracts. Added missing VM.Clone on the isolated probe pool via
the scoped deployment script (no unrelated VM permissions). Independent request
failed before receiving a task handle; capture recovery proved target absence
and complete source-clone task absence using the existing reconciler, preserved
its old receipt, and began a new linked-probe attempt. GUI job
6e7f0a0f20a54af48dadd7a6aa9a6eba is now running after reconciling old absent
probe capture-p100-2f571d3a89d3-probe. Full lab release remains unverified.

Linked probe CT20000/P2 was created from CT130. Fixed marker selection to prefer
the current crange-job marker over one inherited crange-capture marker; multiple
current job markers remain rejected. Exact linked backing is checked on the
host, not inferred from requested clone mode. Probe resume then rejected a
trailing newline added by PVE to its description; line-ending-only normalization
is covered by the existing CT resume regression. Current GUI job
6ac926cf6f76428ca2663ed9d759df35 resumes the same stopped probe rather than
allocating another. Its independent identity/readiness/cleanup result is pending.

### First GUI CT capture accepted — October 12

6ac926cf6f76428ca2663ed9d759df35 succeeded. Captured APP template CT130/P2,
verified archived filesystem/config checksum, and independently booted a linked
CT20000 probe. Physical thin-LVM origin was base-130-disk-0; probe identity and
readiness passed. Clone/start/stop/delete task receipts are all confirmed
terminal, and the disposable probe was removed. Published project-scoped image
p100-image-2f571d3a89d3 and immutable partial acceptance release p100-0.0.1,
integrity ffdb7de4a5612d46899a6ae08a216690ab78438b1e81f9f4ec935326e60bf0db.
This proves the selected CT capture path; it does not prove complete six-machine
release capture, cross-node archive restore, business-release fresh testing or
five-team rollout. Do not retire the image/archive while referenced by 0.0.1.

### Windows scenario snapshot preflight — October 12

Added separate crange_windows_snapshot.py contract, collector and clone
comparison. It preserves business hostname/domain/MachineGuid while requiring
a distinct PVE generation UUID and, for DCs, a changed directory invocation ID.
Base workgroup-source validation was not relaxed. Three contract tests passed.
Live read-only QGA preflight accepted VM129/member and VM128/DC after correcting
AD invocation byte-array conversion to GUID. Both expose generation-counter
devices. This is preflight evidence, not Windows capture/restore acceptance;
no Windows image was published in this continuation.


## Status before October 13 console verification

# Project 100 — current status

Updated October 12, 2026. Historical job evidence is in platform-progress.md.

## Verified staging environment

- Linux DB, application, CI and backup containers are configured. Seven declared
  Linux service checks passed.
- Windows VM128/MHL-DC01 and VM129/MHL-FS01 run on Proxmox1. AD forest mhl.test,
  company identities and warehouse GPO delegation are configured.
- FS is domain joined and provides finance/dispatch/operations/backup shares,
  matching synthetic exports, the tested scheduled consumer and an IIS intranet.
- AD DNS/LDAP/SMB and FS SMB/HTTP health checks passed. RDP negotiation from
  Guacamole passed; full authenticated desktop sessions remain to be checked.
- Company packet is in character; Markdown, HTML and PDF are revision 0.2.

## Current work

Complete immutable release capture. GUI now offers staging-machine selection
and administrator capture settings. CT130/P2 is the retained APP capture,
with verified identity preparation, conversion and central archive checksum.
Its independent linked-clone test passed, with verified physical backing, unique
identity/readiness and confirmed cleanup. GUI capture job
6ac926cf6f76428ca2663ed9d759df35 succeeded and published project-scoped image
p100-image-2f571d3a89d3 and acceptance release 0.0.1.
The partial 0.0.1 acceptance capture is not the full six-machine lab release.

Windows snapshot capture/export/catalog/clone identity integration is deployed,
with project-only approval and the workgroup-base contract preserved. GUI job
13bd2ed4b1c64d2f8f0124f57abd3bbe exposed a real capacity blocker: P1 has 11
committed guest cores against a 12-core limit. The source DC restarted successfully.
P1's full-copy disk commitment also exceeds its conservative limit. A pending
file-server allocation already represented by VM129 needs reconciliation.
Do not keep retrying local capture; resolve placement/capacity accounting first.

Dashboard reconciliation job 4a7ba2fa4a9b4995a6390c15f1811310 verified the saved
placements against Terraform/PVE ownership and terminal provider tasks, then
released the retained file-server reservation. The new project overview button
is **Reconcile applied reservations**; mismatched or active allocations fail.
Remote VM capture now selects an approved destination with full-copy headroom,
using a verified central transport backup and stopped restore before isolation.
GUI capture job 91949b7a12cd4f54953e62e8099ef2d0 failed without allocation:
P5 had CPU/RAM headroom but only about 5 GB of full-copy local-lvm headroom.
Live audit found its larger ssd-lvm is thick LVM. Administrator capture-settings
job 0cb1f95bcefe4086a804b7cb9b879bcd audited and approved that pool for VM
copies. Container capture retains approved snapshot-capable thin storage.
Storage audit/allocation permission is restricted to /storage/ssd-lvm; backup
permission is restricted to CRANGE source/probe pools, clone to the probe pool.
GUI capture job a638ffe8176f40fdb990c6807f797561 is now the live acceptance
attempt. This path is not accepted until independent clone verification succeeds.

## Required remaining work

- Finish container capture/independent clone acceptance and the full CT set.
- Implement and verify service-preserving Windows domain snapshot capture.
- Verify isolated fresh-release recipe replay, startup orders and health.
- Deploy the tested immutable release to five isolated teams.
- Provision ten assessors with twenty Linux/Windows jumpboxes and verify grants.
- Verify desktop access, isolation, reset/rollback and the documented assessment
  paths, then finalize the scope schedule and private operator evidence.

The lab is not production-ready until these acceptance checks pass.

Latest capture checkpoint: the initial central backup finished successfully with
the known MTU warning. GUI recovery job f28a6411da914c11b18d32cc005b4e0f
reused that frozen archive, verified its checksum and started a stopped restore
as VM131/P5. No duplicate source backup was created. The immutable snapshot
and independent clone are not yet accepted.

The restored copy initially failed to boot due to a QEMU10.1 machine pin on
QEMU10.0.2/P5. GUI recovery2ba5917ac05d4ad58f3e841f25e61c15 normalized
only the owned stopped copy to the supported same-family Q35 baseline. Its
provider start task succeeded. Source128/P1 was not reconfigured; live Windows
snapshot identity and independent clone acceptance remain pending.

Windows capture boot is verified but identity collection failed. GUI diagnostic
job05e170f550e243a6abe5b77f59e43fda confirmed a disconnected NIC, tentative
static address and DNS waiting to start. The new Scenario inspection action
starts/stops only the retained isolated copy. Private-bridge host isolation must
be enforced and verified before enabling NIC carrier. Current P5 bridge audit
found a link-local IPv6 host address, so link-up has not been enabled.

After full lab acceptance, create reusable agent skills for GUI-driven lab
design, creation, release testing/deployment, recovery and cleanup, based only
on the workflows verified here; never include runtime credentials.

Private bridge guard preparation passed on P5 through the GUI. The guarded
inspection now sees a valid IP and successful directory lookup. Current capture
retry391f328f9bfd47a2ab8d52bf8ac321ee uses that proven isolation/carrier path;
immutable Windows snapshot acceptance is still pending.

Additional authorized requirement: provide the Guacamole admin hypervisor
console access to lab VMs without opening guest RDP/SSH/VNC ports or changing
lab enumeration. Requests must go through Guacamole to controller authorization,
and retain Proxmox service credentials on the server. Console implementation
and end-to-end verification remain pending.

Windows identity collection and template conversion now passed for131/P5.
The central immutable export was last observed at91%; checksum and independent
clone verification remain pending. This is the first actual Windows snapshot
capture checkpoint, still only a partial acceptance artifact, not the complete
six-machine release.

Windows immutable export checksum is verified. Independent-probe admission
initially failed without retained allocation. GUI retry0609f23d8ab748ef86a6bfe5fba6cc70
now reuses the verified template/archive checkpoint; no new capture or backup
is being made. Clone acceptance and release publication remain pending.


2026-10-13 — Dedicated range storage workflow

Added administrator-only Cluster controls and fixed queued review/apply operations
for an unused HDD volume group on Proxmox5. The root helper pins its VG UUID,
protects 20% VG free space, journals creation before mutation, and refuses blind
retry when the exact owned pool is missing. It creates no filesystem and changes
no existing disks. Native snapshot COW uses tagged disposable 64 MB volumes;
only a successful proof and verified cleanup can enable this storage for linked
placement. The placement policy update preserves other limits and network proofs.

Seven targeted checks passed, including stale inventory, malformed requests,
non-admin project scope, uncertain-create retry, and unverified storage proof.
The first review job failed on an integer CLI argument before root execution;
that argument is now serialized and regression covered. Reviewed job
c9e9784823c847c6a74edaf64bb76c64 approved 512 GB in hdd-lvm as crange-thin.
Apply job 5875bd799a734cde83bffa6db1df6f37 was submitted through the dashboard;
its result must be confirmed before fresh-test staging resumes.

The first storage apply created the exact tagged pool and Proxmox storage, then
stopped before approval because LVM defaults to an inactive thin snapshot.
Recovery now checks the retained proof LV names, ownership tag, thin-pool
backing, exact 64 MB size and original bytes before explicitly activating the
snapshot. It does not recreate the pool or overwrite an unknown volume.
Two further tests cover inactive-snapshot recovery and refusal to write/delete
a foreign retained proof volume (nine storage tests total).

The native COW recovery completed and deleted both disposable volumes. Controller
approval initially hit HTTP 403 on administrator storage configuration; the
controller now trusts the pinned helper's exact configuration proof and audits
node storage availability. New storage also needs its own Proxmox ACL. The
fixed root workflow copies only the pre-existing PVEDatastoreUser grant for
crange-iac@pve and its terraform token from local-lvm to crange-thin, checks both
grants, and leaves all other ACL paths and roles unchanged.

Storage apply 2782f84fbebe4020bf5a3c5f9038c505 succeeded: 512 GB crange-thin
on Proxmox5 is active, native COW verified, disposable proof volumes absent,
controller storage grants installed and placement policy updated. The original
SSD-backed library and existing VM/CT disks were preserved. Fresh-release Stage
Changes has been resubmitted to verify actual image-cache and guest placement.


### Superseded detailed status evidence from October 13

## Current task

Deploy and test fresh clones of release 0.1.0 on its isolated test network.
Dashboard test preparation job `92127c0130664d728c54f6bc83796ebf` succeeded:
two VMs, four containers and custom LAN `p100c100` are declared.
Stage Changes job `50c9f796741a4b4685a2cca9ba7e5331` failed before resource
changes because this new LAN has no network acceptance. Standard staging LAN
`p100t254` is accepted on nodes 1 and 2.

The controlled network-foundation phase is implemented and deployed. Stage
Changes automatically reviews new staging routers/subnets before guest placement;
the existing Deploy control identifies the foundation plan. Actual reviewed plan
`b06ace3e8802ab89b198d3a239e6ed9d` contained only custom VNet `p100c100`,
a new 4 GB router on Proxmox2, and the required SDN reload. Existing guests
were unchanged. Router CT138 and its VNet were created by dashboard deploy
job `62e794c5370c478ead20c82d3157c703`.

That job stopped at apply completion because provider tasks were still visible.
Subsequent authoritative inspection found no active provider tasks. The gate now
waits up to 90 seconds for the reviewed allocations only; unrelated guest activity
cannot block completion. Recovery verifies the exact reviewed plan, unchanged
inputs, applied identity/node/pool/storage/interfaces and terminal tasks before
resuming configuration without reapplying Terraform.

Reconciliation job `b82a9c19b5cb4ff38a3a28823140671b` released the retained
allocation but encountered an unreadable historical administrator-owned review.
Recovery now skips those out-of-scope files without changing permissions.
Subsequent scoped configuration reached CT138 but SSH timed out. Read-only
hypervisor inspection confirmed Proxmox2 still had `10.0.0.102/24`; CT138 had
both intended addresses/routes and an active SSH daemon. Its log showed accepted
controller public-key authentication followed by a 25-second PAM wait because
`systemd-logind` failed with `226/NAMESPACE`.

The router playbook now recovers failed login management using a fixed
LXC-compatible service override before gathering facts. Working login services
are left alone; the container remains unprivileged and host networking is not
changed. Router SSH transport permits the initial PAM wait. Ansible's global
15-second keepalive was taking precedence over common arguments, so the generated
router connection-plugin arguments now explicitly provide the 40-second window.
The updated playbook passed the controller's actual Ansible syntax check.
Dashboard recovery job `510aa8bde41749cf86f6d251a48becee` succeeded. CT138's
routes, firewall, DHCP and IPv6 isolation are configured. Independent network
acceptance is now the active step.

The deployed custom-LAN verifier checks the complete project/parent/custom
router chain against live nodes, tags and interfaces. SSH preserves the project
router source, matching the managed firewall policy. The ordinary-placement
network gate remains intact. The tested two-router
probe implementation is deployed. It scopes temporary permissions to both the
parent and custom router and verifies cleanup on both, including partial failure;
this is required for Guacamole-origin SSH through a nested network. The planned acceptance
candidate is Proxmox3, with a verified Alpine VM image replica. The API's older
standard-LAN-only preflight was replaced with the same declared-LAN validator
used by the worker; HTTP tests prove a declared custom LAN is accepted and an
undeclared or unauthorized subnet is rejected.

Verification job `622b53b9175b4fcba26fa50f8c8ea931` stopped at capacity
reservation because the generic 2 GB probe exceeded available memory headroom.
GUI reconciliation `be29b6399e5843e8b3ceb7b8ead56b3a` verified owned cleanup
and reservation absence. Probe sizing now derives from the approved source,
with the existing bounded minimum/maximum and any approved explicit profile.
Proxmox reports the Alpine template memory as the numeric string `"512"`;
this is now strictly normalized before validation. No capacity limit or network
acceptance is weakened. The next probe uses 512 MB and one core. A subsequent Proxmox3 attempt stopped
on incomplete storage inventory and was reconciled by dashboard job
`2e214d7662db46db977420b07bf69dee`. Fresh discovery shows Proxmox5 with
about 12 GB of memory commitment headroom, seven cores, available local thin
storage and a ready Alpine replica; verification is moving there rather than
forcing an unavailable placement.

The first Proxmox5 probe exposed a budget mismatch: its reviewed Terraform plan
uses a linked clone, but the ledger requested a full-clone commitment window.
The network-specific reservation now matches the local linked plan, requires
snapshot proof, and retains physical free-space reserves and all normal quotas.
Ordinary VM capture cannot claim this exception without an exact owned network
scope. Tests cover shared/unverified storage, physical free-space failure and
unscoped VM rejection. The failed attempt was reconciled by dashboard job
`e01353fa476445499a19df10c957850e`. Verification job
`6a5c4b6c5db84390b9d45401f0a3c174` created the probe and passed controller
access and DNS. Guacamole-origin verification failed; the worker confirmed
owned guest, temporary-rule, Terraform-state and reservation cleanup.
The privileged relay still restricted VNets to standard team LANs, and its
controller trust pin had become stale relative to the matching deployed reader
copies. The custom-LAN validator is now deployed on both controller and
Proxmox4. Their SHA-256 and the root-private trust pin all match
`d641f867dd1096ea4833b436c78265316ee7ad486dcca65ba93e62aae339e432`.
Relay scope/peer/address tests pass. Independent acceptance job `6242e52a936f46b89fb6d1bea3c2ed62` succeeded.
Controller access, DNS, Guacamole-origin access, internet egress, team isolation
and VXLAN MTU passed. Owned guest, temporary rules, state and reservation
cleanup were confirmed. The worker then published exact `p100c100` acceptance
on Proxmox5, receipt SHA-256
`c2cea6dcde938cd418fcacbeb28f0a24ec2bf37bd74c5bfe68c19d75bed4a3e3`.
Placement on other nodes still needs its own exact acceptance. The current task
is fresh-release image preparation and review/deployment of the test cohort.





## Fresh-release staging capacity

The test LAN is now accepted on Proxmox1 as well as Proxmox5. Verification job
`ce23ff2351b849968d5c33651960ebd4` passed all six checks and cleanup, then
published receipt `2ec5ff4b0e59506b2a8503dca8d435c96536ea72e0fe8676dcc9d9915c09a5b6`.
Stage Changes job `e65d5c23e44146958dd2ac15ff2173e0` rejected the DC's
cache-plus-guest preparation on Proxmox5: roughly 205 GB required versus 86 GB
available after reserves. No guest plan was applied.

The planner and worker now budget serialized cache verification and future
deployment by their peak, rather than adding the disposable probe to future
guests and retaining its CPU/RAM/disks in subsequent forecasts. The published
base remains in the forecast. Physical reserve checks and worst-case cache
probe writes remain enforced. Regression checks prove multiple cache fills can
share a serial resource window, malformed demand is rejected, and existing
catalog/inventory objects are not mutated. These changes are deployed. Stage Changes job
`624be22aa8424cee843675c7c09eab08` checked the corrected forecast and still
stopped before changes: Proxmox1 has about 2.46 GB memory headroom for the 4 GB
requirement, while Proxmox5 has 86.4 GB physical disk headroom for a 134.4 GB
cache-preparation peak. Other nodes lack exact test-LAN acceptance, and
Proxmox4 remains excluded. No existing guest was resized or deleted. Next work:
controlled GUI capacity-reserve/storage preparation and broader exact network
acceptance, followed by fresh cohort deployment and scoped service replay.

## Host reserve controls and storage audit

Admin Cluster now exposes per-node host RAM admission reserves in GB and percent.
The queued workflow validates an enrolled online node, a 2 GB minimum, a 10–50%
fraction and the displayed policy revision. It preserves guest sizes, CPU/RAM/disk
commitment ratios, storage approvals and network acceptance. Policy writes check
for concurrent changes. The ratio editor also accepts valid capture/network
revision names while preserving its bounds and physical reserves.

GUI cluster refresh `0252c68ed6ba4f3f9efc780eb95e1feb` succeeded. The GUI
reserve job `3e47ac8252fc451382ae2fae8fe12281` set Proxmox1 to a 2 GB minimum
and 12.5% host reserve; reported headroom increased from 2.44 GB to 6.44 GB.
The updated code is deployed and targeted policy, command, authorization and
snapshot checks pass. No existing guest was resized or deleted. Staging retry `4b042864233c48499edf2563dae21aa1` confirmed the memory
constraint is resolved, then stopped before changes: Proxmox1 has one CPU core
of headroom for a two-core request, and Proxmox5's current local thin datastore
still has 86.4 GB available for the 134.4 GB cache peak. No guest size or CPU
commitment limit was changed. Next: add a controlled review/apply workflow for a
dedicated range thin pool on the unused Proxmox5 HDD VG, verify its clone backing
and storage capabilities, then let normal placement distribute the test cohort.

Read-only Proxmox5 storage inspection found HDD VG `hdd-lvm`, UUID
`owjdcT-tctf-scbC-Peiq-w3xw-De7r-yDrBkv`, entirely free at 931.51 GB with no
logical volumes. Its SSD VG remains separate, with the existing library and
Windows templates intact and 331.50 GB free. The dashboard's reviewed storage
workflow created a dedicated 512 GB thin pool (`crange-thin`) in the previously
unused HDD VG on Proxmox5. Apply job 2782f84fbebe4020bf5a3c5f9038c505 succeeded.
Native snapshot COW and deletion of both disposable 64 MB proof volumes passed;
the controller and its token have their existing datastore role on this pool.
Automatic placement policy now approves this local storage. Existing SSD/library
volumes, guest disks and commitment limits were preserved. Stage Changes for
the isolated fresh-release test cohort has been resubmitted; deployment and
service replay remain unproven.


Stage Changes 3871bdceaf1a4982841b3cce6f361ff6 passed storage eligibility but
stopped before cache preparation on the second Windows clone's RAM demand.
The 512 GB pool is usable; the remaining limit is aggregate queued guest RAM
plus existing observed host use. Read-only P5 inspection found approximately
16 GB total, 10 GB MemAvailable, 5.8 GB Proxmox used memory and no running base
templates. Placement is checking another node rather than weakening its physical
memory guard or changing immutable-release resource definitions.

A direct preview-versus-ledger comparison found a batch RAM accounting bug:
cache preview added full queued guest RAM to observed memory, then applied the
configured 2:1 multiplier again. Preview now subtracts queued nominal demand
once, matching the unchanged reservation ledger. CPU, storage, physical host
measurements, reserve settings and commitment ratios are unchanged. A regression
uses an 8 GB nominal admission window: both preview and ledger accept two 4 GB
guests and reject a third. All 13 cache-staging and 27 placement tests passed.
Only the scheduling module was deployed, after reviewing its 16-line diff.

Stage Changes 6fa13060662049e2ac0f2cc5ab2daccc admitted all six guests and
entered real cache preparation, then stopped at a legacy local-lvm-only worker
gate before provider allocation. Local thin-storage proof is now carried through
worker acceptance, catalog validation and cache activation/eviction. Container
backing inspection accepts the new P5 storage only after checking its exact
local thin-pool definition and actual snapshot origin. Other unverified stores
remain rejected. Cache Stage Changes also reconciles only locked, journal-free
pre-allocation reservations with matching archive pins; journals or published
generations prevent that automatic release. No archive or guest deletion occurs.

Release-test service replay is now inferred from immutable MHL profiles and
bound to exactly the six disposable test guests, their captured images, addresses
and owned custom LAN. Both Linux and Windows selectors reject original staging
guests even when they coexist in the same canonical inventory group. This
implementation is unit checked; actual service replay still awaits deployment.
Checks passed: 44 image-distribution, 8 image-cache, 10 cache-job, 13 cache-staging,
11 MHL service, 4 cohort-scope and 28 root-relay tests. The Windows static test
assertion had doubled a PowerShell SID/group separator; its expectation now
uses the actual single separator, without changing the captured recipe.

Stage attempt 1fac2373b56241329222761475a27300 exposed the retained admission
from the earlier pre-allocation gate failure: cache-4545afc31c0849b6889cd19aa3f2c52a
remained in reservation state running, with no allocation journal. Inspection
confirmed its exact project/image/destination. Recovery now includes that stale
reservation state under node/library/allocation locks; a live writer holds the
same locks, and provider writes require an ownership journal first. The regression
fixture now reproduces this actual running state, retaining any journal-backed
operation, mismatched pin or foreign project. Its 10 cache-job tests passed.

Stage Changes 0fc5de177abc4d8eaaf4d7bca5134881 released the exact journal-free
old reservation, admitted six cache fills and started DC restore VM139/P5 on
crange-thin. Journal cache-d9a4208b35fa4ad5a95b8f80455fe885 records the live
qmrestore task. This is actual provider progress; cache clone acceptance and
final reviewed guest placement remain pending.

Continuation verified the existing dashboard job remained running and advanced
DC restore to 17% (10.88 GiB read). No restore was restarted. A fresh provider
status check is used rather than treating the journal's task state as sufficient
liveness evidence. Additional service-replay checks require the captured source
IP and the declared captured image to match actual applied guest metadata; four
cohort-scope and eleven service-selector tests passed. Those two source modules
remain local until the active cache job ends; services are not restarted mid-job.

October 14 — DC restore VM139 and linked clone VM140 completed with terminal OK
provider tasks. Snapshot verification stopped before starting the probe because
it still required an old UUID ownership tag. The compact marker fix binds
crange-cache-140 to cache-d9a4208b35fa4ad5a95b8f80455fe885 in the durable journal
and root-verified description, without reintroducing persistent UUID tags.

Stage Changes can now resume only a stopped, owned Windows scenario-snapshot
probe from its unchanged archive pin/admission. It checks all original provider
tasks terminal OK, original cache and probe identities, native storage approval
and pool membership before verification. It neither restores nor creates another
clone. Only the exact owned disposable probe is deleted after verification, then
the cache is published and its reservation reconciled. Active/foreign objects
remain untouched. Two resume tests, 45 image-distribution, 29 relay and 13 staging
tests passed. Live recovery remains unverified at this checkpoint.

Windows recovery 58a4c82ce38d4cf98944df237d6e97d8 succeeded for existing
VM139/VM140: the stopped probe was verified and deleted, native cache139 was
published and its reservation released without another restore or clone. Stage
Changes then stopped at the first container cache. Inspection confirmed policy
only contains vm-archive-restore/local-linked-vm; CT capabilities are not accepted.

Added administrator-only template-container-cache-acceptance and the Cluster
“Verify container cache support” control. Project ownership is inferred. A private
runtime policy permits only one approved complete CT image and one verified
local snapshot destination for the isolated test; no global capabilities change
before successful restore/clone/physical-origin/identity/readiness/cleanup proof.
The published receipt binds image hash, job/template, storage and journal hash.
Existing policy limits and network proofs are preserved under the policy lock.
Three acceptance workflow tests (including failed restore/backing preservation),
10 cache-job, five cluster-inventory, 23 job-schema and 11 role/API tests passed.

First container-acceptance attempt 64d86b6766f64f39b174198b0482957d stopped
before mutation because the new preflight consulted a deprecated scalar node
flag. It now reuses provider_container_placement_verified, the same structured
provider/node/disposable-Terraform-CT receipt used by ordinary placement. Tests
use that current receipt shape. The isolated acceptance is being retried; global
container cache paths are still unaccepted until the actual test succeeds.


## Proxmox 6 enrollment (2026-10-15)

New member verified online at 10.0.0.106 with six-node quorum. Extended the
existing VXLAN peer/node sets, shared library/mirror client access and storage
registrations, fixed-address root relay, provider/node validation, GUI suggestions
and Cluster inventory to node 6. Management bridge/IP and existing guests remain
unchanged. Measured CPU baseline and thin snapshot backing passed. Fresh pinned
Terraform Debian CT creation/network readback/deletion passed. Immutable VM
restore + linked probe acceptance passed (retained VM153); CT restore + physical
linked backing/identity/readiness/cleanup passed (retained CT154). Dashboard
Cluster browser acceptance passed with all six nodes. Exact staging and five
participant LAN verification jobs are running/queued; admission remains scoped
to each successfully published LAN receipt. See proxmox6-enrollment.md for exact
job IDs, source archive permissions, capacities, and final acceptance results.


Proxmox6 enrollment acceptance completed: all six Project100 LAN receipts are
published; both VM and CT local-lvm cache checks passed. At administrator request,
the empty local-hdd VG became a 4,016 GiB local thin pool with 4 GiB metadata and
10% VG reserve. Native write-divergence COW proof and fresh VM/CT archive restore,
linked-clone identity/readiness/backing/cleanup checks passed. A transient P4
inventory API error after VM cache publication was reconciled without copying
again. VMID reuse across guest kinds is accepted for reservation cleanup only
with confirmed successful exact original deletion. Existing guests, management
addresses, quorum configuration and unrelated services were preserved. Project
100 release 0.1.1 capture/testing and participant rollout remain unfinished.


## 2026-10-15 — native desktop acceptance and recipe replay

- Actual native Guacamole HTTP-tunnel RDP acceptance passed FS01: rendered
  screen, no protocol error and read-only QGA confirmation of the expected
  active Windows RDP user session. DC01 fails with Windows `0xc000015b` after
  successful authentication. Delivery-plan review confirms ordinary-user DC RDP
  is not required: preserve its default rights and use verified admin hypervisor
  console access. Participant RDP acceptance targets assessment workstations.
- Capture job `d716569816bd4fb5a02772dcfcf43a17` progressed through verified
  BKP and CI images on Proxmox6 and began DB capture. Do not restart the worker
  or duplicate transport tasks while this job is active.
- Implemented local immutable fixed-recipe preservation and release replay.
  Capture checks the current staging recipe, fresh-test preparation preserves
  legacy source, and rollout forwards the release recipe SHA. 38 focused tests
  pass, including replay after source edits, tamper/permission/path rejection,
  stale staging rejection, selected-team rollout and capture worker coverage.
  Deployment and live archived replay remain pending until capture completes.

- Additional CLI, release topology and isolated test-scope regression checks pass:
  75 tests total across both test runs. Updated a stale test fixture to supply
  live node inventory while preserving its assertion that changed guest identity
  blocks every power mutation.
- Direct provider polling confirms DB CT159 restore on Proxmox6 is currently
  running; the observation is not merely a persisted dashboard status.


## 2026-10-16 — release 0.1.1 published, fresh test declared

- Capture job `d716569816bd4fb5a02772dcfcf43a17` succeeded. All six immutable
  captures were independently verified and original staging guests are running.
  Manifest hash and native inventory evidence are in
  `release-0.1.1-capture-acceptance.json`.
- Deployed only the three reviewed recipe-preservation modules with guarded
  source hashes, backups and idle-worker checks; worker and dashboard returned
  active. No guest playbooks or Windows logon policies changed.
- Dashboard `release-test-prepare` job `a3372f8b809248e49f2a5f456bd103be`
  succeeded, declaring six disposable test guests and one isolated LAN. Both
  fixed recipe archives match the immutable release fingerprints.
- Stage Changes `c3f358d849414bf0b33cb5738df661fe` is preparing only the new
  network foundation before guest placement. No participant rollout yet.

- Foundation plan `d9943ca7aab046a23934cb5b2a9740d5` mutated only the new
  test VNet/router, SDN revision and safe SDN reload resource. Stage Deploy
  `2f72d73dbfd249e2b0d5c4fc164e91e8` succeeded, including parent/custom
  routes, firewall, DHCP, IPv6 isolation and Guacamole refresh. The test router
  uses Proxmox6/local-hdd with the future-only 4 GB router default.
- Submitted fresh `p100c100` acceptance through the same typed Cluster action:
  P1 `2c4cfbb3a9e0448f8f2b72838a1f00b9`,
  P2 `83a055f247804e53b41b4f76703eb262`,
  P3 `fa89049aa23e48f1af03c6563c7093bf`,
  P4 `fad2ab2c02594df4bae701154723fa88`,
  P5 `7dc2a58e25f140dfb67c764902f90eea`,
  P6 `56296a2ec5854c0bb15a78c67dc6eecd`.
  These are pending acceptance, not six passing claims. No scenario guest
  placement will rely on the former deleted test router's historical receipts.


### Recreated custom router SSH identity and owned cleanup

The first three fresh custom-LAN checks could not manage temporary rules because
strict SSH rejected the recreated router's old cached host key at
`10.100.254.2`. The existing refresh code handled guest VMs/CTs but omitted
custom routers. Extended it only for an applied plan's newly created custom
router, checking applied VMID/node, project/custom tags, parent/downstream
bridges and both exact interface addresses before removing the obsolete pin.
Eleven SSH identity/configuration/recovery tests pass. The fixed source is
deployed; staging configure job `806c21f1bc814eeebb781beb13075d76` performed
refresh, and strict SSH/UFW readback now succeeds on parent and custom routers.

Cancelled remaining probes through the Jobs API. Reconciliation jobs
`cac2f7b201594771a918be6d27303120`, `1683eb199569427d93cc0ad962b5782a`
and `fe5fa3824c7a4e5684ef96c6b09f5432` all succeeded, confirming guests absent,
temporary rules absent, Terraform probe state empty and reservations released.
No failed acceptance receipt was published. A fresh batch is now being queued.

### Actual application assessment checks

Read-only staging HTTP requests confirm cross-customer record access and the
legacy report-query weakness against three synthetic records. The CI artifact
failed HTTP acceptance with 404/permission denied. The symlink file task's
ownership settings followed the link and reset the target group to root.
Added `follow: false` plus an HTTP verification task; YAML parsed successfully
on the controller. Atomic role deployment preserved old release recipes and
restarted no services. Dashboard business configuration job
`6f388022951145fc88f279cd87ad817f` is queued behind scoped staging maintenance.
A subsequent immutable release will carry this correction; 0.1.1 is unchanged.

- Scoped staging configure `806c21f1bc814eeebb781beb13075d76` succeeded.
  Updated business configure `6f388022951145fc88f279cd87ad817f` also succeeded;
  MHL-CI01 now serves its synthetic deployment bundle with HTTP 200. Repeat
  read-only application acceptance passes all three checks, recording only token
  presence and artifact hash, never the credential value.
- Retried P1 custom-LAN verification `00412548038c4dd39057a85ac89d606d`
  passed all six checks, confirmed owned cleanup, and published receipt
  `b97ac6c00b78805e088632d057373a7cea0a37dbf66208a58b8db79f4d1d0a08`.
  P2/P3/P4/P5/P6 fresh jobs are `66374ee4655d45579cf53e30f418f11e`,
  `7bd3e050898d416aaf0a44c328b166fd`, `95c94872ad4b4a408f6390076fb48cee`,
  `d5c0c88c5a9946a28ffdd9f22937ffea`, `8b1fd5dae6a24a9b96e714fc0d350316`.


### Compact controller GUI deployed

The explicitly requested GPT-6.1 Sol/high frontend agent implemented the
reference-driven compact refactor. Parent reviewed screenshots, requested
removal of redundant title eyebrows and consolidation of maintenance controls,
and deployed only the three frontend files atomically. Served hashes matched;
no worker or web service was restarted. Authenticated live testing exposed a
native disclosure-event race; the agent fixed synchronous state capture and
ignored stale detached events. The patch is deployed, and live rerender,
menus, project selection, running tasks, all ten views and 390px layout pass.
Evidence: `dashboard-compact-gui-acceptance.json`. No test created or cancelled
any lab job. The Guacamole interface was not changed by this controller refactor.

- P1/P2/P3/P5 fresh custom-LAN checks all passed, cleaned their probes/rules/
  reservations and published receipts. Exact evidence is in
  `release-0.1.1-test-network-acceptance.json`. P4 failed preflight because no
  approved ready Linux cloud-init/QGA probe template exists there. P6 declined
  before reservation because its custom router is colocated; the existing guard
  requires another node to prove the VXLAN hop. Neither produced a false pass.
- Submitted Stage Changes `0541ce4bfa8940dc997dc3a0348c5692` for the fresh
  six-server cohort, keeping normal automatic placement and accepted candidates.
- Final compact GUI live acceptance also verifies real placement review stays
  within a 360px scroll area with readable-width columns. Preserved both active
  job and idle indicator acceptance: `dashboard-compact-gui-active-jobs-acceptance.json`
  and `dashboard-compact-gui-acceptance.json`. No services/jobs restarted.


### 2026-10-16 — Guacamole native-admin login loop repaired

- `guacadmin` was enabled and its password was accepted. Browser launch query
  parameters (`data` and an empty `token`) survived interactive login, so route
  reauthentication overrode the freshly issued token and failed repeatedly.
  Guacamole's ban extension then temporarily blocked the proxy source address.
- Added success-only launch credential cleanup in `auth-launch.js`, preserving
  unrelated query parameters and the selected route. Initial dashboard tickets
  still override a cached session and are consumed before cleanup.
- Deployed only this authentication fix into the existing installed extension,
  with a JAR backup; restarted only the Guacamole web container. No lab guests,
  passwords, grants, controller services, or pending frontend redesigns changed.
- Live browser regression: before fix `[403, 200, 403]` returned to login; after
  fix `[403, 200, 200, 200]` remained signed in. The initial 403 is the pre-login
  request. Authenticated dashboard launch and reload both retained `admin`,
  removed credential query parameters, and returned only 200 token responses.
- Evidence: `guacamole-login-acceptance.json`. Native password login used the
  protected connector test account; guacadmin credentials were not reset.


### 2026-10-16 — Corrected isolated-test recipe and staging health gate

- Previous turn restored native Guacamole login and verified ticket launch/reload;
  no lab delivery acceptance was inferred from that infrastructure repair.
- The cancelled stage job `0541ce4bfa8940dc997dc3a0348c5692` is authoritatively
  interrupted. Its exact Proxmox5 VM162 restore task is now stopped/OK, while
  cache `cache-92286976fec94a8da363baebe8b5adaf` remains allocated/unverified.
  The reservation and artifact pin are retained; no duplicate restore was sent.
- Fixed the Linux replay scope: the worker now supplies `mhl_test_scope` only
  after exact applied guest/image/cohort selection. The four business roles
  accept a dedicated test LAN only with that flag. Ordinary calls still reject
  network 100; changed applied image identity also fails selection. Twenty-one
  focused service/scope/recipe tests passed, and deployed YAML parsed.
- Dashboard business configure `eb9c8e4c10914277abf9a2f202b6d465` succeeded on
  Team 0 with the updated recipe. Read-only application, IDOR, report-query and
  CI artifact proof still passed. Participant environments were not configured.
- Dashboard container edit `8e54c52a7fcc4214ab2bdd9a2de4486c` added the actual
  warehouse artifact HTTP-200 health gate. No guest resources or address changed.
- Dashboard cleanup `94646b7675774acbb434fdf87f578ee0` removed the obsolete
  0.1.1 test declarations/record. Its disposable router/VNet still require the
  reviewed destroy apply. Stage Changes `635377e3a4954e3e8de9a9e3f994e0d1`
  required zero cache fills and is generating that review.
- Capture 0.1.2 and full fresh-release replay remain pending; immutable 0.1.1
  images and archived recipes remain unchanged.

- Cleanup apply `ed8f52ccf40e4613bbcfe7775908a7a3` subsequently succeeded,
  including Team 0 configuration and Guacamole access refresh. Before capture,
  live inventory proved router 160 absent and all six staging originals running.
- Dashboard capture `5ae46494df98439192b79c9f29212a96` is running for release
  0.1.2, selecting exactly six staging keys and recording forward/reverse boot
  orders. Current output is checking source health; capture is not yet accepted.
  Preparation evidence: `release-0.1.2-preparation.json`.


### 2026-10-16 — Capture capacity gate and restored-cache dashboard recovery

- Capture 0.1.2 failed before export: central archive has 97.6 GiB available,
  below 70.4 GiB capture plus 39.3 GiB protected reserve. All six source health
  checks passed; DC128 was restarted by the capture finally block. Preserve
  its interrupted journal; expansion/reconciliation precedes retry.
- Added generic admin Templates recovery for a journaled, completed archive
  restore. It verifies source hash/pin, exact running reservation and approval,
  terminal successful UPID, no active guest tasks, range pool, fixed name/tags,
  stopped template, isolated NICs and exclusively owned local disks before
  finishing the interrupted metadata step. Established disposable-clone
  identity/backing/readiness verification still gates publication.
- The template is never restored, converted or booted again. Active/failed
  tasks, changed ownership/disks/network/pins and foreign platform scope fail
  before mutation. Thirty-six cache/job tests passed. The live browser button
  sends only its protected source job identity; no raw command options exist.
- Deployed the fixed worker/API operation and coherent dashboard assets,
  including compact menus, accessible icon actions and one custom tooltip.
  Worker/broker/web reloaded only when the queue was idle; Guacamole was not
  restarted. Initial readiness check raced web binding; a subsequent HTTP200
  and all nine source/served hash checks passed. Live acceptance passed all ten
  dashboard views, node expansion persistence, menus and 390px layout without
  browser errors or unintended mutations.

- Live Templates button submitted cache recovery `609d1dcbced24ccbb72ec633535ef1a3`
  for `cache-92286976fec94a8da363baebe8b5adaf`. It succeeded: template 162 is
  published ready/linked on P5, probe 160 is absent, all provider tasks terminal,
  reservation released and pin absent. No second restore was submitted.
  Evidence: `cache-restore-recovery-acceptance.json`.
- Added fixed online NFS archive growth to Cluster's Library & storage menu.
  Only CT145's exact SSD data disk is eligible, with fixed 64 GiB increments,
  original VG UUID, exact ext4 mount, host reserve and durable interrupted-growth
  guards. Native `pct resize` plus config/filesystem readback performs growth;
  there is no format, shrink, arbitrary command/path or service restart option.
- Design adjustment avoided changing the signed network diagnostic relay,
  which would invalidate existing network proofs. A separate root-owned,
  hash-pinned helper and peer-UID checked single-action Unix relay use the same
  protected controller namespace. All network relay hashes remain unchanged.
- Seventy-five library/relay/storage/job tests passed. Live dashboard growth
  `7215e99442154084af7cc0ec95985ba4` succeeded (400→464 GiB), without NFS/guest
  restart. Fresh archive status returned about 158 GiB available.
- Live UI execution exposed refresh closing open action menus before clicks.
  Fixed restoration only within the same view/project/team and unique menu
  identity; explicit dismissal remains closed. Browser regression forces two
  renders while open, verifies Escape dismissal, and submits exactly one fixed
  grow job. The ten-view action audit also passed without unintended mutations.
- Source inspection and exact job-identity lookup proved the failed capture
  created no receipt/reservation. Retried the same six-server 0.1.2 capture
  through authenticated dashboard payload as `552a9bd379524bf183fd4d1130b01c65`.
  The retry is running, not accepted.

- Post-growth readback confirms 149,778,595,840 bytes (~139 GiB) of physical SSD
  VG free space, above both reserve floors, and active NFS. The six-node
  short-deadline storage reads timed out during backup load; those observations
  are inconclusive, not acceptance or proof of inactive storage. NFS CPU was
  low while host full I/O PSI avg10 was 46.63%. No service restart was attempted.
  Retry capture progressed past capacity admission into stopped-DC backup.
  Evidence: `archive-growth-acceptance.json`.


### 2026-10-16 — Compact Guacamole interface integrated and deployed

- Integrated the frontend agent's compact grayscale theme and row interface:
  one row per real server, explicit addresses/protocol actions, project/team
  grouping, floating management menus, keyboard dismissal, snapshot focus
  handling, single custom tooltips and high-contrast native icons/settings.
- Rebuilt the extension with the previously accepted launch-auth cleanup.
  Installed JAR SHA-256:
  `cad0aaa58fcae5ae8e5bbab61c512fb3f3cfce740108dce73d044c4c4203ac35`.
  Backup: `/opt/guacamole/theme-backups/compact-final-1792176891.jar`.
  Reloaded only the Guacamole web container; the capture worker was untouched.
- Served JavaScript/CSS contain the new interface and auth fix; public root
  returns HTTP200 and unauthenticated management still returns HTTP401.
- Native connector password login and dashboard JSON launch/reload passed on
  the actual installed extension. The live dashboard launch shows exactly six
  current P100 staging servers with the six declared IPs. Floating-menu Escape
  and 390px layout passed, with no browser errors or unexpected control calls.
- Read-only live power check: Guacamole's DC row and its explicit authenticated
  status request both report stopped during capture, matching host `qm status
  128`. The other five rows report running. No power/snapshot mutation was
  submitted by these UI checks. Evidence:
  `guacamole-compact-interface-acceptance.json`.
- Stock-settings/login/logout fixtures, protocol links, pointer-gap traversal,
  mouseleave, snapshot focus, running/failed-job retention and title-free custom
  tooltip checks passed. Source and built artifact were synced to the controller
  repository without restarting controller services.
- Release capture `552a9bd379524bf183fd4d1130b01c65` continues independently;
  the stopped-DC transport backup reached 31% in the observed output. No claim
  of release completion or participant delivery is made.


## October 16 — Guacamole session isolation and container consoles

- Reproduced the reported failure: after a console ticket login the home page
  fell from eight protocol/console controls to two console controls. Native
  inventory returned 403 because the login context had become JSON-only.
  Guacamole's shared auth-token storage also allowed a console login to revoke
  the normal login used by other tabs.
- Ticket sessions now use tab-scoped token storage. A new launch ignores tokens
  inherited from its opener; reload retains its own token, and logout leaves
  normal login storage intact. URL credentials are still cleared only after
  successful authentication. Database-only sessions retain normal storage.
- Native inventory uses a database context actually available to the session;
  ticket-only sessions do not ask for an unavailable native tree. Protocol links
  match only connections in authenticated trees, across all available contexts.
  Server-side identity, applied grants and live guest ownership checks remain.
- Global textarea styling was exposing Guacamole's zero-size keyboard sink and
  offscreen clipboard sink. Targeted overrides preserve focus/IME functionality
  while keeping them invisible.
- Extended the existing admin-only VNC relay to Proxmox LXC console endpoints.
  Live browser clicks connected successfully to all four Linux containers and
  both Windows VMs. All six use VNC, not guest RDP. No guest listeners, firewall
  rules, power state, accounts or lab content were changed.
- Deployed extension SHA256
  `816eaee784f6dcb0935f2ea576e06919ce01c047b6f080721c2c0815ddd4fdae`;
  source/artifact copies in the controller repository match. Controller web and
  Guacamole broker restarted only while the ordinary job queue was idle.
- Three JavaScript regression suites and 20 focused Python console/bridge/native
  inventory tests passed. Live browser checks confirmed all 12 controls survive
  console launch, console reload, return home and native-page reload, with no
  bridge errors or JavaScript exceptions. Evidence:
  `guacamole-ticket-session-acceptance.json` and
  `guacamole-console-coverage-acceptance.json`.
- Rechecked release capture `552a9bd379524bf183fd4d1130b01c65`: it is failed,
  not still running. Existing source backup, restored owned template and capture
  journals must be reused after fixing the Proxmox template export regression.
  This UI repair does not complete the release or participant delivery.


## October 16 — native template-backup repair through Cluster

- Added the fixed admin-only node action **Prepare template backups**. Actual
  browser click on Proxmox6 submitted project 0 operation
  `template-node-backup-prepare` with only `node: proxmox6`.
- Job `fdd72f01edde47ad81af6879a011e19a` succeeded: installed qemu-server
  9.0.16 → 9.0.18, the exact pinned official package. APT simulation and full
  installed-package readback confirmed no other package changes. No guests
  rebooted. Only pvedaemon was reloaded on that node.
- The separate root helper relay preserves the signed network relay SHA
  `ab20e0da5496642ba27adf90aa113ca3869f32b24add046686181887aa9e5a8b`.
  The worker can choose an enrolled node but cannot choose a package or command.
- Added bounded export recovery for the exact confirmed terminal readonly
  SATA/IDE template backup failure. It verifies fresh installed-version state,
  task/node/VMID/owned base disk/log and absence of a new archive before retaining
  failed history and retrying the same immutable template. A newer available APT
  candidate does not qualify. Active/unpacked/foreign/changed/partial states deny.
- 33 node-preparation/job tests and 54 capture/transport/export regression tests
  passed. Deployed export helper without restarting services.
- Submitted existing Scenario capture payload for version 0.1.2 through the
  authenticated dashboard API. Current job is
  `a096e2912cca4949bd13b794c8852f07`; staging health validation is running.
  No claim of capture publication, fresh release-test or rollout acceptance yet.
- Separate Guacamole finding: SSH port 22 is reachable from Guacamole on all four
  service containers, but saved connections incorrectly use the jump-box `user`
  account, which is absent on the checked service container. Admin console access
  is verified; service-container SSH credential correction remains outstanding.

- Follow-up: exact replacement export task
  `UPID:proxmox6:00093D30:008DD6CA:6AD2AA41:vzdump:160:crange-iac@pve!terraform:`
  is running on the same owned template160. The previous failed export journal
  is retained as `capture-p100-44891a7a6389-export-failed-7dd5d8153991.json`.
  Installed qemu-server readback is fully `Installed` at 9.0.18; available 9.2.11
  was not installed. Export reached 15%; no repeated source backup or restore.
- Dashboard Remote access now stays beside Edit and power actions as a monitor
  icon instead of being folded into the overflow menu. Live read-only browser
  checks covered six Scenario rows, six staging team rows and two jump-box rows.
  The existing settings dialog still opens; no job or configuration was submitted.
  Static app deployment left the active capture worker PID unchanged. Evidence:
  `dashboard-remote-access-icon-acceptance.json`.


## October 16 — exact dashboard-to-Guacamole launch acceptance

The previous console/native-account tests did not cover a CRANGE dashboard
identity without a separate database account. Actual **Open Guacamole** click
as dashboard admin reproduced the reported banner: JSON-authenticated `admin`
was advertised PostgreSQL contexts but native verification returned 403
`Guacamole account not found.` Native `guacadmin` is a separate identity.

Ticket sessions now use their authorized JSON trees without assuming a native
account exists. Native password sessions retain native verification, and links
still require the authenticated connection tree. No accounts, grants, passwords
or backend authorization rules were changed. Source tests cover an advertised
native context without native verification, alongside native-session verification
and cross-context link matching.

Deployed extension SHA
`18f84841da70784785544b86c9deb72d175c91fa420179d306d1182a8a46a34c`.
Actual dashboard-link browser acceptance passed: six authorized protocol controls,
no banner or failed bridge requests, and reload remains clean. Three JavaScript
regression suites passed. Evidence: `guacamole-dashboard-launch-acceptance.json`.
Release capture remains independent; its same-template repaired export reached
100%, with archive checksum and fresh-clone acceptance still pending.

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

### October 17 evening: six caches and expired-review recovery

All six 0.1.2 caches passed independent identity/backing/readiness verification
and cleanup. APP template182 uses P3/local-lvm; backup template183 uses
P1/local-lvm. P3 network job `a0c578e9f7944e158866956a2573b838` passed all
six gates, cleaned up and published receipt
`050cdf4a9afaf4fa71baa4b8246026c3729843aff2a0590d14303858aef6b503`.
The old guest review `59a603af91b3efa2c7a12ff7f5b98550` succeeded with exactly
six creates, but expired during the disconnected session. Refresh job
`2fa551fa57dd48c6b6d73d08852310b8` exposed double-counting of that retained
reservation. No capacity limit was raised to bypass it.

Added a Stage Changes sweep of expired, unapplied, same-owner guest reviews
with identical input hashes, exact retained placement bindings, no applied or
matching provider guests, and no active provider tasks on their target nodes.
Its 59 targeted tests pass. Live job `627bb7431aac4962aa9c0d75cf9f6425`
released only the old six-guest reservation; interrupted Parrot cache jobs and
the separate old router review remain reserved. No live guest was changed.

### October 17 evening: fresh six-server cohort deployed

Review `627bb7431aac4962aa9c0d75cf9f6425` succeeded with zero cache fills.
The saved plan `80cc7d6e19a2dc5e6a57e2d9d56bba71` was independently inspected
with Terraform show JSON and verified to contain exactly six linked-clone
creates, with no other changed resources. Dashboard deploy
`9ef82c396c894cf4b35b4b08d46bd59a` succeeded, including router routes/firewalls,
container/root access, Windows snapshot initialization, authenticated WinRM
baseline and Guacamole synchronization for 12 managed server connections.

New guests: backup CT184/P1, app CT185/P3, DB CT186/P5, CI CT187/P5,
FS VM188/P5 and DC VM189/P5. All six originals124–129 remain running.
These IDs are current applied guests, not disposable cache probes.
Acceptance `bda8b4b9830e4db18db18b76517ad46d` now replays the release's
archived recipes on only the isolated test cohort and tests both boot orders.
Passing deployment is not passing complete service/boot-order acceptance.

### October 17: first fresh-service replay failure and input fix

First acceptance job `bda8b4b9830e4db18db18b76517ad46d` replayed the DC
recipe successfully, then the file-server local-account task rejected a
domain-qualified username. A newly created forest vault derived the shared
local-account variable from the DC's authenticated UPN. Existing staging had
an older bare username, so the mismatch had not appeared there.

The worker now passes a fixed per-host variable expression for the bare SAM
account name while retaining qualified WinRM principals and all bound private
passwords. It does not modify the archived 0.1.2 playbooks, manifest, images or
vault. Two new and eleven existing service tests pass; controller-local Ansible
also resolves different local account names correctly without contacting guests.
Deployed backup: `template-publication-checkpoint-1792281578`.
Retry `05ab81b98cf0400f869b86ab1c07d7ab` is running; full fresh-service and
both boot-order acceptance remain unproven.

### October 17 evening: Windows replay accepted; restored DB bind repaired

Acceptance `05ab81b98cf0400f869b86ab1c07d7ab` passed both archived Windows
plays, including all19 file-server tasks. It then found PostgreSQL15/main down
in CT186: its configuration still listened on captured10.100.254.30 while
the test guest owns10.100.100.30. The archived database role asserts an online
cluster before rewriting its listen address.

Added a fixed worker-owned PostgreSQL rebind preflight before the unchanged
archived business recipe. It uses only the already-validated applied DB inventory,
refuses ambiguous clusters, writes the assigned IP and starts/restarts only the
matching PostgreSQL unit. Missing packages/clusters remain for the normal
installation recipe. Failure prevents profile recording. Nineteen targeted
tests and controller-local Ansible syntax validation pass. Deployment backup:
`template-publication-checkpoint-1792282078`.
Retry `6d2f8aaf4f6f4f258828963089e738ef` is running. The immutable manifest,
archived recipes and images were not modified; full boot acceptance is pending.

### Release 0.1.2 Windows boot-health follow-up

All six archived service recipes passed in job `6d2f8aaf4f6f4f258828963089e738ef`. First-order checks passed 13/15; only Windows ICMP checks failed. Correct isolated-LAN DNS records were independently observed. That known-failed attempt was cancelled using the dashboard Jobs API. Current baseline adds router-source-only Windows echo access and a shared 600-second per-Windows-guest boot budget (180 seconds for Linux). Targeted readiness, service, Windows recipe and release-network tests and Ansible syntax checks passed. Source deployment backup: `template-publication-checkpoint-1792290461`. Retry `23d112ca76ae4908acb24c0b4aedf9c7` is running; DC recipe passed, file-server configuration in progress. Neither complete boot-order acceptance nor participant rollout is claimed.

### October 18, 02:35 UTC — selected release review

Acceptance job `23d112ca76ae4908acb24c0b4aedf9c7` passed both Windows and all four Linux service recipes and reached the first boot-order readiness checks. Windows 188/189 were running approximately two minutes after startup when observed; no boot failure is claimed before the shared ten-minute deadline.

Added fixed `release-stage` job, CLI command and **Review rollout** dashboard action. Placement projection contains only selected recorded release resources; Terraform targets include old/new recorded release resources, and actual plan changes are checked before retention. Saved plans record selected teams and apply checks that set. Existing unrelated declarations remain intact. Seven new tests cover ownership/drift, invalid team input, removed-resource targets, image preparation scope, and actual Terraform scope rejection; 65 existing rollout/staging/release-network/job tests passed (72 total). Live Chromium check passed for the deployed action and team form with no JavaScript errors and no job submission. Source backup `template-publication-checkpoint-1792290894`; dashboard restarted successfully with the active worker PID unchanged. Worker reload and actual release-review submission are deferred until the current acceptance job is terminal.

### October 18, 02:43 UTC — fresh-clone release acceptance passed

Job `23d112ca76ae4908acb24c0b4aedf9c7` succeeded. Both captured startup permutations passed 15 checks each, followed by 15 final checks. Verified per-guest counts with counters, including both CI HTTP checks sharing the `http:80` label. Independently read the controller result record `0.1.2-20261018T024238Z.json`, matching release ID and `passed`; SHA-256 `7a4581d5c4ecb6fe6ef1ee8ef9deb6253cf8af14d59b7fb20cd052e69073650b`. This accepts the six-server fresh-clone release, not participant rollouts/desktops. Worker was refreshed only after authoritative Jobs DB showed no queued/running jobs. Cleanup was declared via the fixed dashboard API; scoped Stage Changes review is running before any deletion.

Cleanup review `9c5dcdc1fb7f4748835c33eb1b19963f` succeeded. Independently inspected protected saved plan `a3d8017843d83cfd3f40d256222003e5`: exactly six disposable guest deletions (184–189), custom router177/P6, VNet p100c100, SDN applier replacement and SDN revision update. No original staging server or cache template changes. The quiet refresh ended with a warning that DC189 did not publish guest-agent network interfaces within the configured five-minute timeout; the plan itself succeeded. Reviewed Stage Deploy job `2373c915e564463888c0938251e3aa2c` is running, with explicit deletion/replacement flags for this inspected scope.

Reviewed cleanup job `2373c915e564463888c0938251e3aa2c` succeeded. Authoritative cluster inventory confirms IDs177/184–189 absent, originals124–129 running at their expected names/nodes, and caches178–183 still templates at their expected nodes. Guacamole access is applied with six connections. Captured evidence: `release-0.1.2-test-cleanup-acceptance.json`. Worker refreshed while idle after deploying the long release-apply timeout change (backup `template-publication-checkpoint-1792292061`; worker tests passed). Dashboard API release-deploy job `32634bfa69ea43079fa83bd8892808b0` declared 0.1.2 for all five teams successfully; exact new GUI payload release-stage job `d2f277eee7794f6ab2363523f79638b2` is now preparing/reviewing those releases. No participant release apply is claimed.

### October 18, 13:35 UTC — retained P6 file-server cache recovery

Five-team review `d2f277eee7794f6ab2363523f79638b2` failed after its qmrestore wait reached 1800 seconds. Retained task `UPID:proxmox6:00141111:01296182:6AD43871:qmrestore:185:crange-iac@pve!terraform:` independently returned stopped/OK, with task history showing no other operation on that allocation since restore. Source data read completed in179 seconds; total task start1792292977/end1792311660 was18,683 seconds. Final stopped template185/P6 has owned local-hdd base disks, isolated crprobe1450 NIC and the expected cache marker/name. Its cache journal is `cache-35457367952349e28ab18beff15b7065`; reservation and archive pin were retained. No archive restore was repeated and no ownership state manually changed. Used the existing dashboard action's exact API payload for recovery job `2e82ff28df9a4ca296dddae912f93112` (project0, template-cache-restore-resume, source_job_id matching that cache). This generic guarded recovery was already implemented in `crange_cache_restore.py`; the older P2-only helper is not the CLI handler.

DB cache `cache-3fa02cb812454d469fb12a50bfdc1857` is ready as template177/P6 and CI cache `cache-808a5f0d63a84aaa98976f6935429b7d` is ready as template184/P6. These IDs reuse freed test IDs; earlier cleanup absence evidence is historical and must never authorize deleting these new allocations. The local dashboard SSH forward reset during the interruption; it was recreated and the dashboard session refreshed before polling the same job. A root storage-warning read over P4-to-P6 SSH failed host-key verification; no host trust records were changed, and this optional diagnostic did not affect recovery.

Recovery `2e82ff28df9a4ca296dddae912f93112` succeeded without repeating restore. Independent catalog/journal/ledger/pin checks confirm P6 DB177, CI184 and FS185 ready, all reservations released and artifact pins removed; evidence `release-0.1.2-proxmox6-cache-acceptance.json`. Used the cluster-owned `/etc/pve/nodes/proxmox6/ssh_known_hosts` with HostKeyAlias proxmox6 for subsequent read-only diagnostics, preserving host-key verification and all trust files. No matching kernel warnings or uninterruptible tasks were observed; targeted clock, udev, LVM error logs did not establish a root cause. Same-boot journal realtime-minus-monotonic offsets agree across the interval, giving no evidence of a clock step. Thin pooldata1.38%, metadata 0.59%; storage fullness does not explain the stall. No live storage policy or guest was changed. Submitted exact existing GUI Prepare local cache payload for the next required DC image44891a7a6389 on P6/local-lvm (SSD), job `c42ecbfa46904dce9deedeb0593c4d17`, rather than immediately repeating the Windows restore on local-hdd.

### DC SSD cache, twenty workstation declarations and cache-pool UI

DC preparation job c42ecbfa46904dce9deedeb0593c4d17 succeeded in 1,042 seconds (created1792330739.1132288, finished1792331781.1157615), with verified P6/local-lvm cache186 and released reservation/pin. Runtime kernel stacks caught VMA's worker in fdatasync -> blkdev_fsync -> folio_wait_writeback. This corrects the earlier interpretation that the full post-100% wait was necessarily rescan/template conversion: buffered disk writes were still flushing. PVS/lsblk map local-lvm to pve on non-rotational sda3, local-hdd to rotational sdb. Sampled SSD throughput33.78MiB/s with roughly 12 GiB writeback, later 0 writeback pages; no host storage/cache settings changed. This is evidence of writeback latency, not a conclusive explanation of the earlier five-hour HDD duration.

All ten Windows custom-jumpbox jobs succeeded using the dashboard's /api/custom-jumpboxes payload and approved mhl-windows-server-2025-base-p100-v1 image. Desired-state verification confirms two Windows VM jump boxes per team on .12/.14, two dedicated Linux jump boxes on .11/.13, twenty declared workstations total. Deployment and desktops not yet accepted.

Added fixed cache phase output (checksum before allocation, submitted operation, disposable verification, completion, and explicit warning that100% read still awaits disk durability/finalization). Fifty distribution tests and Python compilation passed. Cluster cache UI now offers only eligible approved linked-clone pools, preserves selection across rerenders and checks readiness on the chosen pool. A live test exposed an unverified source template falsely disabling cache preparation; fixed readiness to require verified local linked-clone cache metadata. Actual Chromium clicks then queued APP664fe5e41eb1462ca5cbd97b8d300f46 and BKP3223f4038e884b13912e5a664b45455d on P6/local-hdd with no JavaScript errors. Evidence saved in cache-storage-ui-acceptance.json; GUI source backup template-publication-checkpoint-1792331647.

### Five-team plan accepted and applied through the actual dashboard

APP cache664fe5e41eb1462ca5cbd97b8d300f46 and BKP cache3223f4038e884b13912e5a664b45455d succeeded, yielding P6 templates 187/188. Independent catalog/journal/ledger/pin evidence now covers all six P6 release caches177/184/185/186/187/188.

Cache-preview inspection identified a real duplicate-copy issue: a larger empty pool could outrank a safe verified cache on the same host. Added a valid-capacity regression that fails on the previous planner. Preview now filters redundant same-node cache-fill candidates when a verified/already-planned pool fits; it still admits other pools when necessary and can balance to larger other nodes. All 16 cache-staging and 29 scheduling tests pass. Deployment backup template-publication-checkpoint-1792332288.

Actual selected-release review6c40a1f6d0624071a6df99bd9669ae9d succeeded with zero new cache fills. Inspected protected saved plan0d1d2fecd2c86b84ca3dab24b5fcf4c2: exactly 30 creates, ten VMs plus twenty CTs, six per team, all P6, all linked clones from verified parents177/184/185/186/187/188. No staging/workstation changes or deletions. Real Chromium opened Apply reviewed release, selected this exact plan and teams 1–5 with destruction/replacement both false, and submitted release-apply job83754c92f74c44d3b6fc6096806ccb25. Centered output opened; closing it did not cancel the running job. First DC VMs 189/190 created in 44/41 seconds. Completion and team health are not yet claimed. Evidence: release-0.1.2-five-team-plan-acceptance.json and release-0.1.2-five-team-apply-browser-acceptance.json.


### Five-team infrastructure completed; configuration-only recovery

Apply83754c92f74c44d3b6fc6096806ccb25 created all 30 company servers. Independent API identities and P6 root-LV origins confirm running guests and correct linked backing. Configuration failed because an undeployed Windows workstation placeholder had an address but null VMID; no corresponding applied placement existed. Inventory and Guacamole now filter unallocated guest outputs rather than treating desired addresses as live machines. Real allocated guest generation checks remain enforced.

Added release-configure fixed worker/CLI action and dashboard Continue configuration controls. Recovery preflights every selected applied identity, frozen endpoint, pool, running state, network, release ownership and archived recipe. It never runs Terraform and checkpoints each company's health before moving to the next. Seven new recovery tests pass; node syntax validation passes. The previous implementation checkpoint passed 94 targeted tests. Source deployed with backup template-publication-checkpoint-1792334273; ordinary worker/dashboard restarted only after no active ordinary jobs, broker retained.

Guac-refresh b9dac5d4da124840bab762590aa38e93 succeeded, applying exactly 36 real server connections. All 20 undeployed workstations excluded; ten participant accounts still have zero workstation grants. Actual Chromium clicked Continue configuration once, verified exact release-configure teams[1,2,3,4,5] payload and centered output popup, and closed it without cancelling. Job4c1bf68bab4d4fcabad30a5e94206f06 passed preflight and started Team1 configuration; health remains unaccepted until terminal evidence. Browser and infrastructure/root-origin evidence saved alongside this log.


Configuration recovery4c1bf68bab4d4fcabad30a5e94206f06 reached Windows initialization, then failed on windows-routes.yml's `when: item and ...`: a blank prior-target line evaluated to a string under strict Ansible. Replaced with separate boolean `item | length > 0` and nonmembership conditions. Deployed playbook without service restart (backup1792334634). Tested the actual installed Ansible engine against blank/current/obsolete target cases and syntax checked the real playbook. Initial test invocation hit the SSH environment's unsupported locale; reran with LC_ALL/LANG C.UTF-8 and passed, no guest mutation. Actual Chromium resubmitted Continue configuration as7ec764af704a4db4a7e44f5ac8d4a202.

Read-only full-project capacity preview identifies Windows base and Parrot P6/local-hdd caches for the20 pending workstations; full declared growth included. Retained Parrot cache169 has an exact terminal-OK qmrestore and stopped template ownership. Queued existing fixed dashboard recovery36db335306804229bb028baec32ac3a3; it created verification clone219 and is deleting it after verification (no duplicate restore). Windows base preparation56cdd1735d964caea7381fead177f623 was cancelled while still queued to let configuration retry go first; replacement queue entryf7f7d42b839e4fc892c0569690a7c71d follows7ec. These cache jobs are not deployment/desktop acceptance.

Parrot recovery36db335306804229bb028baec32ac3a3 is now terminal SUCCEEDED: template169 published ready, disposable probe219 deleted, reservation released. Retry7ec is actively running company configuration. Evidence: release-0.1.2-recovery-checkpoint.json.


### Large-project Guacamole launch repaired

Actual browser launch from the dashboard hit Tomcat HTTP400 Request header is too large after the applied connection set grew to36. The encrypted JSON-auth grant was embedded in the ordinary URL query. Verified the proposed fragment transport against the actual Guacamole client, then changed both project and console ticket URLs to use client route query fragments. The browser POSTs the grant to /api/tokens; it no longer enters the HTTP request target. A regression with100 granted connections asserts the large payload stays out of the HTTP query and all authorized entries survive encryption. Ten ticket/broker and seven console Python tests plus both auth-launch/session-storage JS suites pass. Deployed with backup1792335064. Broker restart was guarded by zero active broker TCP connections and left the active configuration worker PID unchanged.

The actual production dashboard launch now renders36 real rows, verifies all30 company IPs and running states, retains a nonempty All projects selector and survives reload with zero error banners or failed control requests. Evidence: guacamole-company-server-browser-acceptance.json. Five draft scope-register handouts were generated from the deployed endpoint evidence; no credentials or team terminology appear in the company-facing copies. Initial generation caught the file-server key's actual `file01` spelling rather than assuming `fs01`; corrected before writing the handouts. They remain draft pending delivery acceptance.


### All five companies accepted; workstation image remains in provider finalization

Release-configure7ec764af704a4db4a7e44f5ac8d4a202 succeeded. Independent desired/applied records show all five releases healthy with no failures and15 declared checks each, accepted from14:58 through15:54UTC. Actual direct guest status reads confirm30 running servers. The cluster resource summary omits names and reports unknown, so acceptance used fresh guest configuration/name and status endpoints rather than interpreting missing summary fields as stopped guests. P6 pvestatd is active; cluster remains quorate with6/6expected votes. See release-0.1.2-five-team-health-acceptance.json.

Native guacadmin actual console launch to10.100.1.40 connected over hypervisor VNC after the fragment ticket change. Native session survived console close and reload,36server rows and no errors. The earlier extra console click from dashboard identity admin found no Console control: console access intentionally uses the explicit Guac management-admin allowlist. It was a test-identity mismatch, not a missing connection. Successful native-admin evidence: guacamole-company-console-acceptance.json.

Windows workstation base jobf7f7d42b839e4fc892c0569690a7c71d failed its1800second wait, but exact qmrestore219 still RUNNING at18:30–18:35UTC. Protected journalcache-9d9d22dc04d24e349d31f75244fad7ad retained; no duplicate restore submitted. VMA thread waits on submit_bio_wait/blkdev_issue_flush under fdatasync; host has947757writeback pages, sampledHDD1.43MiB/s,190I/Oinflight. Read-only hardware inspection identifiesHPSmartArrayP420i(hpsa), both logical devices reportwrite-through. No controllercache/battery diagnosis yet, no unsafe writeback override. Use existing cache-restore recovery only after exact provider terminalOK. All20workstations still undeployed.


### Proxmox6 cache fault identified through HPE read-only diagnostics

The five-company HTTP/IDOR/query/artifact acceptance reader timed out on its first controller-to-APP SSH command (60s); no application acceptance file was created and no proof was marked passed. Existing service health is a prior accepted checkpoint, not proof of responsiveness under current I/O pressure.

Fetched pinned ssacli6.15-11.0 from HPE's official HTTPS repository, bounded32MiB download, extracted into a temporary directory without package installation and ran only `controller all show config detail`. SHA2569c296720f36be148316690de8b45c542a3c54f87e6fc32d2a93d1d4351aee2b5. Output identifies embedded P420i firmware8.00; CacheStatus PermanentlyDisabled, backup power source failed to charge, capacitor Failed(ReplaceBatteries). Both logical drives caching disabled, drive write cache/no-battery cache disabled. All8physical drives reportOK, no logical unrecoverable media errors. Temporary tool extraction removed. No cache policy, arrays, quorum, networking or guest power changes made. This establishes a real hardware cache-protection failure explaining the severe parity-write cost; restore timings can recur until hardware remediation. Recorded proxmox6-storage-cache-fault.json; informed user to replace the failed cache backup capacitor/power module. Continue exact provider task observation and lighter acceptance; do not force unsafe no-battery write caching.

Assessment audit also found that the published CI artifact's synthetic token has no demonstrated authentication consumer in the current recipe. Its accessibility was proven in staging; usable scoped-credential behavior remains an explicit acceptance gap. Do not count a token-looking string alone as a working credential or silently modify the frozen0.1.2recipe. A corrected recipe must follow the normal configured-staging/versioned-release path.


### Participant workstation grants saved through all ten actual user forms

All ten Users-page edits were performed with Chromium, selecting each participant's own dedicated Linux jumpbox and custom Windows VM. User1 receives j01/Linux and assessor-win-j02/Windows; user2 receives j03/Linux and assessor-win-j04/Windows. Teams grants remain empty, passwords unchanged, accounts enabled and password reset disabled. API reread verifies20distinct declared workstations across10accounts, exactly2peraccount. All ten guac-users jobs succeeded; actual effective connections remain absent until workstation deployment. Job IDs and actual save evidence are in participant-workstation-grants-browser-acceptance.json.

The live user list counted only `jumpbox:` connections, omitting custom VM/container jump boxes. Reused existing userJumpRows classification for its count. JavaScript syntax passed; remote baseline hash matched before deployment (backup1792349103), worker/broker untouched. Actual browser now shows2assigned jumpboxes for all10accounts. This is the fourth confirmed software bug fixed in the latest resumed sequence, separate from the discovered hardware capacitor fault.


### Guest account controls accepted through real forms and cleaned fixture

Initial fixed API list08591f4831f8405195b51e0930f09bde failed because account resolve used declared node_name=auto as a physical node. Reused the existing power/snapshot live guest resolver, including name, ID and network checks, so actual node migration is supported. A regression with stale placementP4/liveP6 plus foreign-bridge rejection passes; five guest-account and five power/snapshot tests passed. Source deployed with baseline hash guard and backup1792349331, no worker/broker restart. GUI/API retry list2da8c333ad084cfb9536367f1354b5c4 succeeded, original login-account list empty.

Actual Chromium used Team0 → staging APP row → Users for all fixture mutations. Create4a8302ec163c44a8b59cc4a3fc9f4a1e and passworde550924c5efb468287e6309c267f6a93 succeeded. Private shadow crypt checks confirm password1accepted initially, then rejected while password2accepted. Grant42b83a33a039424e846aba931e40d121 failed before privilege change: minimal image lacked sudo and /etc/sudoers.d. Grant action now ensures package and policy directory before validation; no global unattended sudo permission. Deployed playbook backup1792349552; retry2c8b3e6cc7474d96a78645e4c296a63b succeeded. An actual password-authenticated sudo id command returned root0. Revokeb93ce1b2d990456c968eb0b4fae765bf succeeded and the same command was denied. Deletebc77770d4c784f7e9dc3ce4f8429b26c succeeded; independent read verifies account, home and managed policy absent. Controller root-key access passed after each successful action. Secrets stayed in mode600 runtime fixture data and SSH stdin, not evidence or Git. The sudo package remains as the intended management prerequisite. Evidence guest-account-acceptance.json.

These are two additional confirmed software defects repaired in the latest sequence (six total since the resumed checkpoint), separate from the hardware cache fault. No claimed Windows/BSD guest account support. The six original staging servers remain; all account mutations targeted only t254-mhl-app01 and the uniquely named disposable user.

## Latest checkpoint — 2026-10-18 19:34 UTC

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

Remaining: workstation deployment/access, fresh 0.1.3 test and rollout,
assessment and isolation proofs, operational exercises, final cleanup and
handover. Agent lab-authoring skills come after full lab acceptance.
