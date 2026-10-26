# Configure networks and team addresses

The default LAN is normally sufficient: Project P, Team T uses `10.P.T.0/24`, gateway `10.P.T.1`. Team 0 maps to T=254. Configure the project's shared-transit address separately; do not use the project-transit subnet as the staging LAN.

## Give a server a stable address

In the machine form, select the team/default or custom network and enter a static host octet from **2–100**. CRANGE derives the complete address and checks declared conflicts, including router uplinks. `.1` belongs to the router; `.101–.254` are DHCP clients.

An address declaration and a working guest configuration are different. Managed images can receive approved network initialization. A manually configured ISO image needs its guest settings changed inside the machine. The GUI should describe the intended platform network; application connection strings remain manager-owned.

## Add a custom subnet

Use the Workspace **Networks** controls, or the owning team's network controls. Give the network a name, unused subnet ID and owner, then save. Its router sits below the team/staging router so that it retains the owner's isolation boundaries.

Review and deploy its foundation. Run **Verify network** where offered before placing machines there. Verification uses a disposable, exactly owned probe to check required paths and isolation for the admitted node/network/image scope. A successful test is not blanket permission for all nodes or networks.

Remove dependent machine declarations before removing the network. Then review and apply the deletion. Saving a network removal alone does not destroy its VNet/router.

## Team-relative changes

A release maps default staging LAN addresses to the selected team's LAN while retaining host octets. For a server at `.20`, Project 100 staging is `10.100.254.20`, Team 1 is `10.100.1.20`. Do not embed staging addresses in an application and assume the infrastructure layer will rewrite arbitrary application files.

If your application needs derived addresses, use hostnames or a lab-manager script appropriate to that application. CRANGE's platform network initialization is separate from database/application setup. Verify custom-network behavior and image support before publishing a scenario that depends on them.

See [network topology and isolation](../concepts/networking.md) for router order, shared networks, routing and firewall ownership.
