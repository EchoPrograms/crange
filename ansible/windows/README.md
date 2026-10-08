# Project 100 Windows and Active Directory

The Windows portion of Meridian Harbor Logistics (MHL) uses an isolated
`mhl.test` forest in Team 0 staging and an independent forest in each released
team. The project infrastructure workflow owns network and Linux services;
these Ansible playbooks configure the Windows domain controllers, file servers,
and RDP jumpboxes.

The Microsoft Server 2025 Evaluation ISO is the official x64 English media.
Install **Standard Evaluation (Desktop Experience)**. The evaluation lasts
180 days and has Microsoft's online-activation requirements; tell lab admins
that this is evaluation media. Windows Server Desktop Experience also serves
as the Windows GUI/RDP jumpbox because Windows 11 Evaluation media requires
registration. Do not describe these jumpboxes as Windows 11.

Staging host addresses are `10.100.254.10` (DC), `.20` (file server), `.30`
(Linux PostgreSQL), `.40` (Linux app), `.50` (Linux CI), and `.60` (Linux
backup). Each released team uses the same host octets on its own isolated
`10.100.T.0/24` LAN. The Windows jumpbox is `.12`. Team 1 and Team 2 have
separate forests, DNS, and credentials; do not connect one team's machines to
another team's DC.

Install the pinned Ansible collections and WinRM transport in the controller's
managed Ansible environment:

```sh
ansible-galaxy collection install -r ansible/windows/requirements.yml
python3 -m pip install 'pywinrm>=0.4.3'
```

Create `/etc/crange-secrets/project-100/windows.vault.yml` as an Ansible Vault
file with local administrator, domain administrator, DSRM, backup-service,
warehouse-deployment, and employee account credentials. Define
`mhl_guest_admin_passwords` as a map keyed by each inventory hostname, with a
unique random value for every guest; the playbooks rotate the shared base-image
administrator password before domain membership and use the guest-specific
value for later controller access. Keep domain and service credentials in the
protected vault and keep its vault password outside Git. The example inventory
contains only addresses and connection settings. Secret tasks use `no_log`.

Run `project-100-domain.yml` against only the staging DC first. It creates the
forest, DNS, OUs, employee accounts, and the deliberately weak but
non-administrative `svc_backup` and `svc_warehouse_deploy` accounts. The
`WarehouseDeployers` group can edit the `Warehouse Deployment` GPO, which is
mistakenly linked at the domain root instead of only the Warehouse OU. The
employee group can also modify files in the `MHL-Backups` share. These are
bounded authorization mistakes to investigate; neither service account is a
Domain Admin and SMB signing remains at the Windows default.

After the Team 0 services pass their health checks, create a release and
explicitly deploy independent copies to selected teams. Update each team's
DC address and DNS before joining that team's file server and jumpbox. Run
the DC play before `project-100-members.yml`. Never run the member play across
unreleased teams or connect different isolated forests.

The general Windows base must remain workgroup-only. The fixed administrator
builder installs the signed QEMU Guest Agent and limits RDP/WinRM firewall
access to the controller/Guacamole management address. After confirming the
guest is healthy, run `generalize-base.ps1` on the owned base only, wait for
Sysprep to shut it down, then publish through the reviewed image workflow.
Never Sysprep a promoted domain controller or joined member. Do not put
administrator or project credentials into the base image, image manifest, or
Git.

The Windows installer answerfile is generated into a local-only Proxmox ISO
with a temporary administrator password retrieved by the fixed builder from
the controller's root-owned secret store. That ISO must not be copied to the
shared library or mirrors. The official installation ISO and its verified
digest are platform media and may be mirrored normally.
