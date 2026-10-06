# VM release capture worker

`python scripts/crange.py release-capture 3 VERSION` now captures actual staging VM contents, verifies a fresh isolated source clone, and automatically publishes the immutable release image into the project-scoped distribution catalog. Subsequent `stage-changes 3` prepares approved destination caches for test/rollout desired state. Containers are deliberately gated until their complete capture path is accepted.

Administrator prerequisites in the protected `distribution-policy.json`:

- `enabled: true` and source node enrolled in `allowed_nodes`.
- `capture_isolated_bridge`: an existing bridge also listed in `isolated_bridges`.
- `capture_datastore`: approved image storage listed in `approved_storage` for the source node and `allowed_storages`.
- `capture_backup_storage`: backup storage listed in `approved_backup_storage` for the source node, marked shared in Proxmox, with adequate capacity.
- Existing dedicated `crange-probes` pool and controller API permissions for that pool.
- An approved, tested VM hardware profile with `agent_enabled: true`; staging must actually have QGA enabled and installed. The worker does not install software or override profiles. Systemd must report `running`, and machine-id plus Ed25519 SSH host key must exist.

The worker reserves capacity before the full staging clone. It assigns pool ownership and exact image/job tags, disconnects all inherited NICs on the approved bridge before first boot, reads identity through fixed QGA commands while running, then shuts down and converts to a template. The template fingerprint must match the identity record. Export uses fixed vzdump operations into central backup storage and hashes the resulting complete archive. Source verification has its own reservation and uses the existing isolated worker with reconciled deletion. Release publication binds both protected journals and the reconciled probe reservation; legacy aliases are never assigned manufactured evidence.

Capture receipts live in `/var/lib/crange-distribution/capture-journal`, and protected identity/export/probe journals live in `source-journal`. Failed or uncertain operations retain receipts, artifacts, and reservations for review. Do not blindly rerun or delete an uncertain guest: confirm provider tasks, exact ownership, and capacity before reconciliation. Source-probe failures use the existing `source-probe-resume` / `source-probe-reconcile` workflow. Successful template capture reconciles capacity only after terminal provider tasks and complete discovery observe the template, so future reservations account for its actual allocation.

If automatic publication fails after the manifest is written, its references contain `distribution_evidence.export_job` and `probe_job`. After resolving the error, rerun the fixed publication operation:

```
python scripts/crange.py release-publish 3 VERSION ALIAS --export-job EXPORT_JOB --probe-job PROBE_JOB
```

Inspect `release-publish --help` for the current argument form. Publication rereads source configuration, verifies stopped/template state and archive checksum, and validates protected journals; editing manifest evidence is not a recovery mechanism.
