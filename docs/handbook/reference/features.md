# Feature map and implementation limits

This maps the current interface and fixed workflows. Availability still depends on role, approved images, installed revision and accepted infrastructure scope.

| Capability | Entry point | Important behavior |
| --- | --- | --- |
| Projects and quotas | Projects / Project Overview | Separate state; administrator quota editing with revision checks |
| Teams and staging | Teams / Scenario Workspace | Team 0 uses network 254; team removal still requires complete reviewed retirement |
| VM/container authoring | Workspace → Add VM/container | Approved images, static addressing, resources, protocols and checks |
| Scenario VM disk sizing | Workspace → Add/Edit VM | Request 8–1024 GiB when verified image metadata is available; fresh clones respect the image OS-disk minimum, and supported guest filesystems grow after reviewed increases |
| Infrastructure change visibility | Project Overview / Scenario | Reviewed infrastructure plan and task status on Overview; Scenario rows mark saved or planned machine changes |
| Custom networks | Workspace / owning team | Separate foundation, verification and guest admission may be needed |
| Staging review/apply | Workspace | Cache preparation plus exact retained plan; separate reviewed deployment |
| Capture/versioning | Capture release / Releases | Immutable manifests/images; source stop/restart may be required |
| Reduced captured disks | Supported Project 100 Windows profiles | Isolated copy, original preserved; not a general live shrink feature |
| Fresh-copy testing | Releases | Deploy copies, run defined startup/check scope, record result |
| Test cleanup | Release action | Scoped review/apply in one job; receipt-based finish/recovery |
| Team rollout | Team deployments | Choose exact passing version; blank team list means all regular teams; review/apply remain explicit |
| Upgrade/rollback | Team/release actions | Version guard and reviewed rebuild; guest-local data not automatically retained |
| Configuration resume | Team deployments | Reuses applied guests; current team configuration/check loop is serial |
| Power/group power | Machine tables/details | Exact observed targets; graceful and force stop differ |
| Native snapshots | Machine detail / More actions | Restore requires stopped guest; not an independent backup |
| Health checks | Machine forms/details, staging/test/team actions | Manager-defined ping/TCP/HTTP including optional HTTP Host |
| Remote access | Connect / Guacamole | Explicit grants, SSH/RDP/VNC, private launch broker |
| Project accounts | Project users | Independent account queue, home team, explicit machine grants, bulk lock/unlock/delete |
| Dashboard accounts | Dashboard users | Administrator-managed project roles, participant team access |
| Linux guest accounts | Supported machine Users | Fixed audited account/sudo/password operations; separate from dashboard/Guac |
| Custom jump boxes/presets | Team jump-box options | Approved VM/CT, bounded resources/services, future-only preset changes |
| ISO template builds | Templates | Bounded upload/install/publish; manual guest preparation and acceptance |
| Image publication/distribution | Templates / Cluster | Immutable catalog, verified local replicas, fixed resume/GC workflows |
| Cluster discovery/capacity | Cluster | Sanitized snapshot, commitments, reservations, policy and eligibility blockers |
| Project queues/jobs | Contextual tasks / Jobs | Independent project lanes, bounded output, cancel/rerun and phase-aware recovery |
| Network verification | Network action / fixed worker | Owned disposable probe, fixed checks, cleanup before evidence |
| Project lifecycle | Project actions | Reviewed delete/restore/purge, deferred offline ownership where required |
| Redundancy/router recovery | Administrator workflows | Modeled quorum and guarded recovery; no general scenario-guest automatic failover |
| Audit | Jobs/account/project audit records | Actor and operation history; not a full unbounded terminal archive |
| Local documentation | This handbook | Docusaurus static build, screenshots and local search |

## Limits to account for

Ordinary management should be intuitive and finish its controlled automation, but some workflows still expose separate review/apply/foundation steps. Team retirement is not yet a single completed ordinary UI workflow. Parallel post-apply team configuration is not yet implemented.

Manager-supplied application scripts should run in the lab under an explicit owner; there is no general arbitrary-controller-shell GUI. Legacy Project 100 application recipes are still invoked by its release configuration. General persistent data disks and universal node-loss recovery are not complete guarantees.

Older Markdown guides and incident evidence can describe earlier restrictions or UI labels. Use this handbook plus the installed source/help and current protected policy for operations.
