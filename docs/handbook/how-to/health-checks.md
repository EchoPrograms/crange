# Define and run health checks

Health checks describe what **you**, the lab manager, want to verify. Infrastructure deployment establishes resources and network/access paths; it does not establish that an exercise is functionally correct.

## Choose a useful check

| Type | Configure | What a pass establishes |
| --- | --- | --- |
| Ping | Type and timeout | The scoped guest responds to ICMP |
| TCP | Port and timeout | A connection to that guest/port succeeds |
| HTTP | Port, path, expected status, optional Host and timeout | That request returns the expected HTTP status |

An empty custom list retains the default ping behavior. A port accepting a connection does not prove login, data correctness or an application's full dependency chain. Combine checks with manual exercise validation when needed.

In the add/edit form or the machine detail's health editor, choose the type and enter its parameters. Save changes updates only that machine's declared checks; it does not automatically run them or deploy infrastructure.

![Editing a machine's declared health checks](/img/screenshots/health-checks.png)

## HTTP virtual hosts

The optional **Host** field selects a virtual host while the worker still connects to the approved machine IP. For example, choose `portal.lab.example`, path `/health`, port 80, status 200. The field does not permit checking an arbitrary external target.

Checks run from controlled worker/router paths rather than your browser. A success demonstrates that path, not every participant's session or every external vantage point.

## Run checks explicitly

Use **Run health checks** in staging, **Validate** for a machine, **Check test** for deployed fresh release copies, or **Check services** for a deployed team. Follow the queued task to its terminal result. A saved check definition alone is not a passed result.

Health edits are revision-guarded. If another manager edits the same list, reload the current definitions and resolve your draft. Captured release checks stay immutable; capture a new version to include later changes.

## Application ownership

Configure databases, application connection strings, exercise accounts and seed data in the guest or with a manager-maintained guest script. CRANGE currently includes legacy Project 100 service profiles that still perform some application configuration and checks automatically. That is a documented exception, not the recommended contract for new labs; see [application ownership](../concepts/application-boundary.md).
