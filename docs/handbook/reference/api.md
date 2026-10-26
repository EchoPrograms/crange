# API and integration guide

Control's API shares the dashboard origin. Browser calls authenticate with a server-side session; mutating JSON requests require the session's **X-CSRF-Token**. Infrastructure tokens never belong in browser requests.

Use the dashboard forms for daily operations. This reference is for maintainers integrating controlled workflows; it does not grant authority beyond the actor's current role/project scope.

## Submit and observe a job

A normal JSON request to `POST /api/jobs` contains only the supported project, operation and typed options:

```json
{"project": 3, "operation": "stage-changes", "options": {}}
```

An accepted submission returns HTTP 202 and `job_id`. Read the authorized Jobs endpoint or exact job result to follow status/output. Acceptance means queued, not completed. The worker validates authorization/scope again on execution.

Cancellation uses the supported action/job identity payload. A cancelled wrapper may leave successful native work; read the outcome before a new submission. Rerun is a new audited operation using stored typed options under current authorization.

## Main routes

| Route family | Purpose |
| --- | --- |
| `/api/session`, `/api/login`, `/api/logout` | Dashboard session lifecycle |
| `/api/projects`, `/api/project-status` | Authorized declarations and runtime view |
| `/api/jobs` and job detail routes | Submit/observe/cancel/rerun supported jobs |
| `/api/users` | Administrator dashboard accounts/roles |
| `/api/guac-users`, `/api/guac-user-stats` | Project accounts, grants and activity |
| `/api/project-settings` | Validated project defaults |
| `/api/connector`, `/api/guac-ticket` | Connector status and authorized launch |
| `/api/box-remote-access`, `/api/box-users` | Fixed guest access/account operations |
| `/api/custom-jumpboxes` | Bounded project custom boxes/presets |
| `/api/templates`, `/api/template-media` | Administrator templates/media |
| `/api/cluster` | Administrator sanitized capacity/image snapshot |
| `/api/audit` | Authorized audit records |

The [route inventory](../generated/api-routes.md) is extracted from server source. Check handler methods for exact payloads and permissions. `/api/guac-bridge` has separate server-to-server authentication; browser session/CSRF is not its authorization mechanism.

## Fixed operation schemas

[Queued operations](../generated/operations.md) lists the allowlisted names and validator keys. A job name need not match its CLI command: `plan` maps to retained `infra-plan`, for example. Unknown option keys, arbitrary executable fields and raw Terraform are not a supported interface.

Secret-bearing fields are protected broker integration values. Use the UI's sealed credential flow; do not put plaintext passwords in job options, shell arguments, links or audit records. Quotas, project lifecycle, ownership, freshness and approved targets apply regardless of caller.

No API stability/version compatibility promise is made here. Verify the installed source and fixed schemas when writing an integration, and retain an action's returned job ID rather than repeatedly submitting it to test progress.
