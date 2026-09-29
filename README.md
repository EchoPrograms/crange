# CRANGE

## Dashboard

The controller dashboard serves its frontend and API together on port 8000.
The service binds to the controller management interface; forward it through
SSH when working remotely.

```sh
sudo scripts/crange served --user crange
sudo -u crange-web -H scripts/crange password admin --admin
```

`crange password` prompts for a password without putting it in shell history.
For a project manager, use
`sudo -u crange-web -H scripts/crange password NAME --projects 3`.
The dashboard uses server-side sessions and a same-origin API. Project
managers see and operate only their assigned projects. The dashboard runs as
`crange-web` while the automation worker runs as `crange`; the web account
cannot read the Proxmox or OPNsense credential files. Jobs are persisted in
`/var/lib/crange-jobs/jobs.sqlite3`; password hashes and sessions are stored
separately under the web account's home directory.

Project user assignments queue a fixed `guac-sync` workflow. It writes an
auditable `guacamole/access/project-NNN.json` desired-state manifest containing
team users, default jump-box connections, and explicitly assigned scenario
guests. `guac-sync` only renders desired state; applying it is a separate
reviewed action. Guacamole credentials never enter the
dashboard or job payloads.

Configure the protected connector once on the controller with
`scripts/crange guac-config`, then use the project Users page to queue
`guac-sync` and apply the reviewed access manifest.

The matching Guacamole presentation extension is in `guacamole/extension`;
build it with `scripts/build-guac-theme.py` and install the resulting JAR in
Guacamole's protected extensions directory during a planned servlet restart.

Planning from the dashboard retains a saved Terraform plan for one hour and
displays its plan ID. Apply requires selecting that exact plan. Input changes
or a stale Terraform state require a new plan; deletion and replacement need
explicit allowance.

CRANGE provisions isolated cyber-range projects on the Proxmox cluster with Terraform and configures routers and jump boxes with Ansible. Project 3 is the live reference environment; each project keeps its own Terraform state.

## Command line

Run the CLI on the controller from this checkout (`scripts/crange`). Terraform credentials are read from `/home/crange/.config/crange/proxmox.env`; they are never printed. The CLI scopes Terraform to one project directory, uses the cluster’s private TLS setting only for that Terraform process, and applies the exact plan it just displayed.

```sh
scripts/crange projects
scripts/crange project-show 3
scripts/crange box-list 3
scripts/crange team-add 3 3
scripts/crange box-add 3 t003-j01 --team 3 --node proxmox4
scripts/crange network-add 3 corp --team 3 --id 100 --node proxmox4
scripts/crange box-add 3 corp-j01 --team 3 --network corp --node proxmox4
scripts/crange infra-plan 3
scripts/crange infra-apply 3 --yes
scripts/crange inventory 3
scripts/crange configure 3 all --yes
```

`infra-plan` prints Terraform's normal plan and a resource/action summary. `infra-apply` requires `--yes`, creates and inspects a fresh saved plan, and refuses any deletion or replacement unless `--allow-destroy` and (for replacements) `--allow-replace` are explicitly provided. Always review the plan output before applying. Removing a team or jump box changes desired state only; apply is a separate, guarded step. Team removal is blocked while jump boxes still reference that team.

`box-add` accepts nodes `proxmox1`–`proxmox5`, 1–16 CPU cores, and 512–65536 MB of RAM. Boxes placed off the current local template node are full clones. `box-state PROJECT BOX start|stop` changes desired power state, which must then be planned and applied. Jump boxes use `.11` onward within their selected network. `network-add PROJECT NAME --team TEAM --id UNUSED --node NODE` creates an isolated routed /24 behind that team's router; `--team 0` attaches the custom router beneath staging. The CLI reserves a static parent-side router address and adds return routes and management/egress policy. `box-add --network NAME` places a jump box in that custom subnet.

`project-init ID --transit 10.60.2.X/24` copies the reviewed generic Terraform project and creates a separate project directory and state. Check that the transit address is unused before initializing it. The CLI prevents duplicate addresses present in project inputs; it cannot discover addresses reserved outside this repository.

`configure PROJECT routes|routers|boxes|all --yes` regenerates Terraform-backed inventory and runs the existing reviewed Ansible playbooks. It configures project-router routes first, then project routers, team routers through the project-router SSH proxy, staging routers, and jump boxes.

Scenario VM and release commands use approved templates from each project's `approved-scenario-templates.json`. The current approved base is template VM 112 on `proxmox4`. Each release capture powers down the staging VM, makes a full disk copy, converts it to a project-scoped Proxmox template, and restarts staging. The manifest pins that new template ID and node; changing an alias cannot silently change an existing release. Static scenario IPs are `.2`–`.100`; Team 0 is selected with `--team 0`. HTTP/TCP health checks are explicit metadata and run from the team or staging router. A basic flow is:

```sh
scripts/crange scenario-add 3 web --team 0 --ip 20 --template approved-base --node proxmox4 \
  --health '[{"type":"tcp","port":80},{"type":"http","port":80,"path":"/health","status":200}]'
scripts/crange infra-plan 3
scripts/crange infra-apply 3 --yes
scripts/crange scenario-health 3
scripts/crange release-capture 3 1.0.0 --boot-order web,dns
scripts/crange release-test-prepare 3 1.0.0
scripts/crange infra-plan 3
scripts/crange infra-apply 3 --yes
scripts/crange release-test-check 3 1.0.0
scripts/crange release-test-cleanup 3 1.0.0
scripts/crange infra-plan 3
scripts/crange infra-apply 3 --yes --allow-destroy
scripts/crange release-deploy 3 1.0.0 --teams 1 2
scripts/crange infra-plan 3
scripts/crange infra-apply 3 --yes
```

Custom routed subnets can be attached beneath an ordinary team or staging router; an approved jump box may be placed directly on that subnet:

```sh
scripts/crange network-add 3 corp --team 1 --id 100 --node proxmox4
scripts/crange box-add 3 corp-j01 --team 1 --network corp --node proxmox4
scripts/crange infra-plan 3
scripts/crange infra-apply 3 --yes
scripts/crange configure 3 all --yes
```

Container images must be approved by platform administrators in `approved-container-templates.json`. Define a scenario CT with `container-add PROJECT NAME --team TEAM --ip OCTET --template ALIAS --node NODE --health JSON`. Release manifests can record several comma-separated `--boot-order` permutations; rollout accepts per-team overrides such as `--team-override 2:web:cores=4`. Override fields are static IP, cores, memory, node, and started state.

Release manifests are write-once JSON with a SHA-256 integrity check and source revision. Test clones use reserved Team 0 addresses `.90`–`.100`; after checking, cleanup removes them from desired state and the reviewed apply destroys those temporary clones. Rollout declares fresh clones for selected teams and still requires a separate reviewed plan/apply.

## Scope

Scenario images are immutable project-scoped templates and must be retained while deployed clones depend on them. Persistent data disks are not yet separate from scenario VM images. The dashboard invokes fixed, validated CLI operations and does not expose a shell or raw Terraform input.
