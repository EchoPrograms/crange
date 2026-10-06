# VM release capture worker

`python scripts/crange.py release-capture 3 VERSION` now captures actual staging VM contents, verifies a fresh isolated source clone, and automatically publishes the immutable release image into the project-scoped distribution catalog. Use repeated `--machine STAGING_KEY` options to capture an explicit subset; omitted means all staging scenario guests. Subsequent `stage-changes 3` prepares approved destination caches for test/rollout desired state. Containers are deliberately gated until their complete capture path is accepted.

Administrator prerequisites in the protected `distribution-policy.json`:

- `enabled: true` and source node enrolled in `allowed_nodes`.
- `capture_isolated_bridge`: an existing bridge also listed in `isolated_bridges`.
- `capture_datastore`: approved image storage listed in `approved_storage` for the source node and `allowed_storages`.
- `capture_backup_storage`: backup storage listed in `approved_backup_storage` for the source node, marked shared in Proxmox, with adequate capacity.
- Existing dedicated `crange-probes` pool and controller API permissions for that pool.
- An approved, tested VM hardware profile with `agent_enabled: true`; staging must actually have QGA enabled and installed. The worker does not install software or override profiles. The protected source catalog selects Systemd (`running`) or OpenRC (`default`) readiness checks; machine-id plus Ed25519 SSH host key must exist.

The worker reserves capacity before the full staging clone. It assigns pool ownership and exact image/job tags, disconnects all inherited NICs on the approved bridge before first boot, reads identity through fixed QGA commands while running, then shuts down and converts to a template. The template fingerprint must match the identity record. Export uses fixed vzdump operations into central backup storage and hashes the resulting complete archive. Source verification has its own reservation and uses the existing isolated worker with reconciled deletion. Release publication binds both protected journals and the reconciled probe reservation; legacy aliases are never assigned manufactured evidence.

Capture receipts live in `/var/lib/crange-distribution/capture-journal`, and protected identity/export/probe journals live in `source-journal`. Failed or uncertain operations retain receipts, artifacts, and reservations for review. Do not blindly rerun or delete an uncertain guest: confirm provider tasks, exact ownership, and capacity before reconciliation. Source-probe failures use the existing `source-probe-resume` / `source-probe-reconcile` workflow. Successful template capture reconciles capacity only after terminal provider tasks and complete discovery observe the template, so future reservations account for its actual allocation.

If automatic publication fails after the manifest is written, its references contain `distribution_evidence.export_job` and `probe_job`. After resolving the error, rerun the fixed publication operation:

```
python scripts/crange.py release-publish 3 VERSION ALIAS --export-job EXPORT_JOB --probe-job PROBE_JOB
```

Inspect `release-publish --help` for the current argument form. Publication rereads source configuration, verifies stopped/template state and archive checksum, and validates protected journals; editing manifest evidence is not a recovery mechanism.

## Reviewed Project 3 Alpine pilot prerequisites (2026-10-06)

Read-only controller inspection found only existing staging Ubuntu VM121 on P4 (`t254-web-01`, `.2`), and P4 is excluded from distribution policy. No staged Alpine scenario exists yet. The approved Alpine profile already enables QGA and disables upgrades; its protected catalog entry declares OpenRC. P1 already has `local-lvm` image and `crange-library` backup approvals and isolated bridge `crprobe1450`. The proposed protected policy merge is:

```json
{
  "capture_isolated_bridge": "crprobe1450",
  "capture_datastore": "local-lvm",
  "capture_backup_storage": "crange-library",
  "revision": "alpine-p3-release-capture-pilot-20261006"
}
```

After reviewing that delta and independently confirming staging `.50` is unused, the concrete desired-state sequence is:

```
python scripts/crange.py scenario-add 3 alpine-capture --team 0 --ip 50 --template alpine-3-24 --node proxmox1 --cores 1 --memory 512
python scripts/crange.py stage-changes 3
# Inspect the exact retained plan; expect only the intended new staging VM.
python scripts/crange.py infra-apply 3 --yes
python scripts/crange.py release-capture 3 alpine-capture-pilot --machine t254-alpine-capture
```

The explicit capture selection includes only the new Alpine guest, preserving the existing Ubuntu scenario definition. No policy or live VM was changed during this prerequisite review. For deployment, use normal release test/deploy desired-state commands, then `stage-changes 3` to prepare destination caches and retain each reviewed plan.

Publication is per image after the manifest is atomically written. A multi-image failure can leave a valid immutable manifest and some published catalog images; retry `release-publish` for unpublished references using the protected evidence. Capture does not rewrite older manifests or attach new provenance to legacy aliases. A capture interrupted before manifest creation retains protected receipts and reservations for manual reconciliation; there is currently no automatic capture-resume command.
