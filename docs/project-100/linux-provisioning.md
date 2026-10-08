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
role requires `mhl_ci_synthetic_token`. Supply these through an operator-owned,
project-protected Ansible vars file outside Git, for example:

```sh
ansible-playbook -i ansible/inventory/generated/project-100-mhl.json \
  ansible/linux/project-100-business.yml \
  --extra-vars @/home/crange/.config/crange/project-100-mhl-secrets.yml
```

Create that file only on the controller with restrictive permissions (mode
`0600`), use unique synthetic values that meet each role's declared format
checks, and keep it in the approved secrets store. Do not put secret values in
inventory, command-line arguments, source control, shell history, or logs. The
database and application tasks suppress secret-bearing output; the build token
is also installed into a protected root-owned artifact file.

The backup role has no secret input. The database and application roles are
coupled by their network-specific `.30` and `.40` addresses, so deploy the full
selected set only after those guests and their router/DNS path have passed the
Project 100 network acceptance checks. Ansible syntax validation is safe before
deployment; applying the playbook to absent or unaccepted guests is not.
