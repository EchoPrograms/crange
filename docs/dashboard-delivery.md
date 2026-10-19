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

Template option requests are proxied by the long-running
`crange-guac-broker.service`, which imports `crange_templates` independently of
the dashboard and job worker. Deployments that change template options or
their protected status fields must reload the broker as well as the dashboard
and worker, then verify the authenticated `/api/templates` response. A web
restart alone can leave the Templates view serving stale broker data.

The existing GUI is a prototype. The existing worker uses bearer tokens as actor
identities, allows insufficiently constrained arguments, and is not an adequate
production authorization or job queue implementation. Previous successful unit
tests did not cover the empty-inventory regression. Earlier readiness claims
must not be treated as acceptance evidence for this delivery.

### Reload imported automation dependencies

When deploying Python catalog, scheduling, template or guest-access changes,
reload `crange-guac-broker.service` as well as the worker and web services after
active jobs finish. The broker imports these modules into a persistent process.
A source file update alone can leave template readiness and Guacamole requests
using old validation rules. Verify an authenticated project-readiness request
after deployment; missing readiness must not be treated as a ready base image.


## Compact controller interface — 2026-10-16

Deployed a compact console layout based on the supplied Oracle Cloud,
Cloudflare, Portainer and Proxmox references, using the supplied Waybar palette:
black/white and grayscale structure with restrained blue selection and status
colors. The sidebar/header, tables, project rows, forms and metrics are denser;
frequent row actions remain visible and additional actions use floating menus.
Cluster summaries expose commitments and available resources; less frequent
maintenance/capacity controls are expandable. Disclosure state survives an
immediate refresh, including keyboard activation. Job Stop controls follow
backend cancellation capabilities.

Only `gui/app.js`, `gui/styles.css`, and `gui/index.html` were deployed, with
source-hash checks and backups. No services or running jobs were restarted.
Authenticated live Chromium checks passed all ten main views, real Project 100
selection, floating menus, six-node expansion persistence, running-job status
and task indicator, and the 390px layout without page overflow. No unexpected
POST requests or browser errors occurred. Fixture review additionally covered
1280/768px, long names, dialog scroll preservation, popover fallback, and 20
immediate disclosure rerender cycles. Live evidence:
`project-100/dashboard-compact-gui-acceptance.json`.

Real-data follow-up bounded the placement table to a 360px scroll area with a
readable Reason column and sticky headers. This fixed tall rows not exposed by
the initial fixture. Final live checks passed with the actual eight-placement
plan and with the task indicator correctly hidden after the jobs finished;
the earlier active-job check is retained separately.


## October 16 compact-interface follow-up

Both controller and Guacamole interfaces are deployed with compact grayscale
surfaces, explicit status accents, accessible icon actions and grouped menus.
Controller open menus survive same-context polling renders; explicit dismissal
remains closed. Ten real dashboard views and mobile layout passed live checks.
Guacamole retains one row per real server, declared IPs, prominent protocol
links, project/team grouping and floating management menus. The installed JAR
also preserves the launch-credential cleanup that prevents native-login loops.

Live P100 Guacamole evidence is recorded in
[guacamole-compact-interface-acceptance.json](project-100/guacamole-compact-interface-acceptance.json).
Six current staging servers, declared addresses, actual DC power status,
login/reload and mobile/menu behavior were checked. These UI checks do not
substitute for pending participant workstation, session and release acceptance.

## October 19 interface audit and workspace update

Applied shared contrast, form spacing, focus, disabled-state and mobile navigation
fixes to the controller and Guacamole interfaces. Square corner tokens now apply
throughout both themes. Dashboard startup shows loading and retry states.
Guacamole adds project/server cards, Grid/List, grouped preferences and a compact
session toolbar while preserving native connection and advanced tools.
Slow-host power polling is bounded to avoid overlapping request queues.

See [the audit](interface-audit-2026-10-19.md) and
[interface direction](interface-design.md) for findings, evidence and design rules.
Guest/lab availability remains separate from this UI deployment.


### October 19 follow-up — compact sans-serif and project context

The owner requested a proportional sans-serif face after reviewing small Mono
text. Both UIs now use self-hosted IBM Plex Sans, with 12px functional metadata
and neutral surfaces plus semantic state/address/protocol accents. The project
selector is in the controller sidebar. Project creation, display-name settings,
stale project user lists and startup retry persistence were corrected. Native
Guacamole favicon candidates are replaced consistently, including the touch icon.
See `interface-audit-2026-10-19.md` for the isolated functional test scope.


### October 19 — retained project deletion and native administrator access

Project managers can use **Project → Settings → Delete project**. This queues
an audited teardown, checks every Terraform delete against the exact project
identity, and removes its guests, disks, VNets, Guacamole grants, and managed
OPNsense aggregate route/gateway. It never edits OPNsense firewall policy.
Configuration, jump-box presets, releases and immutable images remain retained.
Deletion is fenced against queued/running/reconciliation jobs in the project.
A failed or interrupted teardown remains visible to administrators for retry.

Administrators see **Deleted projects** on Projects. **Restore** returns the
saved definitions to the dashboard for router bootstrap and redeployment; it
does not restore destroyed live disks. **Delete permanently** also removes
project-private release images/archives, configuration and project access records.
It preserves generic/shared base templates, other projects and administrator
audit history. Image removal requires every cluster node online, complete backing
and snapshot evidence, and no dependent guests, operations or retained pins.
Unknown/unpublished image ownership requires administrator reconciliation.
With Proxmox1 offline, destructive release-image cleanup correctly remains blocked.
Neither deletion path was exercised against a real lab during UI verification.

Guacamole headings/selectors use the saved project display name, such as
`Meridian Harbor Logistics (p100)`. Dashboard administrators open Guacamole's
native session/login (normally `guacadmin`); this avoids creating a separate
JSON-auth identity named after the dashboard account. Other dashboard users retain
project-scoped launch tickets. Existing native sessions remain isolated from
console and project tickets.

Control-panel navigation/action icons use a 2.2px stroke. Force Stop/Stop task
uses red. Guacamole management menus use a viewport-anchored top-layer popover,
stay adjacent to the trigger, and check the selected server's power state before
showing the applicable controls. Unknown power status has an explicit retry.

Administrator Projects cards expose **Delete project** (retain definitions/releases). The Deleted projects section offers **Restore** and **Permanently delete** (remove retained private artifacts after teardown completes). Project managers retain Delete in Settings. Restore returns definitions for explicit redeployment; it does not recover destroyed live disks. CRANGE Users is now a table: use **+** to create an account or select a username to edit access, roles, status or delete it. Cluster reachability is refreshed independently of the cached capacity snapshot; Unknown is not Online.

Delete sits beside Open project on administrator project cards. During a node outage, lifecycle cleanup remains an active `waiting-for-node` job and resumes after required nodes reconnect; other projects can use the worker. Templates loads saved build records first and collects live storage/build eligibility in the background, reusing the resulting observation for 30 seconds. The worker independently revalidates every mutation.
