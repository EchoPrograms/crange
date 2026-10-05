# CLI and API reference

[Documentation index](../README.md) · [User guide](user-guide.md) · [Administrator guide](administrator-guide.md)

Run from the controller checkout as the authorized worker/operator account. `scripts/crange --help` lists commands; `scripts/crange COMMAND --help` is the authoritative option list for the installed revision. CLI administrative access is not a substitute for browser role checks. The examples below describe checked-in source, not proof of deployment or live acceptance.

## CLI workflows

| Area | Commands and required shape |
| --- | --- |
| Discovery | `projects`; `project-show PROJECT`; `box-list PROJECT`; `scenario-list PROJECT` |
| Project creation | `project-init PROJECT --transit ADDRESS/CIDR` |
| Teams | `team-add PROJECT TEAM`; `team-remove PROJECT TEAM` |
| Custom network | `network-add PROJECT NAME --team TEAM --id SUBNET_ID`; `network-edit PROJECT NAME --id ID --uplink OCTET --cores N --memory MB`; `network-remove PROJECT NAME` |
| Dedicated jump box | `box-add PROJECT NAME --team TEAM`; `box-remove PROJECT NAME`; `box-state PROJECT NAME start\|stop` |
| Scenario VM | `scenario-add PROJECT NAME --team TEAM --ip OCTET --template ALIAS`; `scenario-edit PROJECT NAME --template ALIAS --cores N --memory MB --health JSON`; `scenario-remove PROJECT NAME` |
| Scenario container | `container-add PROJECT NAME --team TEAM --ip OCTET --template ALIAS`; `container-edit PROJECT NAME --template ALIAS --ip OCTET --cores N --memory MB --health JSON`; `container-remove PROJECT NAME` |
| Live scenario power | `machine-state PROJECT NAME --kind vm\|container --state start\|stop` |
| Health | `scenario-health PROJECT [NAME]`; `container-health PROJECT [NAME]` |
| Stage and deploy | `stage-changes PROJECT`; `stage-deploy PROJECT --plan-id ID --yes` |
| Infrastructure | `infra-plan PROJECT [--retain]`; `infra-apply PROJECT --yes [--plan-id ID]` |
| Configuration | `inventory PROJECT`; `configure PROJECT routes\|routers\|boxes\|all --yes`; `configure-team PROJECT TEAM --yes`; `route-sync PROJECT` |
| Capture and inspect | `release-capture PROJECT VERSION [--boot-order NAME,NAME]`; `release-show PROJECT VERSION` |
| Fresh release test | `release-test-prepare PROJECT VERSION`; `release-test-check PROJECT VERSION`; `release-test-cleanup PROJECT VERSION` |
| Team release declaration | `release-deploy PROJECT VERSION --teams TEAM ...`; `release-upgrade` / `release-rollback` with the same arguments plus `--from-version VERSION` |
| Release apply/check | `release-apply PROJECT --teams TEAM ... --plan-id ID --yes`; `release-check PROJECT --teams TEAM ...` |
| Guacamole | `guac-config`; `guac-json-key`; `guac-sync PROJECT`; `guac-apply PROJECT`; `guac-refresh PROJECT`; `guac-users PROJECT` |
| Live remote box control | `guac-box-control PROJECT RESOURCE_KEY start\|shutdown\|stop\|restart` |
| Native snapshots | `guac-snapshot PROJECT RESOURCE_KEY list\|create\|restore\|delete [--snapshot NAME]` |
| Custom box/preset | `custom-jumpbox-create PROJECT --definition JSON`; `jumpbox-template-save PROJECT --definition JSON`; `jumpbox-template-remove PROJECT KEY` |
| Admin ISO templates | `template-create --definition JSON`; `template-publish BUILD_ID --projects PROJECT ... --prepared`; `template-upload UPLOAD_ID --node NODE` |
| Distribution | `distribution-inventory`; `distribution-prepare IMAGE_ID --project PROJECT --node NODE --storage STORAGE` |
| Cache eviction | `cache-gc-review IMAGE_ID --node NODE --template-id VMID`; `cache-gc-apply PLAN_ID` |
| Services/accounts | `served [--user crange]` as root; `password USER [--admin] [--projects PROJECT ...] [--role participant\|manager]`; `serve [--host HOST] [--port PORT]`; `worker` |

VM/container/jump-box/network declarations accept `--node auto` and optional `--preferred-node proxmoxN` in new source. A concrete `--node` is a hard pin. Distribution is gated; existing pinned paths remain available while Automatic is disabled. VM/container adds accept `--network NAME`, `--cores`, `--memory`, `--health JSON` and `--guac-protocol ssh|rdp`; exact defaults/bounds are command-specific. `--team 0` means staging. An ordinary team must already be declared.

A retained plan is valid for one hour. `infra-apply` without a plan ID creates and inspects a fresh saved plan; for separately reviewed work prefer an explicit ID. `--allow-destroy` permits reviewed deletions; replacement additionally requires `--allow-replace`. They do not bypass stale-input, identity or quota validation. `infra-plan --replace-sdn-applier` is an explicit repair option; use only after diagnosing partially applied SDN.

Release boot-order options can be repeated to record permutations. Per-team overrides use `--team-override '2:web:cores=4'`; approved fields are `ip_last_octet`, `cores`, `memory_mb`, `node_name` and `started`. A release needs recorded passing fresh-clone tests before rollout. Removing declarations/test definitions is not physical deletion until apply.

`box-users` and `jumpbox-settings` expose fixed guest-account/remote-access operations, but their sealed secret arguments are broker integration interfaces. Use dashboard forms for password/credential changes; do not supply plaintext passwords in command arguments. Definition JSON commands have validated schemas in `crange_jumpboxes.py` and `crange_templates.py`; use the dashboard to construct supported definitions rather than raw Terraform.

For administrator-only export/publication tooling, use `python3 scripts/crange_distribution.py --help` and its subcommand help. It requires explicit catalog/journal/policy paths, immutable identities and verification records. Those filesystem inputs are not accepted through normal GUI jobs.

## HTTP API

The browser API and static dashboard share an origin. Session cookies authenticate browser requests; mutating JSON requests require `X-CSRF-Token` from the authenticated session. Use the dashboard login flow; never put controller tokens in API requests. Server code validates roles and exact payload fields, returning 403 for unauthorized access and 400 for invalid input.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/session` | Current session, roles and CSRF token |
| `POST /api/login`, `/api/logout` | Dashboard session lifecycle |
| `GET /api/projects` | Authorized project declarations/applied metadata |
| `GET /api/jobs` | Authorized job history and output |
| `POST /api/jobs` | Submit a fixed typed operation or cancel a job |
| `GET/POST /api/users` | Administrator dashboard accounts/roles/access |
| `GET /api/audit` | Authorized audit records |
| `GET/POST /api/guac-users` | Project Guacamole account/access management |
| `POST /api/project-settings` | Project user defaults |
| `GET /api/guac-user-stats` | Project user's Guacamole activity |
| `GET /api/connector` | Remote-access connector availability |
| `GET /api/guac-ticket` | Broker-mediated assigned connection ticket |
| `POST /api/guac-bridge` | Protected Guacamole bridge operations |
| `POST /api/box-remote-access`, `/api/box-users` | Fixed remote service/settings and guest account operations |
| `POST /api/custom-jumpboxes` | Validated custom jump boxes/presets |
| `GET /api/templates` | Administrator template/media inventory |
| `POST /api/template-media`; media upload paths | Bounded administrator ISO lifecycle |

Example job body (send within an authenticated session with CSRF header):

```json
{"project":3,"operation":"stage-changes","options":{}}
```

Submission returns HTTP 202 with `job_id`; inspect `GET /api/jobs` for result/output. A cancellation body is `{"action":"cancel","job_id":"ID"}`. HTTP 409 means the job is no longer running or could not be stopped. The Guacamole bridge is a separate authenticated server-to-server endpoint: browser sessions/CSRF have no authority there.

Worker operation names are not always CLI names: `plan` maps to retained `infra-plan`, `apply` to guarded `infra-apply`, and administrator cache operations are `template-cache-gc-review` / `template-cache-gc-apply`. The allowlist/schema is `scripts/crange_jobs.py`; arbitrary executable/argument fields are rejected.

## State and configuration locations

| Location | Meaning |
| --- | --- |
| `terraform/environments/project-NNN/` | Separate project source/inputs/state and retained plans |
| `crange.auto.tfvars.json` inside a project | CLI-managed desired state |
| Project `approved-scenario-templates.json`, `approved-container-templates.json` | Approved VM/container references |
| Project `quota-policy.json` | Resource policy |
| `releases/project-NNN/` | Immutable manifests, test definitions and results |
| `ansible/inventory/generated/project-NNN.json` | Applied Terraform-backed inventory |
| `guacamole/access/project-NNN.json` | Generated remote-access desired state |
| `/home/crange/.config/crange/` | Worker secrets, distribution policy/catalog/identities |
| `/var/lib/crange-web/.local/share/crange/auth.sqlite3` | Installed dashboard accounts, sessions and audit |
| `/var/lib/crange-jobs/jobs.sqlite3` | Installed shared persistent jobs |
| `/var/lib/crange-distribution/` | Shared reservations and distribution lifecycle data |
| `terraform/infrastructure/library/` | Independent library infrastructure state/source |

Standalone/dev runs default account/job DBs to the invoking user's home unless `CRANGE_AUTH_DB` / `CRANGE_JOB_DB` override them. Installed service units set shared canonical paths. `CRANGE_DISTRIBUTION_DATA` exists for explicit isolated test/configuration overrides; never use per-user ledgers concurrently against the same live cluster.
