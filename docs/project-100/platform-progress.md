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
