# MERIDIAN HARBOR LOGISTICS

**Authorized scope register · MHL-NSA-100-03 · Revision 0.1**

**Draft — awaiting IT acceptance and engagement-window confirmation.**

This register accompanies the MHL assessment engagement packet. The service
desk must confirm the assessment window and issue your individual remote-access
credentials before work begins. Credentials are delivered separately.

## Authorized business systems

| System | Address | Business function |
| --- | --- | --- |
| MHL-DC01 | `10.100.3.10` | Directory and internal DNS |
| MHL-FS01 | `10.100.3.20` | Dispatch, finance shares and intranet |
| MHL-DB01 | `10.100.3.30` | Operations database |
| MHL-APP01 | `10.100.3.40` | Shipment portal |
| MHL-CI01 | `10.100.3.50` | Warehouse build and artifact service |
| MHL-BKP01 | `10.100.3.60` | Operations backup service |

Authorization is limited to the six targets above. Network connectivity does
not expand scope. Gateways, remote-access services, virtualization hosts,
controllers, shared infrastructure and public Internet services are excluded.

## Application and directory references

- Shipment portal: `http://10.100.3.40:8080/`.
- Company intranet: `http://10.100.3.20/`.
- Warehouse build service: `http://10.100.3.50/`.
- Internal Windows domain: `mhl.test`.
- Internal directory/DNS server: `10.100.3.10`.
- Supplied synthetic customer reference: `4101`.

Use the internal DNS server explicitly when resolving company names if your
assessment workstation uses another resolver. IP-based web URLs allow initial
access independently of workstation DNS settings.

## Remote assessment workstations

The service desk assigns two workstations to each assessor through the
remote-access gateway. Your account's connection list identifies your assigned
workstations. Potential addresses in this zone are `10.100.3.11` and
`10.100.3.13` (Linux), and `10.100.3.12` and `10.100.3.14` (Windows).
These are assessment tools, not business targets. Do not assess another person's
workstation or attempt to expand your gateway permissions.

## Evidence and operating limits

Follow the engagement packet's rules of engagement. Use only synthetic records
needed for a controlled proof. Coordinate disruptive changes, persistence,
account lockouts and bulk changes with MHL IT first. Send credentials and
sensitive evidence through the service desk's designated private channel.
