# CRANGE webapp capability audit

The Guacamole customization and integration have been verified: the CRANGE
monospace dark theme and visible login labels, separate project Guacamole
accounts, scoped connection grants, access revocation, and connection refresh
following deployment/configuration are deployed. A native project account also
opened the staging jump box through RDP and ran a read-only command in its
remote desktop.

**Audit verdict:** ordinary lab construction and remote access are supported;
the dashboard is not yet sufficient for the complete custom-topology release,
upgrade, and rollback workflow. The workflow matrix below identifies those gaps.
This audit does not claim that missing capabilities are implemented or that the
full mixed VM/container/network propagation scenario has passed acceptance.

| Capability | Evidence in the current source | Status |
| --- | --- | --- |
| Create and edit custom labs | Fixed VM, container, and custom-network job schemas; Team 0 scenario forms | Implemented |
| Stage infrastructure | Terraform plan IDs, stale-plan detection, reviewed staging deploy | Implemented |
| Propagate scenarios | Immutable release capture, fresh-clone release testing, selected-team rollout | Implemented; live end-to-end run still needed |
| Manage teams and jump boxes | Dedicated team pages and team-scoped jump-box lifecycle/configure jobs | Implemented |
| Participant-only access | Per-project manager roles; participant API filtering and lab-only dashboard; worker rechecks queued actors | Live participant browser/API, promotion, and demotion verified |
| Separate project Guac users | Project Users native accounts; protected password sealing; account-only worker jobs | Live GUI creation, native login, scoped grants, password reset, disable/re-enable, and removal verified |
| Default Guac access | Team membership selects the lowest-numbered jump box | Implemented |
| Explicit Guac access | Per-user jump box, VM, and container grants validated against project declarations | Implemented |
| Revoke Guac access | Applied ownership sidecar; launch tickets intersect applied grants with current CRANGE assignments | Live verified for team/grant removal and account disabling |
| Open Guac with CRANGE identity | One-minute JSON-auth ticket broker validates CRANGE session and project scope | Deployed; live non-admin browser login verified |
| Health validation | Default ping plus TCP/HTTP checks and container/VM validation jobs | Implemented |

The live Guacamole host is running 1.6.0 with the CRANGE theme and exactly one
JSON-auth extension loaded. Five Project 3 connections are managed by CRANGE.
The login heading, field labels, connection names, empty states, navigation,
menus, and keyboard focus use the dark palette. Router checks reached deployed
jump boxes and SSH-enabled scenario guests.

The controller runs separate web, worker, and Unix-socket ticket broker
services. On 2026-09-29 a temporary non-admin user logged in through CRANGE and
opened Guacamole without a separate Guacamole account. Its browser showed
exactly the Team 1 default jump box and one explicit scenario connection.
Team removal took effect in a fresh ticket in the same browser before apply;
removing all grants, another project, and a disabled account were denied.
Verification users and their grants were removed and the normal manifest
reapplied. Real socket tests cover permitted and rejected operating-system
caller UIDs.

## Remaining capability gaps

- A complete live release capture, fresh-clone health test, and selected-team
  rollout still needs acceptance testing. Source tests exercise these paths;
  the Guacamole checks do not establish release reproducibility.
- Scenario VM forms use the staging/team network and do not expose custom
  network placement or IP changes on edit. Container forms support both.
- Guacamole connection grants do not provide guest OS credentials. Users still
  need the SSH/RDP account credentials appropriate to each lab machine.

Project roles are independent of Guacamole team/connection grants. New accounts
and new project memberships default to participant; platform administrators
can select manager access per project in CRANGE Users. Existing project
memberships migrate once to manager access. A demotion takes effect for the
existing dashboard session and rejects queued work before execution; already
running infrastructure work finishes normally.
Live verification confirmed that participants cannot inspect management data,
submit management jobs, or promote themselves, while their lab-only dashboard
still launches exactly the assigned Guacamole connections. Promotion and
demotion were checked against the same signed-in session. Automated HTTP and
worker tests also cover cross-project restrictions and rejecting a queued job
after demotion.

Persistent data disks remain outside the scenario release model. Existing
Guacamole tunnels cannot be forcibly terminated by CRANGE when an account is
disabled; new tickets are denied immediately and deployed ACLs are reconciled
through the reviewed workflow.

The project Users tab now manages native Guacamole accounts separately from
CRANGE Users. Live verification on 2026-09-29 created a temporary account
through the GUI, showed its queued job, and authenticated directly to Guacamole
with the inferred `p003-` login. Only the selected team default jump box and
explicit scenario connection were visible. Password reset rejected the old
password, removing team access removed the default connection, disabling
rejected login, re-enabling restored the selected grant, and deletion rejected
subsequent login. The native credentials could not log into CRANGE. Passwords
were absent from job payloads and logs. Temporary accounts were removed.
Guacamole 1.6.0 uses the top-level boolean `disabled` field for this control
([Apache APIUser source](https://github.com/apache/guacamole-client/blob/1.6.0/guacamole/src/main/java/org/apache/guacamole/rest/user/APIUser.java)).
Project Users forms no longer reset when background jobs change status.

## Workflow audit against AGENTS.md (2026-09-29)

The dashboard can build ordinary staging guests, run reviewed automation, and
provide scoped Guacamole access. It is **not yet verified as a complete custom
network lab release and rollout interface**. The checks below distinguish source
support from live acceptance evidence; a passing account test does not prove a
release rollout or a working guest desktop.

| Required manager workflow | Current authoritative evidence | Finding |
| --- | --- | --- |
| 1. Create/edit staging VMs and containers | `gui/app.js` scenario forms; `cmd_scenario_add/edit`, `cmd_container_add/edit` | Implemented. VM edit does not expose IP or custom-network placement; container edit does. |
| 2. Approved images and bounded resources | Template selectors and allowlists; `validate_project_quota`; `tests/test_quota.py` | Project quotas enforced before desired-state writes. No cluster-wide aggregate resource reservation or capacity scheduler was found. Captured CT images use node-local storage. |
| 3. Networks, static IPs, services, health | `validate_custom_networks`, scenario/container validators, fixed ping/TCP/HTTP checks | Containers and jump boxes can use custom networks. Scenario VMs use their team's standard subnet. Services must be configured inside guests; the GUI offers health definitions rather than arbitrary scripts. |
| 4. Arbitrary VM/container start and stop | Team pages and job schemas; `cmd_release_test_check` boot permutations | Jump-box desired start/stop is exposed. Individual scenario VM/container start/stop controls are absent from the GUI; release tests have fixed boot-order operations. |
| 5. Complete immutable staging release | `cmd_release_capture`, `load_release`, captured image allowlists | Guest disks and definitions are captured with integrity checks. Custom router/network definitions are not captured. Capture assumes every guest's address is `10.P.254.H`, so a container on a custom staging subnet cannot be captured. |
| 6. Fresh-clone release testing | `cmd_release_test_prepare/check/cleanup`; live release/test files inspected | Current source supports VM/CT clones and boot permutations. Live `image-smoke-20260928` is schema 2 with one VM and a passing result dated 2026-09-28. This does not prove the current schema-3 VM+CT+custom-network workflow. |
| 7. Explicit rollout to selected teams | `cmd_release_deploy`; project Terraform plans | Selected-team clone declarations require a passing release test. Old release resources are not removed automatically, so same-address upgrades can fail uniqueness validation. Staging deployment uses the project plan but now rejects changes to ordinary teams, the project router/transit, or unrecognized resources before apply. General Project Overview apply remains explicit. |
| 8. Jobs, usage, releases, rollback | Jobs modal/list, `crange_server.py` project summaries, release table | Job output and release summaries exist. Displayed CPU/memory totals and running/stopped labels describe desired state, not live utilization/status. No dedicated rollback workflow or durable per-team rollout history is exposed. Persistent data disks remain outside the release model. |

### Guacamole lifecycle fixes from this audit

- Worker jobs now run `guac-sync` and `guac-apply` after a successful apply,
  staging deploy, project configure, or team configure. This reconciles deployed
  connections and separate native-account permissions without restarting Guac.
  A refresh failure marks the job failed and preserves the infrastructure output;
  the message explicitly states that the infrastructure operation completed.
- Project Users now has a one-click **Refresh connections** action backed by the
  fixed `guac-refresh` job. A live manager browser queued it, saw a successful
  worker result, and kept the job modal open until close. Desktop and 390px mobile
  checks passed, and typing in the user form survived background polling.
- Release VM and CT clone definitions now preserve `guac_protocol`; legacy
  releases without it retain the SSH default. VM/CT edit forms now preselect the
  current protocol so editing health checks does not silently change RDP to SSH.
- Regression tests cover SSH/RDP preservation for both guest kinds, successful
  post-deploy reconciliation, refresh failures, and avoiding refresh after an
  unsuccessful infrastructure operation. The current suite contains 62 tests.
  No Terraform apply, live release capture, or guest restart was performed during
  these account/connection checks.

### Remaining acceptance work

The staging-plan scope gate and full change review are now implemented. Preserve
custom topology in capture/test/rollout, support replacement of prior team
releases explicitly, and expose VM/container lifecycle and rollback controls.
Run an isolated live acceptance scenario containing a VM, a container, a custom
network, a staging jump box, and a separate Guacamole user. Verify capture,
fresh-clone health tests, selected-team rollout, real SSH/RDP session establishment,
access revocation, and rollback while recording that an unselected team remains
unchanged. Older one-VM smoke results and the current account checks are useful
partial evidence, but do not establish that full scenario.


### Staging scope gate verification

`crange_staging.staging_blockers` classifies the actual Terraform JSON using both
prior and desired network ownership. It permits staging guests, staging routers,
staging VNets/custom networks, and the required SDN apply resources. Ordinary
team resources, project-router/transit mutations, unknown resource addresses, guest
moves from a team into staging, and unproven prior ownership are rejected.
Destruction/replacement flags do not override this gate. Deleted custom-network
resources are classified from their router's prior uplink and downstream bridge.

`cmd_stage_deploy` enables the gate for both saved and newly generated plans.
Saved plans are checked after binary/input integrity validation and immediately
before apply; a manifest claiming there are no team changes cannot bypass the
actual plan check. The GUI's Stage Changes table now includes every changed
resource, including deleted guests and ordinary-team guests. New plans report
staging blockers and disable the staging deployment button with an explanation.

The live Project 3 plan was generated and inspected without apply. Its pending
changes were a staging VM create, staging VM update, and staging container
create; the gate accepted their actual provider JSON. Those user declarations
were not deployed. Browser checks against deployed assets with an injected
ordinary-team change confirmed the resource is visible and the staging deploy
button is disabled at desktop and 390px mobile widths. Ten new tests cover
staging resources, custom-subnet deletion, team/shared/unknown changes, moving
team guests, inconsistent plan networking, saved-plan enforcement, fresh-plan
enforcement, and enabling the guard in the combined workflow. All 72 tests pass.
The remaining full release acceptance scenario is still outstanding.


### Live remote desktop acceptance

A temporary project manager created a separate native Guacamole account through
`POST /api/guac-users`; the queued account job succeeded. A fresh browser signed
into Guacamole with that project's inferred login and selected the granted
`P003 · staging-j01` connection. The RDP server presented the guest login for
`p003t254-jump-01`, accepted the guest's separate OS credentials, and resumed its
desktop. Keyboard input opened a terminal and ran only
`printf 'CRANGE_SESSION_VERIFIED\n'; hostname`. The rendered terminal showed the
marker and `p003t254-jump-01`, proving an interactive remote session rather than
only an open TCP port or an authenticated Guacamole connection list.

The ready login screen was visually inspected: CRANGE heading, Username and
Password labels, dark input backgrounds, contrasting text, and monospace font
match the dashboard. Screenshots of login and the interactive guest terminal
were captured locally for verification. The test terminal was closed and the
guest desktop returned to its original locked state; the temporary native and
dashboard accounts were removed. No infrastructure apply or VM restart occurred.
The controller web, worker, and ticket broker services remained active.

Jump-box controls currently manage CPU/RAM, node, desired power state, and
standard/custom network placement. Their OS base is the platform template
wired in Terraform (`template_id = 112`), rather than a project-selectable custom
jump-box image. This is a further limitation for projects needing a different
jump-box OS. The existing RDP acceptance proves the deployed staging jump box;
it does not certify every future image or protocol choice.
