# Dashboard delivery and acceptance

Source: user attachment `pasted-text-1.txt`, September 29, 2026.

## Required outcome

- A complete project dashboard with a black, white and gray interface, limited
  accent colors, clear navigation, and no taglines.
- Password authentication with server-side sessions; no token entry or worker
  address configuration in the interface.
- A frontend and backend served together on port 8000 by `crange serve`.
- `crange served` installs a service that starts on boot and restarts on failure.
- CLI and dashboard creation and management of projects, teams, networks,
  jumpboxes, scenario VMs/containers, releases, tests and deployments.
- Persistent job history and logs, real project data and clear error states.
- Resolve incomplete CLI behavior and investigate Proxmox refresh delays.
- After the core dashboard is verified, implement user/team assignments and
  infrastructure-managed Guacamole access. A team user receives a jumpbox by
  default; scenario authors can assign additional jumpboxes and headless guests.
- Guacamole connections and permissions follow those assignments; inspect the
  installed authentication mechanism before deciding whether a plugin is needed.
- Customize Guacamole with a dark interface and no taglines.

## Verification gates

1. Password/session tests cover incorrect login, session expiry, logout, CSRF,
   project authorization and secret-free responses/logs.
2. Jobs use fixed typed operations, serialize conflicting project changes,
   preserve history across restart, and do not automatically apply a different
   Terraform plan from the one reviewed.
3. Browser checks cover login, navigation, creation forms, real project loading,
   log updates and error reporting at desktop/mobile sizes.
4. CLI tests cover nested Ansible inventory, absent optional groups, parameter
   parsing, resource quotas and release lifecycle boundaries.
5. Live reviewed plans and disposable scenario tests establish actual routing,
   access and release behavior; unit tests alone do not satisfy this gate.
6. Port 8000 serves both assets and API; service installation, restart and boot
   enablement are verified without affecting unrelated Proxmox workloads.
7. Guacamole tests prove allowed connections work, unassigned connections are
   hidden/denied, and removals revoke managed access without deleting unrelated
   Guacamole accounts or connections.

## Initial evidence

The live controller repository is `/home/crange/crange`; a source-only snapshot
was fetched into `/home/echo/dev/crange` for development. Terraform state,
credentials and generated inventory were excluded.

The existing GUI is a prototype. The existing worker uses bearer tokens as actor
identities, allows insufficiently constrained arguments, and is not an adequate
production authorization or job queue implementation. Previous successful unit
tests did not cover the empty-inventory regression. Earlier readiness claims
must not be treated as acceptance evidence for this delivery.
