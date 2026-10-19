# MERIDIAN HARBOR LOGISTICS

**Office of Information Technology · Assessment Engagement Packet**<br>
**To:** Northstar Security Assurance<br>
**Reference:** MHL-NSA-100 · Revision 0.2 · Controlled copy

## Letter of engagement

Meridian Harbor Logistics requests an assessment of the technology supporting
our dispatch, customer shipment tracking, finance reporting, and recently
acquired warehouse operation. Our next major customer requires assurance that
customer records and internal business systems are appropriately protected.

Please assess the environment as a connected business. We need clear evidence
of business impact, practical recommendations, and a prioritized remediation
plan that our IT department can carry out. A list of scanner findings alone will
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

Please use the engagement coordination channel supplied by the MHL service desk
for routine questions, evidence transfer, and incident escalation.

## Authorized environment

The scope register supplied with your account handover identifies the approved
systems, application URLs, credentials, and assessment window. The internal
Windows domain is `mhl.test`. Authorization applies only to assets named in that
register; network connectivity alone does not expand the scope.

Company systems include MHL-DC01, MHL-FS01, MHL-DB01, MHL-APP01, MHL-CI01 and
MHL-BKP01. The service desk provides access to assessment workstations through
our remote-access gateway. The gateway and supporting infrastructure are
outside scope unless separately authorized in writing.

## Rules of engagement

- Assess only the assigned targets and supplied application URLs/accounts.
- Use controlled proofs of concept and the minimum records necessary to demonstrate
  impact; explain the attack conditions and resulting business risk.
- Preserve service availability and evidence. Coordinate before disruptive
  changes, account lockouts, bulk data changes, or persistence testing.
- Stop and report access to systems or data outside the approved scope register.
- Exclude denial of service, destructive encryption, uncontrolled malware,
  social engineering of real people, and attacks on public Internet systems.
- Exclude infrastructure and third-party services not listed in the scope
  register. Any proposed expansion requires written approval from MHL.

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
MHL's designated engagement channel. Include assumptions, limitations, and
unverified findings explicitly.

---

**Evelyn Vale**<br>
Director of Technology<br>
Meridian Harbor Logistics
