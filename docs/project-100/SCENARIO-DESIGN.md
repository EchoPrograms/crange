# Meridian Harbor Logistics — operator design notes

These are intended assessment paths, not a claim that the vulnerabilities or
services are deployed. Replace planned items with verified observations as the
lab is built. Runtime credentials and keys stay outside Git.

## Business systems

The public-facing shipment portal accepts customer accounts, shipment queries,
delivery notes and invoice requests. Dispatch staff use an internal API backed
by MHL-DB01. MHL-FS01 retains finance and dispatch shares; a scheduled export
syncs reports for a small acquisition-era build system on MHL-CI01. MHL-BKP01
stores operating backups and monitors service availability. AD supplies staff
and service accounts throughout the company.

Create synthetic customers, invoices, refrigerated shipments, delivery issues,
employee directories, support tickets, quarterly reports and deployment notes.
Interfaces and documents should consistently use the MHL name and story.

## Staging observations — 2026-10-16

Read-only HTTP checks against the actual staging application establish two
implemented paths: requesting shipment 61002 with customer 4101 returns the
other customer's record (4102), and a bounded read-only report filter changes
the result from one delayed shipment to all three synthetic shipments. No write
requests or database mutations were used. Evidence:
[staging application checks](staging-application-acceptance.json).

The build artifact check returned 404. Nginx logged permission denied: the
symlink task changed the target directory's group to root. The fixed role now
uses `follow: false` and verifies the published artifact with HTTP. The role is
deployed; dashboard configuration job `6f388022951145fc88f279cd87ad817f`
succeeded. Repeat HTTP acceptance now returns 200 and verifies the synthetic
deploy identity/token fields without recording the token. A new immutable
release is still required to carry the corrected recipe. Release
0.1.1 retains its original hash-checked recipe and is not silently patched.

## Assessment paths and remaining proof

| Mistake | Business explanation | Required proof before acceptance |
| --- | --- | --- |
| Shipment/invoice tenant checks inconsistent | Legacy API assumes the portal already checked ownership | One supplied customer account can read another synthetic customer's record |
| Unsafe legacy report query | Warehouse report code was imported without query parameterization | Controlled SQL proof against synthetic database records |
| Application DB role too privileged | Schema-migration privileges were not removed from runtime credentials | Actual DB role privileges and a controlled business-impact demonstration |
| Build artifacts expose configuration | CI troubleshooting bundles include environment/configuration files | An actual accessible artifact with a scoped synthetic service credential |
| Backups readable beyond the backup team | Warehouse support was added to simplify recovery | Actual permission and a readable synthetic sensitive backup |
| Windows automation consumes writable content | Dispatch and IT share an operations directory | Actual share/NTFS ACL and controlled test of the scheduled consumer |
| Warehouse deployment can affect the whole forest | The Warehouse Deployment GPO was linked at the domain root for convenience, and WarehouseDeployers received GpoEdit rights | Actual domain-root link and delegated permission, followed by a reversible proof on an explicit test computer |

These should form connected, believable paths across the application, Linux
hosts, Windows shares and directory. Avoid placing every service account in
Domain Admins or depending on a single obsolete exploit. Modern network and
host defaults can coexist with meaningful application and authorization errors.

## Guardrails and completion evidence

- Business target networks must route correctly but deny other teams and
  infrastructure. Broad business trust is modeled within a team's company.
- Assessment workstations receive their owner's Guacamole permissions only.
- Preserve normal Windows firewall/SMB security unless a specific documented
  business configuration calls for a bounded change.
- Each finding needs an actual implementation check and a minimal reversible
  proof; a comment saying “vulnerable” is insufficient.
- Health checks cover AD/DNS, RDP/SSH, portal identity, database service, backup
  availability and the captured release's fresh-clone behavior.
- Operator credentials, reset secrets, certificates and sensitive solution
  material remain in a protected runtime directory outside the repository.

## Release and reset

Capture the configured staging company as a release. Use fresh team copies,
verify IP/DNS rewriting and independent team authentication, then perform a
selected-team upgrade/rollback exercise without touching the other four teams.
Snapshot/revert and destructive account/delete tests use named disposable
fixtures, followed by confirmed cleanup. The final running lab must contain
the intended company systems and twenty participant jump boxes, not leftover
acceptance probes.


## Deployment checkpoint — October 18

All five six-server company copies passed their declared15-check health matrices
(75 checks total), with immutable service recipes replayed through the dashboard's
configuration recovery. See release-0.1.2-five-team-health-acceptance.json.
Actual deep HTTP acceptance from the controller timed out during a subsequent
HDD flush; no five-company application proof is accepted yet. Proxmox6's P420i
reports failed cache backup capacitors and permanently disabled write caching.
Resolve/reassess current storage responsiveness before delivery acceptance.

The CI bundle contains a generated synthetic token, but the current build recipe
only publishes it; a real authentication consumer for that identity has not been
implemented/proven. The scoped-credential assessment path remains incomplete.
Do not claim that possession of the string provides authenticated access. Fix
through configured staging and a new immutable recipe/release, preserving the
existing deployed release provenance.

### October 18 — artifact credential authentication consumer

The earlier token-only limitation is fixed in staging by the reviewed build
role. Its HTTP artifact token authenticates warehouse-bot over local SSH;
acceptance checks its real unprivileged UID and lack of administrative groups.
No secret values are stored in evidence. Release 0.1.3 publishes this service
recipe while retaining the six immutable 0.1.2 source images. The five company
environments remain on 0.1.2 until fresh testing and explicit rollout pass.
See staging-ci-artifact-authentication-browser-acceptance.json and
release-0.1.3-service-update-acceptance.json. Participant-path exploitation and
cross-network isolation still require separate acceptance.


Independent read-only HTTP checks now passed in all five deployed 0.1.2 company
environments: database health, the intended cross-customer shipment read, the
bounded report-query behavior (one normal row versus three broadened rows),
and the published artifact identity/token presence. Evidence records no token
values. Authentication binding is proved in staging only until 0.1.3 rollout.
See [five-company application checks](five-company-application-acceptance.json).
