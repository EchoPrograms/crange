# Desired state, reviews and immutable releases

CRANGE separates authoring, acceptance and rollout. A running guest, a definition, a saved plan and an image archive each answer a different question.

## State transitions

| Stage | Input | Output / boundary |
| --- | --- | --- |
| Declare | Manager form | Desired machine/network settings; no general immediate provisioning |
| Review | Declarations, current state, policy, approved images | Exact saved plan and placement/reservation evidence; eligible caches may be prepared |
| Apply | Fresh reviewed plan and explicit permissions | Native resource changes within the reviewed scope |
| Configure | Applied guest/router inventory | Common network/access baseline and supported fixed workflows |
| Capture | Prepared staging machines | New immutable release manifest and image identities |
| Test | Fresh copies of that release | Scoped recorded checks/startup-order results |
| Cleanup | Owned disposable test resources | Reviewed/applied removal; originals retained |
| Rollout | Accepted version and selected teams | New desired/applied rollout history and explicit team replacement where reviewed |

## Saved reviews

A retained plan has an identity and expires after one hour. Inputs/state and relevant source/policy identity must still match. An apply cannot silently change placement when a node disappears; a different target requires a fresh review.

Destruction and replacement are separately gated. Their flags permit only reviewed actions; they do not waive quotas, image ownership, freshness or scope checks. Unexpected changes to other teams/shared resources stop a bounded workflow.

## Release identity

A release refers to immutable captured artifacts, not a mutable staging guest. Later staging edits, base-image updates and health-check edits do not rewrite that version. Test and rollout records refer back to the release ID/version and artifact identity.

An upgrade or rollback makes a new explicit target selection. Guest-local writes after deployment are drift from the captured source and may disappear on rebuilding. Keeping persistent data separate is a design requirement for workloads that need it, not a universal implemented guarantee.

## Recovery without repeating everything

Native task identity, protected journals, reservations and applied state allow supported resumes to continue an existing operation. A failed wrapper can leave the native restore running; a failed configuration can leave all VMs created. Inspect the boundary that failed rather than starting the lifecycle again.

An ambiguous write is not safely treated as absence. Journals and reservations remain until ownership and task outcome are reconciled. A failed cleanup is not permission to delete any guest with a similar name.
