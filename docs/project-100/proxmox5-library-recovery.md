# Proxmox5 shared-library recovery — approved maintenance in progress

Observed October 14, 2026 during Project 100 release 0.1.1 capture. No host or
container reboot has been performed.

## Evidence

- Proxmox5 is reachable over LAN and Tailscale, uptime 62 days. Load exceeds 140;
  I/O pressure is about 99%, while CPU pressure is zero and about 10 GiB RAM is
  available. Physical disk counters showed no outstanding block I/O.
- Kernel logs report blocked `ganesha.nfsd` tasks. Sample stacks show memory
  migration/page-fault and `madvise` waits. These observations establish a
  kernel-blocked process; they do not prove a specific kernel defect.
- Library CT145 is `crange-library`, address 10.0.0.249, with SSD-LVM root and
  `/srv/crange-library` disk. Its original 768 MiB/no-swap allocation accumulated
  millions of cgroup memory-high events.
- Dashboard resource job 7f1bd118a7344a6db32ede910036f60b raised it online to
  2 GiB RAM, 2 GiB swap and 2 cores. Live cgroup limits match. No reboot occurred.
- Journaled service-only recovery 601c655570bb43c29e050a4ee72f1725 submitted
  one graceful NFS service restart. It remains `deactivating/stop-sigkill`,
  MainPID 160, Result=timeout. Its journal is `observing`; another restart must
  not be submitted while this request is unresolved.
- The library has disk references from VM146 Proxmox Datacenter Manager,
  generic range bases and Windows acceptance images, as well as ISO users.
- Proxmox5 has a QEMU PID file for OpenMediaVault VM103, and for capture
  template VM138's backup helper. Range routers CT116/117 and library CT145
  are configured to start on boot.

## Proposed maintenance scope

The user authorized the proposed maintenance plan with “you can continue”.
OpenMediaVault must shut down gracefully before rebooting Proxmox5. Its
availability will be temporarily affected; unrelated data and configuration
must be preserved.

1. Confirm current running guests and preserve the exact pending capture/export
   journals, provider UPID, original guest power state and network configuration.
2. Gracefully shut down OpenMediaVault before any host reboot; stop and reassess
   if a clean shutdown cannot be confirmed. Do not force-stop it.
3. Reboot only Proxmox5 during the approved window. Do not change host IPs,
   bridges, storage definitions or guest disks.
4. Verify cluster membership, networking, local storage and the library's live
   resource limits. Verify NFS service and primary storage responsiveness.
5. Restore the previously running OpenMediaVault state and verify its service;
   verify range routers 116/117 and their existing routes/isolation.
6. Reconcile the exact interrupted export task. Never publish an unverified
   partial archive or resubmit backup while the prior outcome is uncertain.
   Continue capture from the existing verified template, avoiding another
   source backup/restore.
7. Update the dashboard job evidence and Project 100 status before resuming
   fresh-release testing and participant rollout.

No host reboot, unrelated-guest power action, force kill or archive deletion
has been performed under this plan.

## Maintenance observations

- Initial host boot ID: `af831691-1672-44d6-8eea-28da9bcc395b`.
- OpenMediaVault VM103 has a live QEMU process (PID 2324). The normal
  `qm status 103` request timed out after 20 seconds. Graceful shutdown
  has not yet been confirmed; no host reboot has been requested.

- Direct guest SSH failed: Tailscale timed out; LAN 10.0.0.134 returned
  no route to host. The VM's QMP socket did not return a greeting.
- VM103 threads include `folio_wait_bit_common` and
  `get_user_pages_unlocked`; its main thread waits on a futex.
- Submitted one `qm shutdown 103 --timeout 30 --forceStop 0`, bounded
  by an outer 50-second timeout. The command exited without a shutdown
  confirmation; QEMU PID 2324 remains alive and host boot ID is unchanged.
- Saved host network/storage configuration, VM103 configuration and NFS
  recovery journal under root-only `/root/crange-p5-maintenance-20261014`.
- Reboot sequence paused as required by the approved plan. A force-reboot
  requires separate explicit authorization because the unrelated storage
  guest cannot be shut down cleanly. No forced reboot or force-stop issued.

## Cluster/quorum check after outage report

At 17:52 EDT October 14, all five nodes independently reported quorum: expected
votes 5, total votes 5, required 3, ring `1.82c6`, configuration version 21.
P1/P2 link checks showed every Corosync peer connected. P3/P4 reported active
Corosync, cluster filesystem, PVE API/proxy and status services. No quorum or
cluster networking change was made.

P5 remained LAN-accessible with its original boot ID, but Tailscale stopped.
Systemd had a pending reboot with `pve-guests.service` stopping. Its journal
records `root`, `TTY=pts/0`, `COMMAND=/usr/sbin/reboot` at 17:50:09 EDT. This
identifies a root terminal request, not the individual operator. This agent
did not issue that request. Routers CT116/117 stopped cleanly at 17:50:28–29;
VM103 and VM138 processes remained alive during inspection. The existing
blocked shared-library process and incomplete host shutdown remain unresolved.

## Post-reboot verification

At 20:27 EDT October 14, P5 boot ID is
`3474395a-c405-4d19-a0bd-f8ed01116732`; no reboot/shutdown jobs remain.
All five quorum votes are present. Management address remains 10.0.0.105/24.
NFS library, both media mirrors and local storage report active. VM103 and
CT145/116/117 are running; NFS-Ganesha is active. I/O PSI avg10 is 0.02%,
memory pressure zero. Captured template VM138 is stopped with backup lock;
identity and config hash match its checkpoint. Exact export outcome is being
reconciled before any new backup is permitted.
