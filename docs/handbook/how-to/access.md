# Give users access to their lab

There are three separate identities. A password or role change in one does not change the others.

| Identity | Use | Where to manage |
| --- | --- | --- |
| Dashboard account | Sign in to CRANGE Control; manager/participant roles | Administrator: **Dashboard users** |
| Project Guacamole account | Native CRANGE Access login and connection grants | Project manager: **Project users** |
| Guest OS account | Log in inside a VM/container | Guest itself or supported machine **Users** |

## Create a project user

Open **Project users**, create the user, select a home team and choose allowed connections. The home team controls placement. A team grant covers its designated legacy default jump box; it does **not** grant every machine or other users' desktops. Assign extra machines explicitly.

Numbered user batches use the project's naming/password and jump-box defaults in **Settings**. Check that those defaults fit quota before creating a large batch. Connection grants and guest provisioning are separate resources.

![Project Guacamole accounts, status and assignments](/img/screenshots/project-users.png)

Changing a password, grant, disable or removal queues account reconciliation. This has an independent worker lane so that a long image capture does not block account updates. Watch the job result. Bulk lock/unlock/delete validates the selected accounts before writing and supports up to 100 users.

## Assign a jump box or machine

A jump box is a remote entry point, not a special isolation bypass. Create a custom jump box from an approved VM/container image and network, or use supported remote-access settings to promote an existing scenario guest without changing its Terraform identity.

Select SSH/RDP/VNC only when the guest has that service and a matching account. Custom connection credentials must match accounts already present in the image; publishing them does not create the OS account or install a server. Project presets save base/resources/services for future custom boxes without changing existing boxes or releases.

Use **Refresh connections** when deployed endpoints/grants need republishing. Control launch tickets are short-lived and authorized by a private broker. A copied machine/monitor URL does not grant access.

## View several connections

Use the session's connection selector to open several assigned connections. Guacamole displays them together in its native pane layout. Use each pane's controls to focus or close that connection.

## Place the session controls

A connection's options bar starts minimized in the bottom-left corner. Select its small edge tab to open it, or use **Ctrl+Alt+Shift** for quick access to session options. Drag the bar to move the expanded controls; drag the minimized tab to place it along an edge.

CRANGE remembers the expanded position, minimized-tab position and open or closed state separately for each connection and account in that browser. Switching to another connection restores its own arrangement. Clearing browser storage resets these choices.

**Disconnect** from **Other connections** returns to the main page showing all projects. A managed lab connection returns to its owning project. Disconnecting a remote session leaves the machine running.

## Clipboard access

Selecting connections or opening session controls does not request clipboard permission. Automatic clipboard sync runs only when the browser has already granted clipboard-read access. If it requires approval or does not support that permission, use **Options → Clipboard** to paste text into Guacamole's clipboard editor. Ordinary fields keep their normal paste behavior.

## Dashboard roles

Administrators use **Dashboard users** to create enabled accounts and assign per-project manager/participant roles and team scope. Participants receive assigned access, not project infrastructure authority. The signed-in account cannot disable/delete itself.

Account lock/removal revokes access through reconciliation; it does not delete the guest machines. Deleting one user's shared jump box affects everyone granted that box. Examine assignments before removing it.

If Connect fails, check deployment, grant, published protocol, guest listener/credentials and router path separately. See [troubleshooting](../admin/troubleshooting.md).
