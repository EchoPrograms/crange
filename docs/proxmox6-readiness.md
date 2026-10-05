# Proxmox6 enrollment readiness

Proxmox6 was inspected read-only on 2026-10-05 through Tailscale `100.117.80.18`.
It is **not a member of the cyber-range cluster**. CRANGE does not include it
in the active placement policy or the five-node discovery requirement.

| Item | Observed state |
| --- | --- |
| PVE | 9.0.3, kernel 6.14.8-2-pve |
| CPU | Two Xeon E5-2697 v2 sockets, 48 logical CPUs, VT-x |
| RAM | 405.7 GB physical (about 378 GiB) |
| `local-lvm` | 656.8 GB local LVM-thin, images and rootdir |
| `local-hdd` | 4.8 TB local thick LVM, images and rootdir; about 2.65 TB free |
| Network | Tailscale `100.117.80.18`; Wi-Fi `10.211.195.17` is the current default route; `vmbr0` has `10.67.0.1/16`; wired ports are down |
| Existing workload | Running container 100, `testcontainer`, with a 2 TB disk on `local-hdd` |

The existing cluster already has **container 100 (`grafana`) on proxmox1**.
Proxmox's cluster join replaces the joining node's `/etc/pve` configuration
and requires it to have no guests. Preserve and back up Proxmox6's container,
then restore it with an unused cluster ID after the join. Keep its existing
service running until an explicit migration window. See the
[Proxmox VE Administration Guide](https://pve.proxmox.com/pve-docs/pve-admin-guide.pdf),
section *Adding Nodes to the Cluster*.

Before joining, move the host to its final wired address `10.0.0.106`, ensure
the final hostname resolves there, and verify stable cluster-network latency.
Proxmox recommends final hostname/IP configuration and a reliable physical
network for Corosync before joining. Do not use the current Wi-Fi default route
for cluster membership. After joining, restore the existing container, verify
its storage mapping, register the local pools only on Proxmox6, and test the
`crange` SDN/VXLAN and MTU 1450 end to end. The central NFS export must admit
`10.0.0.106` before image cache distribution can run there.

CRANGE can name Proxmox6 in desired state, but placement only admits nodes
listed in the protected active policy and backed by live network, storage,
provider, and guest-image acceptance. Proxmox6 remains excluded until those
checks pass. An offline, unused node must not affect placement on other nodes;
park or relocate its running guests before powering it down. Check cluster
quorum before routine shutdown once it has joined.
