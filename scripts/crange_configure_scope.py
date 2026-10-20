"""Restrict post-apply Ansible to reviewed guests and their management path."""
import copy


def configuration_resources(detail, cfg):
    """Include a deleted guest's surviving custom router to revoke access."""
    from crange_apply_scope import guest_key
    keys = set()
    custom_bridges = {f'p{int(cfg["project_id"]):03d}c{int(net["network_id"]):03d}': name
                      for name, net in cfg.get('custom_networks', {}).items()}
    for item in detail.get('resource_changes', []):
        change = item.get('change', {})
        key = guest_key(item.get('address', ''))
        if key is None or change.get('actions') == ['no-op']:
            continue
        keys.add(key)
        if 'delete' in change.get('actions', []) and key.startswith(('vm:', 'container:', 'jumpbox:')):
            before = change.get('before') or {}
            for nic in before.get('network_device', before.get('network_interface', [])):
                if nic.get('bridge') in custom_bridges:
                    keys.add('router:custom:' + custom_bridges[nic['bridge']])
    return sorted(keys)


def configuration_inventory(inventory, cfg, network_id, resource_keys):
    if not isinstance(resource_keys, (list, tuple)) or any(not isinstance(k, str) for k in resource_keys):
        raise ValueError('Configuration resource scope must be a list of reviewed guest keys.')
    if len(set(resource_keys)) != len(resource_keys):
        raise ValueError('Configuration scope contains duplicate guest keys.')
    project = f'p{int(cfg["project_id"]):03d}'
    selected, custom_keys = set(), set()
    needs_path = False
    fields = {'vm': 'scenario_boxes', 'container': 'scenario_containers', 'jumpbox': 'jump_boxes'}
    for key in resource_keys:
        if key in ('router:project', 'router:staging', f'router:team:{network_id}'):
            if key == 'router:staging' and network_id != 254:
                raise ValueError('Router is outside the selected configuration team.')
            needs_path = True
            continue
        if key.startswith('router:custom:'):
            custom_keys.add(key.removeprefix('router:custom:'))
            needs_path = True
            continue
        kind, separator, name = key.partition(':')
        if not separator or kind not in fields:
            raise ValueError('Unrecognized reviewed configuration resource.')
        box = cfg.get(fields[kind], {}).get(name)
        if box is None:  # Reviewed deletions have no surviving guest to configure.
            needs_path = True  # Revoke the removed endpoint on its parent path.
            continue
        if str(box.get('team_id')) != str(network_id):
            raise ValueError('Guest is outside the selected configuration team.')
        needs_path = True
        if box.get('network_key'):
            custom_keys.add(box['network_key'])
        if kind == 'jumpbox':
            net = cfg.get('custom_networks', {}).get(box.get('network_key'))
            vnet = f'{project}c{int(net["network_id"]):03d}' if net else f'{project}t{network_id:03d}'
            selected.add(f'{vnet}_jump{int(box["jump_number"]):02d}')
        else:
            selected.add(f'{project}_{"vm" if kind == "vm" else "ct"}_{name}')
    if needs_path:
        selected.update((project + '_router', f'{project}t{network_id:03d}_router'))
    for key in custom_keys:
        net = cfg.get('custom_networks', {}).get(key)
        if net is None:
            continue  # Removed custom router: retain only its parent path.
        if str(net.get('owner_team_id')) != str(network_id):
            raise ValueError('Custom router is outside the selected configuration team.')
        selected.add(f'{project}c{int(net["network_id"]):03d}_router')
    result = copy.deepcopy(inventory)

    def all_hosts(group):
        hosts = set(group.get('hosts', {}))
        for child in group.get('children', {}).values():
            hosts.update(all_hosts(child))
        return hosts

    missing = selected - all_hosts(inventory['all'])
    if missing:
        raise ValueError('Applied configuration hosts are missing from inventory: ' + ', '.join(sorted(missing)))

    def prune(group):
        if 'hosts' in group:
            group['hosts'] = {host: value for host, value in group['hosts'].items() if host in selected}
        for child in group.get('children', {}).values():
            prune(child)

    prune(result['all'])
    return result, needs_path
