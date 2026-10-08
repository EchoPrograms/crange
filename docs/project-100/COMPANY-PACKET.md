# MERIDIAN HARBOR LOGISTICS

**Office of Information Technology · Assessment Engagement Packet**  
**To:** Northstar Security Assurance, Technical Assessment Team  
**Reference:** MHL-NSA-100 · Revision 0.1 · Draft pending environment acceptance

## Letter of engagement

Meridian Harbor Logistics requests an assessment of the technology supporting
our dispatch, customer shipment tracking, finance reporting, and recently
acquired warehouse operation. Our next major customer requires assurance that
customer records and internal business systems are appropriately protected.

Please assess the environment as a connected business. We need clear evidence
of business impact, practical recommendations, and a prioritized remediation
plan that a small IT team can carry out. A list of scanner findings alone will
not meet our objectives.

## Company context

We coordinate regional freight and temperature-controlled deliveries. Dispatch
staff rely on Windows domain accounts and shared documents; customers use a
Linux-hosted shipment portal. Warehouse deployment and backup tooling was
retained after an acquisition because replacing it would disrupt operations.
Integration was performed under a tight schedule and documentation is uneven.

## Points of contact

| Contact | Responsibility | Contact channel |
| --- | --- | --- |
| Evelyn Vale, Director of Technology | Executive sponsor and scope decisions | security@meridianharbor.example |
| Jonas Reed, Infrastructure Lead | Windows/domain and server operations | operations@meridianharbor.example |
| Mira Chen, Service Desk Lead | Assessment coordination and incident escalation | servicedesk@meridianharbor.example |

The organizer-provided engagement channel is the operational channel during
the assessment. These company contacts are fictional scenario identities.

## Authorized environment

Each assessment team receives a separate company environment. Its assigned
system addresses, custom subnets, credentials and engagement window will be
provided in the final environment schedule. The Windows domain is `mhl.test`.
Only that team's listed company targets are in scope. The overall Project 100
aggregate is `10.100.0.0/16`; it is not permission to access another team's
systems or assessment workstations.

Expected company systems are MHL-DC01, MHL-FS01, MHL-APP01, MHL-CI01 and
MHL-BKP01. Assessors receive personal Linux and Windows workstations through
the designated remote-access portal. Access to a workstation does not confer
authorization over the portal or its underlying infrastructure.

## Rules of engagement

- Assess only the assigned targets and supplied application URLs/accounts.
- Use controlled proofs of concept and synthetic records to demonstrate
  impact; explain the attack conditions and resulting business risk.
- Preserve service availability and evidence. Coordinate before disruptive
  changes, account lockouts, bulk data changes, or persistence testing.
- Stop and report access to an unexpected environment or another team's data.
- Exclude denial of service, destructive encryption, uncontrolled malware,
  social engineering of real people, and attacks on public Internet systems.
- Hypervisors, storage infrastructure, the range controller, Guacamole,
  shared firewall, underlying management networks and other teams are out of
  scope. Do not attempt to escape a guest or compromise these systems.

## Requested deliverables

1. An executive summary describing material business risks.
2. A technical report with affected assets, reproducible evidence, realistic
   impact, severity rationale, and remediation.
3. A connected attack narrative where findings combine into greater impact.
4. A prioritized remediation roadmap, separating immediate containment from
   longer-term improvements.
5. A short technical handoff and executive briefing.

Use screenshots and minimal proof-of-concept artifacts where useful. Keep
credentials out of general report copies and submit sensitive material through
the organizer's designated channel. Include assumptions, limitations, and
unverified findings explicitly.

---

**Simulation notice:** Meridian Harbor Logistics and Northstar Security
Assurance are fictional. All business data is synthetic. This draft does not
claim that the laboratory has been deployed or accepted; the final target
schedule and engagement window are issued after technical acceptance.
