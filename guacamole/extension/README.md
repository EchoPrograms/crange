# CRANGE Guacamole interface extension

This directory is a Guacamole extension source artifact. Build a JAR whose
root contains `guac-manifest.json`, `theme.css`, `ui.js`, and the declared `assets/`
resources, then install it in the
Guacamole `GUACAMOLE_HOME/extensions` directory. The manifest is pinned to
Guacamole 1.6.0, matching the live range deployment. Update the version field
when the Guacamole host is upgraded.

The extension registers `assets/remote-access.svg` as a web resource and sets
it as the browser favicon and Access sign-in logo. Both sign-in pages use
`gui/login.css`; the build packages that exact file as `login.css` in the JAR.
Control and Access retain their own names and logos, with reciprocal public
portal links. `login.js` replaces only Guacamole's login template and preserves
its native credential fields, submission, errors and continuation flow.

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

`auth-launch.js` consumes `data`/`token` URL parameters only after successful
authentication. The empty token in a dashboard launch intentionally replaces a
cached session initially, but must not override the new token on later route
authentication. Keep this cleanup when changing launch or login flows. Regression
check: `node tests/test_guac_auth_launch.cjs`.

Ticket authentication uses tab-scoped session storage for `GUAC_AUTH_TOKEN`.
A console launch must neither read/revoke the normal browser login token nor
replace it for other tabs. Fresh launches also discard tokens inherited through
an opener's session-storage snapshot; console reloads retain their own token.
Ordinary native login and shared preferences keep Guacamole's normal storage.
Test: `node tests/test_guac_ticket_storage.cjs`.

Ticket-authenticated sessions use their authenticated JSON connection trees and
do not request native database inventory, even when Guacamole advertises database
contexts. A dashboard identity need not be a separate database account. Native
password sessions still verify native inventory through the authenticated bridge;
server-side grant validation is unchanged.
Keyboard and clipboard capture textareas remain focusable but invisible; never
apply minimum field dimensions to these internal input sinks.

Admin console access supports both QEMU VMs and LXC containers. The broker
resolves the applied resource, rechecks current admin access and live ownership,
then selects `/qemu/{id}` or `/lxc/{id}` VNC proxy endpoints. LXC renders the
Proxmox terminal console; a VM renders its virtual monitor. Neither opens a guest
network listener. Provider tickets stay in the controller relay, which accepts
only the Guacamole server and expires unused launches after 45 seconds.

## Project workspace and session controls (October 19, 2026)

`workspace.js` and `workspace.css` extend the existing theme with project cards,
Grid/List server views, grouped preferences and a floating session toolbar.
The native client template is enhanced rather than replaced: the tiled clients,
keyboard/IME handling, clipboard, file transfer and advanced protocol controls
continue to use Guacamole's controllers. Password changes retain Guacamole's
self-update permission checks. Presentation preferences use `preferenceService`.

Session power requests use the same authenticated bridge and server-side
resource authorization as the home page. The network map includes only the
current managed project's team and copies declared addresses. Unknown or
unmatched native sessions keep their native controls without invented ownership.
The toolbar may be hidden; Ctrl+Alt+Shift opens it again.

Square corner tokens and typography/spacing direction are recorded in
[interface-design.md](../../docs/interface-design.md). Run
`node tests/test_guac_workspace.cjs` with the existing Guacamole UI/auth checks
before rebuilding the JAR.


Management menus use manual native popovers to escape transformed or clipped
application ancestors. Position them while hidden, then reveal; remeasure after
power status arrives. Mouse leave has a short grace period, and opening a menu
prioritizes its server status over the three-request background limit. Preserve
outside-click/Escape dismissal and keyboard focus return. Administrator dashboard
launches use the ordinary native Guacamole session/login; project launches and
hypervisor console tickets continue to use isolated session storage.

The session toolbar has a dedicated drag grip (arrow keys also move it; Home resets), a hide control and a small reopen control. Position/visibility are local to the browser/account. Network map lists authorized hosts for the focused connection’s team, refreshed from controller inventory. Click a host to save a local alias or notes; managed IPs remain read-only. Add host accepts a manual IPv4/IPv6 address, name and notes; only custom hosts can be deleted. Local records do not grant connections, modify the lab or reach the controller. Browser storage failure is reported explicitly.

The session title is a multiselect dropdown backed by Guacamole 1.6’s native connection-list context and client route updates, retaining its permission-filtered tree and tile manager. Checkboxes add/remove connections from the current view; the selector does not mint extra permissions. Native source: https://github.com/apache/guacamole-client/blob/1.6.0/guacamole/src/main/frontend/src/app/client/templates/connection.html

Preferences includes a Back link to the prior project view. Leaving a known CRANGE session returns to that session’s project, including when it was launched from All projects. The project return marker is scoped to the browser tab. Access branding uses a two-letter CR mark with an inverse square background for small-size readability.


Native Settings and the account dropdown share CRANGE Control's header, square
controls, IBM Plex Sans typography and neutral navigation surfaces. The six native
settings sections still use Guacamole's original controllers, permission-filtered
page lists, edit links, filters and pagers. The account menu uses a viewport-bound
popover with outside-click, Escape and keyboard navigation support. The portal
and Settings header include a plain CRANGE Control link to
https://crange.brogano.dev/ in a new tab, preserving any current remote session.
Project card network/server counts align at the bottom even when names wrap.

Run `node tests/test_guac_settings.cjs` alongside the workspace, UI and launch
checks when changing this navigation. Desktop and narrow browser fixtures cover
the native settings markup, menu placement/dismissal and portal header.
