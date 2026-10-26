# Create and maintain projects

A project is an independent Terraform environment plus approved images, quota, network allocations, users, releases and rollout records. Do not reuse another project's state or initialize a project template over a real project.

## Prepare a project

Create a project from the Projects view where supported, or use the administrator CLI `project-init PROJECT` after reading the installed help. Current initialization allocates shared transit; older examples requiring `--transit` are historical. Check external reservations as well as CRANGE records before deployment.

Set image approvals, quotas and manager access. Add ordinary teams 1–253; Team 0 staging maps to network ID 254. Review/bootstrap the project network/router foundation, then configure it before guest deployment. Existing projects retain their explicit address inputs and state.

## Resource limits

Administrators use **Projects → Edit limits** for CPU, RAM, team/network/machine limits. The form shows declared usage, including router overhead, and a GiB equivalent for RAM entered in MiB. Limits cannot be below existing declared usage or above platform ceilings.

Saving limits changes admission policy, not existing guest allocation. Revision guards reject concurrent edits; reopen current limits before retrying. Per-project quota and physical cluster headroom are independent gates.

## Team foundations

Adding a team declares its router/LAN. Review and apply its foundation before placing guests or a release there. Configure project routers first, then downstream routers; generated return routes must include staging and custom networks. A successful desired-state edit is not foundation acceptance.

The current team-removal workflow still needs reviewed infrastructure/access retirement after declaration removal. Preserve unrelated teams and staging. Offline resources and unfinished native tasks need exact ownership reconciliation before being discarded from state.

## Project lifecycle

Project delete, restore and purge use separate typed workflows and exact confirmation. Read the resulting review and lifecycle state carefully. Delete/retirement and irreversible purge have different consequences; retaining an archived record does not mean guest data can be automatically restored.

Node-loss workflows can defer exactly owned offline resources for later retirement. A returning node must synchronize and run its guarded rejoin/deletion reconciliation before those resources are considered gone. Do not remove the metadata merely to clear a UI warning.

Keep a backup of state, declarations, release manifests, immutable artifacts and required secret material before destructive lifecycle work. See [backup and recovery](backup-recovery.md).
