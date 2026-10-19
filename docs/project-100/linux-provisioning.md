# Project 100 Linux business provisioning

The MHL Linux roles configure the four staging service containers declared as
`t254-mhl-db01`, `t254-mhl-app01`, `t254-mhl-ci01`, and `t254-mhl-bkp01`.
They are not provisioned until their guests exist and appear in the canonical
generated inventory. Do not use `ansible/linux/project-100-inventory.example.yml`
as a runtime inventory; it describes illustrative team placements rather than
deployed guests.

After deployment, generate the canonical inventory through the normal project
workflow, then select only Team 0 staging:

```sh
python3 scripts/generate-mhl-inventory.py \
  --input ansible/inventory/generated/project-100.json \
  --output ansible/inventory/generated/project-100-mhl.json
```

The adapter defaults to network 254 and refuses to write output unless the
canonical inventory includes all four expected Project 100 container guests
under `staging_containers`, with their assigned `.30`, `.40`, `.50`, and `.60`
addresses. The canonical generator prefixes Terraform keys with
`p100_ct_`, so the staging identities are `p100_ct_t254-mhl-db01` and the
corresponding `app01`, `ci01`, and `bkp01` keys. For a regular team, the adapter
expects release-rollout keys shaped as
`<release-prefix>-<captured-name>-tNNN` in that team's canonical scope. It copies
canonical `all`, container-group, scope, and host SSH settings, including the
SSH user, key path, and any nested proxy options. A regular team is deliberately
opt-in; select one explicitly with `--network-id 1` through `--network-id 5`.
Only deploy after reviewing the resulting four-host inventory and confirming
that the selected guests are reachable on the accepted project network.

The database and application roles require `mhl_db_runtime_password`; the build
role requires `mhl_ci_synthetic_token`. The Project 100 team page now exposes
**Configure MHL business services** for the selected staging/team network. Its
fixed worker generates stable per-network values in
`~/.config/crange/secrets/project-100/mhl-business-network-N.yml` with mode `0600`
and runs only `ansible/linux/project-100-business.yml`. The UI accepts only a
network ID inferred from the selected Team page; it cannot supply playbook,
inventory, Ansible arguments, or secrets. A regular team is unchanged until
that team is explicitly selected and configured. Team targets must also be
applied release containers; hand-declared team services and release-test
clones are refused.

The equivalent operator path remains:

```sh
ansible-playbook -i ansible/inventory/generated/project-100-mhl.json \
  ansible/linux/project-100-business.yml \
  --extra-vars @/home/crange/.config/crange/project-100-mhl-secrets.yml
```

Keep any manually provisioned vars file only on the controller with restrictive
permissions (mode `0600`). Do not put secret values in inventory, command-line
arguments, source control, shell history, or logs. The
database and application tasks suppress secret-bearing output; the build token
is also installed into a protected root-owned artifact file.

The fixed job refuses to run unless canonical applied inventory identifies all
four expected guests in the selected network and their guest IDs, image IDs,
address, declared template, and worker SSH identity match
`crange-applied-placements.json`. If the scenario has not been placed yet, the
job reports that the reviewed scenario plan must be applied first; it does not
probe or contact undeclared guests. Release capture records the configured
staging service profile metadata without copying any secrets into the release.

The service-role job and API/GUI controls are deployed. All four staging
containers exist and the fixed staging business workflow has run successfully.
Seven declared Linux service checks passed. APP template CT134/P2 and backup
template CT135/P2 now passed independent linked-clone verification for pending
release 0.1.0. CI/database capture, complete fresh-release replay, and production
rollout acceptance remain pending. See [STATUS.md](STATUS.md) for current jobs.

The backup role has no secret input. The database and application roles are
coupled by their network-specific `.30` and `.40` addresses, so deploy the full
selected set only after those guests and their router/DNS path have passed the
Project 100 network acceptance checks. Ansible syntax validation is safe before
deployment; applying the playbook to absent or unaccepted guests is not.

## Captured-container acceptance — October 12

APP CT125 was captured through GUI job 6ac926cf6f76428ca2663ed9d759df35 to
immutable template CT130/P2 and project-scoped image p100-image-2f571d3a89d3.
The central archive checksum is verified. Independent linked clone CT20000
passed identity/readiness and physical origin checks, then was deleted through
confirmed tasks. Identity preparation clears only the isolated capture clone's
machine-ID files before sealing; the staging source's identities are preserved.
The partial release 0.0.1 is an image acceptance artifact, not the complete lab
release. Cross-node restore and complete scenario testing remain required.
