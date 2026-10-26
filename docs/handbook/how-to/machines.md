# Operate machines and snapshots

Open **Machines** to search by name, address or image, then filter by team/type. Click a row to open one machine's management page. It combines declared resources, observed power/node, network, user grants, health checks, snapshots and recent tasks.

![Machine management and health check controls](/img/screenshots/machine-detail.png)

## Connect and power

**Connect** launches an authorized Guacamole connection. **Start/Shutdown** uses the current observed state. The More actions menu includes restart and force stop. Graceful shutdown requests guest cooperation; force stop removes power and can lose pending writes.

For group actions, select visible machines and choose start, shutdown or restart. Confirmation lists exact targets and skipped states. Unknown/unverified runtime states cannot be operated on from those group controls. A failed response may mean observation failed after submission; inspect Jobs before submitting again.

Closing Control or its task dialog does not stop the worker. Guest startup and service readiness can take longer than a successful power API request.

## Native snapshots

Expand **Snapshots** on a machine. Create a named snapshot or restore/delete an existing exact name. Restore requires an observed **stopped** guest, checked both in the browser and controller. Restore changes that machine's disk state; it is not a project release rollback.

Snapshots are local native resources and can depend on their node/storage. They are not an independent backup. A snapshot list/observation error should retry the existing observation, not issue another restore. Record the task ID if the native result is uncertain.

## Resize and remove

Edit resource declarations, review and apply. Quotas and placement headroom still apply to increases; a stop/start may be required to expose hardware changes. Increasing a virtual disk and growing its guest filesystem are different operations. General live disk shrinking is not supported by changing a number in a machine form.

Removing a machine declaration requires a reviewed deletion to remove the actual guest. Deletion affects all users assigned to that machine. Deleting a project user retains that user's machines unless you explicitly remove them.

## Linux guest accounts

Where supported, the machine **Users** action manages accounts inside a running Linux guest: create, password, grant/revoke sudo, delete. Dashboard users, Guacamole users and guest accounts are distinct.

Root, bootstrap and service accounts are protected. Deleting a guest user removes its home and ends its processes; revoking sudo does not terminate an existing root session. Operations are audited fixed jobs against one validated guest. Password input uses the protected broker rather than plaintext job arguments. Capture a new release to retain these changes in a scenario image.
