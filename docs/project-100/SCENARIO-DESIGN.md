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

## Planned assessment paths

| Mistake | Business explanation | Required proof before acceptance |
| --- | --- | --- |
| Shipment/invoice tenant checks inconsistent | Legacy API assumes the portal already checked ownership | One supplied customer account can read another synthetic customer's record |
| Unsafe legacy report query | Warehouse report code was imported without query parameterization | Controlled SQL proof against synthetic database records |
| Application DB role too privileged | Schema-migration privileges were not removed from runtime credentials | Actual DB role privileges and a controlled business-impact demonstration |
| Build artifacts expose configuration | CI troubleshooting bundles include environment/configuration files | An actual accessible artifact with a scoped synthetic service credential |
| Backups readable beyond the backup team | Warehouse support was added to simplify recovery | Actual permission and a readable synthetic sensitive backup |
| Windows automation consumes writable content | Dispatch and IT share an operations directory | Actual share/NTFS ACL and controlled test of the scheduled consumer |
| Helpdesk delegation covers privileged staff | The admin account was placed in the ordinary Staff OU | Actual AD permission plus a reversible proof on an explicit test account |

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
