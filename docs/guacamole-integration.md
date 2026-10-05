# Guacamole integration

CRANGE controls the Guacamole connections users can reach. The dashboard opens
Guacamole with a short-lived, signed JSON-auth ticket, so dashboard accounts do not
need separately provisioned Guacamole passwords. Tickets are minted by the
unprivileged `crange` broker over a private Unix socket. The web service passes
the current CRANGE session to that broker; it cannot read the signing key or
the Proxmox, OPNsense, or Guacamole administrator credentials.

## Separate project accounts

Project **Users** manages native Guacamole accounts, separate from dashboard
accounts in **CRANGE Users**. Its default view is a selectable user list. Create
one or multiple users from a dialog; open a user to manage credentials, grants,
locking, jump boxes and activity. Passwords require at least six characters.
Users cannot change their own passwords unless a manager enables **Allow this
user to change their own password**. Managers can always set passwords.

Usernames are not automatically prefixed. The default project format is
`p{project-number:03d}-t{team-number}u{user-number}`, for example `p003-t1u1`.
Project settings can instead use `comp{user-number:02d}` to create `comp01`,
`comp02`, etc. Password formats support the same tokens. With no saved password
format, the generated username is also the default password. A saved format
remains encrypted outside Git; leaving the field blank preserves it. Supported
tokens are `{project-number}` (or `{project-id}`), `{team-number}` and
`{user-number}`. Add `:02d` or `:03d` for zero padding. Staging displays team 0.
All expanded passwords must still contain at least six characters.

Project settings define a reusable per-user jump-box setup: zero to ten boxes,
each with network, node, CPU and memory settings. Bulk creation allocates unused
static addresses and explicitly assigns each new account its own boxes. Managers
can add another default setup to an existing user or assign existing boxes.
The **Add jump box** dialog can create a new Guacamole account or attach that box
to an existing account. All box declarations pass the same ownership, address and
quota validation as CLI declarations. Review and apply their infrastructure plan
to deploy them; enrollment does not apply Terraform automatically.

Names are checked across projects, dashboard accounts and native Guacamole
accounts before creation. A unique database index prevents concurrent duplicate
creation, and a collision rolls back the entire batch. New internal account keys
are stable, so changing a login does not change its box assignments. Guacamole's
REST user update does not rename identifiers: CRANGE creates the new owned login,
applies its permissions, then removes the old owned login. Renaming requires a
manager-supplied password and preserves previously retrieved login history.
Deleting users leaves their jump boxes for reassignment. Multi-select deletion
validates that every selected user belongs to the current project.

User detail pages retrieve successful login history, last login and active
connections through the protected broker. Results are filtered to this account
and project-owned connections. Failed-login attempts are not exposed by the
standard Guacamole REST user history API and are marked unavailable. Refreshing
activity preserves fields currently being edited. Project display names are
stored separately from infrastructure identity and shown with a grey `(pNNN)`.

All account changes queue auditable `guac-users` or `guac-enroll` jobs and open
the output dialog. Project display/default settings are audited separately.

The broker authorizes the manager's live session and encrypts the password with
an authenticated cipher bound to the project and username. The dashboard cannot
read the key. The worker decrypts only when creating an account or changing its
password; secrets are absent from job payloads, audit entries, and ownership
files. Keep the signing key backed up securely: rotating it also requires
resetting the stored native account passwords before further password updates.

Native users are tracked in `project-NNN.users-applied.json`. Reconciliation
refuses to take over unrelated existing accounts and deletes only owned users.
Connection access is limited to READ on selected deployed connections, and
revoked grants are removed on apply. `guac-apply` refreshes these permissions
when connection identifiers change. Native account jobs do not run Terraform,
Ansible, or restart Guacamole. Existing remote sessions may remain connected
until they close; disabling blocks subsequent logins.

## Dashboard launch tickets

The broker checks the live CRANGE session, project membership, enabled state,
current team membership, and explicit connection grants. It intersects current
assignments with the last applied Guacamole sidecar. Therefore a new grant only
appears after `guac-apply`, while removed team or connection grants stop
appearing in newly issued tickets immediately. Tickets expire after 60 seconds
and contain only the user's authorized deployed connections.
Each launch clears the token parameter used for cached authentication, so
opening a new dashboard ticket refreshes access even in an already signed-in
Guacamole browser. Existing remote sessions are not forcibly disconnected.

`guac-sync PROJECT` renders `guacamole/access/project-NNN.json`;
`guac-apply PROJECT` reconciles the manifest through the protected connector.
Jump boxes are associated with their team; team membership grants the
lowest-numbered jump box for that team. Users can also receive additive access
to selected jump boxes, scenario VMs, and containers. Platform administrators
receive all deployed connections in projects. For dashboard accounts, JSON-auth mode does not
persist user READ permissions in Guacamole's database; CRANGE tickets define
the available connection set for each session. Existing CRANGE-owned database
grants are removed on the next apply.

Dashboard project roles are separate from connection grants. Participants can
open their assigned lab connections but cannot inspect management jobs, change
user assignments, or submit automation. A manager role applies only to the
selected project. Administrators select the initial role when creating users
and change individual project roles in CRANGE Users. New accounts default to
participant; upgrading preserves existing project managers. Role changes do
not change guest OS credentials or team/connection assignments.

Undeployed resources are omitted until Terraform reports an address. The
worker records owned connection identifiers and complete deployed connection
details in `project-NNN.applied.json`; it updates and removes only connections
in that sidecar and rejects name collisions with unrelated Guacamole entries.
Router policy admits Guacamole SSH only to deployed guests whose declared
protocol is SSH; removed target rules are pruned on the next router configure.

## Enabling launch tickets

The live theme extension is pinned to Guacamole 1.6.0 and has been confirmed
loaded at startup. JSON authentication must also be enabled on the Guacamole
host. This requires one restart of the Guacamole web application; active
Guacamole sessions will be disconnected. The installer stages and validates
the change without restarting unless passed `--apply`.

1. Install Python's `cryptography` package for the controller's system Python.
2. On the controller, run `sudo -u crange /home/crange/crange/scripts/crange guac-json-key`.
   The command creates a 128-bit key in a mode-0600 file and does not print it.
3. Copy `scripts/install-guac-json-auth.sh` to the Guacamole host and install
   it as a root-owned executable. Pipe the controller key directly over SSH
   into `install-guac-json-auth.sh --stage`; do not print, paste into shell
   history, or commit the key.
4. Re-run `sudo /home/crange/crange/scripts/crange served --user crange` on
   the controller. This installs and starts `crange-guac-broker.service`.
5. Confirm the broker is active, then run the Guacamole installer with
   `--apply` during a suitable maintenance moment. The script installs the
   Apache JSON-auth extension matching Guacamole 1.6.0, checks its published
   SHA-256 checksum, validates Compose, and recreates only the `guacamole`
   service without dependencies.
6. In CRANGE, sync and apply the project's Guacamole manifest. The same apply
   removes any prior CRANGE-managed persistent user permissions. The theme,
   PostgreSQL authentication, connector, and unrelated connections are left
   in place.

Keep the controller signing key and Guacamole `JSON_SECRET_KEY` identical and
outside Git. Rotate them together: install the new key on both systems and
restart Guacamole and the broker. The URL contains an encrypted ticket with a
60-second validity window; treat it as sensitive until it expires.

## Verification

The source includes tests for encryption, project/team filtering, immediate
removal on changed membership, short ticket lifetime, manifest-backed
connection scope, and real Unix-socket caller credentials. Live non-admin
browser verification passed on 2026-09-29: dashboard login, exactly the team's
default jump box plus one explicit scenario connection, team revocation before
the next apply, removal of all grants, rejection of another project, and
rejection of a disabled account. Temporary verification users were removed
and the normal project manifest reapplied.

The live theme has a CRANGE login heading, persistent field labels, dark forms
and menus, readable empty states, and visible keyboard focus. The JSON-auth
extension is installed once: `JSON_ENABLED=false` disables Docker's automatic
bundled copy while the verified mounted JAR remains active. Guacamole reads the
shared secret through environment properties independently of that switch.

An active Guacamole tunnel can remain connected until it closes; disabling a
CRANGE account prevents new launch tickets but is not an out-of-band disconnect
for an already-open remote session.
