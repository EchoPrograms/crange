# OPNsense route API and project deployment

The OPNsense API key/secret are read from the controller environment. Do not commit them.

Create the OPNsense API user with only Routing: Settings and Routes: Routes privileges.
The RangeTransit interface ID is `opt2`; override Terraform variable `opnsense_transit_interface` if it changes.

After applying Terraform in one project environment:

```bash
cd ~/crange
set -a
. ~/.config/crange/opnsense-api.txt
set +a
export OPNSENSE_URL=https://10.60.2.1
# Temporary only if OPNsense certificate verification is not configured yet:
export OPNSENSE_INSECURE=1

terraform -chdir=terraform/environments/project-template output -json opnsense_route | \
  python3 scripts/opnsense-project-routes.py ensure
python3 scripts/generate-project-inventory.py \
  --terraform-dir terraform/environments/project-template \
  --output ansible/inventory/generated/project-$(terraform -chdir=terraform/environments/project-template output -raw project_id).json
ansible-inventory -i ansible/inventory/generated/project-$(terraform -chdir=terraform/environments/project-template output -raw project_id).json --graph
ansible-playbook -i ansible/inventory/generated/project-$(terraform -chdir=terraform/environments/project-template output -raw project_id).json ansible/routers.yml --limit project_routers -T 15
ansible-playbook -i ansible/inventory/generated/project-$(terraform -chdir=terraform/environments/project-template output -raw project_id).json ansible/routers.yml --limit team_routers -T 15
ansible-playbook -i ansible/inventory/generated/project-$(terraform -chdir=terraform/environments/project-template output -raw project_id).json ansible/jumpboxes.yml -T 15
```

`OPNSENSE_INSECURE=1` is only a bootstrap workaround. For regular use, trust the firewall certificate and unset it. The OPNsense helper refuses to overwrite an existing route that it does not identify as CRANGE-managed. It does not modify firewall rules.
