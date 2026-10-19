# Fixed archive growth service

The dashboard's admin-only `template-library-grow` job calls `crange library-grow`
as the controller worker. This connects to `library-grow.sock` in the controller's
existing private `/run/crange-ct-read` directory. The separate host service
verifies the worker's mapped Unix peer UID and accepts exactly `{"action":"grow"}`.

Install the reviewed `library_grow.py` and `library_grow_relay.py` root-owned,
mode 0644, under `/usr/local/lib/crange` on Proxmox4. Install the accompanying
systemd unit root-owned under `/etc/systemd/system`, then enable/start it. The
unit pins the growth helper SHA-256; update that pin only after reviewing source
changes. The daemon reads the existing root-only controller relay policy solely
for the mapped worker UID and fixed Proxmox5 address. It never modifies the
network relay, its trusted digest, credentials or signed network receipts.

The root-owned helper travels as fixed source over the existing cluster SSH
trust to Proxmox5. No controller-provided command, path, size, node or disk is
forwarded. The helper grows only CT145's exact SSD-backed ext4 data volume by
64 GiB, retaining at least 100 GiB and 10% of the original VG as free space.
It verifies volume-group UUID, container/address/mount/disk identity and the
post-resize configuration/filesystem. It does not restart NFS or guests.

`/var/lib/crange-library-grow.json` and its lock protect interrupted operations.
If a previous resize completed but its final reply was lost, recovery verifies
that same target rather than adding another increment. Ambiguous state or
insufficient host reserve fails closed. Never delete the journal to bypass a
failed readback. The service logs only bounded status/error metadata.

Tests: `PYTHONPATH=scripts:tests python3 -m unittest test_library_growth`.
