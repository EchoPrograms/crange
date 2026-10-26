# Build, approve and distribute templates

Platform administrators maintain OS bases. Managers choose approved bases and may create project-scoped scenario derivatives/releases; they do not receive template-host credentials.

## Build from an ISO

Use **Templates → Upload ISO** or choose an existing node ISO. Uploads are bounded: at most 16 GiB each and eight pending uploads. A transfer job moves the uploaded media to the selected approved node.

**Create from ISO** selects OS family, CPU/RAM/disk, disk controller, BIOS/UEFI, machine type, TPM, agent, optional driver ISO and bounded NIC models. Installer VMs belong to the range pool and build network. The current maximum is 20 active installers.

Open the installer console using your Proxmox administrator login. Install the OS, intended remote services and guest accounts. Prepare supported customization and unique clone identities; for Windows use the appropriate generalization process. Shut the installer down before publication.

**Publish template** requires explicit preparation confirmation and project approval. It detaches install media and converts the stopped guest to a template. Use a new versioned identity for changes; retain images referenced by releases or clones.

Windows defaults use UEFI/SATA/e1000/TPM to avoid requiring VirtIO storage drivers for a basic build. Use a driver ISO when selecting VirtIO. Cloud-init/Cloudbase-Init should be enabled only after it is installed and configured in the image. Otherwise declared addresses require manual guest networking.

An ISO produces a VM, not an LXC base. Container images use the separate approved catalog. Administrator Linux VM/container builders exist in `scripts/`; use installed `--help` and protected password/checksum/key inputs.

## Publication and acceptance

A build record is not acceptance. Verify normal fresh-clone boot, unique machine ID and SSH keys where relevant, supported network customization, required agent/access paths, graceful shutdown and confirmed probe cleanup. Published images have immutable artifact/source identity and explicit project scope.

Local cache verification is separate from base publication. A restored cache becomes selectable only after its native backing, owned disposable clone, readiness/identity and cleanup evidence pass. Nodes also need pinned-provider and scoped network acceptance, especially for LXC compatibility.

## Distribution and cache recovery

Cluster's image matrix shows published images and each node's replica/readiness. **Prepare local cache** uses a fixed audited worker workflow under the same capacity/approval rules as staging review. Refresh does not prepare a cache.

Recovery actions operate on the journal's exact template/probe/task/reservation. Resume a verified existing native operation when supported; do not restore another full copy just because the wrapper timed out. A retained partial allocation cannot be treated as free space.

Cache removal needs exact garbage-collection review/apply and fresh dependency proof. Central immutable artifacts remain retained. Never replace an archive under an already published identity.
