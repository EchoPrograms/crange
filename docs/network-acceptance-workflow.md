# Exact project network acceptance evidence

`python scripts/crange_network_acceptance.py /protected/path/to/bundle`
validates an offline administrator evidence bundle. It executes no commands,
creates no guests, edits no firewall or policy, and prints only the validated
project/node/VNet/probe identity. The authenticated GUI also has a fixed queued
network-verification workflow. Managers choose only a declared standard team
LAN or Team 0 staging LAN and an enrolled node. The privileged worker derives
the probe address, VMID, approved image, bounded size, and single-resource
Terraform plan. The browser cannot supply paths, commands, image IDs, arbitrary
endpoints, or raw Terraform. A completed check applies only to that exact
project/node/VNet tuple. After all six checks and cleanup pass, the root job lane
publishes that exact VNet membership for placement; global enrollment, capacity
and storage policy remain unchanged.

Existing scheduler policy has node-wide `passed_checks` plus an exact
`verified_networks` list. A proof for `proxmox2/p003t254` cannot justify another
node, another team VNet, Project 240, or the whole `crange` zone. Keep live policy
unchanged until the exact tuple passes review. Do not copy the node's existing
passed checks onto a newly discovered VNet. P4 capacity admission is a separate
gate; a network proof does not bypass it. Container/provider acceptance and
image-cache acceptance remain separate gates too.

## Fixed administrator deployment procedure

For each real project's desired LAN and each candidate node:

1. Read that project's own desired state and actual router/inventory outputs.
   Select its declared `pNNNtTTT` or `pNNNcCCC` LAN, a free static address
   `.2`–`.100`, its `.1` gateway, and an unused cluster VMID. Confirm both address
   and VMID are unallocated. Do not create a VNet merely to claim existing
   workloads on another VNet are accepted. Project 240 has separate state.
2. Use a new temporary Terraform directory/state, pinned `bpg/proxmox 0.104.0`,
   an approved immutable VM image and one resource named
   `proxmox_virtual_environment_vm.network_acceptance_probe`. Declare the exact
   target node/VMID, one enabled NIC on the selected VNet, static initialization
   address/gateway, and tags `crange-network-probe` and
   `network-<32 lowercase hexadecimal digits>`. Use the reviewed image's normal
   bounded CPU/memory/disk sizes. No project routers or other resources belong
   in this state. Never apply a project-wide plan to deploy this probe.
3. Save `terraform plan -out=create.tfplan`, inspect it, and export
   `terraform show -json create.tfplan` to `create-plan.json`. It must contain
   exactly one create, with known ownership/node/VMID/NIC/static address. Apply
   that exact saved plan, then verify live config matches it. The evidence
   validator can inspect the create plan with `probe_from_plan` before apply;
   full CLI validation intentionally requires cleanup evidence afterward.
4. Run the six reviewed tests below. Each test has a 60-second maximum, records
   its target and result, and stores its command-level output in the matching
   local log. Check runtime probe addressing against desired state. Record
   exact source revision and UTC timestamp. No credentials/key material belong
   in logs. This offline tool does not provide guest commands: a future worker
   must select fixed tests internally, never accept shell text from a GUI.
5. If a management-path rule is needed, use only an explicitly reviewed
   temporary Debian-router rule restricted to the management source, probe
   `/32`, and TCP/22. Record its exact identity and remove it in a cleanup path.
   Never edit OPNsense firewall policy. Existing aggregate return routing must
   be correct before testing.
6. Save and inspect a destroy plan for the disposable state; export its JSON
   as `delete-plan.json`. It must delete exactly that same tagged probe on the
   same node/VNet/VMID/address. Apply the reviewed saved destroy plan. Retain
   `cleanup.log`: before/after cluster VMID absence, empty disposable state,
   temporary-rule absence, and original project input checksum comparison.
   Check actual provider task completion, not just command exit status.
7. Populate the receipt, hash the retained logs, and run the CLI validator.
   A failed check or uncertain cleanup rejects the bundle. An administrator
   reviews retained commands/results and the live inventory before any separate
   policy change. The validator checks format, plan boundaries and log integrity;
   it cannot authenticate self-reported test results or prove live absence.

## Required tests

| Check/log name | Required evidence |
| --- | --- |
| `vxlan_mtu` | Probe NIC and routed VNet path MTU 1450; successful non-fragmenting path test with bounded packet count. |
| `controller` | Controller `10.60.0.20` can reach probe SSH via intended management path; probe cannot initiate unauthorized controller access. |
| `guacamole` | Guacamole `10.60.0.10` reaches probe TCP/22 through intended route; bounded SSH handshake without credential testing. |
| `dns` | Probe resolves the reviewed external test hostname using declared DNS; capture actual resolver and response. |
| `internet_egress` | Probe reaches the reviewed external HTTPS endpoint with connection/total timeouts. |
| `team_isolation` | Probe cannot initiate access to representative other-team, project-transit and shared/internal targets using the router policy's protected scopes. Negative tests include positive controls, and firewall deny evidence so a broken target is not mistaken for isolation. |

## Evidence schema

The bundle contains `receipt.json`, `create-plan.json`, `delete-plan.json`, six
check logs named above, and `cleanup.log`. All logs must be regular files of at
most 4 MiB. Protect the whole directory; Terraform JSON may contain sensitive
initialization values even though this validator prints none.

Example receipt shape (replace every placeholder with actual evidence):

```json
{
  "schema_version": 1,
  "scope": {
    "project_id": 240,
    "node": "proxmox2",
    "vnet": "p240t254",
    "address": "10.240.254.98/24",
    "vmid": 9911,
    "job_id": "network-<32 hexadecimal digits>"
  },
  "zone": "crange",
  "mtu": 1450,
  "source_revision": "<40 hexadecimal digits>",
  "tested_at": "2026-10-06T12:00:00Z",
  "checks": {
    "vxlan_mtu": {"passed": true, "duration_seconds": 10, "artifact_sha256": "<64 hexadecimal digits>"},
    "controller": {"passed": true, "duration_seconds": 10, "artifact_sha256": "<64 hexadecimal digits>"},
    "guacamole": {"passed": true, "duration_seconds": 10, "artifact_sha256": "<64 hexadecimal digits>"},
    "dns": {"passed": true, "duration_seconds": 10, "artifact_sha256": "<64 hexadecimal digits>"},
    "internet_egress": {"passed": true, "duration_seconds": 10, "artifact_sha256": "<64 hexadecimal digits>"},
    "team_isolation": {"passed": true, "duration_seconds": 10, "artifact_sha256": "<64 hexadecimal digits>"}
  },
  "cleanup": {
    "vmid_absent_before": true,
    "vmid_absent_after": true,
    "probe_state_empty": true,
    "temporary_rules_absent": true,
    "input_unchanged": true,
    "artifact_sha256": "<64 hexadecimal digits>"
  }
}
```

Future worker integration must bind each collected result to the actual owned
probe, retain create/delete task identities, and enforce fixed endpoint sets,
timeouts and cleanup internally. It must derive policy candidates from reviewed
receipts for one tuple at a time, preserve existing grants, and leave capacity,
quota, image, provider and storage gates intact. This CLI deliberately does not
synthesize `network_attestations` or widen `allowed_nodes`.

## Fixed queued lifecycle

`scripts/crange_network_runner.py` now implements a source-only lifecycle with a
`ProbeBackend` interface. It claims a protected durable journal exclusively,
reserves resources, gates the exact create plan on an administrator-reviewed
SHA-256, applies the saved plan, installs only the scoped controller/Guac TCP/22
rule specification, dispatches six fixed check names with a 60-second deadline,
and attempts independent rule/probe cleanup even after partial create or test
failure. Destroy also requires an exact saved-plan digest after the fixed worker validates that it deletes only the owned probe. Only
confirmed guest/state/rule absence permits reservation release; only complete
valid evidence after cleanup permits a receipt. Public job output and journal causes contain fixed categories. Precise exceptions
are retained only in the root-owned, mode-0600 `workflow-error.txt` inside the
protected nonce bundle, before classification; subsequent wrappers append rather
than replace the original cause. All retries with an existing journal are rejected.

The fixed `network-verify` operation is submitted through authenticated
`/api/jobs`. An ordinary worker cannot claim it. A separate root
`crange-network-worker` lane claims only network verification and fixed
administrator reconciliation jobs. It rechecks the actor before starting and
derives every probe input from protected project, catalog, and live cluster
state. During the Guac check, a narrowly scoped relay verifies the protected
review and active journal phase, rechecks the tagged probe and approved source,
confirms Guac VM 110 is running on P4 at `10.60.0.10`, then performs one bounded
TCP/22 SSH-banner diagnostic inside Guacamole VM110 through one fixed host-root
QGA command. The worker receives no general guest-execution grant. The command
binds `10.60.0.10`, connects only the derived owned probe address on TCP/22, and
retains its claim, raw submission, PID and same-PID polling result before parsing.

Running jobs use a durable cooperative cancellation request. The runner stops
before another check and enters its owned cleanup path; the queue does not kill
its process group. Jobs stay active as `reconcile-required` while cleanup is
uncertain. Administrators can request the fixed reconciliation action from that
job card. Startup recovery inspects the same protected journal before retrying
or releasing a reservation; missing or ambiguous ownership stays nonterminal.

Journal states cover reservation, create pending/created, rule pending/testing,
rule cleanup/destroy pending, cleanup uncertainty, confirmed cleanup with pending
reservation release, and terminal failed/accepted cleanup. A crash at any
nonterminal phase therefore blocks blind reallocation and identifies the exact
owned guest/rule to reconcile.

## Concrete adapter increment

`scripts/crange_network_adapters.py` supplies three concrete building blocks:

- `SavedTerraformProbe` inspects root-owned saved plans against reviewed binary
  and decoded-JSON hashes and exact probe identity. Its internal apply method
  applies only the same saved plan using parsed protected API credentials and
  a fixed environment; inherited Terraform CLI overrides are excluded. Only
  `main.tf` and the pinned provider lockfile are permitted as configuration.
- `BoundedPVE` caps each API call at ten seconds and the remaining overall
  deadline. `OwnedQGADiagnostics` verifies live job tags, one exact VNet NIC,
  static initialization address, running status, and no template/lock before
  dispatching one of five fixed BusyBox diagnostics. It retains one QGA PID
  across polling responses and never accepts a command string.
- `FixedRouterUFW` adds/removes only two job-tagged source-specific TCP/22
  forwarding rules to the exact probe `/32`, through the desired-state project
  and team routers using SSH with host-key checking. It verifies matching rule
  status before deleting by exact specification, never a mutable rule number.
  It requires already-active UFW and never operates on OPNsense. Provision a
  dedicated root-owned `/etc/crange/network-probe-ssh-key` and pinned SSH host
  keys before considering production use.

The controller administrator dry-run entrypoint is:

```sh
sudo python3 scripts/crange_network_adapters.py network-<32-hex-job-id> create
```

It only validates and reports scope/digest; it cannot apply or execute a guest.
The protected job directory is `/var/lib/crange-network-probes/<job-id>/`.
Create that root-owned directory with restrictive permissions, then retain
`main.tf`, `.terraform.lock.hcl`, the saved `create.tfplan`/`delete.tfplan`, and
`review.json`. Review JSON has this shape:

```json
{
  "scope": "same scope object as the evidence receipt",
  "input_sha256": {
    "main.tf": "<SHA-256 of reviewed main.tf>",
    ".terraform.lock.hcl": "<SHA-256 of reviewed provider lockfile>"
  },
  "plans": {
    "create": {
      "binary_sha256": "<SHA-256 of saved create.tfplan>",
      "json_sha256": "<SHA-256 of canonical decoded plan>"
    },
    "delete": {
      "binary_sha256": "<SHA-256 of saved delete.tfplan>",
      "json_sha256": "<SHA-256 of canonical decoded plan>"
    }
  }
}
```

Canonical decoded-plan hashes use `crange_network_runner.plan_digest` (sorted
JSON keys, compact separators, UTF-8). Plan outputs can contain secrets; retain
them locally with restrictive permissions. A destroy plan only exists after creation. The fixed controller may generate it,
inspect the exact one-delete ownership boundary, persist both hashes, and apply
that same saved plan. This programmatic inspection is the review; it requires
no separate human approval.

These adapters are tested with fake process/API responses and are not yet a
complete production `ProbeBackend`. Missing integration includes shared-ledger
network probe reservations, immutable approved-image/resource validation,
controller/Guac path tests, cross-node MTU and firewall-backed isolation tests,
artifact capture/publishing, and cooperative queue cancellation/recovery. The
five QGA diagnostics deliberately do not generate acceptance receipts: a
successful MTU read, DNS lookup or HTTPS download cannot prove all six gates.
No live dry-run bundle or router mutation has been performed by this increment.


## Owned reconcile and actual management origins

The root-only adapter CLI now also supports:

```sh
sudo python3 scripts/crange_network_adapters.py network-<32-hex-job-id> reconcile
```

This is a mutation command, unlike `create`/`delete` inspection. It requires the
protected manifest, matching `journal.json` ownership/rule scope, and desired
router addresses under `review.json.routers.project_router` and
`review.json.routers.team_router`. It locks the exact job, queries cluster VMID
identity and live tags/VNet/static initialization address, removes only the
recorded management rules, generates a destroy plan, asserts exactly one delete
of the owned probe, persists plan hashes and phases durably, and applies that
exact saved plan. It then confirms actual guest absence, empty standalone state
and rule absence. Cleanup uncertainty remains journaled and never causes blind
creation. A live-absent guest with retained Terraform state is deliberately not
forgotten: outstanding provider tasks/state need independent reconciliation.
The command leaves the shared reservation retained, reporting
`cleanup_verified_reservation_pending`; its release path still needs ledger
integration. It never emits acceptance evidence or widens placement policy.

`FixedManagementChecks` now implements checks from the actual origins. A
protected manifest must bind controller CT114 at `10.60.0.20` and Guac VM110 at
`10.60.0.10`, with their actual cluster nodes and types. The controller check
validates CT114's address then binds a local TCP socket to `.20`; this therefore
runs inside the controller rather than simulating that source elsewhere. The
Guac check confirms VM110's cluster identity and QGA-reported `.10`, then runs a
bounded SSH handshake from its QGA with source binding and every authentication
method disabled. Expected final authentication failure counts only when SSH
connection and protocol negotiation are established. Neither check accepts a
user-selected origin ID, source address or command string.

These paths have fake transport/process tests, including partial cleanup,
wrong-owner rejection, generated destroy-plan boundaries, explicit controller
source binding, exact Guac QGA origin and authentication-disabled handshake.
They have not yet been executed against live infrastructure. Cross-node MTU,
firewall-backed isolation, reservation integration and complete receipts remain
required before publishing automatic-placement network attestations.

## Complete controller CLI

`scripts/crange_network_workflow.py` combines the concrete adapters with the
shared reservation ledger. It remains root-only and has no GUI/job-queue action.
Use `preflight <job-id>` for read-only guest/desired-state checks, `run <job-id>`
for one owned lifecycle, and `reconcile <job-id>` for exact cleanup after an
interruption. The run retains six command-level artifacts, the inspected plans,
a cleanup artifact and a validated receipt. It never changes placement policy.

The protected manifest additionally binds an approved ready local VM replica
under `source` (`image_id`, `template_id`, `datastore_id`, `config_sha256`), all
project input hashes under `project_input_sha256`, exact management origins,
another regular `other_team_id`, and the reviewed 40-character source revision.
The CLI checks the catalog's project approval and source configuration identity,
local full clone, 512–4096 MiB memory, 1–2 cores, at most 32 GiB source/disks, and
approved storage. It derives/validates the team router from project Terraform
outputs and actual router CT tags/downstream interface. Custom LAN router
origins remain unsupported; their different upstream topology needs an explicit
adapter. Service credential/key files retain their existing root-or-`crange`
ownership with restrictive permissions; they are never copied or printed.

Reservation uses the shared capacity/quota ledger and includes the exact probe
VMID and LAN static address. Conflicting active probe leases, occupied VMIDs and
desired/live static addresses are rejected. The existing node/library locks are
held across the lifecycle. Production callers must still serialize independent
project edits: the CLI compares all project input hashes before create and after
cleanup and rejects drift. Ordinary team deployment retains its image, provider,
capacity and network trust gates.

Actual guest checks now include:

- Cross-node VNet/routed path: router node must differ, NIC MTU and expected
  default route/source must match, iputils is mandatory, 1422-byte payload DF
  ping to `1.1.1.1` succeeds, and 1423-byte payload fails locally as too large.
- DNS queries OPNsense `10.60.2.1`. DHCP checks the declared `.101`–`.254` range
  and options, performs bounded DORA with a no-op lease script, releases its
  lease, and explicitly restores the static address/default route before
  verifying routing. This does not change project firewall policy.
- Internet uses bounded HTTPS to `example.com`.
- Controller and Guac use their actual fixed origins described above.
- Isolation requires a working public positive control, bounded TCP attempts to
  another team gateway, project transit gateway, controller and Guac, and a
  small positive increment of the exact existing static `ufw-before-forward`
  `10.0.0.0/8` DROP rule for each attempt. Counter resets or large background
  deltas fail evidence. These shared counters are operational evidence rather
  than per-source packet capture; avoid concurrent noisy traffic during testing.

Recovery bypasses source-image approval/current project revision requirements:
retiring an image or editing desired state must not prevent owned cleanup. It
uses the saved router identity, exact live probe ownership, inspected one-delete
plan, actual absence checks and shared reservation scope. Reservations remain
held if node provider tasks are still active. No acceptance receipt is generated
by recovery. Fixture tests cover typed scope, relay ownership checks, durable
cancellation, and startup handling; they do not attest a live run. Deployment
and an authenticated GUI button request in the target session are still
required before reporting Project 100 acceptance as passed. Neither the worker
nor the receipt publisher changes `allowed_nodes`, `verified_networks`, firewall
policy, or OPNsense rules.

A capacity rejection before reservation commit is not proof of an allocated
probe. The fixed administrator GUI reconciliation action can terminalize such a
`reserving`/`reservation_uncertain` journal only after binding its exact manifest
and rule nonce, locking the ledger and finding no job row, confirming live VMID
absence, empty standalone state, absent temporary rules, no active node tasks
and unchanged project inputs. It retains `unadmitted-cleanup.json`; it releases
no reservation and deletes no guest. Any uncertain identity or committed lease
uses the normal owned reconciliation path instead.

The P4 Guacamole relay reads the protected job bundle through the fixed running
controller CT114 `/proc/<pid>/root` path, with its expected mapped root UID
100000. It reads regular JSON files using `O_NOFOLLOW`, checks exact manifest
and journal scope, and rejects a changed CT PID/root identity after the check.
PVE initialization fields are compared as exact parsed IP/gateway fields so
provider ordering does not change ownership. Generated saved delete plans are
verified as owned regular files and set to mode0600 before protected decoding;
the service umask must not make them group-writable.

New GUI receipts use schema version 2. `source_binding` version 1 records SHA-256 hashes
for all seven fixed controller network files, the administrator-installed trusted
relay code SHA256, and an explicit `git_dirty` boolean. The Git revision remains
a label; a dirty worktree is identified and its exact code bytes are bound by
hashes. The root-private relay trust file is
`/etc/crange/network-relay-source.sha256`. The relay reports its module-load code
hash and rejects changed on-disk code; the client checks that hash against the
protected manifest. Cleanup checks source and project-input stability before
issuing the receipt. Guac receipt evidence includes source `.10`, the exact
probe target, TCP/22 and the same reviewed relay hash. Legacy offline schema 1
receipts remain readable and cannot claim source-binding fields.

A failed fixed guest check retains a root-private `<check>.failed.log` with its
bounded commands, exit status and stdout/stderr. MTU acceptance requires NIC MTU 1450,
a successful 1422-byte DF positive control, and local rejection of the 1423-byte
(1451 total) DF packet. The observed musl/iputils `sendmsg: Message too large`
variant is accepted only with the exact 1451-byte packet output and nonzero exit;
generic packet loss is rejected.

Successful queued network verification now publishes its accepted schema 2
receipt automatically before reporting job success. Previously completed
receipts have an administrator recovery button, **Use verified network**, which
queues `network-publish` using only the source job ID. The privileged publisher
validates the protected accepted journal, receipt/artifact hashes, exact queue
scope, source binding, current project inputs, live probe absence and released
reservation. It serializes policy writes with capacity controls and preserves
policy ownership, permissions and every unrelated setting. Only the exact
node/VNet is added, alongside receipt scope and hashes; existing valid grants
remain intact. No other node or team is inferred.

`publication.json` records a durable prepared/published transaction with the
original receipt hash, reviewed source-binding digest, consumer code hash and
policy hashes before/after. Replays are idempotent. Once a protected publication
record has established a reviewed historical producer, later relay code updates
do not rewrite or falsely relabel that old evidence as current source. Placement
still requires independent image-cache, provider, capacity and quota admission.
