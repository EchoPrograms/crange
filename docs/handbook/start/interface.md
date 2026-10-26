# Find your way around Control

Use the sidebar's project selector to choose context. Project actions operate on that project; platform administration is separate. Browser Back/Forward restores supported views, and names in tables open their related detail pages.

| Page | What you do here |
| --- | --- |
| Overview | See projects and recent work |
| Projects | Open a project; administrators create/manage projects and edit limits |
| Dashboard users | Administrators manage Control accounts and project roles |
| Jobs | Follow task progress, output, failures and reruns |
| Templates | Administrators build, publish and distribute approved bases |
| Cluster | Administrators inspect refreshed capacity, workloads and image replicas |
| Project Overview | See allocation, release state, project tasks and infrastructure tools |
| Teams | Open a team, its machines and users; manage team definitions |
| Scenario | Work on staging, releases and team deployments |
| Machines | Search the project's deployed/declared machine inventory |
| Project users | Manage Guacamole accounts, team grants and assigned machines |
| Settings | Manage project user and jump-box defaults |

## The Scenario tabs

**Workspace** is Team 0's authoring area: machine definitions, default/custom networks, current review and staging deployment. **Releases** holds captured versions and fresh-copy test status. **Team deployments** shows selected and applied versions, service checks and rollout history.

The release chooser opens a version menu. Each entry explains whether it is ready or still needs a fresh-copy test or cleanup. Selecting a version opens its own team form.

## Machine rows and detail pages

Click a machine row for resources, membership, network, observed power, assigned users, health checks, snapshots and recent tasks. The main controls are Connect, Edit and Start/Shutdown. The vertical **More actions** menu contains less common operations such as restart, force stop, snapshots or removal.

![Machine inventory and its filters](/img/screenshots/machines.png)

Tables support search and relevant filters. Selecting machines exposes group power actions with exact target confirmation. Hidden selections are cleared when filters change. Click an IP address or subnet to copy it.

## Progress and dialogs

A form collects parameters; a job performs the action. Closing its dialog leaves the job running. Reopen output from the contextual task card or Jobs. Task rows show the actor, current status, activity and runtime. A disabled action usually means a required stage has not completed; check the adjacent explanation and task result.

Keyboard users can move Scenario tabs with arrow keys, Home and End. Forms have labels, and menus and dialogs retain keyboard interaction. On small screens the navigation collapses and wide tables scroll within their area.
