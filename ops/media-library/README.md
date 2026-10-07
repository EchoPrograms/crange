# Cluster installation media library

Primary uploads: `crange-library`, CT145 on Proxmox5, NFS `10.0.0.249:/crange-library`.
Read-only fallback storages (available on every cluster node):

* `media-mirror1`: `10.0.0.101:/mnt/pve/ssd-dir/cluster-media`
* `media-mirror4`: `10.0.0.104:/var/lib/vz/cluster-media`

Only `template/iso` and `template/cache` are mirrored. Guest disks, backup archives,
snippets and CRANGE disk caches are unaffected. Kernel NFS on nodes1/4 allows only
cluster management addresses `10.0.0.101` through `.105`, read-only, root-squashed.
The storage mounts use bounded soft retries because installation media is read-only;
these options must not be reused for active guest disks.

Run `configure-mirrors.py` as root on each designated mirror after installing
`nfs-kernel-server`. It enables boot-persistent exports without changing existing
exports. Register both NFS storage IDs with `iso,vztmpl` content and all five nodes.
Install `sync.py` at `/usr/local/sbin/crange-media-sync` on node1 and the units in
`/etc/systemd/system`; enable `crange-media-sync.timer`. This requires the existing
node root SSH trust and NFS primary mount. Sync runs every fifteen minutes.

The sync has a singleton lock, bounded subprocesses, and atomically installs files
with rsync delayed updates. It reuses SHA256 values while the primary and mirror
size/mtime/inode fingerprints stay unchanged, verifies changed files immediately,
repairs mismatches, and runs a full mirror checksum audit every 24 hours. It then
atomically writes `/var/lib/crange-media-library/status.json` and copies it into
controller CT114. Zero-length/hidden uploads are excluded from usable files.
Missing primary files do not propagate deletion to mirrors. The last checksum
status survives outages.

This is three independent media copies with creation-time selection fallback, not
transparent failover of an already mounted NFS CD-ROM. A guest actively using a
failed server's ISO may need its CD-ROM switched to the matching surviving volume.
Already installed guests do not depend on installation media unless they still use
it. New uploads are protected after the next successful sync; run the service
manually after urgent uploads.

The initial audit found live ISO CD-ROM references on VMs 112, 146, 110, 104, 102,
and 105 on proxmox4. After verifying the primary copies, all seven CD-ROM device
references were changed to `crange-library:iso/...`; the guests were not rebooted
and no disks were detached. The local originals remain available. Twelve proxmox4
ISO/archive files are hard-linked into the mirror4 tree on the same filesystem,
so both storage IDs can use them without consuming duplicate blocks. Other mirror4
files were copied from the verified primary and SHA256 checked. The invalid
zero-byte Debian 13.4 ISO remains local and is excluded from the library manifest.

The first consolidation pass verified all 23 usable unique ISO/archive files on
the primary, mirror1, and mirror4 against the source inventory. The status manifest
is also published to controller CT114 for media selection fallback. Local archive
files remain in place for any future or retired `local:` references.

Scoped controller permissions: reuse `CrangeISOUpload` (only
`Datastore.AllocateTemplate`) on `/storage/crange-library` for both the service user
and its privilege-separated Terraform token. Mirror storage paths get read-only
`PVEAuditor`. The primary already had `PVEDatastoreUser`; guest/pool ACLs are unchanged.
