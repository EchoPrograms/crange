# Admin templates and custom jumpboxes

## Custom jumpboxes

Open a project's **Teams**, select the team (including Team 0), then choose
**Create custom jump box**. Select an approved VM or container base, its network,
CPU/RAM and SSH/RDP/VNC services. The team is inferred. Address `0` chooses the
next available static address, avoiding boxes and router uplinks.

Guest credentials default to `user` / `toor`. Custom credentials are encrypted
outside Git and may be disclosed to the assigned user when opening Guacamole.
They must match accounts already present in the image; connection settings do
not create guest accounts or install the selected remote services.

**Jumpbox templates** saves project presets for base image, size and services.
Presets pin the approved image identity. Editing a preset affects future boxes;
existing boxes and immutable releases retain their settings. User-default/bulk
provisioning still uses the existing dedicated jumpbox setup.

Creation declares desired state. Review a plan before deployment; staging
changes do not modify deployed team releases. Assign the resulting box in the
project Users tab to grant all its selected Guacamole services.

## New ISO-based VM templates

Platform admins have a **Templates** page:

1. **Upload ISO** transfers a local ISO in bounded chunks, then queues a transfer
   to the selected node. Existing node ISOs are also selectable. Uploads may be
   retried or removed from Pending ISO uploads; duplicate filenames are rejected.
2. **Create from ISO** chooses Linux, Windows, BSD or Other; CPU model, cores,
   RAM, disk size/controller, BIOS/UEFI, machine type, TPM, guest agent, optional
   driver ISO and up to eight NIC models. Installer VMs are owned by the range
   pool and attach to its `craccess` build network.
3. **Open installer console** uses Proxmox's console with your administrator
   login. Install the OS, remote services and desired guest accounts. Prepare
   unique identities for cloning (for example Windows Sysprep), and shut down.
4. **Publish template** requires explicit preparation confirmation and project
   approval. It detaches installation media and converts the stopped guest to a
   template. Existing images and dependent releases are retained; use a new
   versioned name for a different image.

Windows defaults use UEFI, SATA, e1000 and TPM, avoiding a mandatory VirtIO
storage driver. A driver ISO permits VirtIO builds. Every cloned NIC attaches
to the selected project/team subnet, preserving range isolation.

Enable cloud-init/Cloudbase-Init only after installing/configuring it in the
image. Otherwise, configure each clone's declared static IP in the guest.
Linux Ansible tasks are skipped for non-Linux and manual-network VM images.
Guest user/sudo automation remains for managed Linux images; it does not manage
Windows or BSD accounts. An ISO builds a VM, not an LXC container; container
jumpboxes use the separate approved LXC image catalog.

ISO uploads are admin-only, at most 16 GiB each and eight pending uploads.
Installer hardware is bounded and there may be at most 20 active installer VMs.
The scoped API token has ISO allocation permission on `local` storage only;
no controller/API credentials are exposed to the browser.
