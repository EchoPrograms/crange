# Back up and recover the platform

Source control alone is not a lab backup. Rebuilding requires project inputs/state, immutable images, release/access records, database state and protected credentials/keys appropriate to the installation.

## What to preserve

| Material | Why |
| --- | --- |
| Reviewed source revision/provider lock | Reproduce the automation behavior |
| Each real project environment and Terraform state | Map declared ownership to native resources |
| Release manifests, checks, journals and immutable artifacts | Recreate accepted versions and recover incomplete work |
| Authentication/session/audit DB | Dashboard accounts, roles and audit |
| Job DB and protected operation journals | Running/unfinished task identity and history |
| Distribution catalog/policy/identity/reservations | Verified images, admission and outstanding allocations |
| Guacamole desired/applied state and connector configuration | Rebuild authorized remote access |
| Encryption/broker keys and service secrets | Decrypt retained credentials and restore integrations |

Keep independent backups for the library. Do not back up its data volume to its own NFS export. A native snapshot or `backup=1` flag is not proof of an independent, restorable backup.

## Consistent database copies

Use SQLite's online backup facility or stop writers for a consistent copy. Copying only a live main `.sqlite3` file can miss WAL transactions. Coordinate related state, declarations, journals and image backups with quiescent worker actions. Preserve ownership/modes and protect secret backups independently.

Do not restore an old queue/state over running workers or active native Proxmox tasks. First establish which jobs/tasks/resources exist. A stale restore can otherwise replay a mutation or lose the evidence identifying its owned allocation.

## Recover by phase

For a timed-out native restore, inspect the exact UPID and retained journal. Wait for completion and use its supported resume, rather than issuing another restore. For applied guests with failed setup, resume selected configuration. For pending test cleanup, resume the cleanup receipt. For unknown writes, keep reservations/resources until reconciled.

After any state restore, compare immutable image identities and actual guest ownership before a fresh reviewed plan. An expected no-op is a useful reconciliation result; unexpected replacement/destruction is a reason to investigate.

## Test a restore

Restore backups into an isolated environment, verify DB readability and key availability, validate manifests/archive identities, inspect a plan without mutating production and create a disposable clone with scoped checks/cleanup. Record the source backup time, restored revision and acceptance scope. Do not infer a tested disaster-recovery path from successful backup creation alone.
