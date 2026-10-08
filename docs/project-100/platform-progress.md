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
