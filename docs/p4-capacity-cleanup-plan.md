# Proxmox4 capacity and CRANGE cleanup plan

Read-only audit dated 2026-10-06. These measurements are a planning snapshot, not an authorization to delete deployed scenarios or change storage policy. No guests, disks, routes, or admission rules were changed by this audit.

## Required boundary

Preserve unrelated guests and services, including IDs 101, 104, 105, 106, 108, and 146. Do not stop, resize, migrate, delete, or reconfigure them to make CRANGE fit. Preserve shared CRANGE infrastructure IDs 102, 110, and 114 unless a separately reviewed migration explicitly covers their continuity. Changing placement must continue to account for every guest's CPU/RAM commitment, including unrelated guests.

## Capacity evidence

| Measurement | Audit value | Existing limit |
| --- | ---: | ---: |
| Non-template guest committed vCPU | 106 | 56 |
| Non-template guest committed RAM | approximately 164.3 GiB | 109.5 GiB |
| local-lvm virtual disk commitment | approximately 2,362.8 GiB | 635.4 GiB |
| local-lvm physically free | approximately 102.4 GiB | 158.9 GiB minimum reserve |
| Unrelated local-lvm virtual disks alone | 1,202.81 GiB | 635.4 GiB |

Unrelated disk commitments are ID101: 400.81 GiB; ID104: 132 GiB; ID105: 400 GiB; ID106: 20 GiB; ID108: 250 GiB. Small auxiliary disks are included at measurement precision. Their total already exceeds the local-lvm virtual commitment limit. Consequently, **CRANGE-only cleanup cannot make local-lvm eligible under the existing rule**.

Unrelated guests, including ID146 on NFS, commit 37 vCPU and approximately 90.87 GiB RAM. This leaves 19 vCPU and 18.63 GiB for retained CRANGE guests under the existing node limits. Storage relocation on the same node does not reduce these commitments.

P4 has one approximately 931.5 GiB NVMe device. Its pve VG is 930.51 GiB with 16 GiB unallocated. There is no spare device or substantial separate VG. Storage entries ssd-lvm and hdd-lvm belong to P5; ssd-dir belongs to P1. The root filesystem has approximately 49 GiB free and is not a bulk runtime storage solution.

## Staged cleanup and migration

1. Review verification VMs **3110–3123** for active jobs, current deployment references, and child disks before retirement. They are tagged CRANGE verification and have no LVM origin dependents in the audit. Six IDs appear in template manifests as historical verification evidence, which must be preserved. Retirement would remove 28 vCPU, 28 GiB RAM, and 280 GiB virtual disks, but only approximately **18.94 GiB mapped physical blocks**. Measure actual reclaimed pool space afterward; physical savings are not the virtual disk total.
2. Review superseded VM bases **3010, 3011, 3012, 3016** and running builder **3013** against catalog, desired state, retained releases, and queued/running jobs. Current gathered Terraform/release inputs do not reference these IDs, and their disks have no LVM child origins. They remain candidates until the job checks pass. Combined virtual disks are 100 GiB; mapped blocks approximately 5.70 GiB. Only non-template builder3013 reduces node commitments, by 2 vCPU and 2 GiB RAM. Running status alone does not establish that its work is complete.
3. Preserve and redistribute deployed project resources through reviewed migration and desired-state reconciliation. Project003/240 Terraform state owns **23 guests**, totaling **29 vCPU and 29.5 GiB RAM**: IDs 109, 111, 113, 115, 118–130 excluding 116/117, 133–135, 138, 139, and 144. Explicitly enumerate the state addresses during execution rather than relying on this compressed list. Removing all 14 verification VMs and moving all 23 managed guests off P4 would leave approximately **49 vCPU and 106.8 GiB RAM**, below both node limits. This is capacity arithmetic, not proof that receiving nodes have sufficient capacity. Every destination must pass admission first.
4. Select new P4 VM runtime storage separately from overloaded local-lvm. Using NFS requires reviewed placement/storage eligibility and full shared-storage reservation accounting. Excluding local-lvm as a deployment target must not hide its pressure or unrelated CPU/RAM commitments. If admission requires every node datastore to be within its existing virtual commitment limit, P4 remains ineligible until that policy is explicitly reconsidered or storage is expanded.

## Dependency retention

The audit found these LVM origins:

| Base guest | Dependent guests |
| --- | --- |
| 112 | 117, 118, 119, 120, 125, 130, 144 |
| 136 | 135 |
| 3014 | 121, 139 |
| 3015 | 138 |

Do not retire these bases while dependents remain. Retain release/catalog templates **116, 131, 132, 137, 140, 141, 142, 143**, even when no LVM origin appears. Current approved container bases **3000–3002** and VM base **3017** are also retained. LVM dependencies alone do not prove that a template is unused: desired-state, release, catalog, and job references are independent protections.

## NFS runtime option

`crange-library` was confirmed active by `pvesm status`, mounted read/write, permitted on all five nodes, and configured for images/rootdir content. The audit measured approximately **392.65 GiB total and 354.56 GiB available**. Existing unrelated guest146 already has a 32 GiB virtual disk there. The datastore is shared, not exclusively owned by CRANGE; preserve that guest and reserve its commitment alongside all other images, archives, caches, and concurrent jobs.

[Proxmox NFS storage documentation](https://github.com/proxmox/pve-docs/blob/master/pve-storage-nfs.adoc) supports VM snapshots and linked clones through **qcow2** features. The pinned bpg/proxmox 0.104.0 VM resource supports linked cloning through `clone.full = false` and qcow2 disk format. These capabilities establish feasibility; they do not prove this application's release workflow or storage performance.

Use immutable qcow2 VM bases and fresh linked clones for new deployments only after a pilot passes. Copying/flattening existing LVM clones may require substantially more space than their exclusive changed blocks; preserve the source base until the migrated/rebuilt descendants and rollback are verified. NFS rootdir content does not provide LXC snapshot/linked-clone support equivalent to LVMthin; retain appropriate local container replicas. Runtime on NFS adds a shared availability and I/O dependency, so verify export capacity, network performance, locking, and restart behavior before expanding use.

## Validation gates

- Refresh cluster guest configurations, per-node commitments, storage status, physical free space, LVM origins, catalog replicas, retained releases, Terraform state/inputs, and queued/running jobs immediately before execution. Resolve discrepancies before mutations.
- For retirement, prove CRANGE ownership and absence of active desired-state/job/deployment references; preserve immutable verification evidence. Review exact IDs and disks. Do not use name matching or a broad ID range as destructive selection.
- For migration, inspect Terraform plans for unintended replacement/destruction; reconcile node/storage inputs and state without recreating live guests. Keep project state separate. Verify destination CPU, RAM, virtual disk reservations, physical reserve, approved networking, and source/destination API permissions.
- For the NFS pilot, use a disposable approved VM: verify qcow2 format and backing chain, clone identity uniqueness, power cycle, service health, capture/release, snapshot/restore, and clone deletion without affecting the base or another clone. Check all permitted nodes' storage visibility. Preserve current infrastructure throughout.
- Verify routers before dependent guests, SSH proxy access, DNS, Internet egress, isolation, controller/Guacamole management, and persistent routes after reboot. Keep OPNsense firewall policy unchanged.
- After each stage, compare measured commitments and physical storage with projections. Require a clean desired-state plan and unchanged unrelated guest configurations. Enable new P4 placement only after all applicable limits and workflow checks pass.

Raw API/configuration audit files remain outside Git. This document intentionally omits credentials, account names, unrelated guest names, export server details, and private configuration payloads.
