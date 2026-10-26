# State, configuration and units

Paths below describe the installed layout. Development invocations can use per-user defaults; inspect installed unit environment before diagnosing a different database.

| Location | Purpose |
| --- | --- |
| `terraform/environments/project-NNN/` | One real project's source, inputs and state |
| `terraform/environments/project-template/` | Clean source template; no live project state |
| Project `crange.auto.tfvars.json` | CLI-managed desired declarations |
| Project `terraform.tfvars` where present | Existing explicit inputs to preserve |
| Project `approved-scenario-templates.json` | Approved VM aliases/references |
| Project `approved-container-templates.json` | Approved LXC aliases/references |
| Project `quota-policy.json` | Project limits |
| Project `.crange-plans` / worker plan records | Saved reviewed plan metadata/binaries |
| `releases/project-NNN/` | Immutable manifests, test and rollout-related records |
| `ansible/inventory/generated/project-NNN.json` | Applied Terraform-backed inventory |
| `guacamole/access/project-NNN.json` | Generated desired connection state |
| `/home/crange/.config/crange/` | Protected infrastructure credentials, connector, catalog/policy/keys |
| `/var/lib/crange-web/.local/share/crange/auth.sqlite3` | Dashboard accounts, roles, sessions and audit |
| `/var/lib/crange-jobs/jobs.sqlite3` | Shared durable queue and bounded task output |
| `/var/lib/crange-distribution/` | Shared distribution lifecycle data and reservation ledger |
| `/var/lib/crange-network-probes/` | Protected fixed network-probe journals/reviews |
| `terraform/infrastructure/library/` | Separate library infrastructure source/state |
| `templates/` | Administrator image build manifests/evidence |
| `docs/website/build/` | Generated static handbook, including local search |

## Environment variables

`CRANGE_AUTH_DB`, `CRANGE_JOB_DB` and `CRANGE_DISTRIBUTION_DATA` override their relevant locations. Installed units use canonical shared paths. `CRANGE_WORKER_EXTERNAL=1` prevents the web process starting an embedded worker. `CRANGE_COOKIE_SECURE=1` requires secure session-cookie transport in the installed configuration.

Do not use separate per-user capacity ledgers against the same live cluster. Environment overrides are useful for isolated tests, not for bypassing active reservations. Never print the Proxmox/OPNsense environment or dump secrets into job output.

The service generator is `scripts/crange_service.py`; inspect `systemctl cat` for actual installed unit configuration. Database backup/restore needs SQLite consistency and coordination with native tasks, not just copying filenames.
