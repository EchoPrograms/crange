# Follow jobs and recover failures

Web actions submit fixed, typed jobs. The worker rechecks authorization and scope when starting them. Jobs persist in SQLite and continue after you close a dialog or sign out.

## Read status and output

| State | Meaning |
| --- | --- |
| Queued | Accepted; waiting for its execution lane or prerequisites |
| Running | Worker owns the job; read the current phase/output |
| Waiting for node/resources | Shared provisioning capacity/locks are not yet available |
| Succeeded | The submitted workflow completed; check its stated scope |
| Failed | A stage returned an error; earlier changes can still have completed |
| Interrupted | The process stopped before a confirmed terminal workflow result |
| Reconcile required | Ownership/native cleanup is uncertain and must be resolved |

![Task history and expandable output](/img/screenshots/jobs.png)

Open the contextual task or **Jobs** for the actor, runtime and recent output. Runtime excludes queue wait where execution timestamps are available; older records are approximate. The worker retains the most recent **50,000 characters**, not an unlimited terminal transcript. Tool output can still be buffered.

A submitted job's success means what that workflow says. For example, a desired-state edit can succeed before any deployment, and Terraform creation can succeed before a later guest configuration fails.

## Long-running work

Large captures/restores on slow or congested storage can run for hours. Read its phase and native task ID. A controller-side timeout is not proof the Proxmox copy stopped. Wait for the existing native task or reconcile it before resubmitting a write.

Monitoring should wait on an existing job/task with bounded script polling and terminal notification, rather than repeatedly submitting work or asking for a new plan. Do not restart a service or guest simply because output is quiet.

Projects have independent execution lanes; same-project infrastructure actions stay ordered. Per-node locks, the capacity ledger, SDN and route locks can still serialize shared work. Account reconciliation has its own lane, as do exact-guest power controls. Parallelism is bounded by resources and ownership, not just the number of buttons clicked.

## Stop and rerun

Cancel/Stop is operation-specific. Queued work can be cancelled; a running command can be terminated, leaving completed infrastructure changes. Network verification uses cooperative cancellation so it can clean up its owned probe. Force Stop is a separate guest power operation.

**Rerun** creates a new audited submission from stored typed options under your current authorization. It does not reset infrastructure or revive a stale saved plan. After a failed apply, determine whether the failure was creation, configuration, checks or publication; resume the failed phase where supported.

Give an administrator the project, release/team, job ID, native UPID if present, last meaningful error and any completed phases. Do not include passwords, tokens or raw credential files.
