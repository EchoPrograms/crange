"""Restrict post-apply Ansible to reviewed guests and their management path."""
import copy


_COMPUTED_VM_OUTPUT_FIELDS = frozenset({
    'ipv4_addresses',
    'ipv6_addresses',
    'mac_addresses',
    'network_interface_names',
})


def _scenario_vm_disk_only(item, key):
    """Skip guest configuration only for an input-only scenario VM disk update."""
    if not key.startswith('vm:') or not item.get('address', '').startswith('module.scenario_box['):
        return False
    change = item.get('change', {})
    if change.get('actions') != ['update']:
        return False
    before, after = change.get('before'), change.get('after')
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    missing = object()
    before_disk, after_disk = before.get('disk', missing), after.get('disk', missing)
    if before_disk is missing or after_disk is missing or before_disk == after_disk:
        return False
    before_inputs, after_inputs = dict(before), dict(after)
    for values in (before_inputs, after_inputs):
        values.pop('disk', None)
        for field in _COMPUTED_VM_OUTPUT_FIELDS:
            values.pop(field, None)
    return before_inputs == after_inputs


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
        if _scenario_vm_disk_only(item, key):
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


def online_configuration_keys(cfg, network_id, applied, nodes, resources):
    """Select reachable applied guests without contacting an offline node."""
    online = {n['node'] for n in nodes if n.get('status') == 'online'}
    project = int(cfg['project_id'])
    selected, skipped = [], []

    def available(key):
        value = applied.get(key)
        if not value:
            return False, 'undeployed'
        vmid = value.get('vm_id')
        if type(vmid) is not int or str(value.get('id')) != str(vmid):
            raise ValueError('Configuration requires an exact applied guest identity.')
        matches = [r for r in resources if r.get('vmid') == vmid]
        if len(matches) > 1:
            raise ValueError('Configuration found an ambiguous native guest identity.')
        if matches:
            native = matches[0]
            kind = 'lxc' if key.startswith(('container:', 'router:')) else 'qemu'
            expected = (value.get('initialization') or [{}])[0].get('hostname') if kind == 'lxc' else value.get('name')
            if (native.get('type') != kind or native.get('node') != value.get('node_name')
                    or native.get('pool') != 'crange-iac'
                    or (value.get('node_name') in online and native.get('name') != expected)
                    or f'p{project:03d}' not in str(native.get('tags','')).split(';')):
                raise ValueError('Applied guest ownership or node changed; reconcile before configuration.')
        if value.get('node_name') not in online:
            return False, 'node offline'
        if not matches:
            raise ValueError('An applied guest is missing from its online node.')
        return (True, '') if matches[0].get('status') == 'running' else (False, 'not running')

    for key in ('router:project', 'router:staging' if network_id == 254 else f'router:team:{network_id}'):
        ready, reason = available(key)
        if not ready:
            raise ValueError(f'Configuration needs {key}: {reason}. Wait for its existing router or native HA recovery.')
        selected.append(key)
    custom_ready = {}
    for name, network in cfg.get('custom_networks', {}).items():
        if str(network['owner_team_id']) != str(network_id):
            continue
        key = 'router:custom:' + name
        custom_ready[name], reason = available(key)
        if custom_ready[name]:selected.append(key)
        else:skipped.append((key, reason))
    for kind, field in (('jumpbox','jump_boxes'), ('vm','scenario_boxes'), ('container','scenario_containers')):
        for name, box in cfg.get(field, {}).items():
            if str(box['team_id']) != str(network_id):
                continue
            key = kind + ':' + name
            ready, reason = available(key)
            if box.get('network_key') and not custom_ready.get(box['network_key']):
                ready, reason = False, 'custom router unavailable'
            if ready:selected.append(key)
            else:skipped.append((key, reason))
    return sorted(selected), sorted(skipped)
