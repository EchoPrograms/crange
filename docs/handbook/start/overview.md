# What you need to know

A **project** owns isolated networks, teams, scenario definitions, access permissions and a separate Terraform state. A project manager works only within authorized projects. An administrator maintains the platform and approved images. A participant opens assigned machines without receiving infrastructure credentials.

## The five-step workflow

1. **Build staging.** Add VMs or containers in **Scenario → Workspace**, review the changes and deploy them to Team 0.
2. **Prepare the exercise.** Connect to the guests, install/configure applications, and define useful health checks.
3. **Capture a release.** Save an immutable version containing the machine definitions and captured images.
4. **Test fresh copies.** Deploy disposable copies, check the saved startup orders and manager-defined checks, then clean them up.
5. **Deploy to teams.** Choose the passing release and target teams, review the rollout, then apply it.

Staging edits do not silently modify a deployed release. A release captures a point in time; changing a staging machine later requires a new release.

## Three states you will see

| State | Meaning | Example |
| --- | --- | --- |
| Desired | What you saved in CRANGE | A VM should have 4 GiB RAM |
| Reviewed | The exact saved deployment proposal | Terraform plans to create that VM on a specific node/storage |
| Observed/applied | What was deployed or recently read from Proxmox | The guest is running with its recorded deployment identity |

Saving a form does not necessarily change a running guest. Follow the page's **Review changes** and deployment action. Runtime power observations are separate from saved power intent.

## Team 0 is staging

Regular teams are numbered **1–253**. Team 0 is displayed as **Staging** and uses network number **254**. For Project 100, staging is `10.100.254.0/24`, and Team 2 is `10.100.2.0/24`. Their gateways end in `.1`.

A scenario can use the same host octet in each team's isolated LAN. For example, a server at `.20` becomes `10.100.254.20` in staging and `10.100.2.20` for Team 2. Custom networks have their own allocations; see [networking](../concepts/networking.md).

## What is required before you begin?

An administrator must provide an active project, an enabled manager account, sufficient quotas, approved VM/container images, verified network placement and a working remote-access connector. The controller is built for an existing Proxmox/OPNsense environment; creating a project does not install those products.

Your browser needs access to Control and, for remote sessions, Guacamole. The documentation itself can run entirely on your local machine.
