# Project 100 — Meridian Harbor Logistics

Status: implementation in progress. This document describes the intended lab;
runtime deployment and acceptance evidence are recorded separately.

## Required outcome

- Five isolated teams, numbered 1–5, with two assessors each.
- Each assessor owns a Linux jump box (SSH and RDP) and a Windows jump box
  (RDP only): twenty participant jump boxes in total.
- Team 0/network 254 is the staging environment. Changes there become immutable
  releases, are tested with fresh clones, and are explicitly deployed to teams.
- A believable Windows/Linux business environment with Active Directory,
  application security, realistic operational mistakes, and synthetic data.
- Project 100 is named **Meridian Harbor Logistics** in the dashboard.
- Exercise and verify the dashboard features, fix discovered bugs, and retain
  an audit trail. A feature is not verified merely because its button exists.
- Deliver a participant company packet and separate private operator notes.

## Business story

Meridian Harbor Logistics (MHL) is a regional freight and cold-chain logistics
company. It acquired a smaller warehouse operator, kept its deployment tools,
and connected them to the main Windows domain. A small IT staff maintains the
customer tracking portal, dispatch schedules, finance shares, and a homegrown
build/backup system. The company has commissioned Northstar Security Assurance
to assess the combined environment before onboarding a pharmaceutical client.

Use `mhl.test` for each team's independent AD/DNS environment. All company data,
employees, shipments, customers, contacts, and credentials are synthetic.

## Target blueprint

| System | Role | Intended OS |
| --- | --- | --- |
| MHL-DC01 | AD DS, DNS, company users/groups and service accounts | Windows Server |
| MHL-FS01 | Dispatch/finance shares and a legacy intranet component | Windows Server |
| MHL-DB01 | Shipment, finance and customer databases | Linux/PostgreSQL |
| MHL-APP01 | Customer shipment portal and internal operations API | Linux |
| MHL-CI01 | Acquired warehouse build scripts and artifact workflow | Linux |
| MHL-BKP01 | Operational backups, exported reports and monitoring | Linux |

Participant jump boxes are assessment workstations, not business targets.
Windows and Linux templates must be actually installed, clone-tested, and
prepared before the corresponding declarations are offered as ready.

Within each standard team LAN use DC `.10`, file server `.20`, database `.30`,
application `.40`, CI `.50`, and backups `.60`. Team 0 uses the same host octets
on network 254. Any additional warehouse subnet must be declared and captured
with the release, not added through an unmanaged host route.

The current no-secret assignment draft is [`scenario-draft.json`](scenario-draft.json).
It records all six Team 0 business hosts, ten assessor accounts, and twenty
participant boxes. Linux jump boxes are declared in Project 100 at `.11` and
`.13`; their paired Windows boxes are reserved for `.12` and `.14` after the
Windows base image is published. The draft does not assign a Windows image or
credential value before those are available through the approved workflow.

## Design principles

The network uses normal routing and team isolation; do not exploit or weaken
the hypervisor, controller, Guacamole, shared firewall, or other teams. Business
segmentation should reflect a rushed acquisition: broad trust between servers
and excessive reach from warehouse tooling, rather than intentionally broken
network infrastructure.

Application issues should arise from plausible implementation and operational
mistakes: inconsistent tenant authorization, unsafe legacy report queries,
exposed deployment artifacts, excessive service-account privileges, and backup
permissions that exceed business need. Keep packages on supported versions;
do not depend on a single deliberately obsolete network CVE. Private operator
notes will describe the final verified paths without spoiling the participant
packet.

## Execution and evidence

1. Check shared infrastructure and clean only confirmed disposable artifacts.
2. Verify Windows media/templates and Parrot/Linux image readiness.
3. Initialize Project 100 with an automatically allocated transit address.
4. Bootstrap owned routers/VNets using a reviewed plan, configure isolation,
   and verify the exact management and team network paths.
5. Build and configure staging business systems, health checks and users.
6. Capture, fresh-clone test, deploy and health-check an immutable release.
7. Create ten users and their two-box default setup; verify Guacamole grants
   for each assessor and absence of cross-team access.
8. Exercise dashboard workflows, including safe edit/delete/cancel/snapshot/
   rollback tests on explicit disposable fixtures.
9. Audit the deployed topology, access, application behavior, documentation,
   and all deliverables before marking the goal complete.

Resource sizing and node placement will follow measured available capacity.
Node suggestions remain preferences. Do not bypass image, provider, capacity,
ownership, or actual network-verification checks to make a plan pass.

## Progress files

- `platform-progress.md`: live project, Linux/network work and deployment.
- `windows-progress.md`: Windows media, installation, AD and guest readiness.
- `FEATURES.md`: dashboard feature acceptance matrix.
- `CHANGES.md`: concise implementation fixes and operational changes.
- `COMPANY-PACKET.md`: participant-facing engagement document.
- Private operator solution/credential material belongs outside Git.
