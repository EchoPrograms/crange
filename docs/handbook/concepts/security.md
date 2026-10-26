# Authorization and trust boundaries

The web UI is a convenience layer, not an authorization boundary. API submission and worker execution both enforce roles, project scope and typed parameters. Infrastructure secrets remain on the controller under the appropriate service account.

## Roles and credentials

Administrators control the platform, templates, approvals, quotas and dashboard roles. Managers operate only their assigned projects. Participants receive authorized remote connections without direct Terraform, controller, Proxmox or OPNsense credentials.

Dashboard sessions use server-side records and CSRF protection for browser mutations. Project Guacamole accounts and guest OS accounts are separate. Guest passwords and custom connection credentials use sealed storage/broker paths rather than plaintext job options or audit output.

The web service runs as `crange-web`, and normal automation as `crange`. Private broker/host relays use protected Unix sockets, peer identity and fixed requests. Network verification has a separate privileged worker. Those components do not expose a general root-command field to managers.

## Guarded operations

Workflows validate project/team IDs, names, IP allocations, approved templates, CPU/RAM/disk bounds, quotas, network acceptance and exact resource ownership. Saved-plan and artifact identities prevent apply from silently changing reviewed targets.

Disposable probes have dedicated ownership/pool checks, fixed diagnostics and cleanup journals. Similar names or tags alone do not authorize guest execution/deletion. Unknown writes retain journals/reservations rather than assuming no allocation occurred.

The Guacamole bridge is a separately authenticated server-to-server path. A dashboard session alone does not authorize bridge calls. Monitor/launch URLs resolve against the authenticated user's allowed connection; the URL is not a bearer grant to a machine.

## Network policy

Debian router isolation and explicit management paths protect team boundaries. OPNsense static route/gateway automation is allowed; project workflows must not edit its firewall rules. Do not work around a missing connectivity acceptance record by broadly opening router or edge access.

## Administrator responsibility

Keep secrets outside Git and readable only by intended service accounts. Back up encrypted secret material and its keys separately from public source. Review privileged relay policies and source digests when upgrading fixed workflows. Use scoped API permissions rather than broad host-root credentials in the web application.

The documentation site serves public project documentation and synthetic screenshots only. It reads source syntax for references, not live controller state or credential files. It is independent of infrastructure authorization and does not submit jobs.
