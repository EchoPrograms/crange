# Glossary

| Term | Meaning |
| --- | --- |
| Applied state | Terraform/tracking record of what a reviewed deployment produced |
| Base image | Administrator-maintained approved OS template |
| Cache | Node-local verified immutable replica used to create guests |
| Capture | Save prepared staging guests as immutable release artifacts |
| Check | A defined observation, with a scope and result; not proof of every service |
| Desired state | Saved declarations of intended infrastructure/settings |
| Foundation | Routers/VNets/routes needed before guest deployment |
| Fresh-copy test | Instantiate a release into new disposable guests and run its defined checks/orders |
| Guacamole | Browser remote-access system for authorized SSH/RDP/VNC sessions |
| Journal | Protected durable record of operation ownership, native tasks and recovery phase |
| Jump box | A guest used as a participant's remote entry point |
| Lane | Worker execution ordering for a project or bounded operation family |
| Library | Central immutable artifact/archive store |
| Linked clone | Guest sharing an immutable local base; retain its dependencies |
| Manifest | Release's recorded identities, resources, network and check definitions |
| Observed state | Recent runtime/native information, distinct from desired settings |
| Plan | Exact reviewed Terraform proposal bound to inputs/state and validity limits |
| Project | Isolated range ownership/scope with independent Terraform state |
| Probe | Disposable, exactly owned guest used for fixed verification |
| Quota | Declared resource ceiling for one project; separate from physical headroom |
| Recipe | Fixed versioned automation definition; some legacy recipes configure applications |
| Reconciliation | Establish actual ownership/task/resource outcome after uncertainty |
| Release | Immutable versioned scenario manifest and captured images |
| Reservation | Shared capacity commitment retained while a workflow owns resources |
| Rollback | Explicitly rebuild/select a retained older release, not undo arbitrary guest writes |
| Staging / Team 0 | Manager authoring environment using network ID 254 |
| Team | Isolated participant environment, normally numbered 1–253 |
| UPID | Proxmox native task identifier used to observe the same submitted operation |
| VNet | Proxmox SDN virtual network |
