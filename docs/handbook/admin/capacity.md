# Placement, capacity and parallel work

Use **Cluster** for sanitized worker-published inventory: snapshot age, policy revision, node capacity, configured commitments, reservations, headroom, workloads and image replicas. The view refreshes while open; a controller timer publishes new snapshots after each completed scan. A failed refresh retains and marks the last successful result stale.

## Why a deployment may not fit

Eligibility is the intersection of online/admitted node, allowed storage, approved and verified image/cache, provider capability, scoped network acceptance, project quota, resource reserves and current reservations. Raw cluster free bytes are not the same as capacity available for this release.

Stopped guests still have configured commitments. Fresh deployments may require new caches plus probes and room for old and new guests at once. Retained originals/snapshots still consume capacity after captured disks are reduced. A VM source's allocated size and a release clone's smaller captured size must be read from their respective objects.

Proxmox1's historical router-only restriction has been removed for ordinary workloads. Proxmox6 `local-hdd` has an explicit write-storage exception; this is not a blanket approval for every node's HDDs. CPU/RAM/disk ratios and reserves remain bounded administrator policy, not a reason to bypass individual admission failures.

## Preferences and pins

Automatic placement chooses among admitted candidates using storage priority and capacity pressure. A preferred node is a soft suggestion; a concrete node pin is a hard requirement. Existing applied placements stay fixed. Losing a selected destination requires a fresh review for a different node.

Do not classify physical SSD/HDD backing from datastore names alone. Check real backing/capability evidence. Allowing a slow HDD pool can increase available capacity while making restore/preparation phases much slower.

## What runs in parallel today?

Different projects have independent worker lanes. Guest-only applies can run independently when they do not conflict; per-node provisioning and reservation rules remain shared. SDN changes and edge route operations take their relevant short locks. The adapter durably allocates automatic guest IDs across concurrent callers.

A selected multi-team release uses one reviewed Terraform apply. Its resource graph can provision independent resources subject to provider dependencies/limits. The following configuration-and-check phase currently has an explicit **serial team loop**. Team networks are independent, but functions still read/write shared project inventory, credentials/access and rollout records.

Parallel team configuration could shorten guest setup, but it needs bounded concurrency, protected shared updates, combined progress, per-team failure/resume and shared-foundation ordering. Simply running multiple configuration jobs or adding threads risks lost tracking or conflicting shared work. This is a current limitation, not an implemented parallel-team control.

Parallel disk copies on one saturated storage array may only divide the same throughput. Use storage-aware limits; independent guest configuration often offers more useful parallelism than starting every restore at once.

## Node loss

The redundancy model treats Proxmox4 as the storage/controller/quorum core. It models weighted quorum and alternative router placement, with guarded router recovery/rejoin workflows. A passing model or a single-node outage test does not establish universal runtime failover. Ordinary scenario guests/workstations are not automatically restarted elsewhere. Fencing and exact observed ownership remain prerequisites for router recovery.
