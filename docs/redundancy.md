# Node-loss behavior and implementation status

The core node is Proxmox4. The target is to keep creating and editing labs with
any three of the other five nodes unavailable. Existing immutable images are
assumed to survive. Ordinary team jump boxes and deployed scenario machines are
not automatically moved or restarted elsewhere. Routers need fenced runtime
recovery as well as alternative placement during creation.

Only **Proxmox1** is restricted to router containers. Proxmox2 remains eligible
for ordinary workloads. This is an admission rule: it does not relocate or
replace existing guests. Installer VMs, scenario VMs/containers, jump boxes and
release-capture destinations honor the same restriction. Cache target planning
filters restricted destinations before reserving cache-plus-guest capacity.
Approved image storage and existing image ownership are not rewritten.

## Implemented and tested

- `router_only_nodes` restricts new workload admission, including explicit pins.
  A VM cannot evade the rule by using a router-shaped resource key. Existing
  placements remain fixed. Default configuration restricts only Proxmox1.
- Automatic router bootstrap treats accepted nodes as alternatives. An offline
  or draining unused candidate no longer prevents selection of an online one.
  Pinned routers still require their exact destination to be available. A saved
  plan never silently moves an allocation after review.
- Offline capacity discovery has a root-owned, peer-authenticated shared-config
  reader for every non-core cluster node. It verifies quorum, exact guest
  membership, offline state, configuration digest and tags before/after the read.
  Only CPU/RAM/disk limits and identity metadata are returned, including for
  unrelated services; raw configuration and credentials are withheld. Historical
  snapshot sections do not replace the active resource limits. Repeated low-level
  LXC mount options are ignored; duplicate actual capacity fields still fail.
- Cache target planning uses immutable published image CPU/RAM sizing before
  reading an online replica. An offline original template host does not block a
  cache fill when the archive and verified sizing remain available.
- Apply admission/completion are bound to guest resources in the reviewed binary
  Terraform plan, including unchanged dependencies and explicit deletions.
  Untouched guest state is fingerprinted and must remain identical. Reservations
  outside the reviewed scope are rejected. Existing placement/image records for
  unrelated guests remain intact without contacting their offline hosts.
  Guest modules depend on SDN bridge application, not every other router module;
  Ansible still enforces router configuration order. A local, unrefreshed
  comparison selects only changed staging targets; the final saved review uses
  normal provider refresh. An empty comparison retains the staging VNet target
  instead of accidentally planning the whole project. Live Project 100 review
  succeeded with Proxmox1 offline, no resource changes and all 50 existing guest
  states untouched. This is planning acceptance, not creation/failover proof.
  See [staging review evidence](project-100/redundancy-staging-review-acceptance.json).
- Ordinary project review now uses the same two-step approach: a local comparison
  selects only fixed project resource addresses, then a normally refreshed saved
  provider plan reviews those changes. Bootstrap and selected-release boundaries
  remain separate. Existing VM boot order is retained when a base profile advances,
  avoiding unrelated reboots. A live dashboard review proposed only 13 missing
  workstation creates and preserved all 50 existing guest state fingerprints.
  Its apply is still in progress; this is not guest health or failover acceptance.
- Six-node quorum is modeled with two votes for Proxmox4 and one for every other
  member. Seven expected votes require four present votes. Proxmox4 plus any two
  other nodes has four, so all 26 combinations of zero through three non-core
  failures retain a majority. No two disjoint partitions can both reach four.
  `crange_redundancy.prepare_quorum_config` changes only the core vote and config
  version, rejecting conflicting quorum mechanisms. It never installs its output.

Corosync supports node vote weights and live changes to `quorum_votes`.
[Corosync votequorum documentation](https://manpages.debian.org/trixie/corosync/votequorum.5.en.html)
The configuration is prepared, tested with `corosync -t`, backed up, and installed
through a separate privileged operator action with an unchanged-file check.
No expected-vote override, network edit, forced node shutdown or service restart
is needed for this vote change.

## Remaining work — not yet established

1. Scoped staging creation, configuration, health checks and cleanup now passed
   through the normal dashboard API/queue with Proxmox1 offline. Both reviewed
   plans changed only the disposable container; all 50 existing guest states
   stayed identical. Deleting it revoked its Guacamole firewall permission and
   removed its native connection. The temporary manager and sessions were also
   deleted. This establishes the observed single-node outage, not every failure
   set. See [deployment evidence](project-100/redundancy-staging-deployment-acceptance.json).
2. Retry placement and cache preparation after a destination is lost during
   creation. A fresh review is required for a different target; uncertain provider
   writes must be reconciled rather than replayed. Existing team machines stay
   outside this recovery scope.
3. Configure router runtime recovery with storage accessible to surviving nodes
   and verified fencing. Never start a second router with the same network
   identity merely because its previous host cannot be reached. Current cluster
   HA has no managed resources, and an unreachable previously unmanaged node
   cannot be assumed fenced.
4. Verify creation, editing and router recovery through the normal web workflows
   under the required outage sets. Scheduler simulations and vote arithmetic do
   not establish that full end-to-end behavior. Resource capacity, accepted
   networks and surviving images remain requirements on each chosen target.

## Router runtime recovery prerequisites

Proxmox HA supports containers, shared storage and watchdog fencing. Use the
native fencing authority before allowing another instance of a router identity.
The current `crange-library` NFS server is on Proxmox5, so it is not sufficient as
the sole live router disk store under a three-non-core-node failure requirement.
Router disks need storage on the designated surviving core, or another store
with equivalent verified availability. The operator-only
`scripts/setup-core-router-storage.py` installed a dedicated 96 GiB thin volume
and NFSv4.2 export on Proxmox4, with a fixed six-host client allowlist. It preserves
existing volumes and refuses uncertain retries. All five online nodes passed
shared read/write checks; Proxmox1 remains untested. See
[storage evidence](project-100/router-core-storage-acceptance.json).
Only a disposable, network-isolated container is enrolled for migration testing.
No production router disk has been moved or enrolled, so production runtime
recovery is not yet established.

Provider v0.104.0 also makes a container's `node_name` replacement-only, and reads
its configuration through the node recorded in Terraform state. Native HA
relocation therefore needs a separately verified state-adoption path before a
subsequent Terraform plan. Adding HA alone could otherwise cause an accidental
replacement after recovery. Preserve exact VMID, project/router ownership,
shared disk identity, state lineage and unrelated resource state during adoption.
Ordinary team workloads remain excluded from this recovery/adoption mechanism.

Sources: [Proxmox HA documentation](https://github.com/proxmox/pve-docs/blob/master/ha-manager.adoc),
[pinned provider container implementation](https://github.com/bpg/terraform-provider-proxmox/blob/v0.104.0/proxmoxtf/resource/container/container.go).

## Rollback and operational evidence

The live quorum review, original configuration, tested candidate and verification
receipts are retained under `/root/crange-resilience/quorum-review` on Proxmox4.
A rollback must advance `config_version` beyond the currently installed value;
never copy an old lower-version backup over the live file without review.
The owner could confirm only that Proxmox1 is unreachable, not physically off.
Its existing unmanaged router identities remain untouched until positive fencing
or a verified shutdown; loss of connectivity is not fencing evidence.
A returning Proxmox1 must synchronize cluster configuration before it is trusted
for any future router recovery. Changing the node count requires reviewing the
voting model again.

Run `python3 -m unittest discover -s tests -p test_redundancy.py` for the role
admission and 26-case placement/quorum models. These tests do not power off nodes
or claim runtime HA acceptance.
