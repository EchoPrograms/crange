# Custom jump boxes

Use **Remote access** beside a VM, container or dedicated jump box to set its
display name and selected Guacamole services. **Use as a jump box** moves a
scenario VM/container into its team's jump-box list without replacing it.
Scenario definitions remain editable in Scenario. Team/network ownership,
guest names, addresses and Terraform resource identities stay unchanged.

Select SSH (22), RDP (3389), VNC (5900), or a combination. The guest or its
template must already run the selected services; this action does not install
a desktop or remote-access server. Connection credentials use protected controller defaults or encrypted
per-box credentials. Project managers choose whether to disclose custom
credentials when an assigned user opens Guacamole.

Saving queues an audited `jumpbox-settings` job. For deployed guests it applies
only Guacamole access tasks on the project router, the owning team router and
the selected custom router, then publishes the Guacamole connections. It does
not run DHCP configuration, package installation or guest power operations.
Undeployed guests enter Guacamole when deployment gives them an endpoint.
Renaming/removing a service removes its old Guacamole connection and can
interrupt existing sessions.

Assign a box once in project Users to grant all its selected services. Removing
a service revokes its grant on the next access publication. Existing single
service connection names and default team-box assignments remain compatible.
Display names must be unique across generated Guacamole connections. Captured
VM/container releases retain their jump-box role, name and selected services;
editing staging does not edit existing release manifests or deployed teams.
Custom names include the team in Guacamole, so staging and deployed teams can
reuse a release's box display names. Promoted VMs and containers also appear
in their assigned user's jump-box list and retain resource editing controls.

Project managers can create VM/container jumpboxes and reusable presets from
approved images. See [Admin templates and custom jumpboxes](admin-templates.md).
User-default/batch provisioning from those presets remains separate from the
existing dedicated Parrot jumpbox workflow.

## Verified acceptance

In isolated Project 240, VM 130 and CT 129 on staging custom subnet
`10.240.100.0/24` were named and promoted through audited jobs
`56729e367fbf441fa8a9b7a731196fa8` and
`a2eaddbb6bc943b084023e130fa9ffd4`. A separate native Guacamole test login
received six SSH/RDP/VNC connections through two box assignments. Native HTTP
tunnels returned interactive display frames for VM RDP and VM/container SSH.
The container's RDP/VNC servers were not installed, so these tests do not prove
those guest services were usable.

Job `f330f820563f43f4b1b9f13ad76d200c` removed the CT VNC service. A fresh native
login had five remaining connections; its CT VNC grant was absent while the VM
VNC grant remained. UFW access to CT port 5900 was removed on routers 109, 111
and 128, retaining the VM's exact endpoint permission. VNC session establishment
was not tested. Guest configuration hashes and running PIDs for 130, 129, 113,
135 and 134 remained unchanged. A manager authorized only for Project 240
received HTTP 403 when attempting this operation in Project 3.
