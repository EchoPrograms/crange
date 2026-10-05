# CRANGE administrator templates

These manifests record built images and the checks actually performed. Templates
are maintained by platform administrators; the dashboard selects approved
references and never invokes a root image builder.

The initial Debian 13 container base is CT 3000 on proxmox4. It is an unprivileged
LXC with a 4 GiB filesystem, OpenSSH, sudo, curl, CA certificates and ping tools.
The guest account is `user`. The controller public key and a password supplied
through a protected external file are installed during the build. Guest passwords
and private keys are not stored in these manifests or Git. Verify the guest
credentials with the platform administrator before using Guacamole SSH.

Build on the intended Proxmox node, using an unused cluster-wide ID:

```sh
python3 build-container-base.py \
  --vmid 3000 \
  --archive debian-13-standard_13.6-1_amd64.tar.zst \
  --ssh-key-file /path/to/controller.pub \
  --password-file /path/to/protected-guest-password
```

The password file must be owned by the operator with mode 0600. The tool refuses
an occupied guest ID, refreshes the signed Proxmox appliance catalog and verifies
the cached archive's SHA-512 checksum before creating anything. Builds use the
approved `craccess` management bridge with DHCP. The finished template has no
assigned IP, stays stopped and is never started on boot. New clones regenerate
SSH host keys and machine IDs. Build records remain under
`/var/lib/crange-template-builds/` on the node, including failed build records.
A failure does not automatically destroy a guest; inspect the recorded ID and
identity before cleanup or recovery.

Approval is separate from building. Verify fresh clones, network customization,
controller SSH, sudo and graceful shutdown before registering a template ID/node
in an environment's `approved-container-templates.json`. Preserve immutable
release image references. Update the broken legacy `approved-ct-base` alias only
when its replacement has passed those checks; never convert or remove VM 120,
which is Project 3's staging jump box.

Ubuntu Server 24.04 (CT 3001) and Alpine 3.24 (CT 3002) are now verified
container bases. Both passed fresh-clone SSH/sudo, distinct SSH host identities,
static addressing, host-key persistence and graceful shutdown. Disposable
verification clones were removed. Their manifests record the exact source
checksums and validation results. Alpine uses OpenRC and does not provide the
systemd machine-id file; uniqueness was verified through distinct served host
keys and cloned guest names.

Sources: [Proxmox container documentation](https://github.com/proxmox/pve-docs/blob/master/pct.adoc)
and [signed appliance catalog verification](https://github.com/proxmox/pve-manager/blob/master/PVE/APLInfo.pm).

## Linux VM bases

`scripts/build-vm-base.py` builds Debian 13 and Ubuntu Server 24.04 VM bases on
a Proxmox node. Supply the exact official amd64 cloud-image URL, its published
SHA-512 (Debian) or SHA-256 (Ubuntu) checksum, a controller public key and a
protected guest-password file. This administrator command is never a dashboard
workflow. It refuses occupied IDs, verifies the downloaded standalone QCOW2
image before creating a guest, and runs fixed offline package/SSH customization.
It records the official-source and customized-image checksums separately.

The resulting stopped template uses a 20 GiB primary disk, a cloud-init drive,
DHCP on the approved build bridge, serial console and QEMU guest agent. Clone
identity state and SSH host keys are sanitized. Approval remains separate and
requires real boot, static networking, SSH/password access, guest-agent and
graceful-shutdown checks. A build failure preserves its record and any guest or
disk for operator inspection; it does not destroy existing infrastructure.

Ubuntu VM 3010 is built and awaiting fresh-clone verification. Debian VM 3011
is building. Alpine VM and OPNsense VM paths are still outstanding.
