"""Classify a Terraform plan before permitting a staging-only deployment."""
import re


def staging_blockers(detail, cfg):
    project = f"p{int(cfg['project_id']):03d}"
    staging = project + 't254'
    before_bridges = {staging}
    after_bridges = {staging} | {
        f"{project}c{int(net['network_id']):03d}"
        for net in cfg.get('custom_networks', {}).values()
        if str(net['owner_team_id']) == '254'}
    resources = detail.get('resource_changes', [])

    def interfaces(value):
        return {item.get('name'): item.get('bridge')
                for item in (value or {}).get('network_interface', [])}

    def guest_on(value, bridges):
        devices = (value or {}).get('network_device', (value or {}).get('network_interface', []))
        return bool(devices) and all(item.get('bridge') in bridges for item in devices)

    # Terraform includes unchanged routers in resource_changes. Their prior
    # uplink proves ownership of a deleted/renumbered custom staging subnet.
    for resource in resources:
        if resource.get('address', '').startswith('module.custom_router['):
            before = interfaces(resource.get('change', {}).get('before'))
            if before.get('eth0') == staging and before.get('eth1'):
                before_bridges.add(before['eth1'])

    blockers = []
    fields = {'jumpbox': 'jump_boxes', 'scenario_box': 'scenario_boxes',
              'scenario_container': 'scenario_containers'}
    for resource in resources:
        address = resource.get('address', '?')
        change = resource.get('change', {})
        actions = change.get('actions', [])
        if resource.get('mode') == 'data' or actions == ['no-op']:
            continue
        before, after = change.get('before'), change.get('after')
        allowed = False
        if address in ('proxmox_sdn_applier.apply', 'proxmox_sdn_applier.finalizer',
                       'proxmox_sdn_vnet.staging', 'terraform_data.sdn_revision'):
            allowed = True
        elif address.startswith('module.staging_router.'):
            allowed = True
        else:
            match = re.fullmatch(r'module\.(jumpbox|scenario_box|scenario_container)\["([^"\]]+)"\]\..+', address)
            custom = re.fullmatch(r'module\.custom_router\["([^"\]]+)"\]\..+', address)
            vnet = re.fullmatch(r'proxmox_sdn_vnet\.custom\["([^"\]]+)"\]', address)
            if match:
                kind, key = match.groups()
                declared = cfg.get(fields[kind], {}).get(key)
                # Check both sides: moving an existing team guest to staging is
                # still a change to that team, even if the desired owner is 254.
                prior = before is None or guest_on(before, before_bridges)
                target = after is None or (
                    declared is not None and str(declared.get('team_id')) == '254'
                    and guest_on(after, after_bridges))
                allowed = prior and target
            elif custom:
                net = cfg.get('custom_networks', {}).get(custom[1])
                prior = before is None or interfaces(before).get('eth0') == staging
                target = after is None or (
                    net is not None and str(net.get('owner_team_id')) == '254'
                    and interfaces(after).get('eth0') == staging)
                allowed = prior and target
            elif vnet:
                net = cfg.get('custom_networks', {}).get(vnet[1])
                prior = before is None or before.get('id') in before_bridges
                target = after is None or (
                    net is not None and str(net.get('owner_team_id')) == '254'
                    and after.get('id') in after_bridges)
                allowed = prior and target
        if not allowed:
            blockers.append(address)
    return sorted(set(blockers))
