# CRANGE Project Guidance

## Purpose

Build a reproducible cyber range on the existing Proxmox cluster. Infrastructure is managed as code with Terraform and Ansible. A future web application will let project managers create and manage scenarios through a GUI without giving them direct Proxmox, OPNsense, or controller credentials.

The GUI should call controlled automation workflows. It must not let project managers run arbitrary shell commands as root on the controller.

## Existing environment

- Controller: Debian Trixie VM/container at `10.60.0.20`.
- Repository: `~/crange`.
- Proxmox cluster nodes: `proxmox1` through `proxmox5`.
- Proxmox API provider: `bpg/proxmox`, pinned to `0.104.0`.
- OPNsense: `10.60.2.1`.
- OPNsense API credentials: `~/.config/crange/opnsense-api.txt`; never commit or print credentials.
- Proxmox API environment: `~/.config/crange/proxmox.env`; never commit or print credentials.
- Shared access network: `10.60.0.0/24`.
- Shared project-router transit network: `10.60.2.0/24`, VNet `crtrans`, OPNsense interface currently `opt2`.
- Proxmox SDN zone: `crange`; underlying VXLAN `crange`.
- Terraform state must remain separate for each real project.

## Addressing and topology

- Project `P` uses project transit subnet `10.P.0.0/24` and project aggregate `10.P.0.0/16`.
- Project router transit address is configured separately on `10.60.2.0/24`.
- Project router owns `10.P.0.1/24` on the project's transit VNet.
- Team `T` has transit-side router address `10.P.0.(T+1)/24`.
- Team `T` LAN is `10.P.T.0/24`; its router LAN address is `10.P.T.1`.
- Team VNets use `pNNNtNNN`, for example `p003t001`.
- Reserve network number 254 for the project manager's Team 0 staging environment:
  - Display name: `Team 0` or `Staging`.
  - Routing/network ID: 254.
  - LAN: `10.P.254.0/24`.
  - Staging router LAN gateway: `10.P.254.1`.
  - VNet: `pNNNt254`, for example `p003t254`.
  - Team 0 router needs an unused transit-side address, e.g. Project 3 uses `10.3.0.4/24`, gateway `10.3.0.1`.
- Do not use `10.P.0.0/24` as Team 0's LAN. That is the project transit subnet.
- Ordinary teams are numbered 1–253. The Team 0 staging environment maps to network ID 254.
- Device addressing within team/staging LANs:
  - `.1`: router gateway.
  - `.2`–`.100`: reserved for static assignments.
  - `.101`–`.254`: DHCP range for DHCP clients.
  - Scenario machine IPs should be declared in desired state and checked for uniqueness.

## Project manager workflow

Project managers should be able to:

1. Create/edit scenario VMs and containers in Team 0 staging.
2. Select approved base images/templates and set resource sizes within platform quotas.
3. Configure networking, static addresses, services, and startup/health checks.
4. Start and stop machines in arbitrary/ambiguous order to verify the scenario works.
5. Capture the complete staging scenario as an immutable, versioned release.
6. Test the release by creating fresh clones and running defined health checks.
7. Explicitly roll a selected release out to all or selected team environments.
8. View job status, logs, resource usage, release history, and rollback options.

Changes made in Team 0 must not silently change deployed team environments. Release, test, and rollout are explicit operations.

A release should include a manifest with:
- release ID/version and source revision;
- VM/container definitions and resources;
- template/image references;
- network and static-IP allocation;
- startup dependencies or boot-order test permutations;
- service readiness/health checks;
- per-team overrides, if allowed;
- creation metadata and rollout status.

Keep base OS templates maintained by platform administrators. Project managers may create project-scoped scenario images/templates derived from approved bases. Retain older versions while deployed linked clones depend on them.

For production team rollout, prefer fresh clones from an immutable release. Keep persistent data disks separate where data must survive replacement. If in-place patching is supported, record drift and provide a path to rebuild from the release.

## Security and authorization

- Project managers may operate only within projects they are authorized for.
- Enforce per-project resource quotas and platform-wide limits.
- Allow only approved images, networks, storage pools, and VM/container options.
- Validate project IDs, team IDs, addresses, names, resource counts, and all user-supplied parameters.
- No arbitrary shell scripts or raw Terraform from the GUI.
- Run a controller worker with narrowly scoped Proxmox and OPNsense API credentials. Do not expose its credentials to the web application or users.
- Use a job queue/worker to execute fixed, reviewed Terraform, Ansible, Guacamole, and OPNsense workflows.
- Keep an audit trail for edits, captures, approvals, deployment, deletion, and rollback.
- Store secrets outside Git with restrictive file permissions.
- Never update OPNsense firewall rules from project automation. OPNsense automation may manage static routes/gateways only.
- Apply team/project isolation on Debian routers and keep static firewall policy independent of project lifecycle.

## Current Terraform/Ansible state

- Project 3 is a real deployed project. Its Terraform environment was moved from `terraform/environments/project-template` to `terraform/environments/project-003`.
- Project 3 Terraform state currently contains:
  - SDN applier resources;
  - VNets `p003tr`, `p003t001`, `p003t002`;
  - project router CT 122;
  - team routers CT 123 and CT 124;
  - jumpbox VM 125 (`p003t001-jump-01`, `10.3.1.11`).
- Project 3 vars: `project_id = 3`, `transit_address = "10.60.2.32/24"`, `team_ids = ["1", "2"]`.
- A `terraform.tfvars` file was recreated to represent the existing jumpbox, preventing Terraform from planning to destroy it. Preserve this input.
- A clean `project-template` directory was copied from the live configuration without `.terraform`, Terraform state, or saved plan files. It must not own Project 3 resources or state.
- Before any apply, inspect the Terraform plan. Stop if it proposes destroying or replacing live resources unexpectedly.
- Terraform must be initialized separately in each real project directory, with separate state per project.
- Existing inventory generator: `scripts/generate-project-inventory.py`.
- Existing OPNsense route helper: `scripts/opnsense-project-routes.py`.
- Existing Ansible project-router return-route playbook: `ansible/router-routes.yml`.
- Its task `ansible/tasks/project-routes.yml` reads `router_routes` from inventory and installs persistent routes through an if-updown hook. The inventory generator currently supplies routes only for regular teams; extend it to include Team 0's `10.P.254.0/24` via Team 0 router transit IP.
- Existing router automation playbook: `ansible/routers.yml`.
- Router Ansible must run project routers before team routers because team-router SSH is proxied through the project router. Use `--limit project_routers`, then `--limit team_routers`.
- Debian routers are unprivileged LXCs. Do not use `sysctl --system`; apply only the intended sysctl file (e.g. `sysctl -p /etc/sysctl.d/99-crange-router.conf`).
- Traceroute masking and IPv6 blocking are managed through UFW Ansible tasks.
- Router default routed policy is deny; explicitly permit required traffic. Preserve DHCP and DNS behavior.
- The shared transit/project route in OPNsense currently routes `10.P.0.0/16` via the project router. This aggregate is enough for return routing to Team 0, assuming all project subnets are reachable behind the project router; do not add redundant per-team OPNsense routes without a clear need.
- The route helper is intentionally cautious: it refuses to overwrite unmanaged routes.
- Current project 3 router outputs:
  - project: `10.60.2.32`, CT 122;
  - team 1: `10.3.0.2`, CT 123, LAN `10.3.1.0/24`;
  - team 2: `10.3.0.3`, CT 124, LAN `10.3.2.0/24`.

## Immediate implementation task: Team 0 staging

Implement and test Team 0 for Project 3 before generalizing into the clean project template.

1. Review current Terraform in `terraform/environments/project-003/main.tf` and verify its state/inputs.
2. Add a staging VNet `p003t254` to the project SDN resources.
3. Add a dedicated Team 0 staging router module:
   - transit VNet: `p003tr`;
   - transit address for Project 3: `10.3.0.4/24`;
   - transit gateway: `10.3.0.1`;
   - downstream VNet: `p003t254`;
   - downstream address: `10.3.254.1/24`;
   - tags include `p003` and `p003t254`;
   - Debian 13 Trixie template and current router module.
4. Ensure the project router can route `10.3.254.0/24` via `10.3.0.4`:
   - add this route to Terraform's `routers` output or another stable desired-state output;
   - teach inventory generation to include it in the project router's `router_routes`;
   - run `ansible/router-routes.yml` against project routers to persist/apply it.
5. Extend generated inventory to include the staging router in a dedicated `staging_routers` group or equivalent, with SSH proxied through the project router and `router_forward_source=10.3.254.0/24`.
6. Ensure router policy allows staging LAN internet/DNS as intended while denying access to project/team/internal networks. Do not accidentally treat the `10.3.254.0/24` staging LAN as unrestricted project transit.
7. Confirm the OPNsense aggregate route `10.3.0.0/16` via project router is installed and correct. No new per-staging OPNsense route should be needed.
8. Run `terraform fmt`, `terraform validate`, `terraform plan`, inventory generation, Ansible syntax checks, and inspect the plan before apply.
9. After deployment, verify: router address/routes, firewall policy, DNS, Internet egress, isolation from other teams, controller/Guacamole management access, and persistent routing after router reboot.
10. Once Project 3 Team 0 is verified, port the generic implementation into `project-template`.

Do not proceed to the GUI until the CLI workflow can create a staging router, stage scenario definitions, capture/version releases, test, and deploy to chosen teams reproducibly.
