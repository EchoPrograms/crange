# Distribution implementation and activation

The automatic-placement code and Cluster GUI were deployed to the controller on
2026-10-05. Automatic admission is enabled for a deliberately narrow pilot:
the protected policy allows only `proxmox1`. Other cluster nodes have not been
admitted. This does not establish multi-node placement acceptance.
The live policy `alpine-p1-pilot-v2-20261005` sets CPU commitment to 3.5 times physical
cores, RAM commitment to 2 times physical RAM less host reserve, and verified
local linked-disk commitment to 1.5 times physical disk less reserve. These
limits do not override live physical-pressure or image/network gates.

The five-node inventory refresh succeeds, and the verified network scope is
`p003t254` for this pilot. A temporary-network check on proxmox5 used disposable
VMs 8824 and 8825. DNS, Internet egress, team isolation, controller SSH, and the
1450-byte MTU passed. Guacamole SSH to undeclared address `10.3.254.98` was
blocked by the existing router rule, so proxmox5 has **not** been admitted by
policy. Both probe VMs were removed after the checks. These results are partial
network acceptance and do not widen the current P1 policy.

Detailed storage discovery for NFS-backed storage has a latency issue. Until its
detailed capacity, content, and locality query completes successfully, that
storage is treated as unavailable for placement; summary storage data is not
sufficient evidence for admission.

The protected acceptance record at
`/var/lib/crange-distribution/acceptance/alpine-p1-20261005.json` supports the
published catalog image `alpine-3-24-20261005`, backed by VM template 8820 on
Proxmox1 `local-lvm`. It records the archive restore and validated local linked
clone path. Project 3's retained infrastructure plan reported **no resource
changes**. Acceptance is limited to this image, node, storage and network scope.
The existing catalog record still has `captured_source_identity: false`, so
source provenance is not fully captured even though the image is approved for
this restricted pilot. The deployed fixed identity-capture workflow documents
how to close that gap; do so before broadening catalog approval or placement
scope. Live policy and catalog were not changed as part of that deployment.
Proxmox2 storage API discovery has been intermittent; placement safely falls
back by excluding it when current storage evidence is unavailable. Proxmox4's
configured commitments exceed its conservative admission window. Proxmox3 and
Proxmox5 have not been admitted. Proxmox6 remains a future optional node and
has not been enrolled or changed.

The isolated Parrot provider probe VM 8819 was removed on 2026-10-05 after
confirming its exact disposable name, tags, bridge, and owned disks. Source
and cache templates 112 and 8818 were retained. The probe booted with SSH
and RDP active, but four optional services left the guest in a degraded
systemd state; that is not yet a publishable readiness proof.

An existing Alpine 3.24 VM archive from Proxmox2's source template 8809 was
restored onto Proxmox1 as owned template 8820. Two disconnected linked clones
booted with QEMU guest agent and SSH, showed different machine IDs, and were
destroyed after verification. Their thin disks both reported template 8820 as
their origin. The archive checksum was verified by the controller over
key-authenticated SSH to CT145; this does not require controller SSH access
to Proxmox management addresses. The template backs the published catalog image
`alpine-3-24-20261005` and is enabled only for the P1 pilot. Network acceptance
is limited to `p003t254`. The catalog's `captured_source_identity: false` value
remains a source-provenance limitation until the image is recaptured through
the new identity-bound workflow.

## Implemented controls

- `crange_catalog.py`: immutable logical versions, artifact checksums, approval scopes, unique template VMIDs and verified archive/replica identities.
- `crange_placement.py`: deterministic batch admission, conservative memory/CPU/disk reserves, stopped-workload commitments, soft preferences, hard pins and a shared transactional SQLite reservation ledger. Running reservations do not expire automatically.
- `crange_scheduling.py`: fresh whole-cluster discovery, existing placement preservation, retained-plan overrides, policy/catalog fingerprints, exact pre-apply checks, destination-node locks and applied placement records.
- `crange_distribution.py`: immutable artifact publication, complete backup exports, central archived versions and local verified template caches, captured CT restore, disconnected disposable readiness/identity probes, durable allocation journals and serialized catalog writes. A fixed identity-capture operation reads a running owned source's machine ID and Ed25519 SSH host key before template conversion; export binds that evidence to the source's stable configuration fingerprint, and publication carries the capture provenance into the catalog.
- CLI/worker/GUI: new guest declarations accept Automatic and optional preferred nodes. Legacy `--node` remains a hard pin. Admin preparation jobs accept only fixed image/node/storage fields. Plan summaries expose reviewed placement and reservation details without controller credentials.
- Terraform: every guest/router accepts resolved node, datastore and replica inputs. Stock archives and captured CT templates use separate provisioning paths. Linked clones require the exact local node/datastore, a verified linked replica, snapshot support and a controller-supplied proof flag for captured CTs. Legacy full-clone validation remains without this proof.
- Runtime operations and rollout records use applied physical identity, independently of desired preferences.

Existing resource increases/replacements are admitted conservatively on their existing node, without crediting their current allocation during planning. This may reject a change that would fit after a reviewed destruction. It does not silently migrate or weaken reserves.

## Configuration

Worker-owned files reside in `~/.config/crange`:

- `distribution-policy.json`: revision, enabled flag, allowed nodes/storage, headroom and verified network/capability attestations. Start disabled.
- `image-catalog.json`: immutable logical images and ready replicas; each published image needs a defensible `disk_gb`.
- `distribution-identities/<image-id>.json`: captured machine and SSH host identities for disposable-clone verification. The identity-capture job record is worker-journaled and must match the source node, type, ID, and stable configuration fingerprint at export time.

The policy additionally specifies `approved_storage` and `approved_backup_storage` as node/storage pairs, `isolated_bridges`, `probe_bridge`, and `tested_paths`. `vm-cross-node-full-clone` and `captured-ct-restore` must only be recorded after live acceptance, not inferred from API schema support.

All CLI and HTTP infrastructure workflows share `/var/lib/crange-distribution/reservations.sqlite3`. Uncertain applies retain reservations. Do not delete the ledger or release running entries to make capacity appear available. Partial provider allocations require ownership and terminal-task reconciliation.

## Administrator commands

Read-only discovery:

```
crange distribution-inventory
```

Prepare a published image using fixed trusted configuration:

```
crange distribution-prepare debian-13-v1 --project 3 --node proxmox2 --storage local-lvm
```

The worker operations are `template-distribution-inventory` and `template-distribution-prepare`, restricted to dashboard administrators. Export/publication commands are available through `python3 scripts/crange_distribution.py --help`; file paths are administrator CLI inputs and are not accepted by the GUI worker interface.

For a captured source, run identity capture while the owned VM or container is
running and before converting it to a template. It reads only fixed readiness
and identity files; QEMU guests use the guest agent and containers use the
controller's fixed `pct exec` adapter:

```
python3 scripts/crange_distribution.py \
  --catalog /path/to/image-catalog.json \
  --journal-dir /var/lib/crange-distribution/jobs \
  --policy /path/to/distribution-policy.json \
  identity-capture --node proxmox2 --kind vm --source-id 8809 \
  --job-id capture-alpine-20261006
```

Save the emitted record as a protected administrator file. After stopping and
converting the source to a template, pass that record to `export` with
`--identity-record`. Export rejects a different source or a changed stable
configuration fingerprint. `publish` then requires the exported identity and
capture provenance alongside the existing artifact verification evidence.
Identity records contain a machine ID and public SSH host key; retain them as
release provenance.

## Remaining activation gates

1. Dedicated CT145/NFS library and scoped storage/discovery permissions were deployed and verified on 2026-10-02; see [library-deployment.md](library-deployment.md). Independent backup/restore protection remains pending. Local linked-clone acceptance is complete only for the P1 Alpine pilot; broader clone/archive acceptance remains pending.
2. Global read-only VM.Audit and library storage ACLs are installed; preserve those narrowly scoped roles and validate fresh discovery before admission.
3. For each additional image or node, publish approved artifacts, associate logical IDs with template approvals, retain critical local recovery copies, and verify source identities and destination replicas. The current Alpine pilot still lacks captured source identity evidence; the catalog remains unchanged until the workflow is rerun and verified.
4. Verify cross-node VXLAN/MTU, controller and Guacamole access, DHCP/DNS, egress and isolation. Proxmox5's Project 3 staging network and Guac SSH path passed on 2026-10-06; see [P5 network acceptance](p5-network-acceptance.md). P5 remains outside placement admission pending its local image-cache and snapshot/restore checks. Bridge availability alone is not acceptance.
5. Run the mixed-resource multi-node project, image-library outage, concurrent admission, interrupted provisioning and cross-node capture/test/rollout/rollback checks in DISTRIBUTION-PLAN.md before admitting additional nodes.
6. Keep the current pilot restricted to proxmox1 until each additional node, image and network path passes its reviewed acceptance checks. Proxmox6 enrollment is a separate future decision.

## Validation performed

Both Terraform environments validate against pinned bpg/proxmox 0.104.0 in isolated fixtures. Placement mock tests exercise physical overrides. An isolated read-only plan using the controller's actual Project 3 inputs and state reported **no resource changes**. Python unit coverage includes headroom, races, stale inventory, partial allocations, publication and placement identity. GUI JavaScript syntax checks pass. These checks do not replace the live acceptance gates above.

## Local cache and linked-clone admission

The default policy is `clone_mode: linked`. A replica must be ready, reside on
the exact destination node and datastore, and advertise
`clone_modes_verified: ["linked"]`. The datastore must advertise linked-clone
support and `snapshots_verified: true`; configure `snapshot_storages` only
after disposable storage and guest tests. Captured CTs require the same proof.
A stock OS archive is reported as `archive` creation, not a linked or full clone.

Missing linked proof or local cache fails with an explicit preparation request.
Planning does not copy images. Only an explicit administrator `clone_mode: full`
policy selects full-clone fallback, which appears in the reviewed placement reason.
Existing applied node, datastore, source replica and clone mode remain authoritative
when defaults or soft preferences change. Reviewed apply pins these identities.

Reservations budget the full declared virtual guest disk growth, with existing
copy overhead and datastore reserves. A cached base is counted as one physical
allocation by discovery; its separate audited preparation job reserves full base
allocation and transfer overhead. No zero-sized overlay assumption or automatic
eviction is permitted. Retain old local bases until a refreshed guest/snapshot
reference scan, ownership proof and verified immutable central archive establish
that removal is safe. Storage sharing is a physical optimization, not permission
to overpromise growth capacity.

## Fixed staging and cache lifecycle jobs

`crange stage-changes 3` is the GUI **Stage Changes** worker workflow. It reads
project declarations, plans eligible local cache fills with base/probe/transfer
plus full guest growth capacity, prepares only project-approved immutable images
through audited worker jobs, and then retains the exact guest Terraform plan.
Job output reports each cache preparation phase. Existing guest machines are
changed only by reviewed apply. Declarations changing during preparation abort
staging and request a fresh stage. `crange infra-plan 3 --retain` remains a pure
infrastructure preview: missing caches produce structured preparation requirements
and an actionable staging command; it never copies images.

Cache preparation and guest plans use the same canonical reservation ledger for
CLI and GUI workers, regardless of the caller's home directory. Service install
creates `/var/lib/crange-distribution` for the controller worker. Tests can use
`CRANGE_DISTRIBUTION_DATA` to isolate temporary data. Before enabling a deployment
that used the earlier home-based ledger, stop workers and migrate/reconcile its
reservations into this canonical ledger; never discard pending/running entries.

Administrator-only `template-cache-gc-review` and `template-cache-gc-apply` jobs
map to fixed `cache-gc-review` / `cache-gc-apply` CLI commands. They accept approved
image/node/template identity or a retained plan ID, not filesystem paths or shell
commands. A refreshed dependency/ownership/artifact proof is required at both
review and apply. Central immutable artifacts are retained; ordinary staging does
not evict bases or enable unattended GC.

Container admission additionally requires the node's administrator-controlled
`provider_container_verified: true` setting. Verify disposable Terraform CT
creation with pinned `bpg/proxmox` 0.104.0 before setting it. A linked-cache API
probe alone does not verify the provider's network options. The older Proxmox5
API rejected `host-managed=false` during prior acceptance; keep its container
gate unset until a compatible reviewed provider path passes.

## Administrator cluster view

The admin-only Cluster tab reads a worker-published snapshot; HTTP handlers never
query Proxmox or NFS directly. It shows collection age, policy revision, node
capacity, configured commitments including stopped guests, shared reservations,
remaining headroom, workloads, templates and caches, and explicit eligibility
blockers. `template-cluster-refresh` is a fixed administrator worker job. Project
managers cannot access the cluster inventory endpoint or queue that refresh.

New automatic jumpboxes initially prefer proxmox4 as a soft suggestion. Explicit
empty preference means no preference; saved legacy pins and existing placement
remain unchanged. Eligibility and headroom can override this suggestion, including
proxmox4's configured overcommitment. Reviewed placement reports the fallback.

### Dedicated disposable identity-probe pool

The fixed replica verifier creates disposable VM/CT clones in `crange-probes`.
Base cache templates, range guests, release clones and existing resources keep
their existing `crange-iac` pool. Identity capture and the fixed verifier check
`GET /pools/crange-probes` for the exact node, guest type and VMID before any
in-guest execution. Missing pool, unreadable membership or a different member
fails closed. The `crange-image` ownership tag remains required independently;
a tag alone never authorizes execution. CT verification still uses the existing
fixed, allowlisted host adapter; it receives no QGA privilege.

Administrator deployment checklist (not performed by these source changes):

1. Create `crange-probes` as a dedicated pool. Keep ordinary guests and source
   templates outside it; place only temporary bootstrap guests and disposable
   verification clones there. Never grant guest-agent execution on `/vms`, `/`,
   or `crange-iac`, and do not edit the existing `CrangeDeploy` role.
2. Grant a separate probe role on `/pool/crange-probes` to the worker principal
   and effective token as required by the existing token's privilege separation.
   The fixed operations require `Pool.Audit`, `VM.Allocate`, `VM.Audit`,
   `VM.Config.Network`, `VM.Config.Options`, `VM.PowerMgmt` and
   `VM.GuestAgent.Unrestricted`. A reviewed reduced probe profile additionally
   needs `VM.Config.CPU` and `VM.Config.Memory` (including the VM sockets setting).
   Do not add hardware, disk, console, migration or general administration
   privileges unless a separately reviewed operation actually needs them.
3. On each exact source/cache template `/vms/<template-id>`, grant `VM.Audit` and
   `VM.Clone`; these authorize inspection and cloning without QGA execution on
   the source. Preserve existing narrowly scoped destination-storage
   `Datastore.AllocateSpace` and required network `SDN.Use` permissions. If the
   installed API requires `Pool.Allocate` to assign a new clone to this pool,
   grant it only on `/pool/crange-probes` after observing that exact denial.
4. Bootstrap missing source identity using an administrator-created, owned full
   clone in `crange-probes`, on an approved disconnected bridge. Boot that
   temporary clone, run the fixed `identity-capture`, then stop and convert it
   into a new immutable template without changing its disk/configuration. Export
   with `--identity-record` pointing to its protected capture journal. Publication
   also requires the existing reviewed acceptance record. `VM.Backup` is needed
   only on this exact new source VM/template for export. No original template is
   booted, edited or replaced. After publication, move the retained template out
   of the probe pool and remove any temporary explicit guest-agent/backup grants.
5. Before enabling the workflow, use the actual worker token to prove: pool
   membership read succeeds; clone into `crange-probes` succeeds; fixed QGA
   `exec` and `exec-status` succeed only for the owned disposable QEMU probe;
   the same endpoints are denied on an ordinary `crange-iac` guest and source
   template. On the measured PVE API these endpoints require
   `VM.GuestAgent.Unrestricted`, not just guest-agent audit. Check effective ACLs:
   an existing inherited grant would invalidate the denial test. The current
   shared Terraform token is not a dedicated probe credential.
6. Verify probe stop/delete completes, the journal records `probe_pool`, and
   catalog readiness follows confirmed deletion. Retain failed probes/journals
   for controlled reconciliation. Recheck source-template and pool permissions
   after removing bootstrap grants; do not enable automatic placement merely
   because a role can be installed.

The execution privilege is inherently broad within a guest, so pool isolation
and the fixed controller code are both required. The upstream API permission
checks for `exec` and `exec-status` can be inspected in
[Proxmox QEMU Agent API source](https://git.proxmox.com/?p=qemu-server.git;a=blob;f=src/PVE/API2/Qemu/Agent.pm;hb=HEAD);
confirm the installed node version before assigning a role.
