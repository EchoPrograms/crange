# Template storage audit

Read-only observation on 2026-10-01, before storage provisioning. Inspected cluster
VM/CT configurations, Proxmox storage status, installed API implementation and
proxmox4 LVM thin-volume origins. No live configuration was changed by this audit.

proxmox4 `pve/data` is 794.30 GiB with data usage 86.32%, metadata usage 2.76%,
and reported free space about 108.66 GiB. Guest `local-lvm` storage is already
LVM thin. `ssd-lvm` on proxmox5 is ordinary thick LVM, active and empty with
931.51 GiB available. A filesystem inside an allocated SSD LV can hold sparse
NFS images, but the backing LV itself consumes its allocated size.

## Bases and local dependencies

Mapped GiB is virtual LV size times LVM Data%; it is not unique physical usage.
Thin clones share blocks, so adding Data% values overstates pool usage. Actual
space reclaimed must be measured from pool status after each supported move.

| ID | Type | Template | Virtual size | Mapped GiB | Local thin origin dependents |
| --- | --- | --- | ---: | ---: | --- |
| 112 | qemu | parrot-jump-template | 32.00g | 27.17 | 117, 118, 119, 120, 125, 130, 144 |
| 116 | qemu | p003-release-97e84625da58-captureproof | 32.00g | 27.45 | none observed |
| 131 | qemu | p240-release-7adf01307663-desk | 32.00g | 27.56 | none observed |
| 132 | lxc | p240-release-3dc4c2883dfe-svc | 4.00g | 0.78 | none observed |
| 136 | qemu | p240-release-d97b4fcd18d2-desk | 32.00g | 27.56 | 135 |
| 137 | lxc | p240-release-d89fcdf95651-svc | 4.00g | 0.82 | none observed |
| 140 | qemu | p240-release-e932a432204b-debian-base-che | 20.00g | 1.09 | none observed |
| 141 | qemu | p240-release-723127f1b70e-desk | 32.00g | 27.56 | none observed |
| 142 | qemu | p240-release-ff2a82178988-ubuntu-base-che | 20.00g | 2.18 | none observed |
| 143 | lxc | p240-release-bcafe6db8a94-svc | 4.00g | 0.82 | none observed |
| 3000 | lxc | crange-base-debian-13 | 4.00g | 0.84 | none observed |
| 3001 | lxc | crange-base-ubuntu-24-04 | 4.00g | 1.04 | none observed |
| 3002 | lxc | crange-base-alpine | 4.00g | 0.20 | none observed |
| 3010 | qemu | crange-base-ubuntu-server-24-04 | 20.00g | 2.13 | none observed |
| 3011 | qemu | crange-base-debian-13 | 20.00g | 1.09 | none observed |
| 3012 | qemu | crange-base-alpine-3-24 | 20.00g | 0.17 | none observed |
| 3014 | qemu | crange-base-ubuntu-server-24-04 | 20.00g | 2.18 | 121, 139 |
| 3015 | qemu | crange-base-debian-13 | 20.00g | 1.09 | 138 |
| 3016 | qemu | crange-base-alpine-3-24 | 20.00g | 0.17 | none observed |
| 3017 | qemu | crange-base-alpine-3-24 | 20.00g | 0.17 | none observed |

Candidates with no observed local origin dependencies total 94.07 GiB of
mapped base data. All are stopped templates. Recheck dependency origins and
snapshot/configuration locks immediately before a move. Keep bases 112, 136,
3014 and 3015 on their current storage while their linked clones remain. This
audit does not authorize deleting any dependent guest or immutable release.

## Supported moves and placement

Installed API source `/usr/share/perl5/PVE/API2/Qemu.pm` implements POST
`/nodes/proxmox4/qemu/{vmid}/move_disk` with `disk=scsi0`,
`storage=crange-library`, `format=qcow2`, and `delete=1`. It copies the disk,
converts the destination back to a template base, updates the same VMID and
attempts source deletion. Check UPID task exit status, final configuration and
pool usage; task warnings can leave source storage occupied. Move attached
cloud-init disks separately, preserving their configuration. Use a configuration
`digest` to detect concurrent modification.

Installed `/usr/share/perl5/PVE/API2/LXC.pm` implements POST
`/nodes/proxmox4/lxc/{vmid}/move_volume` with `volume=rootfs`,
`storage=crange-library`, and `delete=1`. It also recreates the destination base
for templates. NFS storage must permit `rootdir`; VM bases need `images`.
A disposable CT move/clone test must verify the chosen NFS filesystem support.

Thin provisioning of local guest disks does not mean NFS-linked local overlays.
Use full clones onto the selected node's `local-lvm`: they allocate written
blocks and remain independent of the NFS base after cloning. Proxmox linked
clones ordinarily keep the overlay with the source backend/base; LVM-thin bases
cannot be moved away independently from their existing LVM origin descendants.
Discard and guest trim are needed to reclaim deleted data where supported;
virtual disk size and filesystem-used bytes are different measurements.

The initial candidates among platform bases are VMs 3010, 3011, 3012, 3016,
3017 and CTs 3000, 3001, 3002. Other dependency-free candidates are project
release templates 116, 131, 132, 137, 140, 141, 142, 143; retain IDs and release
references, and verify immutable manifest/catalog storage references before
using their new locations. Never infer safe deletion from no current clone:
retained versions may still be referenced by release manifests.

## Container OS compatibility

The dedicated library uses Debian 12 (`debian-12-standard_12.12-1_amd64.tar.zst`).
The installed proxmox5 container tools rejected the downloaded Debian 13.6
archive as unsupported; the failed attempt was rolled back before retrying.
This library does not require a host package upgrade. Its OS compatibility is
independent of the Debian 13 templates stored inside the exported library.
