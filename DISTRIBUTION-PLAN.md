# Automatic distribution and node-agnostic range resources

Status: core implementation exists behind a disabled policy gate; not deployed or acceptance-complete. See [distribution operations](docs/distribution-operations.md) for implemented controls and remaining gates.

## Outcome

A project manager declares a VM, container, jump box, or range router without
having to choose its physical Proxmox node, datastore, or template VMID. The
controller resolves those details, reserves capacity, and presents the exact
placement in a reviewed deployment plan. The manager may suggest a node; the
scheduler honors that suggestion when it is eligible and has sufficient spare
capacity. Placement never consumes all advertised free resources.

Existing running machines retain their placement. Automatic distribution applies
to new resources and explicitly approved replacements. Moving existing machines
is a separate, reviewed migration operation, not a background balancing action.
Staging edits must not alter deployed teams or unrelated homelab services.

## Current constraints

The inspected cluster has five nodes, `proxmox1` through `proxmox5`, and no
configured shared datastore. Storage definitions are:

| Storage | Availability | Current purpose |
| --- | --- | --- |
| `local` | Local to each node; verify availability individually | ISOs, LXC archives, backups |
| `local-lvm` | Local to each node; verify availability individually | VM disks and container root disks |
| `ssd-lvm`, `hdd-lvm` | `proxmox5` only | VM disks and container root disks |
| `ssd-dir` | `proxmox1` only | Images, archives, backups, snippets |

Identical storage IDs on different nodes do not imply shared disks. Never mark
local storage as shared to bypass cloning restrictions.

Current code requires `node_name`, pins approvals/releases to `template_id` and
`template_node`, and hardcodes `local-lvm` in guest modules. The container module
requires the destination to equal the template node. VM/jump-box linked clones
also have template-node constraints. These are real constraints to remove through
verified image distribution, not by deleting their safety checks.

The Terraform provider is pinned to `bpg/proxmox` 0.104.0. Provider/API behavior
must be tested with that version before enabling each cross-node operation.

## Storage and template strategy

Use a central NFS library for immutable image artifacts, ISO files, LXC archives,
master VM images where supported, and captured release exports. Keep running
guest disks on node-local storage by default. NFS supports Proxmox template/image
content and QCOW2 cloning, but shared template storage alone does not establish
that every VM or captured-container cloning workflow works across nodes.

OMV is a possible library host, subject to checking its disk backing, capacity,
network throughput, backup coverage, and startup dependencies. Do not repurpose
existing OMV shares or disks. Create a dedicated range export and storage ID,
for example `crange-library`, after reviewing that configuration separately.
Because OMV runs within this cluster, avoid circular boot dependencies: OMV,
the controller, shared firewall, and recovery tooling must boot without the library.
Retain verified local copies of critical bases and backups outside this dependency.

### Image catalog

Expose logical, immutable image versions such as `debian-13-v1` rather than
physical template VMIDs. Maintain a controller-owned catalog with:

- Image ID/version, kind (`vm` or `container`), architecture and provenance.
- Published artifact checksum, build recipe/source revision and verification.
- Supported firmware, disk controller, CPU baseline, guest agent and network setup.
- Project/platform approval scope and retained release references.
- Verified replicas: node, unique cluster VMID, datastore, artifact version,
  verification timestamp, and readiness state.

A replica has a different cluster-wide VMID from its master and other replicas.
Do not try to register the same VMID independently on multiple cluster nodes.
Checksums identify published artifacts; do not assume differently packaged or
converted VM disks have identical raw-file hashes. Record the verified conversion
and source artifact identity when the physical disk representation differs.

For stock Linux containers, a shared OS archive can seed a fresh container on any
eligible node. For captured scenario containers, preserve the complete captured
filesystem and configuration through a verified export/restore or template-copy
workflow. An ordinary OS archive is not a substitute for the captured release.

Store immutable archived image versions on the central NFS library. Prepare a
verified template cache on a destination node only through an explicit audited
preparation job, then create linked clones on that same node and datastore by
default. Running guests and their local bases remain independent of NFS outages.
A missing cache must request preparation before producing a deployment plan;
a read-only preview must not materialize images. VM and captured-container linked
paths require disposable verification and snapshot-capable approved storage.
Stock OS archives seed fresh routers/containers using archive creation.

Retain every exact local base while any guest or snapshot depends on it. Do not
update a published base in place. Central archive availability, artifact identity,
complete guest/snapshot dependency discovery and ownership checks must all be
proven before reviewing cache eviction. An explicit administrator policy may
allow full local clones as a visible fallback; never label them linked clones.


Build replica preparation as an auditable fixed worker job. Validate source
ownership and destination storage, allocate an unused VMID, verify a disposable
clone, and publish the replica only after verification. Failed or partial replicas
remain unavailable to the scheduler and are cleaned up only after ownership checks.
Do not copy live Proxmox configuration files or mounted guest filesystems by hand.

## Node preferences and user experience

Replace the mandatory node field with:

- **Automatic** (default).
- **Prefer node:** optional dropdown of eligible range nodes.

The preference is a suggestion, not a placement requirement. Display the resolved
node, datastore, image version, and concise reason before apply. For example:
“Preferred proxmox4 has insufficient RAM headroom; placed on proxmox2.” A small
preference bonus may choose the suggested node among similarly suitable options;
it must never bypass capability checks, reservations, or headroom rules.

An administrator may additionally define a hard allowed-node set or explicit
pin for hardware-specific guests. Keep that distinct from the manager's soft
preference. If no allowed node fits, explain the limiting resource and stop.
Do not silently relax limits or select an unapproved node/storage.

Persist three separate concepts:

```json
{
  "placement": {"mode": "auto", "preferred_node": "proxmox4"},
  "image": "debian-13-v1",
  "resolved": {
    "node_name": "proxmox2",
    "datastore_id": "local-lvm",
    "template_id": 3012,
    "template_node": "proxmox2",
    "clone_mode": "full"
  }
}
```

User intent belongs in desired state; resolved placement belongs in the reviewed
plan and applied-resource record. A preview must not mutate the user's preference.
Deleting a node preference must not move an already deployed guest.

## Capability discovery

Collect a fresh, read-only cluster inventory through the scoped controller API.
For each node record health/maintenance state, physical RAM/CPU, observed load,
storage availability/type/free space, approved replicas, network bridges/VNets,
MTU, and required virtualization capabilities. Include all homelab workloads in
capacity accounting, even though automation may modify only range-owned resources.

A node is eligible only if:

1. It is online, not draining, and allowed by administrator policy.
2. Required image architecture, CPU/firmware features and guest type are supported.
3. Approved storage supports the intended disk, snapshot and clone operation.
4. Required range VNets and management/transit paths exist and work there.
5. A verified template replica or tested cross-node clone path is available.
6. Resource requests fit project quotas, global limits and node headroom.

Require tested VXLAN connectivity and MTU across all participating nodes. A router
and its downstream guests may be on different nodes only after that network path
is verified. Guacamole/controller access, DHCP, DNS, egress and team isolation must
work across nodes. Preserve Debian router firewall policy; OPNsense automation
continues to manage routes/gateways only, never firewall rules.

A VM using CPU type `host`, passthrough devices, or node-local external mounts is
not automatically migration-portable. Establish a supported CPU baseline for
portable templates after checking every eligible node; expose hardware-specific
requirements as constraints. Node-agnostic creation does not imply that every
running guest can be live-migrated.

## Headroom: leave wiggle room

Use administrator-configurable reserves, not “place wherever free RAM is largest.”
Initial conservative policy to validate against actual node capacities:

| Resource | Starting policy |
| --- | --- |
| RAM | Leave the greater of 25% of physical RAM or 4 GiB unused by scheduled commitments |
| Local datastore space | Leave the greater of 20% of usable space or 20 GiB |
| CPU commitments | Initially reserve 20% of logical CPU capacity; allow an explicit per-node overcommit ratio only after measurement |
| Provisioning concurrency | Initially one clone/restore per destination node and a separate library-wide transfer limit |

These are proposed defaults, not claims about current configuration. A small node
may need a different absolute reserve; an administrator can tune it explicitly.
Use workload demand limits in addition to vCPU counts: vCPU allocations do not
measure actual host load. Sustained high load makes a node less desirable and may
make it temporarily ineligible under configured admission thresholds.

For RAM, calculate usable commitment approximately as:

```text
capacity = physical RAM - configured host reserve
committed = configured RAM of existing guests + pending reservations
observed = current non-reclaimable memory use, normalized for platform overhead
available = min(capacity - committed, capacity - observed - pending reservations)
```

Avoid counting existing guests twice by subtracting both their configured RAM
and their resident RAM in one sum. Reserve stopped guests' configured RAM by
default because users may start them later; do not rely on ballooning or swap to
make a placement fit. Document and test how observed host overhead is measured.

For linked guests, reserve the complete declared virtual disk growth and normal
headroom by default; shared blocks reduce physical usage, not promised growth.
Reserve each local cache base once in its separate preparation job, including
full virtual allocation and transfer overhead. For explicit full clones, reserve
the projected destination disk allocation and temporary copy overhead. Do not treat thin-provisioned free space as unlimited. Budget image
replicas, snapshots, retained releases, and growth separately. If a source/datastore
cannot supply a defensible size estimate, stop rather than assume a tiny disk.

The scheduler may leave some nodes unused when there is insufficient safe
capacity. “Use all nodes” means all eligible nodes are candidates, not that each
must host an equal number of boxes.

## Scheduling and reservation algorithm

1. Validate declarations, authorization, quotas and image approval first.
2. Read a fresh inventory and existing reservations. Reject stale capacity data.
3. Separate already-applied resources from new/replacement resources. Freeze the
   placement of existing resources unless migration/replacement was requested.
4. Sort new guests by constrained capabilities and then largest resource demand.
5. Filter eligible node/storage combinations for each guest.
6. Score remaining combinations using projected RAM/CPU/storage pressure,
   provisioning load, verified local template availability, and a bounded soft
   preference bonus. Spread across nodes when scores are close; avoid unnecessary
   transfers. Use stable node/resource ordering to break ties reproducibly.
7. Account for every placement within this batch before placing the next guest.
8. Atomically reserve the entire feasible batch in a shared controller database.
   If another job changed capacity, re-evaluate rather than overbook. If the whole
   requested batch cannot fit, return an actionable error without partial apply.
9. Resolve template IDs/datastores and generate the retained Terraform plan.
10. Record placement reasons and projected remaining headroom for review.

Reservations include project, job/plan ID, resource key, node/storage, RAM/vCPU/disk
amounts, owner, expiry/heartbeat, and state. All infrastructure entry points use
the same ledger, including CLI and GUI deployment. Reconcile the ledger against
actual guests and active worker processes after a controller restart.

Do not expire a running operation's reservation merely because an observation
call timed out. A reservation becomes releasable when its operation is confirmed
terminal and its actual allocations are reconciled. Limit outstanding reviewed
plans per project/actor so abandoned reviews cannot hold capacity indefinitely.

## Plan/apply integrity

The retained plan pins image versions, verified replicas, resolved placements,
policy revision and reservation IDs alongside existing desired-state/source hashes.
Do not fingerprint fluctuating utilization as if it were an immutable input.

Before apply, recheck node health, required networks/storage, image identity and
reservation validity. If placement is no longer safe, fail with a request to
restage. Never silently move resources to a different node after the user reviewed
an exact Terraform plan. Release reservations for unapplied resources only after
confirming no provider-side allocation or still-running task remains.

If required replicas are missing, first show and run the approved template
preparation job, then generate the guest deployment plan from verified results.
Template preparation must not secretly mutate infrastructure during a read-only
plan preview. Resume failed provisioning using recorded ownership; do not create
untracked duplicate guests on a fallback node.

## Terraform, CLI, GUI and release changes

- Add a placement-policy/catalog module and a shared reservation store. Keep
  scheduling out of the unprivileged HTTP service; it requests fixed worker jobs.
- Change guest/router module inputs to accept resolved node, approved datastore,
  template location and clone mode. Remove hardcoded `local-lvm` where appropriate.
- Retain same-node clone preconditions until their replacement path is verified.
- Make CLI `--node` a documented soft preference, or introduce `--preferred-node`
  and preserve legacy `--node` as an explicit pin during migration. Do not silently
  reinterpret existing scripts' hard placement instructions.
- GUI forms default to Automatic, with an optional preferred-node field. Show
  placement previews, fallback reasons, reservations and headroom in plan output.
- Preserve separate Terraform state per real project. Scope all placement changes
  to the selected project/team, as existing staging and rollout guards require.
- Separate guest logical image references from physical replica IDs in approvals
  and release manifests. Preserve capture-node information as provenance, not an
  implicit destination requirement. Resolve the same immutable release onto any
  eligible node with a verified matching image.
- Support per-team placement preferences at rollout without changing the captured
  release. Record actual guest IDs/nodes in rollout history and status snapshots.
- Make inventory generation, health checks, capture, power actions, cleanup,
  Guacamole reconciliation, rollback and ownership checks use the actual applied
  node/guest identity. Never infer location from a global default such as node 4.
- Keep legacy manifest schemas readable. Treat old template-node constraints as
  real until a verified replica mapping exists; migrate metadata explicitly.

## Implementation sequence

1. Inventory all five nodes, storage pools, CPU compatibility and range networking;
   document administrator reserves and eligible nodes without changing services.
2. Add catalog schema and versioned artifact publication, preserving existing IDs.
3. Provision/review the dedicated library, backups and local critical-image caches.
4. Implement and verify central archived versions, local template caches and same-node/datastore linked VM and captured-CT paths.
5. Implement pure scheduling logic, headroom calculations and reservation races.
6. Add fixed preparation/placement jobs and reviewed-plan integration.
7. Resolve physical module inputs and add optional preferences to CLI and GUI.
8. Port capture/test/rollout/upgrade/rollback and inventories to logical images.
9. Run a small multi-node acceptance project before enabling automatic defaults.
10. Enable gradually for new resources. Keep existing placements and unrelated
    services unchanged; enable explicit migrations only after separate testing.

## Acceptance gates

- A mixed VM/container/jump-box/router project creates across at least two nodes
  using Automatic, without mandatory physical template or datastore choices.
- A healthy preferred node is used when suitable; an unsuitable preference gets
  a visible fallback reason; hard constraints fail instead of being bypassed.
- RAM/storage reserves survive a large batch and concurrent jobs. A stopped guest,
  template-copy operation, external homelab load and stale inventory are covered.
- Concurrent planners cannot double-reserve capacity. Restart, abandoned plan,
  failed clone, task timeout and partial provider allocation recover safely.
- Local clones keep running during a library outage; missing uncached images fail
  clearly. Recovery services do not depend on the library VM booting first.
- Image identities are verified and guest SSH/machine IDs are unique. Linked clone
  references prevent deletion of still-needed templates.
- Cross-node DHCP/DNS, Internet egress, isolation and Guacamole access pass.
- Capture, fresh-clone test, selected-team rollout, upgrade and rollback work when
  build, staging and destination nodes differ. Unselected teams stay unchanged.
- Capacity changes after review refuse apply instead of relocating silently.
- Project authorization, quotas, storage/image allowlists and lifecycle ownership
  checks still reject unauthorized operations. No arbitrary root commands reach
  the GUI, and no OPNsense firewall policy is changed.
- Existing project states and unrelated homelab services remain intact.

## Primary references

- Proxmox NFS storage and supported content/cloning:
  https://raw.githubusercontent.com/proxmox/pve-docs/master/pve-storage-nfs.adoc
- Proxmox VM templates, cloning, CPU compatibility and migration:
  https://raw.githubusercontent.com/proxmox/pve-docs/master/qm.adoc
- Proxmox container lifecycle and storage:
  https://github.com/proxmox/pve-docs/blob/master/pct.adoc
