---
slug: /
title: CRANGE handbook
---

# CRANGE handbook

CRANGE turns a Proxmox cluster into isolated, repeatable cyber-range labs. Build a scenario in **Team 0**, capture it as a release, test fresh copies, then deploy the selected version to teams through **CRANGE Control**. Participants use **CRANGE Access**, powered by Guacamole, to connect to their assigned machines.

Start with [your first lab](start/first-lab.md). It follows the web interface from staging to team deployment. Use the [interface tour](start/interface.md) to find an action, or [troubleshooting](admin/troubleshooting.md) when a task fails.

![The real CRANGE scenario workspace with illustrative demo data](/img/screenshots/workspace.png)

## Find your path

| You want to… | Read |
| --- | --- |
| Understand the basic workflow | [Overview](start/overview.md) |
| Create and configure scenario machines | [Staging](how-to/staging.md) |
| Set team-derived addresses and networks | [Networks](how-to/networks.md) |
| Save a version and prove it restores | [Releases](how-to/releases.md), [fresh-copy tests](how-to/release-tests.md) |
| Deploy, upgrade or roll back teams | [Team deployments](how-to/deployments.md) |
| Give people access | [Users and remote access](how-to/access.md) |
| Install or operate the controller | [Controller setup](admin/controller.md) |
| Understand components and state | [Architecture](concepts/architecture.md), [state reference](reference/state.md) |
| Find every documented capability | [Feature map](reference/features.md) |

## Scope and status

This handbook describes the source and interface reviewed on **October 26, 2026**. An installed controller may run a different revision. Each deployment also has its own approved images, placement rules and verified network scopes.

Screenshots use the **actual checked-out interface with synthetic demo data**. They illustrate where to work; they are not live capacity readings, deployment acceptance evidence or credentials. Historical incident records elsewhere in the repository are supporting evidence, not current installation instructions.

CRANGE is still evolving. The guides identify actions that edit desired state, actions that change running resources, and workflows that still need more than one reviewed step. The [application boundary](concepts/application-boundary.md) explains the intended division between platform networking and manager-owned application configuration, including a current legacy exception.

This is a local **Docusaurus** site. Navigation, screenshots and search are bundled in the static build. Read [how to run and maintain it](reference/maintaining.md); no Docusaurus hosting account or Read the Docs service is involved.
