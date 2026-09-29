# CRANGE webapp capability audit

## Implemented paths

| Capability | Evidence | Result |
| --- | --- | --- |
| Create and edit custom labs | Scenario VM/CT and custom-network job schemas, Team 0 forms | Implemented |
| Review and stage infrastructure | Fixed Terraform plan IDs, stale-plan detection, staging deploy workflow | Implemented |
| Propagate a lab | Immutable release capture, fresh-clone release tests, selected-team release deploy | Implemented |
| Manage teams and jump boxes | Team pages, jump-box lifecycle jobs, team-scoped configure workflow | Implemented |
| Default Guacamole access | Team membership selects the lowest-numbered team jump box | Implemented |
| Additional Guacamole access | Per-user jump-box, VM, and container grants with project validation | Implemented; isolated live grant tested |
| Revoke access safely | CRANGE ownership sidecar and grant removal | Implemented; isolated live revoke tested |
| Use Guacamole | Authenticated dashboard launch link and connector status | Partial; users sign in separately |
| Health validation | Ping default plus TCP/HTTP checks and container/VM validation jobs | Implemented |

## Live evidence and remaining gaps

The live Guacamole host runs 1.6.0. The CRANGE theme is loaded at startup, and
the controller's protected connector successfully authenticated and applied
five Project 3 connections. Their IDs and ownership are recorded in the
project sidecar; unrelated legacy Guacamole connections remain untouched. TCP
checks from the Guacamole host reached all three RDP jump boxes and the
deployed Team 1 and Team 0 SSH scenario guests after narrow router rules were
applied.

The project still has only the administrator in CRANGE. Guacamole currently
has its administrator and the CRANGE service identity, so per-person access
and revocation have not been exercised against a real project user. An
isolated temporary user did pass the complete CLI flow: Team 1 granted only its
default jump box, then removing team membership revoked that grant. Users must
still be provisioned separately in Guacamole, and the dashboard link opens its
login page without single sign-on. This is the main integration gap.

The GUI and CLI contain the fixed workflows for defining VMs/containers and
custom networks, reviewing/staging Terraform, capturing releases, testing fresh
clones, and deploying releases to selected teams. The live Terraform review
also found two staged Team 0 resources and one pending VM update; those changes
were left unapplied. Before calling the webapp ready for project users, add a
controlled Guacamole identity/login flow and verify per-user allow/revoke with
a non-admin account, then run an end-to-end release test and selected-team
rollout in an approved project window.
