# Troubleshooting by symptom

Start with the relevant task's **phase**, **ID**, **last meaningful error** and **observed resources**. A failed wrapper can leave completed infrastructure or a running native task. Avoid repeating a write until its outcome is known.

| Symptom | Check first | Next step |
| --- | --- | --- |
| Cannot sign in / project missing | Enabled dashboard account, role, project membership, HTTPS/session behavior | Administrator reviews Dashboard users and installed service/reverse proxy |
| No approved template / automatic placement unavailable | Image project approval, ready replica, scoped node/network/provider evidence | Administrator prepares/publishes the exact missing scope |
| Review says insufficient space | Eligible pool/node, configured commitments, reservations, cache/probe peak, old guests | Read placement blockers; retire only reviewed unused resources or change approved capacity |
| Plan is stale/expired | One-hour lifetime, changed inputs/state/policy/source | Review again; do not bypass plan identity |
| Staging says 64 GiB, release should be 30/31 | Which object is displayed | Inspect source disk vs captured artifact/clone separately |
| Check test disabled | Expected deployed count, matching release ownership/addresses | Deploy remaining test copies; complete network foundation/verification if needed |
| Check test appears to do nothing | Existing queued/running task or task dialog | Open View test progress / Jobs; do not queue duplicate checks |
| Cleanup pending remains | Native test guest/router/network removal and receipt | Refresh; use Finish cleanup for retained scoped receipt |
| Clean up test unavailable | Active check or no active/pending test | Finish/wait for check, then inspect exact release state |
| Release chooser version disabled | Passing fresh-copy record and completed cleanup | Complete that version's missing test/cleanup stages |
| All VMs exist but deployment failed | First post-apply configuration/check/publication error | Fix cause, then Continue configuration on affected team |
| Job quiet / 1800-second restore timeout | Native UPID and datastore I/O | Wait/reconcile existing task; timeout does not cancel Proxmox |
| Guest responds to ping but service fails | Listener, guest config, endpoint/Host/path, DNS/dependencies | Manager fixes application; rerun meaningful checks |
| Guacamole connection absent | Applied endpoint, explicit grant, selected protocols, publish job | Refresh connections and inspect account/connection reconciliation |
| Guacamole connects but login fails | Guest service/account, protocol, custom credentials | Fix guest or matching protected connection settings |
| Snapshot restore fails | Guest observed stopped, exact snapshot identity and prior task outcome | Stop guest, resolve native outcome, retry only appropriate phase |
| Team configuration happens one by one | Current serial post-apply configuration loop | No parallel-team setting exists yet; see capacity/parallelism |

## Long-running restore or capture

The native task's completion and the controller command's timeout are separate. The current HDD image-preparation path allows a longer bounded wait than normal SSD operations, but I/O can remain slow. Do not reboot or force-unlock a guest/host to clear a slow running copy.

Retain the exact job/template/probe/reservation IDs. Supported cache resume verifies archive identity, native task history, ownership and backing before reusing the partial work. If that evidence is uncertain, the workflow should retain it for administrator reconciliation.

## DNS and application failures

A machine can have correct infrastructure addresses and still contain staging-specific application references. Platform IP/gateway/DNS initialization is not arbitrary application-file rewriting. The lab manager should repair application configuration or their guest script and capture a new release when the image changes.

The legacy Project 100 configuration has a Windows/domain/DNS phase before Linux/business configuration. A failure in those recipes is not a reason to recreate successfully applied guests. These recipes remain a legacy exception to the intended platform/application boundary.

## Evidence to send an administrator

Include project/team/release, job ID, native UPID when present, error text, completed phases and recent observed states. Omit secrets, API tokens, plaintext passwords and raw credential files. Jobs retain a bounded output tail; protected journals/native task history may hold additional evidence needed for recovery.
