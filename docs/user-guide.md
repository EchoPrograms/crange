# Using CRANGE

[Documentation index](../README.md) · [CLI reference](reference.md) · [Administrator guide](administrator-guide.md)

This guide describes the checked-in dashboard and CLI. Installed services may run an earlier revision. Automatic placement is enabled only for the administrator-approved P1 Alpine pilot; other nodes and images remain outside its scope. See [distribution operations](distribution-operations.md).

## Accounts and responsibilities

| Account | Purpose | Managed by |
| --- | --- | --- |
| CRANGE administrator | Platform configuration, templates, projects and dashboard roles | Platform administrator |
| CRANGE project manager | Desired state, deployment and access within authorized projects | Administrator assigns project manager role |
| CRANGE participant | Open assigned Guacamole labs | Administrator assigns project/team access |
| Project Guacamole user | Native Guacamole login and explicit connection grants | Project manager, project **Users** |
| Guest Linux user | Login and sudo inside one machine | Project manager, machine **Users** |

These identities are separate. Changing a dashboard password does not change a Guacamole or guest password. Participants do not receive Terraform, controller, Proxmox or OPNsense credentials.

Sign in to the dashboard, select a project, and use **Project Overview**, **Teams**, **Scenario**, **Users** and **Jobs**. Administrators additionally manage **CRANGE Users** and **Templates**. Available projects and actions depend on your role.

## Projects, teams and networks

A project is a separate Terraform environment with its own state. Ordinary teams are 1–253. **Team 0 · Staging** uses network ID 254, so Project 3 staging is `10.3.254.0/24`, gateway `10.3.254.1`. The project transit `10.3.0.0/24` is not a staging LAN.

Scenario addresses use static host octets `.2`–`.100`; `.1` is the router, and `.101`–`.254` are DHCP. CRANGE checks conflicts among declared guests and router uplinks. Custom networks use an unused project subnet number and belong to one team or staging; select that network when creating a machine. A custom network's router connects beneath its owner router rather than bypassing isolation.

**Teams → a team** shows its machines, jump boxes, custom networks and configuration action. Adding/removing a team or machine normally edits desired state. A reviewed deployment is a separate operation. Remove dependent machines before removing their team or network.

## Build and deploy staging

1. Open **Scenario** and declare staging VMs or containers. Choose an approved image, static address, CPU/RAM, network and health checks. Team 0 is the authoring environment. Templates can require manual guest networking; selecting an address alone does not configure an image lacking supported customization.
2. **Stage Changes** prepares eligible missing approved local image caches when the distribution policy permits, then saves an exact Terraform plan. Read the job output, resource actions and placement information. This can allocate image caches; guest deployment is still separate.
3. Review **Staged Changes**. Check additions, deletions, replacements, node/storage, image identity and reservations. Plans expire after one hour and become stale when inputs or state change.
4. **Deploy and configure staging** applies that plan and configures routing, routers and jump boxes. Staging deployment rejects plans affecting other teams/shared resources; review those in **Project Overview**. A plan with no infrastructure changes can **Accept staged settings**.
5. Run **Validate all VMs** and **Validate all containers**, or validate an individual machine. Open its remote access to install/configure scenario services and exercise start/stop orders.

The default empty health-check list uses ping. Explicit checks support ping, TCP and HTTP; HTTP can check a path and expected status. Checks run through the owning router, not your browser. Passing ping does not prove an application is ready. A CLI example (replace the alias and node with approved values):

```sh
scripts/crange scenario-add 3 web --team 0 --ip 20 --template approved-base --node proxmox4 \
  --health '[{"type":"tcp","port":80},{"type":"http","port":80,"path":"/health","status":200}]'
scripts/crange stage-changes 3
scripts/crange stage-deploy 3 --plan-id PLAN_ID --yes
scripts/crange scenario-health 3 web
```

`PLAN_ID` is the retained identifier printed by staging. `infra-plan 3 --retain` is an infrastructure preview and never fills caches. Automatic placement is available only where the administrator's current policy, image catalog and verified network scope admit it; the current live pilot is restricted to proxmox1 and the Alpine image.

## Machines and jump boxes

VMs boot an operating-system disk; containers share the Proxmox host kernel and require approved LXC templates. A jump box is a remote entry point and can be a dedicated box or a scenario VM/container promoted through **Remote access → Use as a jump box**. Promotion preserves the machine's Terraform identity and network.

**Create custom jump box** selects an approved VM/container image, resources, network and SSH/RDP/VNC services. **Jumpbox templates** saves reusable project presets; editing a preset affects future machines. Existing boxes and release manifests retain their definitions. See [template workflows](admin-templates.md).

Remote-access settings publish connection details and exact router access permissions; they do not install SSH, RDP or VNC or create the configured guest account. The guest must already support the selected services and credentials. Selecting custom credentials uses protected encrypted storage. Assign the box in project **Users** to grant its selected services. See [custom jump boxes](custom-jump-boxes.md).

**Start/Stop** on a deployed scenario VM/container is a live operation; a desired dedicated jump-box power-state edit requires plan/apply. Guacamole box controls distinguish graceful shutdown from force stop. Native snapshots are per-guest local operations, separate from releases and archived restore points. Restore/delete can discard state; inspect the selected guest and snapshot first.

For managed Linux guests, **Users** beside a machine supports account creation, password changes, sudo grants/revocations and deletion. These modify one running guest. Deletion also removes its home and ends its processes; revoking sudo does not end an existing root session. Capture a release if those changes must travel with scenario images. See [guest account behavior](box-user-management.md).

## Capture, test and roll out releases

A release is an immutable versioned manifest and captured machine images. Capturing stops staging machines as required for complete copies and restarts them afterward; allow for service interruption. It records approved image identities, resources, networking, health checks, source revision and configured boot-order permutations. Use a new version rather than modifying an existing release.

Prepare release tests to declare fresh clones in Team 0, deploy them, then check their health/boot permutations. Test addresses are reserved `.90`–`.100`; keep them available. Cleaning up removes test declarations; a reviewed apply must remove the actual clones too. Release deployment requires a recorded passing fresh-clone test and cleaned test declarations.

```sh
scripts/crange release-capture 3 1.0.0 --boot-order web
scripts/crange release-show 3 1.0.0
scripts/crange release-test-prepare 3 1.0.0
scripts/crange infra-plan 3 --retain
scripts/crange infra-apply 3 --plan-id PLAN_ID --yes
scripts/crange release-test-check 3 1.0.0
scripts/crange release-test-cleanup 3 1.0.0
scripts/crange infra-plan 3 --retain
scripts/crange infra-apply 3 --plan-id CLEANUP_PLAN_ID --yes --allow-destroy
```

Then explicitly select target teams. Release staging changes desired state; apply is separate:

```sh
scripts/crange release-deploy 3 1.0.0 --teams 1 2
scripts/crange infra-plan 3 --retain
scripts/crange release-apply 3 --teams 1 2 --plan-id PLAN_ID --yes
scripts/crange release-check 3 --teams 1 2
```

Review any destructive permissions required by the exact plan. Upgrade/rollback commands require `--from-version` to identify the expected deployed version; rollback rebuilds from a selected immutable release rather than undoing arbitrary guest writes. Overrides can change approved resource/address/placement/power fields for selected teams. Staging edits never silently update deployed teams.

These release workflows are implemented; the new multi-node library/cache rollout path is not activated or accepted in production. Persistent data disks are not yet separate from scenario images: replacement can lose guest-local changes and data. Retain old image bases while clones or snapshots depend on them.

## Participant access

Project **Users** creates Guacamole-only logins, normally `pNNN-username` (for example `p003-player01`). Team selection grants that team's default jump box; additional boxes are explicit grants. Default user settings and numbered batches reuse project defaults. Removing a user removes access, not its guest machines. **Refresh connections** republishes deployed endpoints and grants.

Dashboard participants can open assigned labs through short-lived broker tickets. Native Guacamole users sign in using their separate project account. If a connection is absent, ask a manager to verify deployment, assignment and publication before changing guest passwords.

## Jobs and failures

Actions queue fixed worker jobs. Open **Jobs** to inspect queued/running/succeeded/failed/interrupted status and output. Closing a dialog or signing out does not cancel work. Cancellation removes queued work or requests termination of running commands; completed infrastructure changes remain. Interrupted jobs may leave provider tasks or partial allocations: do not repeatedly submit apply, manually delete resources, or clear reservations.

After a failure, read the last useful error, inspect actual resources, and create a fresh reviewed plan. Infrastructure can succeed while subsequent configuration or Guacamole refresh fails; repair the failed phase rather than recreate healthy machines. Managers should send the job ID and error to administrators without sending passwords or tokens.
