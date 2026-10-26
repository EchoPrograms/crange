# Components and how they fit together

CRANGE has a browser control plane over reviewed infrastructure automation. The browser edits validated declarations and submits known workflows; it never receives Proxmox/OPNsense credentials or a general controller shell.

![CRANGE component and control-flow diagram](/img/architecture.svg)

## Technology map

| Component | Technology | Responsibility |
| --- | --- | --- |
| Control UI | HTML, CSS, vanilla JavaScript | Project context, forms, inventory, progress and remote launches |
| Same-origin API | Python HTTP server | Sessions, CSRF, roles, validation and sanitized views |
| Durable queue | Python workers + SQLite | Typed operations, auditing, project lanes and recovery |
| Infrastructure | Terraform, pinned `bpg/proxmox` 0.104.0 | Per-project VNets, routers, VMs/containers and reviewed plans |
| Platform configuration | Ansible | Router paths, firewall baseline, supported guest initialization/access |
| Virtualization | Proxmox VE, QEMU/KVM and LXC | Native guest lifecycle, disks, templates, backups and tasks |
| Networking | Proxmox SDN/VXLAN, Debian routers/UFW | Isolated project/team/custom LANs and permitted management paths |
| Shared edge | OPNsense API | Project aggregate static routes/gateways; independent firewall policy |
| Remote access | Apache Guacamole, guacd, connector/theme extensions | Browser SSH/RDP/VNC sessions and connection authorization |
| Secrets | Protected local files, `cryptography`, private broker | Worker credentials, sealed guest secrets and launch tickets |
| Image distribution | Immutable library archives, node-local caches, SQLite reservations | Verified image identity, capacity admission and reusable local bases |
| Service supervision | systemd and narrowly scoped relays | Web/worker separation, refresh timers and fixed privileged operations |
| Documentation | Docusaurus, React, local search index | This independent static handbook |

## A normal action

The API authenticates a session, checks project role and CSRF, validates a known operation, and stores a job. A worker claims it in the appropriate lane and rechecks the actor. It reads protected project/catalog/policy state, runs a fixed workflow, and streams sanitized progress back to the queue.

A review binds Terraform inputs/state, source identity, placement and reservations. Apply consumes the exact accepted plan, not arbitrary browser-supplied Terraform. Ansible uses applied inventory and correct router order. Guacamole publication uses deployed endpoints and explicit user grants.

The UI reads sanitized worker-published cluster snapshots. HTTP handlers do not perform direct Proxmox/NFS discovery while rendering Cluster. Browsing does not copy images.

## Separation of services

The installed web account is `crange-web`; the main automation worker is `crange`. Independent account and Linux box-user workers handle their bounded operations. A privileged network worker and host-side Unix relays expose only reviewed, fixed operations with peer and ownership checks. Those components are not permission for arbitrary manager shell execution.

Each project has its own real Terraform environment/state. A clean project template must never contain an existing project's state. Shared library infrastructure has separate state and must not be targeted by project deletion/capture.

See [source map](../reference/source-map.md) for implementation files and [state locations](../reference/state.md) for persistent records.
