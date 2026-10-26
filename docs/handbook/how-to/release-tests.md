# Test a release from fresh copies

A passing staging machine does not prove that its image will restore into a clean team. Fresh-copy tests create independent guests from the immutable release and run the configured checks and startup orders.

## Prepare and deploy the copies

1. In **Releases**, choose **Test release** on the exact version.
2. Follow **Workspace** to review and deploy its disposable resources. Current tests use an owned test network; do not assume the old `.90–.100` staging-address method.
3. If review first deploys a network/router foundation, complete **Verify network**, then review/deploy the test machines.
4. Wait for the release row to show every expected machine deployed with matching release ownership and addresses.

**Awaiting deployment** and `0 / 6`, for example, refer to observed/applied test copies. Preparing definitions alone is insufficient. **Check test** is disabled until the complete expected set is ready.

## Run and read the result

Choose **Check test**. Its queued task output opens immediately. **View test progress** reopens that task without submitting another. The check can start/stop disposable guests in the release's saved orders and run defined health checks. An earlier pass is displayed separately from the current active test.

![Release status, including fresh-copy results](/img/screenshots/releases.png)

A failing check does not authorize rollout. Read the actual error: connectivity, guest readiness, a manager-defined endpoint and a scenario-specific legacy profile are different causes. Change the source and capture a new version if the immutable image itself is wrong. A transient environmental failure may allow a retry of the current version once fixed.

## Clean up

Choose **Clean up test** after checking. The current cleanup job removes test declarations, reviews the exact staging deletion plan and applies it automatically. It permits only that release's disposable machines/networks. Unrelated pending changes stop cleanup for review.

Wait for actual removal. **Cleanup pending** means a recorded deletion workflow still has resources to remove or reconcile. **Finish cleanup** resumes its receipt. It must not recreate the test set or delete original staging machines.

A version becomes available for team rollout after a recorded passing fresh-copy test and completed cleanup. The release chooser explains unavailable versions. If cleanup has finished but the page is stale, Refresh and inspect the cleanup task; persistent pending state needs reconciliation, not another capture.

## How much testing is enough?

Use checks that match the exercise's purpose: basic reachability, required ports/endpoints, important startup order and a manager's manual functional check. Repeating equivalent infrastructure tests does not establish more application coverage. Preserve a passing test record and its scope; it is evidence for that version and environment, not a guarantee of every service or future deployment.
