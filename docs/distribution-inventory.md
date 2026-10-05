# Distribution inventory and library proposal

Inspected 2026-10-01 using read-only Proxmox API/SSH, VM configuration,
block-device metadata, live network interfaces, and OMV service discovery.
No VM, disk, export, firewall, or network configuration was changed.
This is observed inventory, not approval to enable automatic placement.

## Capacity and existing workloads

Snapshot includes every homelab guest, including stopped guests, excluding
published templates from guest RAM/CPU commitments. Storage figures are
reported free space, before disk commitments, copy overhead, and reserves.

| Node | Physical RAM GiB | Logical CPUs | Declared guest RAM GiB | Declared guest vCPUs | local-lvm free GiB |
| --- | ---: | ---: | ---: | ---: | ---: |
| proxmox1 | 15.41 | 4 | 7.81 | 4 | 334.89 |
| proxmox2 | 7.63 | 4 | 0 | 0 | 141.50 |
| proxmox3 | 7.63 | 4 | 0 | 0 | 54.21 |
| proxmox4 | 62.57 | 20 | 162.26 | 105 | 108.74 |
| proxmox5 | 15.49 | 4 | 9.77 | 2 | 134.39 |

All five nodes were online. **proxmox4 must reject new automatic allocations**
under the proposed conservative policy: existing configured RAM alone exceeds
physical RAM, before the host reserve. Low current utilization does not justify
admission because stopped guests remain commitments. proxmox1 also exceeds the
initial no-overcommit CPU budget of 80% of four logical CPUs. Existing guests
remain untouched. proxmox2 and proxmox3 are capacity candidates, subject to image,
network, storage, observed-load and concurrent-reservation admission checks.

CPU generations differ: i5-3550, i5-5300U, i5-7200U, i9-13900HK, i5-7500
respectively. All report SSSE3, SSE4.1, SSE4.2, POPCNT, CX16, LAHF/SAHF, AES
and PNI (Linux name for SSE3). This suggests an x86-64-v2-AES candidate baseline;
verify disposable guests on each node before certifying it. Existing `host`
CPU guests and physical-device passthrough are not automatically portable.

`local` and `local-lvm` remain node-local. `ssd-dir` is restricted to proxmox1;
`ssd-lvm` and `hdd-lvm` are restricted to proxmox5. No NFS storage or
`crange-library` storage is currently registered. Reported local free space
must not be interpreted as shared storage or a verified destination image.

## OMV backing and availability

VM 103, `OpenMediaVault`, runs on proxmox5:

- Two vCPUs, 10000 MiB configured memory, `x86-64-v2-AES` CPU.
- OS disk `local-lvm:vm-103-disk-0`, 32 GiB; boot order `scsi0;net0`.
- `onboot: 1`; its OS disk does not depend on a future NFS library.
- Data disk is a physical passthrough device, `/dev/disk/by-id/ata-ORICO_AA000000000000000657`, resolved to `/dev/sdd`.
- Device is about 3.7 TiB / 4 TB decimal; host metadata shows an ext4 partition
  `/dev/sdd1`, not mounted on the Proxmox host. No RAID/redundancy is demonstrated.
- NIC uses `vmbr0`; observed LAN address is `10.0.0.134`, verified by its
  MAC matching the VM configuration. DNS also returns Tailscale address
  `100.114.246.75`; SSH through that address timed out from proxmox1.
- QEMU guest agent is not configured; agent queries cannot inspect guest mounts.
- Cluster SSH keys do not authorize root login to OMV. Guest filesystem mount
  paths, actual free space, existing shared-folder definitions, permissions,
  backup coverage, and OMV configuration remain unverified.
- LAN probes show SSH 22 and HTTP 80 open; NFS 2049, HTTPS 443 and SMB 445 closed.
  `showmount -e` reports the mount RPC program is not registered. This establishes
  no currently reachable NFS export, not an assertion that OMV has no saved shares.
- `/cluster/backup` returned no configured cluster backup jobs. This does not
  establish the absence of guest-managed or external backups. A VM OS backup
  must not be assumed to cover the raw passthrough data disk.

### Concrete dedicated library proposal

After authorized OMV guest/configuration inspection, create a **new** directory
`crange-library` on a confirmed data filesystem with adequate free space and a
separate OMV shared-folder/export definition. Do not reuse an existing share,
format the passthrough disk, mount its live filesystem on the host, or change
existing exports. The exact `/srv/dev-disk-by-uuid-.../crange-library` path must be
resolved from OMV's actual mount configuration, not inferred from host `/dev/sdd1`.

Restrict NFS clients to the five cluster addresses `10.0.0.101`–`10.0.0.105` and
any separately reviewed controller client. Use a dedicated Proxmox storage ID
`crange-library` for approved immutable artifacts/ISOs/archives/releases; keep
running guest disks on approved local storage. Determine NFS permissions and
Proxmox content options through a disposable upload/restore test before publishing
verified catalog replicas. Initially serialize library transfers and reserve
space for artifacts, retained releases, transfer overhead and growth.

The library cannot be provisioned safely from current evidence: guest access,
free capacity, existing shares, throughput and independent backup destination
are outstanding review requirements. OMV's passed-through data disk is an
existing homelab dependency and must remain unchanged during discovery.

Keep controller, OMV OS, firewall, recovery tools and critical local image copies
on independent local storage. Establish a separate backup target outside this
VM/disk dependency and a tested restore procedure before treating the library
as the sole copy of a release. Full local clones should continue without NFS.
OMV onboot alone does not establish export readiness or ordered startup; check
service readiness and fail clearly during outages rather than creating a boot
cycle.

## SDN and MTU observations

On all five nodes, `vmbr0` is UP with MTU 1500. `craccess`, `crtrans`, and
`p003t254` bridges are UP with MTU 1450. Runtime VXLAN links are UP/LOWER_UP;
`vxlan_craccess` uses VNI 10210 and UDP destination port 4789. Generated SDN
interface configuration explicitly sets MTU 1450.

Cluster zone `crange` is VXLAN with MTU 1450, all five nodes, and peers
`10.0.0.101`–`10.0.0.105`. Relevant VNIs are `crtrans=10212`,
`p003tr=100768`, `p003t001=100769`, `p003t002=100770`, and
`p003t254=101022`. The required project/staging VNets exist on every node.

These are configuration and link-presence checks. They do **not** certify
cross-node guest VXLAN delivery, effective path MTU, DHCP/DNS, egress, isolation,
or controller/Guacamole access. Admission must retain a separate verified
network gate until disposable guests exercise those paths across node pairs.
Do not mark a network verified solely because a bridge exists. No production
router reboot, guest migration or infrastructure acceptance apply was performed.
