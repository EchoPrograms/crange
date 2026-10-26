# Repository source map

These paths are relative to the repository root. Generated CLI/API inventories are rebuilt from the current checkout without importing runtime automation.

| Area | Main files/directories |
| --- | --- |
| CLI entry/parser/workflows | `scripts/crange`, `scripts/crange.py` |
| Web/API/session/auth | `scripts/crange_server.py`, `scripts/crange_auth.py` |
| Typed jobs, lanes and ownership | `scripts/crange_jobs.py`, `scripts/crange_shared_operations.py` |
| Service installation | `scripts/crange_service.py` |
| UI shell and navigation | `gui/index.html`, `gui/app.js`, `gui/navigation.js`, `gui/styles.css` |
| Scenario UI | `gui/scenario.js`, `gui/usability.js` |
| Machine/health UI | `gui/machines.js`, `gui/machine-health.js` |
| Account/template/lifecycle UI | `gui/users.js`, `gui/accounts.js`, `gui/templates.js`, `gui/lifecycle.js` |
| Terraform environments/modules | `terraform/environments/`, `terraform/modules/` |
| Router and guest baseline | `ansible/routers.yml`, `ansible/router-routes.yml`, `ansible/tasks/`, Linux/Windows playbooks |
| Inventory and edge routes | `scripts/generate-project-inventory.py`, `scripts/opnsense-project-routes.py` |
| Releases/rollouts | `scripts/crange_staging.py`, `scripts/crange_rollouts.py`, `scripts/crange_release_resume.py`, `scripts/crange_release_test_cleanup.py` |
| Image/catalog/distribution | `scripts/crange_distribution.py`, `scripts/crange_catalog.py`, `scripts/crange_image_cache.py`, `scripts/crange_cache_jobs.py` |
| Scheduling/capacity | `scripts/crange_placement.py`, `scripts/crange_scheduling.py`, `scripts/crange_cluster.py` |
| Network acceptance | `scripts/crange_network_runner.py`, `scripts/crange_network_adapters.py`, `scripts/crange_network_checks.py` |
| Guest ownership/disk workflows | `scripts/crange_guest_identity.py`, `scripts/crange_capture_disk.py`, `scripts/crange_windows_disk_resize.py`, other `crange_disk_*` modules |
| Legacy MHL application recipes | `scripts/crange_mhl_services.py`, `scripts/crange_mhl_domain*.py`, fixed MHL Ansible recipes |
| Remote access | `scripts/crange_guac*.py`, `scripts/crange_guest_credentials.py`, `guacamole/` |
| Project/guest accounts | `scripts/crange_user_management.py`, `scripts/crange_box_users.py` |
| Project retirement/HA | `scripts/crange_project_lifecycle.py`, `scripts/crange_team_retirement.py`, `scripts/crange_router_ha*.py`, `scripts/crange_redundancy.py` |
| Host/controller operations | `ops/`, fixed setup and relay scripts |
| Tests and safe browser fixture | `tests/`, `tests/fixtures/user_dashboard.py` |
| Handbook | `docs/handbook/`, `docs/website/`, `docs/generate-reference.py` |

Historical operator/evidence documents live directly in `docs/` and project subdirectories. They record specific observations and often predate current behavior. Do not treat a historical acceptance record as blanket authorization for unrelated nodes/images or copy its operational commands into a new environment without review.
