# Exact project network acceptance evidence

`python scripts/crange_network_acceptance.py /protected/path/to/bundle`
validates an offline administrator evidence bundle. It executes no commands,
creates no guests, edits no firewall or policy, and prints only the validated
project/node/VNet/probe identity. This is a source-only foundation for a future
fixed worker, not a deployed automated network test or an admission grant.

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

## Fixed lifecycle foundation and blocked queue integration

`scripts/crange_network_runner.py` now implements a source-only lifecycle with a
`ProbeBackend` interface. It claims a protected durable journal exclusively,
reserves resources, gates the exact create plan on an administrator-reviewed
SHA-256, applies the saved plan, installs only the scoped controller/Guac TCP/22
rule specification, dispatches six fixed check names with a 60-second deadline,
and attempts independent rule/probe cleanup even after partial create or test
failure. Destroy also requires an exact saved-plan digest after the fixed worker validates that it deletes only the owned probe. Only
confirmed guest/state/rule absence permits reservation release; only complete
valid evidence after cleanup permits a receipt. Provider exception text is not
persisted. All retries with an existing journal are rejected.

There is deliberately no production backend or queue operation yet. The
`ProbeBackend` is a typed contract, not a guest-command execution adapter.
Tests exercise the lifecycle with a fake backend; they do not claim live
Terraform/SSH/firewall acceptance. Adapters still need to enforce approved
immutable image/resource constraints, atomic project/address/VMID reservations,
actual saved-plan file hashes, provider task completion, exact live ownership,
fixed guest/management tests with subprocess/API deadlines, and retained
artifacts. Calling this module with an untrusted backend would defeat its trust
boundary; it belongs exclusively in the privileged reviewed worker.

Queue integration is blocked by the current `JobStore.cancel` process-group
termination and the worker's interrupted-job recovery: process termination can
skip Python cleanup. Do not expose this runner through a GUI or add an operation
that assumes `finally` is cancellation-safe. The safe integration requires:

1. An administrator-only typed operation containing a reviewed manifest ID and
   scope, never commands, template code, arbitrary endpoint sets or file paths.
2. Cooperative cancellation: set a durable cancellation request, stop starting
   tests, and enter owned cleanup. Keep the queue job and reservation nonterminal
   while cleanup is uncertain. The cancellation handler must not kill this
   operation as it currently kills ordinary worker processes.
3. Startup recovery and manual reconcile: load the exact journal scope, stored
   rule specification and saved-plan identities; independently query cluster
   guest ownership and provider tasks. Remove only the recorded rule; review a
   fresh single-probe destroy plan when an owned probe remains. Never destroy an
   untagged or mismatched VMID. Mark uncertain outcomes for administrator action.
   Verify all absence conditions before releasing reservations or retrying.
4. Keep each lifecycle transition durable by syncing files and parent
   directories, as this runner does. Production adapters must also persist
   reservation and provider-task journals durably before acknowledging mutations.
5. A receipt publisher that retains logs and plan/provider artifacts and validates
   the complete bundle before any separate exact-tuple policy review. It must
   never infer zone-wide trust or change `allowed_nodes` from test success.

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
