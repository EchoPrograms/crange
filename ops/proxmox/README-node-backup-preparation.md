# Native template backup preparation

Cluster administrators can expand a node and choose **Prepare template support**.
The inferred node is submitted as the fixed project-0 job
`template-node-backup-prepare`; project users cannot supply a package, command,
repository, version or filesystem path.

The separate root service on Proxmox4 checks the controller worker's mapped Unix
UID and uses the existing pinned cluster SSH trust to run a fixed reviewed helper
on that enrolled node. It does not change the signed network relay or its source
pin. Install the Python files root-owned, mode 0644 under `/usr/local/lib/crange`,
and the systemd unit under `/etc/systemd/system`. The unit pins the helper digest.

Already-supported qemu-server versions are read-only checks. Versions 9.0.16 and
9.0.17 can be updated to the reviewed 9.0.18 package only. Before installation,
node tasks must be idle, APT's simulation must change only qemu-server, and the
exact downloaded package must match its pinned SHA256. Any dependency change,
removal, unsupported old version or artifact mismatch fails before installation.
The helper verifies every unrelated package remains unchanged and reloads only
pvedaemon. Guest power, disks, host networking and kernel are not changed.

This fixes the native template-backup regression where stopped immutable SATA/
IDE base volumes are incorrectly opened writable. Do not make base LVs writable
or change stored guest machine types as a workaround.

A capture retry may recover this exact failure only when the previous vzdump is
terminal, its node/VMID/UPID/log/owned base disk match, the fixed package is freshly
confirmed fully installed, and no new backup archive appeared. Proxmox's package
API reports the installed version in `OldVersion` when present; `Version` alone
may be the available candidate. Failed task history is retained. All existing
source identity, config hash, ownership and archive checks remain required.

Tests: `test_node_backup_prepare`, `test_readonly_template_export_recovery` and
`test_source_identity_capture` under `PYTHONPATH=scripts:tests`.

## Container template compatibility

The same fixed job also prepares container-template support. On nodes with
`pve-container` 6.0.9, it installs only the pinned official 6.0.10 package
(SHA256 `8b782027b45e78ae3bf6830746c35d00795c0d339f1555693d5788b2bf5f6519`).
The older Debian setup check rejects version `13.6`; 6.0.10 accepts Debian 13
minor releases. Newer installed versions are read-only checks. Node tasks must
be idle, APT simulation must change exactly that package, and all unrelated
package versions must remain unchanged. Only pvedaemon is reloaded; no guest
reboots, cluster/network changes or kernel upgrades are permitted. Downloads
force IPv4 per process, without changing host networking.

Do not use an unconstrained package upgrade: a measured 6.0.18 simulation on
PVE 9.0.3 wanted to update cluster dependencies and remove pve-manager. The
helper refuses that transaction. The supported 6.0.10 simulation changes only
pve-container, and the downloaded artifact hash is checked before installation.
