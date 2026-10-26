# Controller setup and services

CRANGE builds on an existing Proxmox/OPNsense installation. It does not install a hypervisor cluster or configure the entire edge firewall. An administrator prepares scoped credentials, SDN, approved storage/images, SSH trust and a Debian controller before delegating projects.

## Prerequisites

Use a reviewed checkout accessible to the unprivileged `crange` account, Python 3 with the root `requirements.txt` dependencies, Terraform with the checked-in `bpg/proxmox` **0.104.0** lock, and Ansible with the required collections. Guest/node management paths, scoped API roles, approved templates and router bases must be ready.

The current installation has six nodes, `proxmox1` through `proxmox6`, and controller `10.60.0.20`. Those are installation details, not proof that any arbitrary node/image/network combination is admitted. Read the current distribution policy and acceptance records.

## Install services

The worker account must be unprivileged and have a same-name group. On an existing installation inspect its units and wait until jobs are idle: `served` installs/restarts services.

```sh
sudo scripts/crange served --user crange
sudo -u crange-web -H scripts/crange password admin --admin
sudo -u crange-web -H scripts/crange password manager --projects 3 --role manager
systemctl status crange.service crange-worker.service
```

Passwords are prompted rather than passed as shell arguments. The installer separates `crange-web` from the automation account and creates shared queue/state directories. The installed web unit listens on **0.0.0.0:8000**; standalone `serve` defaults to loopback. Restrict management-network exposure and use an approved HTTPS reverse proxy. Installed secure cookies expect HTTPS; a plain HTTP tunnel may not retain a login without a deliberate local-only configuration.

Do not start a development controller worker against production alongside installed workers. Installed web uses `CRANGE_WORKER_EXTERNAL=1`; a standalone development server without it may start an embedded worker. This documentation's Docusaurus server is independent and starts no controller worker.

## Protected configuration

Keep worker files in `/home/crange/.config/crange/` with restrictive permissions. Proxmox API environment, OPNsense API credentials, Guacamole connector, launch-ticket key and guest credential encryption are separate secrets. The web account must not read the infrastructure credentials. Do not commit or print them.

Configure Guacamole with `scripts/crange guac-config`. Broker launch support needs matching protected JSON-auth configuration and the Guacamole extension. `scripts/install-guac-json-auth.sh --stage` prepares installation; its apply can restart Guacamole and disconnect sessions. Coordinate that maintenance.

## Service ownership

| Service | Purpose |
| --- | --- |
| `crange.service` | Same-origin web UI/API under `crange-web` |
| `crange-worker.service` | Main fixed infrastructure workflows |
| `crange-account-worker.service` | Independent Guacamole account reconciliation |
| `crange-box-users-worker.service` | Bounded Linux guest-account work |
| `crange-network-worker.service` | Privileged, fixed network verification/reconciliation |
| `crange-guac-broker.service` | Private launch/secret broker where configured |
| `crange-proxmox-compat.service` | Installed API compatibility/ID-claim adapter |
| `crange-cluster-refresh.service` / timer | Sanitized inventory refresh |

Additional router-HA/tag/host relay services have separate setup and protected policies. Use installed unit files as the authority for paths and optional components. Never restart workers to make a slow native restore finish.

## Verify the installation

Verify enabled accounts/roles, per-project state separation, queue ownership, protected file permissions, catalog/policy identity, router access and Guacamole publication. Establish disposable clone/network acceptance before admitting a new node/image combination. Then create a small staging guest through the normal UI and remove it with a reviewed cleanup.
