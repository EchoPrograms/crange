# Router HA automation — October 20, 2026

Proxmox native HA owns router recovery and fencing. Crange enrolls newly created
project, staging and team routers, then follows verified HA assignments in its
Terraform state and placement records. Team VMs and application containers
remain on their assigned nodes; they are outside runtime failover scope.

Proxmox4 remains the required core, as requested. It hosts the controller and
`crange-router-ha` NFS store. Losing that core is outside this design. Recovery
restarts a router container on another eligible node; it does not preserve
active connections through that restart.

## Creation and recovery

After a reviewed Terraform creation completes, the worker requests enrollment
using the exact project, router key and completed plan ID. A protected core
operator verifies project ownership, state lineage, the creation receipt,
unique native guest identity, independent storage, and eligible worker mounts
and network bridges. It stops that same container, moves its root disk to the
shared store, starts it and performs a scoped provider refresh. It then enables
native HA with one local restart, up to four relocation attempts, strict worker
node affinity, and failback disabled. No duplicate router is created.

Root-owned registrations bind each router's VMID, network/disk identity and
Terraform lineage. A 30-second controller timer adopts only an exclusive,
started native HA assignment on an online eligible node. It checks fresh quorum,
HA manager/LRM evidence, pool membership and exact guest configuration before
updating the node in state. Unrelated resources and image bindings are retained.
State pushes remain locked and preserve the original lineage.

Project locks prevent adoption during an infrastructure operation. Completed
creation plans retain a pending-enrollment receipt if enrollment fails. The
observer retries that exact enrollment. Native enrollment interrupted immediately
before registry publication can resume from its own protected journal. An
uncertain disk move or task submission requires operator inspection of the
journal; the system does not blindly replay it.

## Returning nodes

Native HA controls fencing and avoids two active copies when a host returns.
Crange never infers fencing from a failed ping or starts an old local copy.
Failback is disabled. Proxmox1 is accepted as a source for converting its original
running routers, but remains excluded as a recovery destination. Eligible
recovery workers are Proxmox2–6.

A returning worker omitted during enrollment is admitted only after checking its
exact shared NFS mount and required project bridges, including UP state and MTU.
The operator extends its existing strict affinity rule using the current digest,
then publishes the matching protected registration. An interrupted publication
can complete only when the rule is exactly the verified extension. An unhealthy
worker is left out for a later retry.

## Live acceptance and limits

Disposable Project253 was created from one reviewed plan containing nine creates
and no replacements or deletions. Its project, staging and Team1 routers
(242, 241, 240) enrolled automatically with shared root disks. A native HA
relocation moved CT240 from Proxmox6 to Proxmox3. The scheduled observer adopted
the node automatically, preserving lineage and image metadata.

The returning-worker test removes only Proxmox6's eligibility for disposable
CT240, then checks that the scheduled verifier restores it after validating its
mount and bridges. It does not take a physical host offline. Physical watchdog
fencing and an actual host-loss/rejoin cycle have not been exercised.

The fixture's later Ansible router configuration encountered an SSH timeout.
HA enrollment and assignment adoption succeeded independently; this fixture is
not evidence of complete network-policy acceptance. Its infrastructure is
removed through the normal validated project lifecycle after HA checks.

The live OPNsense connector uses the existing lab's self-signed TLS exception,
as its prior route workflow did. Automation changed static route/gateway records
only. No firewall rules were changed.

## Operations

- Status: `systemctl status crange-router-ha.timer crange-router-ha.service`.
- Last observation: `~crange/.local/state/crange/router-ha-status.json`.
- Protected registrations and policy: `/etc/crange/router-ha/` in the controller.
- Core conversion journals: `/root/crange-resilience/router-ha-enrollment/`.
- Controller state-adoption receipts: `~crange/.local/state/crange/router-ha-reconcile/`.
- An operator can review an existing running router on Proxmox4 with
  `router_ha_operator.py review PROJECT ROUTER_KEY`, inspect the returned exact
  plan, and apply it with `apply PROJECT ROUTER_KEY --plan-id PLAN_ID`.
  Never retry a submitted conversion without checking its protected journal.

The implementation uses native [Proxmox HA recovery and fencing](https://github.com/proxmox/pve-docs/blob/master/ha-manager.adoc).

All seven live Project100 routers are now on shared root disks and started under
native HA. The original Team1 and Team5 containers (120 and 121) were converted
on their healthy Proxmox1 source; native affinity relocated both to Proxmox4,
and the observer adopted both assignments. No replacement identity was started.
An exact Debian12 Systemd252 nesting advisory was reconciled against its
completed start task and running guest; other warnings still fail closed. No
nesting feature or guest security setting was changed.
