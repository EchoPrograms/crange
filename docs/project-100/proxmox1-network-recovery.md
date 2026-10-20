# Proxmox1 outage and cluster return — October 20, 2026

The cluster is repaired: all six members are quorate with configuration version
23, seven total votes, and a quorum threshold of four. Proxmox4 retains two
votes and remains the required storage/controller core. This recovery did not
change votes, management addressing, firewall policy, or guest placement.

## Two separate failures

1. **Original network outage:** the previous Proxmox1 boot recorded its first
   `e1000e 0000:00:19.0 eno1: Detected Hardware Unit Hang` on October 18 at
   21:52:17 EDT (20:52:17 CDT). Repeated hangs continued until the recovery
   power cycle on October 20. NFS to 10.0.0.104 timed out and Tailscale lost its
   network path. The NIC is an Intel 82579LM, using e1000e. Its configured static
   address remains 10.0.0.101/24 on vmbr0. These logs establish a transmit hang;
   they do not establish that its runtime address was removed.
2. **Failed cluster return:** while Proxmox1 was offline, the earlier quorum
   update advanced the healthy cluster to version 23. Proxmox1 retained version
   22. On return its Corosync log explicitly reported
   `Received config version (23) is different than my config version (22)! Exiting`.
   Consequently pmxcfs could not synchronize the newer authentication keys,
   causing cross-node invalid-ticket errors. This earlier automation change
   explains the rejoin failure, not the preceding NIC outage.

Proxmox1's clock was already NTP synchronized. Recovery backed up its old local
Corosync configuration under `/root/crange-recovery-backups/`, syntax-tested an
exact copy of the healthy cluster's configuration, replaced only
`/etc/corosync/corosync.conf`, and started the already-failed Corosync service.
The cluster filesystem then synchronized naturally. Authentication keys were
not manually replaced or rotated. No quorum override, host reboot, or guest
restart was requested during this repair.

Fresh temporary in-memory tickets created on both Proxmox1 and Proxmox4
successfully fetched both nodes' status through their local API proxies: all
four checks returned HTTP 200. Tickets were not logged or written to disk.
An already-open browser may require logout/login to replace its stale ticket.

## Persistent network recovery

Installed on **Proxmox1, Proxmox3, Proxmox4 and Proxmox5**, using
`scripts/setup-host-network-guard.py`:

- `crange-management-guard.timer`: checks after boot and twenty seconds after
  each completed check. It verifies the hostname and configured persistent
  management stanza before changing runtime state. It restores a missing
  address/default route and raises an existing down interface without taking
  links down, restarting networking, or reloading guest bridges. It refuses to
  overwrite an unexpected address or a changed persistent configuration.
- `crange-nic-mitigation.service`: applies the configured Intel mitigation
  after networking and before guest autostart. Proxmox1 and Proxmox5 have
  TSO/GSO disabled on their e1000e uplinks. The periodic guard reapplies that
  mitigation if a later operation re-enables those offloads. Proxmox3 and
  Proxmox4 use r8169 and retain their offload settings. Proxmox2's existing
  recovery/mitigation services are retained unchanged.
- Reboot escalation requires six consecutive demonstrated local recovery
  failures, at least five minutes of uptime, and a one-hour cooldown persisted
  across boots. An unreachable gateway qualifies only with a recent
  interface-specific kernel transmit-hang report and unreachable cluster
  peers. A gateway/switch outage alone cannot trigger a reboot. Configuration,
  Python, journal-reading, or mitigation-tool errors do not themselves request
  a reboot. Reboots use graceful `systemctl reboot`; this is not a forced-reset
  or fencing implementation. Native Proxmox HA remains responsible for fencing.
- Diagnostics are root-private under `/var/log/crange-management-guard/`,
  limited to twenty incident snapshots. Reboot counters/cooldown live under
  `/var/lib/crange-management-guard/`.

The offload mitigation addresses a known class of e1000e transmit hangs; it is
not proof that this specific hardware can never hang again. An Intel driver
maintainer discusses the hardware issue and disabling TSO in the
[Intel wired LAN discussion](https://lists.osuosl.org/pipermail/intel-wired-lan/Week-of-Mon-20190520/016137.html).
If hangs recur despite the mitigation, replacement of the affected NIC is the
appropriate durable hardware option. Firmware/kernel changes require separate
validation; none were applied during this recovery.

## Automatic stale-configuration rejoin

`crange-cluster-rejoin.timer`, installed on the same four hosts, handles this
specific failed-return condition. It acts only when Corosync is **failed** and
pve-cluster is active. It requires two distinct quorate peers to agree exactly
on a newer configuration. SSH uses strict checking against Proxmox-maintained
per-node host-key pins, rather than incomplete interactive known-host histories.

Only version/vote differences qualify; member identities, addresses, links,
and the remainder of the configuration must match. The guard backs up the local
copy, validates the candidate with Corosync, checks for concurrent changes, and
starts Corosync. It never edits `/etc/pve/corosync.conf`, reduces quorum, changes
authentication keys, touches an active Corosync service, or moves guests.
Unrecognized failures remain for operator investigation. This follows the
local-copy recovery mechanism in the official
[Proxmox cluster documentation](https://github.com/proxmox/pve-docs/blob/master/pvecm.adoc#write-configuration-when-not-quorate).

## Verification and limits

- All four installations have enabled, active timers and successful service
  checks. Every host authenticated two distinct quorate peers whose
  configuration matched its own cluster copy.
- All six nodes remain members and quorate. Proxmox1 and Proxmox5 report both
  TSO/GSO off. The Realtek hosts retain their settings.
- Thirteen focused tests cover restoration, idempotence, conflicting addresses,
  interface-specific hang detection, reboot threshold, boot grace, cross-boot
  cooldown, strict peer agreement, compatible rejoin and refusal cases.
- The complete current working-tree suite passed: 1,192 tests run, one skipped.
  That suite also contains the separate, not-yet-deployed router HA work.
- A real isolated network namespace on Proxmox4 proved missing-address and
  missing-route restoration, then an idempotent second pass. Host interfaces
  were not modified by that test.
- Reboot invocation was mocked in tests. Production nodes were not deliberately
  disconnected or rebooted to prove fault recovery. Continued observation is
  required before declaring the hardware issue eliminated.

Rollback: disable `crange-management-guard.timer` and
`crange-cluster-rejoin.timer`; disable `crange-nic-mitigation.service`. Restore
offloads only to the recorded pre-install settings if desired (Proxmox1 had
TSO/GSO on; Proxmox5 had TSO off, GSO on). Removing the recovery services does
not undo the valid version-23 cluster configuration.
