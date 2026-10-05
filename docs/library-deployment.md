# Dedicated library deployment record

Deployed 2026-10-02. CT145 `crange-library` runs privileged Debian 12 on
proxmox5, pool `crange-iac`, with one CPU, 768 MiB RAM and zero swap. Its root
volume is `ssd-lvm:vm-145-disk-0` (8 GiB) and its data volume is
`ssd-lvm:vm-145-disk-1` (400 GiB), mounted at `/srv/crange-library`, backup enabled.
`ssd-lvm` is thick LVM; sparse image/archive behavior inside its ext4 filesystem
is separate from allocation of these host LVs.

Addresses: NFS `10.0.0.249/24` on vmbr0, gateway `10.0.0.1`; administration
`10.60.0.30/24` on craccess with MTU 1450. The initial proposed `10.0.0.150`
collided with another device and was replaced after neighbor inspection. Do not
reuse that address. Debian 13.6 was rejected by the installed host tools; no host
upgrade was performed.

One Ganesha VFS NFSv4 export uses pseudo-path `/crange-library`. Access is limited
to `10.0.0.101`–`10.0.0.105` and controller `10.60.0.20`, using AUTH_SYS and
No_Root_Squash for Proxmox storage operations. Guest nftables permits those NFS
clients, controller SSH, established connections, loopback and ICMP; forward
policy is drop. SSH requires keys.

The stock LXC seccomp baseline denied `open_by_handle_at`, required by Ganesha.
A CT145-only profile `/etc/lxc/crange-library-145.seccomp` removes that single
deny while retaining every other baseline rule. The container stays confined by
its generated AppArmor profile. `ansible/library-host.yml` reproduces this
administrator-only host configuration; profile changes require a reviewed CT145
reboot. No host kernel NFS service was enabled or modified. Guest-only nftables
and logind unit overrides accommodate unsupported systemd mount namespaces.

Terraform was imported separately under `terraform/infrastructure/library` with
`prevent_destroy`; creation-only fields unavailable during import are ignored.
Its reviewed description-only apply completed; the subsequent plan had no
changes. On this old host, bpg 0.104.0 sends an unsupported `host-managed` option
when modifying NICs. Removing an unnecessary default eth0 MTU change avoided
that request; no broad NIC ignore or host upgrade was needed.

Ansible syntax and deployment passed, followed by an idempotent run with zero
changes. NFS, nftables, SSH and logind were active after a CT145 reboot. Wait for
Ganesha's approximately 90-second recovery grace before testing file operations.
All five nodes passed create, fsync, rename, read/compare and delete after reboot
using explicit NFSv4.0 TCP/2049, AUTH_SYS and `nosharecache` mount options. Earlier
mounts lacking these explicit options intermittently timed out; those results
must not be represented as reliable NFSv4.1 support. Probe-only mounts used soft
timeouts; the registered production storage retains normal hard mount behavior.

Cluster storage `crange-library` registers `10.0.0.249:/crange-library` at
`/mnt/pve/crange-library`, options
`vers=4.0,proto=tcp,port=2049,sec=sys,nosharecache`, for all five nodes. Content
includes images, rootdir, backup, iso, vztmpl and snippets to support archives
and transitional immutable templates. This does not approve runtime guest disk
placement on NFS. No existing guest/template disk was moved or deleted.
`ansible/library-storage.yml` reproduces registration after acceptance tests.

The existing scoped user and privilege-separated Terraform token received
`PVEDatastoreUser` on `/storage/crange-library`, matching their local-lvm role.
Global `CrangeDiscovery` retained Sys.Audit and Sys.AccessNetwork and added
VM.Audit; its read-only role was also granted to the token. Before/after metadata
is stored on proxmox4 in `/root/crange-library-discovery-acl-audit.json` and
`/root/crange-library-registration-audit.json`. No credentials are in this record.

Never back up CT145 and its data mount to its own exported storage. Its backup
requires an independent destination and a restore test; backup=1 alone does not
establish protection. Project workflows must reject library infrastructure as
a capture, backup, deletion or rollout target. Capacity reclamation and image
clone/restore acceptance require separate reviewed work.

Final registration status was active/enabled on all five nodes, each reporting
400,113,532,928 bytes available. Own temporary probe mounts and empty probe
directories were cleaned up. Scoped controller API discovery returned all 71
workloads and successfully read unrelated homelab VM101 configuration/snapshot
metadata. Existing `/vms` creation-option ACLs masked inheritance from `/`, so
the same read-only Discovery role was also added for user/token at `/vms`.
A final API storage-status read through the controller exceeded a five-second
probe timeout; root `pvesh` storage-status reads on all five nodes passed. This
short API timeout does not establish failed NFS mounting and remains a bounded
follow-up check if controller library status is needed.
