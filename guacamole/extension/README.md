# CRANGE Guacamole interface extension

This directory is a Guacamole extension source artifact. Build a JAR whose
root contains `guac-manifest.json`, `theme.css`, and `ui.js`, then install it in the
Guacamole `GUACAMOLE_HOME/extensions` directory. The manifest is pinned to
Guacamole 1.6.0, matching the live range deployment. Update the version field
when the Guacamole host is upgraded.

The interface uses Guacamole's current session token to call its same-origin
`/api/ext/crange-control/control` endpoint. The Guacamole server verifies that
session and forwards authorized box control requests to the controller. Browser
requests do not connect directly to the controller. CRANGE access provisioning
remains in the controller worker. Installing the extension requires a
servlet-container restart because Guacamole loads extensions at startup.

The controller web service uses its UID-checked local broker to read protected
grant records and Proxmox status. Power and snapshot changes remain fixed,
audited worker jobs; the worker checks access again before execution.

Install the same random 32-byte hexadecimal bridge key at
`/var/lib/crange-web/.config/crange/guac-bridge.key` on the controller (web user,
mode 0600) and `/etc/guacamole/crange-bridge.key` inside Guacamole (container
user, read-only). Neither key belongs in Git.

Native platform administrators are listed in the protected controller file
`/home/crange/.config/crange/guac-management-admins.json` (a JSON array of
usernames, mode 0600). Keep this list aligned with approved Guacamole platform
administrators when granting or revoking administrator access. Project users
always require current, applied box grants.

The range deployment role needs `VM.Snapshot` and `VM.Snapshot.Rollback` on the
range pool for snapshot management. Preserve the existing ACL scope; do not
grant these permissions across unrelated infrastructure.
