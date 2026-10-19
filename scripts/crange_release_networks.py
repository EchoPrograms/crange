"""Allocate project-local networks for immutable scenario releases."""
import hashlib


def allocate_networks(cfg, release, team, scope, reserved_parent=()):
    team = str(team)
    networks = cfg.setdefault('custom_networks', {})
    used_ids = {0, 254, *map(int, cfg.get('team_ids', []))}
    used_ids.update(int(net['network_id']) for net in networks.values())
    # Old applied VNets may still exist while their replacements are declared.
    # Keep new keys on different IDs until Terraform has destroyed the old copy.
    applied_networks = [net for record in cfg.get('release_rollouts', {}).values()
                        for net in record.get('applied', {}).get('resources', {}).get('custom_networks', {}).values()]
    used_ids.update(int(net['network_id']) for net in applied_networks)
    used_parent = {1, *map(int, reserved_parent)}
    for field in ('scenario_boxes', 'scenario_containers', 'jump_boxes'):
        for box in cfg.get(field, {}).values():
            if str(box['team_id']) == team and not box.get('network_key'):
                used_parent.add(10 + int(box['jump_number']) if field == 'jump_boxes' else int(box['ip_last_octet']))
    used_parent.update(int(net['uplink_last_octet']) for net in [*networks.values(), *applied_networks]
                       if str(net['owner_team_id']) == team)
    mapping, owned = {}, {}
    sources = dict(release['scenario'].get('networks', {}))
    primary_key = None
    if scope == 'test' and any(not box.get('network_key') for field in ('boxes', 'containers')
                              for box in release['scenario'].get(field, [])):
        primary_key = 'test-primary'
        while primary_key in sources:
            primary_key += '-x'
        sources[primary_key] = {'node_name':'auto', 'cores':1, 'memory_mb':512}
    for source_key, source in sorted(sources.items()):
        identity = f"{release['release_id']}:{team}:{scope}:{source_key}"
        digest = hashlib.sha256(identity.encode()).hexdigest()[:10]
        key = f"{'t' if scope == 'test' else 'r'}{digest}-{source_key[:12]}"
        current = networks.get(key)
        if current:
            if (current.get('release_id'), current.get('release_scope'), current.get('release_source_key'),
                    str(current.get('owner_team_id'))) != (release['release_id'], scope, source_key, team):
                raise ValueError('Release network name collides with an unmanaged network.')
            network_id, uplink = int(current['network_id']), int(current['uplink_last_octet'])
        else:
            network_id = next((n for n in list(range(100, 254)) + list(range(1, 100)) if n not in used_ids), None)
            uplink = next((n for n in range(2, 101) if n not in used_parent), None)
            if network_id is None or uplink is None:
                raise ValueError('Not enough free project subnets or parent-router addresses for this release.')
        value = {'owner_team_id': team, 'network_id': network_id, 'uplink_last_octet': uplink,
                 'node_name': source['node_name'], 'cores': source.get('cores', 1),
                 'memory_mb': source.get('memory_mb', 512), 'release_id': release['release_id'],
                 'release_scope': scope, 'release_source_key': source_key}
        if current and current != value:
            raise ValueError('Existing release network differs from the immutable declaration.')
        networks[key] = value
        used_ids.add(network_id); used_parent.add(uplink)
        mapping['' if source_key == primary_key else source_key] = {'key': key, 'network_id': network_id}
        owned[key] = dict(value)
    return mapping, owned


def attachment(source, team, mapping):
    source_key = source.get('network_key', '')
    if not source_key and '' not in mapping:
        return {'network_key': '', 'network_id': int(team)}
    if source_key not in mapping:
        raise ValueError('Release guest refers to a network missing from its captured topology.')
    network = mapping[source_key]
    return {'network_key': network['key'], 'network_id': network['network_id']}
