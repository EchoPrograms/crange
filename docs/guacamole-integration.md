# Guacamole integration

CRANGE treats Guacamole as a controlled projection of project desired state.
`guac-sync PROJECT` renders `guacamole/access/project-NNN.json`; `guac-apply
PROJECT` reconciles that manifest through the protected controller connector.

Team assignments grant the lowest-numbered jump box in each assigned team.
The project Users page also stores additive connection grants for declared jump
boxes, scenario VMs, and scenario containers. Empty health checks normalize to
a five-second ICMP ping. Scenario guests may select SSH or RDP when declared.

The worker records ownership in `project-NNN.applied.json`. It updates and
removes only connections in that sidecar, rejects collisions with unrelated
Guacamole connections, and removes only permissions previously managed by
CRANGE. Undeployed resources are omitted until Terraform reports an address.
Router policy admits Guacamole SSH only to deployed guests whose declared
protocol is SSH; removed target rules are pruned on the next router configure.

The connector credentials are configured on the controller in a mode-0600
file. The live connector identity is `crange-sync`; the live host uses
Guacamole 1.6.0 with PostgreSQL authentication. The CRANGE theme extension is
installed from `/opt/guacamole/extensions` in the container's
`GUACAMOLE_HOME` template and is confirmed loaded at startup.

Identity provisioning remains a manual prerequisite: every project user must
already exist in Guacamole. CRANGE currently synchronizes team defaults and
explicit resource grants, but it does not copy passwords or create Guacamole
accounts. The dashboard's Guacamole link opens the Guacamole login page, so
users need their Guacamole credentials. A real project user's allowed list and
revocation have not yet been validated because the CRANGE auth database has no
project users. An isolated temporary account verified the full CLI grant and
revocation flow; current live connections are applied and ownership-tracked.
