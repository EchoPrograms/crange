# Administering CRANGE

[Documentation index](../README.md) · [Architecture](architecture.md) · [Reference](reference.md)

## Establish the controller

The existing controller is Debian Trixie at `10.60.0.20`, with five Proxmox nodes and OPNsense at `10.60.2.1`. This repository builds on that environment; it is not a turnkey Proxmox/OPNsense installer. Use a reviewed checkout accessible to the `crange` account, Terraform with pinned `bpg/proxmox` 0.104.0, Ansible, Python 3 and the dependencies in [requirements.txt](../requirements.txt). Guest/node SSH keys, approved storage, SDN bridges and scoped API permissions must already be configured.

The worker's protected configuration belongs in `/home/crange/.config/crange`. Required infrastructure credentials are `proxmox.env` and `opnsense-api.txt`; Guacamole configuration and broker secrets are separate. Keep secrets outside Git, readable only by their owner. Never print credential files to diagnose configuration. A controller checkout under a different user's home must still be traversable by services without granting secret access.

For an existing installation, inspect units and paths before reinstalling. `served` restarts services, so schedule installation when jobs are idle:

```sh
sudo scripts/crange served --user crange
sudo -u crange-web -H scripts/crange password admin --admin
sudo -u crange-web -H scripts/crange password manager --projects 3 --role manager
systemctl status crange.service crange-worker.service
```

The existing `crange` account must be unprivileged and have a same-name group. The installer creates `crange-web`, shared job storage and worker-owned distribution storage. Web service listens on `0.0.0.0:8000`, despite a legacy success message saying loopback; standalone `scripts/crange serve` defaults to `127.0.0.1`. Restrict controller network access or use your approved reverse proxy. For remote work, an SSH forward can expose it locally:

```sh
ssh -L 8000:127.0.0.1:8000 OPERATOR@10.60.0.20
```

Open `http://127.0.0.1:8000`. Do not publish the built-in HTTP server directly to untrusted networks. The installed web service uses `CRANGE_WORKER_EXTERNAL=1`; running a separate development server without that setting also starts an embedded worker and must not compete with production.

## Manage dashboard and remote access

Administrators use **CRANGE Users** for dashboard users, enabling/disabling accounts, project memberships, participant/manager roles and team access. New non-admin users default to participant access. Managers can edit infrastructure and access only within their managed projects. Review role assignments independently of Guacamole connection grants.

Configure the protected connector with `scripts/crange guac-config`. `guac-sync PROJECT` renders desired connections; `guac-apply PROJECT` publishes them and native account grants; `guac-users PROJECT` publishes accounts only. `guac-refresh PROJECT` performs the combined refresh used by dashboard workflows. See [Guacamole integration](guacamole-integration.md).

Dashboard launch tickets require the separate broker and matching Guacamole JSON-auth extension:

```sh
sudo -u crange -H scripts/crange guac-json-key
scripts/install-guac-json-auth.sh --stage
```

Transfer the generated key directly through the approved SSH procedure; do not print it. Reinstall services to enable the broker after configuration. Applying the extension installer with `--apply` restarts Guacamole and disconnects active sessions. The theme extension has its own [build instructions](../guacamole/extension/README.md). `cryptography` is required for secret handling/tickets.

## Create and maintain projects

Check transit addresses against both repository inputs and external reservations. CLI duplicate checks cannot discover addresses managed elsewhere:

```sh
scripts/crange projects
scripts/crange project-show 3
scripts/crange project-init 4 --transit 10.60.2.33/24
scripts/crange team-add 4 1
scripts/crange infra-plan 4 --retain
```

Use the exact returned plan identifier when applying. Before any apply, stop if the plan unexpectedly destroys or replaces live resources. Project 3 is real; preserve its jump-box inputs and independent state. Never put its state in `project-template` or copy `.terraform`, state or retained plans into another project.

Each project has `quota-policy.json` and approved VM/container catalogs. Only approved images, bounded resources and validated networks should enter desired state. Configure approvals and quotas before delegating a project. Project source/inputs, applied Terraform state and Proxmox reality are separate: use `project-show`, a fresh plan and generated inventory to reconcile them.

`configure PROJECT all --yes` regenerates inventory and configures routes, project routers, downstream team/staging/custom routers and jump boxes in the required order. `configure-team PROJECT TEAM --yes` narrows changes to one team (`0` for staging). `route-sync PROJECT` reconciles only the project aggregate route on OPNsense. Never use project workflows to change its firewall policy.

## Build and approve images

Maintain OS bases as an administrator. [Template manifests](../templates/README.md) record builds and verification; a build record is not approval. Use [ISO template workflows](admin-templates.md) for installer VMs and custom jump-box presets. Linux cloud VM/container builder scripts offer administrator-only node workflows; inspect their `--help` for exact inputs. Supply passwords from protected files, exact official-source checksums and public SSH keys. They preserve failed allocations for inspection.

Verify fresh clone boot, unique machine/SSH identities, static customization, guest-agent behavior where required, remote services, controller access and graceful shutdown before approving a version. Non-Linux and manual-network VM profiles skip Linux Ansible tasks. ISO creation produces VMs, not containers. Never overwrite image identities referenced by releases or linked clones.

## Distribution and library operations

The deployed library is CT145 on proxmox5; `crange-library` NFS storage is registered on all five nodes. Its NFS address is `10.0.0.249`, administration address `10.60.0.30`. No template disks have been moved. [Library deployment](library-deployment.md) records the precise storage/export/security configuration and reproducible Ansible/Terraform paths.

Automatic placement and multi-node image acceptance remain disabled. Worker-owned `distribution-policy.json`, `image-catalog.json` and image identity records govern activation. Global read-only discovery permission is now recorded in the deployment record; remaining live acceptance is still required. See [distribution operations](distribution-operations.md) and [DISTRIBUTION-PLAN.md](../DISTRIBUTION-PLAN.md) for activation gates and checks.

Read-only capacity discovery and explicit preparation are distinct:

```sh
scripts/crange distribution-inventory
scripts/crange distribution-prepare IMAGE_ID --project 3 --node proxmox2 --storage local-lvm
```

A missing verified local base is a preparation requirement, not permission to clone from an unverified remote source. Linked mode requires the exact destination node/datastore and verified snapshot/clone support. Reserve full declared virtual disk growth plus base/probe/transfer overhead. Do not clear active reservations to make a plan fit.

Cache removal is a two-step administrator operation:

```sh
scripts/crange cache-gc-review IMAGE_ID --node proxmox2 --template-id TEMPLATE_ID
scripts/crange cache-gc-apply GC_PLAN_ID
```

Read retained ownership/dependency evidence before applying. Both steps refresh proof: all relevant guests, pending/current configs, native snapshots, backing chains, provider tasks, reservations, recovery pins and verified central artifacts. Inaccessible or unsupported evidence blocks deletion. GC does not delete central immutable archives, is not automatic, and has not passed live acceptance.

## Backups and recovery

Back up more than Git. Preserve each project's desired inputs and Terraform state, immutable release manifests/images, approved catalogs/profiles, Guacamole desired state, worker configuration/secrets, dashboard users/audit data, jobs and distribution catalogs/journals/reservations. Keep secret backups encrypted with restrictive access. Versioned source alone cannot reconstruct guest data or identity mappings.

Installed databases are `/var/lib/crange-web/.local/share/crange/auth.sqlite3`, `/var/lib/crange-jobs/jobs.sqlite3` and `/var/lib/crange-distribution/reservations.sqlite3`. Use SQLite's online backup facilities or stop writers for a consistent copy; copying only a live main database can miss WAL transactions. Quiesce worker actions before taking a coordinated infrastructure backup. Store backups independently and test restore in isolation.

The library CT's 400 GiB data mount has `backup=1`, but that setting is not evidence of independent protection. Never back up CT145/data to its own export. Use an independent destination and a real restore test. Library infrastructure must never be a project capture/deletion/rollout target.

Native snapshots provide local guest recovery and remain dependent on local storage/bases. Central complete guest archives provide a distinct restore point; they do not preserve the native snapshot tree. Checkpoint manifest support exists in source but is not a finished dashboard backup/restore workflow. Use reviewed administrator Proxmox backup/restore procedures until exposed workflows pass acceptance; do not invent a `crange backup` command.

During recovery, first stop competing workers, restore a coherent catalog/state/database set, then inspect actual cluster guest IDs, disks and tasks. Do not restore Terraform state and immediately apply without reconciling reality. Keep interrupted/running reservations until actual provider ownership and terminal task state are proven. Restore release images before relying on manifests; verify their checksums, clone readiness and identities. Re-establish routes, inventory and remote-access grants only after ownership is confirmed.

## Troubleshooting

| Symptom | Check and next action |
| --- | --- |
| Cannot sign in / no projects | Enabled dashboard account, project membership, participant/manager role; use `password` interactively to reset |
| Jobs remain queued | `crange-worker.service`, shared job DB ownership, worker logs; avoid starting a second production worker |
| Plan expired/stale | Restage; use returned exact plan ID, never bypass integrity checks |
| Unexpected destroy/replace | Stop; verify inputs/state ownership, especially preserved Project 3 jump boxes |
| Automatic/cache preparation rejected | Disabled policy, approved image ID, local replica proof, full cluster discovery, capacity reservations and tested storage paths |
| Router SSH/DNS/egress failure | Project-router reachability first, generated proxy inventory, persistent return routes, DHCP/DNS and explicit UFW policy |
| Guacamole missing/denied | Applied endpoint, box grants, selected services, refresh job and connector/broker health; credentials alone do not install services |
| Infrastructure succeeds but job fails | Inspect whether Ansible or Guacamole refresh failed; repair that phase rather than recreate infrastructure |
| Interrupted apply | Inspect Terraform state, Proxmox terminal tasks and partial allocations; retain uncertain reservations |
| Library access after reboot | Ganesha grace period (~90 seconds), NFSv4.0 options and export client allowlist from deployment record |
| Provider rejects NIC change | Known older host/provider `host-managed` incompatibility; inspect exact plan, avoid broad ignore rules or ad hoc host upgrades |

Useful service logs:

```sh
journalctl -u crange.service -u crange-worker.service --since today
journalctl -u crange-guac-broker.service --since today
```

Read logs locally and redact any unrelated sensitive environment details before sharing. Validate changed Python behavior with relevant isolated tests, Terraform formatting/validation, Ansible syntax checks and reviewed plans. Those checks do not establish live DNS, egress, isolation, reboot persistence, clone/restore or rollback acceptance.
