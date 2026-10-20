"""Classify a Terraform plan before permitting a staging-only deployment."""
import re
import copy
import json


def staging_projection(cfg):
    """Plan capacity only for staging; leave the persisted desired state intact."""
    result = copy.deepcopy(cfg)
    for field in ('jump_boxes', 'scenario_boxes', 'scenario_containers'):
        result[field] = {key: value for key, value in cfg.get(field, {}).items()
                         if str(value.get('team_id')) == '254'}
    result['custom_networks'] = {key: value for key, value in cfg.get('custom_networks', {}).items()
                                 if str(value.get('owner_team_id')) == '254'}
    return result


def staging_targets(cfg, state):
    """Fixed Terraform scope includes desired and removed staging resources."""
    staging = f'p{int(cfg["project_id"]):03d}t254'
    bridges = {staging} | {f'p{int(cfg["project_id"]):03d}c{int(value["network_id"]):03d}'
                          for value in cfg.get('custom_networks', {}).values()
                          if str(value['owner_team_id']) == '254'}
    targets = {'proxmox_sdn_vnet.staging'}
    for field, module in (('jump_boxes', 'jumpbox'), ('scenario_boxes', 'scenario_box'),
                          ('scenario_containers', 'scenario_container')):
        for key, value in cfg.get(field, {}).items():
            if str(value.get('team_id')) == '254':
                targets.add(f'module.{module}[{json.dumps(key)}]')
    for key, value in cfg.get('custom_networks', {}).items():
        if str(value['owner_team_id']) == '254':
            targets.update((f'module.custom_router[{json.dumps(key)}]',
                            f'proxmox_sdn_vnet.custom[{json.dumps(key)}]'))
    def walk(module):
        yield from module.get('resources', [])
        for child in module.get('child_modules', []):
            yield from walk(child)
    resources = list(walk(state.get('values', {}).get('root_module', {})))
    # Learn prior staging subnets first, independent of Terraform state ordering.
    for resource in resources:
        address = resource.get('address', '')
        custom = re.match(r'(module\.custom_router\["[^"\]]+"\])\.', address)
        value = resource.get('values', {})
        nics = value.get('network_interface', [])
        interfaces = {nic.get('name'): nic.get('bridge') for nic in nics}
        if custom and interfaces.get('eth0') == staging:
            targets.add(custom[1])
            if interfaces.get('eth1'):
                bridges.add(interfaces['eth1'])
    for resource in resources:
        address = resource.get('address', '')
        value = resource.get('values', {})
        match = re.match(r'(module\.(?:jumpbox|scenario_box|scenario_container)\["[^"\]]+"\])\.', address)
        nics = value.get('network_device', value.get('network_interface', []))
        if match and nics and all(nic.get('bridge') in bridges for nic in nics):
            targets.add(match[1])
        if address.startswith('proxmox_sdn_vnet.custom[') and value.get('id') in bridges:
            targets.add(address)
    return sorted(targets)


def changed_staging_targets(detail, cfg):
    """Use an unrefreshed comparison only to choose the subsequent live review.

    Including every unchanged staging guest would still contact unavailable
    hosts. This comparison never authorizes an apply; selected resources must
    pass a fresh provider plan, normal scope checks and saved-plan verification.
    """
    blockers = staging_blockers(detail, cfg)
    if blockers:
        raise ValueError('Staging comparison affects resources outside staging: ' + ', '.join(blockers))
    # An empty -target list means the entire project to Terraform. Always retain
    # this owned non-guest target even for metadata-only staging changes.
    targets = {'proxmox_sdn_vnet.staging'}
    for resource in detail.get('resource_changes', []):
        if resource.get('mode') == 'data' or resource.get('change', {}).get('actions') == ['no-op']:
            continue
        address = resource['address']
        if address.startswith('module.'):
            match = re.fullmatch(r'(module\.(?:staging_router|(?:jumpbox|scenario_box|scenario_container|custom_router)\["[^"\\]+"\]))\..+', address)
            if not match:
                raise ValueError('Unrecognized staging guest target.')
            targets.add(match[1])
        else:
            targets.add(address)
    return sorted(targets)


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
