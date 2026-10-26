# Images, caches and disk capacity

An approved OS base, a captured scenario release, an immutable archive, a local cache template and a running guest disk are different objects. Their identities and dependencies explain both deployment speed and why apparent free space can be misleading.

## The image path

1. An administrator builds/prepares an OS base and verifies its clone behavior.
2. A published logical image identifies an immutable artifact and its approval scope.
3. A central library retains the verified archive.
4. An eligible destination restores a local cache and verifies identity, backing and cleanup through a disposable probe.
5. A reviewed deployment creates fresh guests using that verified local base where supported.

Scenario releases have their own captured image identities. They are not automatically offered as general OS base choices. An administrator approving a base for Project A does not grant Project B the image.

## What review may do

**Review changes** can prepare missing approved local caches before retaining the guest plan. Pure `infra-plan --retain` previews infrastructure and reports preparation requirements rather than copying images. Browsing Cluster or refreshing its snapshot does not initiate cache transfers.

Cache preparation can be much slower than creating linked guests afterward. It includes archive transfer/restore, temporary verification, identity/backing checks and cleanup. A verified existing cache can be reused; an ambiguous partial cache must be reconciled against its protected journal before reuse or deletion.

## Capacity is a commitment

Placement counts configured guest resources, including stopped guests, reservations, router overhead and preparation peaks. It reserves room for cache bases/probes/transfer and full supported guest growth. Thin allocation and compressed backups are not permission to promise unlimited virtual disk growth.

| Quantity | What it means |
| --- | --- |
| Guest virtual disk size | Capacity presented to the guest |
| Guest filesystem usage | Data used within partitions/filesystems |
| Host physical allocation | Blocks/LVs currently consumed on a datastore |
| Archive size | Compressed/sparse exported bytes |
| Admission commitment | Capacity reserved under policy for safe placement/growth |

Shrinking a captured disk from 64 GiB to 30 GiB can reduce future guest commitments, but retained 64 GiB originals, archives, local bases, snapshots and temporary copies still consume space. Review may need room for both old and new fleets before old resources can be retired.

## Storage policy

Only administrator-approved runtime storage is eligible. SSD-first priority orders otherwise eligible choices; a soft node preference does not override capacity, approval or network gates. Hard pins require their exact destination. Existing applied placements do not move merely because policy changes.

In the current installation, Proxmox1 is allowed to host ordinary workloads subject to the same gates. Proxmox6 `local-hdd` has an explicit additional write-storage approval. This does not approve every HDD or every pool with a similar name, and it does not waive quota/identity checks. Other policy files and older evidence may describe historical restrictions.

The central NFS library is an artifact store; registration for images/rootdir does not by itself approve runtime guest disks there. Library infrastructure must never be captured, deleted or backed up onto its own export by a project workflow.

## Reclaiming safely

Retain older bases while live guests/snapshots depend on them. Cache garbage collection uses an exact review/apply with refreshed ownership, dependency and artifact proofs. Ordinary staging does not silently evict caches. An uncertain native task or remaining volume keeps its reservation until reconciled.
