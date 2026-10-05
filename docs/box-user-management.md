# Linux box accounts

Open **Teams → a team → Users** beside a deployed VM, container, or jump box.
These accounts belong to the guest operating system; they are separate from
CRANGE dashboard accounts and Guacamole logins.

The menu lists login accounts and supports account creation, password changes
(minimum six characters), granting sudo, revoking sudo, and deleting an account
with its home directory. Deletion ends sessions and processes owned by the
selected account. Revoking sudo does not terminate an existing root session. If an
unmanaged sudo policy still authorizes the account, revocation reports failure
so an administrator can review that policy.

Root, the bootstrap `user` account, and operating-system service accounts are
protected. Operations require project-manager access, enter the audited worker
queue, and verify the deployed guest's identity and subnet against the project
configuration and Proxmox. They operate on one running guest. These are live
guest changes; capture a release to retain them in a scenario image.

Passwords are encrypted by the existing local credential broker before entering
the queue. The worker decrypts them and supplies a salted SHA-512 password hash
to Ansible through a private temporary file. Password tasks suppress output.
No plaintext password is stored in job options or audit records.

The controller's public range key is installed in root's authorized keys with
forwarding and terminal allocation disabled. Root uses key authentication;
password login to root is disabled. Account jobs prefer that root key and use
the bootstrap account with sudo for initial installation. Deployment/configure
also installs the key on Linux guests within the selected team, including VMs.
This integration supports Linux guests with OpenSSH, Python 3, and sudo; it is
not Windows or FreeBSD account management.
