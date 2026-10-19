# Proxmox 2 management network audit

**Observed:** 2026-10-08, 16:26 CDT. **Scope:** read-only inspection through the Proxmox 4 cluster relay. No network file, service, host, or guest was changed during this audit.

## Finding

P2 currently has its expected management address. `/etc/network/interfaces` contains `vmbr0` static address `10.0.0.102/24`, gateway `10.0.0.1`, bridge port `eno1`; `ip -br address` and `ip route` agree. The file's mtime and ctime are both 2026-06-12 17:06:09 CDT. The address was not persistently removed from the host configuration.

P2 did reboot on 2026-10-08. `last -x` and the journal place shutdown at 16:13 and the next boot at 16:15 CDT. The networking service configured interfaces successfully at 16:16:04–16:16:07. This confirms downtime during the observed recovery reboot. The reboot occurred after P2 had already been reported offline, so it does not explain the earlier runtime address loss; that cause remains undetermined. It also does not identify who requested the reboot or why.

Proxmox task history records `stopall` for CTs 113, 118, and 119 at 16:13:55 CDT, then `startall` at 16:16:17 CDT; both are `root@pam` and finished `OK`. The individual guest shutdown/start tasks also finished `OK`. The task metadata does not identify the human, API client, or automation job behind `root@pam`. A matching task-log file and useful pvedaemon/pveproxy journal attribution were not available in the inspected paths, so attribution remains undetermined. There is no evidence here that project automation changed the host management address or caused the reboot.

## Files and automation paths

- `/etc/network/interfaces` has the unchanged June timestamp above. It sources `/etc/network/interfaces.d/*`.
- `/etc/network/interfaces.d/sdn` has `#version:27`, mtime/ctime 2026-10-08 01:37:51 CDT, and contains only Proxmox SDN bridges/VXLANs, including P100 VNets. It has no host management address or gateway. No backup of the main interfaces file was found in `/etc/network`.
- `/etc/network/interfaces.d/crange-probe` was created 2026-10-02 18:34:03 CDT and declares an isolated, unaddressed `crprobe1450` bridge.
- Repository search found two host-network file writers: Proxmox SDN apply resources (`proxmox_sdn_applier`) manage the generated SDN drop-in; `ansible/library-probes-host.yml` writes the isolated probe drop-in. Neither declares or edits `/etc/network/interfaces` or `vmbr0` addressing. Router Ansible tasks configure Debian router guests, which is separate from PVE host management networking.
- The SDN drop-in timestamp confirms an SDN configuration update earlier that day, but does not establish whether an SDN reload affected runtime networking during the earlier outage. A runtime effect is not ruled out by the unchanged persistent management-address file. The observed boot log reports successful network initialization after the later recovery reboot.

## Resource snapshot after recovery

At 16:26 CDT, `/cluster/resources` reported four resources on P2: LXC 113 `p100-router`, 118 `p100t002-router`, and 119 `p100t004-router` were running; QEMU 147 `crange-replica-147` was stopped. The three containers each reported 1 vCPU, 512 MiB max memory, 8,350,298,112 bytes max disk, and about 10.5 minutes uptime. VM 147 reported 1 vCPU, 512 MiB max memory, 20 GiB max disk. This is a post-reboot snapshot, not an incident-time state.

## Answer to “did we change it?”

The evidence says **the persistent configured address remains present and unchanged since June**. P2 was already offline before the observed recovery reboot, so that reboot does not explain the prior runtime address loss. The audit found no evidence that project automation removed the persistent address, but it cannot rule out an SDN reload affecting runtime networking or attribute the outage/reboot to a person or automation job. The same-day SDN update is temporal context, not proof of cause.

## Follow-up: October 10, 2026

Installed the management guard and 20-second systemd timer on P2. It restores
10.0.0.102/24 on an existing vmbr0 and restores the default route only if none
exists. It does not restart bridges/networking. Missing-address events save
address, route, and kernel diagnostics in /var/log/proxmox2-management-guard/.
Sources and units: ops/proxmox2/. Tested missing-address and route restoration
and an idempotent second run in an isolated network namespace.

Both previous boots show repeated e1000e eno1 Hardware Unit Hang errors:
October 8 from 02:06:53 EDT and October 10 from 09:55:45 EDT until recovery
reboot around 11:45. NFS connectivity also failed. Static config is unchanged
since June; NetworkManager/systemd-networkd are inactive. NIC transmit hangs
are confirmed; actual address removal and the underlying driver/hardware
trigger are not established.

Disabled TSO/GSO through a persistent dedicated service as a reversible
mitigation, without bouncing the NIC or rebooting. Gateway ping succeeded
and cluster quorum showed all five nodes. Long-term effectiveness remains
to be observed. A wedged NIC cannot be repaired by adding its address.
Rollback: disable proxmox2-nic-mitigation.service and restore offloads using
ethtool -K eno1 tso on gso on.

### Automatic reboot escalation

User confirms that the first outage was inspected at the console and vmbr0
had no IP address. Reboot restored it; the second outage was also resolved
by reboot, without inspecting its address first. Thus first-outage address
loss is confirmed by the operator, alongside kernel NIC-hang evidence.

Guard now requests a graceful systemctl reboot after six consecutive failed
recovery checks (roughly two minutes), after five minutes of host uptime,
with at most one request per hour persisted across boots. Healthy checks
reset the counter. A gateway failure triggers escalation only when accompanied
by recent eno1 hardware-hang messages from this boot; a router outage alone
does not trigger reboot. Reboot necessarily interrupts guests on this node.
Threshold/cooldown/boot-reset logic was tested with reboot mocked, then the
updated guard was deployed and ran successfully on the healthy host.
