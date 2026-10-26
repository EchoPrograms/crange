# Deploy, upgrade and roll back teams

Open **Scenario → Team deployments**. Each row distinguishes the **selected** release, the **deployed** release and recorded service checks. Selection is desired state; it is not proof of deployment.

## Deploy a passing version

1. Choose **Choose a release** and select a ready version from the menu. Entries explain missing tests or pending cleanup.
2. In **Team numbers**, leave the field blank for all declared regular teams, or enter a comma-separated subset. Review the target list; staging is separate.
3. Review the rollout using the page's **Review rollout** action. Inspect resource actions, target teams, immutable images, node/storage decisions and resource headroom.
4. **Apply reviewed rollout** using the saved plan. Authorize destruction/replacement only when those exact actions are intended.
5. Follow the job through infrastructure, platform configuration and access publication. Read the team rows and perform the required health/manual checks.

![Team rollout status and version selection](/img/screenshots/deployments.png)

The current UI still separates version selection, review and apply. The goal is a complete intuitive workflow, but selecting a release by itself does not run every stage.

## Resume a failed configuration phase

If Terraform created the guests successfully and a later configuration phase failed, use **Continue configuration** for the affected team after fixing the cause. This resumes configuration against the deployed release; it should not create all of the guests again.

Read the phase before retrying. A failed job can leave successfully created machines. **Rerun** submits a new job with the same typed options; it cannot make an expired/consumed plan valid. A different placement needs a fresh review.

## Upgrade and rollback

Use the team's **Upgrade** or **Roll back** action and choose the intended version and expected source version. Review and apply the resulting plan. Version guards prevent silently upgrading a team whose selected/deployed version changed since your review.

Rollback rebuilds from a retained immutable version. It does not reverse arbitrary writes inside the current guests. Guest-local application data and participant changes can be lost on replacement. Persistent data separation is not a general completed workflow; preserve needed data explicitly before rebuilding.

Per-team overrides are bounded to supported resources/address/placement/power fields. They are recorded with rollout state and do not mutate the release. Older bases must remain while any clones/snapshots depend on them.

## Add or remove teams

Use **Teams** for declarations. Adding a team requires its reviewed router/network foundation before a release can run there. Removing a team currently edits desired state and does not complete every infrastructure/access retirement step automatically. Remove dependent machines, inspect the exact removal review, apply it and reconcile routes/access. An administrator may be needed for retained/offline resources. Do not treat a missing team row as proof its guests are gone.
