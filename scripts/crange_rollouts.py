"""Ownership and history for explicit team release transitions.

These functions change desired state only. Terraform apply and health checks
record separate events so a declared release is never reported as healthy.
"""
import hashlib
import json
import json
from copy import deepcopy
from datetime import datetime, timezone

FIELDS = ('scenario_boxes', 'scenario_containers', 'custom_networks')


def now():
    return datetime.now(timezone.utc).isoformat()


def owner(field, resource):
    return str(resource.get('owner_team_id' if field == 'custom_networks' else 'team_id'))


def owned(cfg, team):
    return {field: {key: deepcopy(value) for key, value in cfg.get(field, {}).items()
                    if owner(field, value) == str(team) and value.get('release_scope') == 'team'}
            for field in FIELDS}


def verify_owned(cfg, team, declaration):
    actual = owned(cfg, team)
    if actual != declaration['resources']:
        raise ValueError(f'Team {team} release resources have drifted or ownership has changed; refusing replacement.')
    for field, resources in actual.items():
        for value in resources.values():
            if value.get('release_id') != declaration['release_id']:
                raise ValueError(f'Team {team} release ownership has changed; refusing replacement.')


def begin(cfg, team, action, target_version, from_version=None):
    """Validate and remove only the previous recorded release's declarations."""
    team = str(team)
    record = cfg.get('release_rollouts', {}).get(team)
    if not record:
        if action != 'deploy':
            raise ValueError(f'Team {team} has no recorded release to {action}.')
        if any(owned(cfg, team).values()):
            raise ValueError(f'Team {team} has unrecorded release resources; refusing to adopt them implicitly.')
        return
    desired = record['desired']
    verify_owned(cfg, team, desired)
    if action == 'deploy':
        if desired['version'] != target_version:
            raise ValueError(f'Team {team} already has release {desired["version"]}; use release-upgrade or release-rollback.')
        return
    if action not in ('upgrade', 'rollback'):
        raise ValueError('Invalid release transition.')
    if from_version != desired['version']:
        raise ValueError(f'Team {team} no longer has the expected release {from_version}.')
    if target_version == from_version:
        raise ValueError('Choose a different target release.')
    if record.get('applied', {}).get('release_id') != desired['release_id']:
        raise ValueError(f'Team {team} has a pending release; apply it before another transition.')
    if action == 'rollback' and not any(event['event'] == 'infrastructure-applied' and
            event.get('version') == target_version for event in record.get('history', [])):
        raise ValueError(f'Release {target_version} was never applied to team {team}; use upgrade for a new target.')
    networks = desired['resources']['custom_networks']
    guests = desired['resources']
    for field in ('scenario_boxes', 'scenario_containers', 'jump_boxes'):
        for key, value in cfg.get(field, {}).items():
            if value.get('network_key') in networks and key not in guests.get(field, {}):
                raise ValueError(f'Team {team} release network has other guests attached; refusing replacement.')
    for field, resources in desired['resources'].items():
        for key in resources:
            del cfg[field][key]


def declare(cfg, team, release, action):
    team = str(team)
    records = cfg.setdefault('release_rollouts', {})
    record = records.setdefault(team, {'history': []})
    previous = record.get('desired', {}).get('version')
    desired = {'release_id': release['release_id'], 'version': release['version'],
               'resources': owned(cfg, team)}
    if record.get('desired') == desired:
        return
    record['desired'] = desired
    record['health'] = {'status': 'not-checked'}
    record['history'].append({'event': 'declared', 'action': action,
                             'from_version': previous, 'version': release['version'],
                             'release_id': release['release_id'], 'at': now()})


def endpoint(actual):
    """Persist actual identity separately from immutable deployment intent."""
    placement = actual.get('placement') or {}
    result = {key: deepcopy(actual[key]) for key in ('id', 'address') if key in actual}
    for key in ('node_name', 'datastore_id', 'template_id', 'template_node',
                'template_file_id', 'clone_mode'):
        value = placement.get(key, actual.get(key))
        if value is not None:
            result[key] = deepcopy(value)
    return result


def applied(cfg, outputs):
    """Record infrastructure application only when every declared endpoint exists."""
    changed = False
    for team, record in cfg.get('release_rollouts', {}).items():
        desired = record['desired']
        try:
            verify_owned(cfg, team, desired)
        except ValueError:
            if record.get('applied') != desired:
                raise
            digest = hashlib.sha256(json.dumps(owned(cfg, team), sort_keys=True).encode()).hexdigest()
            if record.get('health', {}).get('drift_sha256') != digest:
                record['health'] = {'status': 'drifted', 'drift_sha256': digest}
                record['history'].append({'event': 'drift-detected', 'version': desired['version'], 'at': now()})
                changed = True
            continue
        endpoints = {}
        for field in ('scenario_boxes', 'scenario_containers'):
            live = outputs.get(field, {}).get('value', {})
            for key, box in desired['resources'][field].items():
                actual = live.get(key, {})
                address = f"10.{cfg['project_id']}.{box.get('network_id', int(team))}.{box['ip_last_octet']}"
                if not actual.get('id') or actual.get('address') != address:
                    raise ValueError(f'Team {team} release guest {key} is not applied at its declared address.')
                identity = endpoint(actual)
                if (box.get('node_name') == 'auto' or box.get('placement', {}).get('mode') == 'auto') and not identity.get('node_name'):
                    raise ValueError(f'Team {team} release guest {key} has no applied node identity.')
                endpoints[('vm:' if field == 'scenario_boxes' else 'container:') + key] = identity
        live = outputs.get('routers', {}).get('value', {}).get('custom_networks', {})
        for key, network in desired['resources']['custom_networks'].items():
            actual = live.get(key, {})
            if (str(actual.get('owner_team_id')), actual.get('lan'), actual.get('upstream_address')) != (
                team, f"10.{cfg['project_id']}.{network['network_id']}.0/24",
                f"10.{cfg['project_id']}.{team}.{network['uplink_last_octet']}"):
                raise ValueError(f'Team {team} release network {key} is not applied at its declared topology.')
            identity = endpoint(actual)
            if (network.get('node_name') == 'auto' or network.get('placement', {}).get('mode') == 'auto') and not identity.get('node_name'):
                raise ValueError(f'Team {team} release network {key} has no applied node identity.')
            endpoints['network:' + key] = identity
        if record.get('applied') == desired:
            if record.get('applied_endpoints') != endpoints:
                record['applied_endpoints'] = endpoints
                record['history'].append({'event': 'applied-identities-recorded', 'version': desired['version'],
                                          'release_id': desired['release_id'], 'endpoints': deepcopy(endpoints), 'at': now()})
                changed = True
            continue
        record['applied'] = deepcopy(desired)
        record['applied_endpoints'] = endpoints
        record['health'] = {'status': 'not-checked'}
        record['history'].append({'event': 'infrastructure-applied', 'version': desired['version'],
                                 'release_id': desired['release_id'], 'endpoints': deepcopy(endpoints), 'at': now()})
        changed = True
    return changed


def health(record, passed, failures):
    status = 'passed' if passed else 'failed'
    record['health'] = {'status': status, 'failures': failures, 'checked_at': now()}
    record['history'].append({'event': 'health-checked', 'version': record['desired']['version'],
                             'release_id': record['desired']['release_id'], 'status': status,
                             'failures': failures, 'at': record['health']['checked_at']})


def plan_blockers(detail, cfg, teams):
    """Reject a plan touching staging, unselected teams or unowned resources."""
    import re
    selected = set(map(str, teams))
    project = f"p{int(cfg['project_id']):03d}"
    before = {field: {} for field in FIELDS}
    after = {field: {} for field in FIELDS}
    for team in selected:
        record = cfg.get('release_rollouts', {}).get(team, {})
        for field in FIELDS:
            before[field].update(record.get('applied', {}).get('resources', {}).get(field, {}))
            after[field].update(record.get('desired', {}).get('resources', {}).get(field, {}))
    prior_bridges = {project + f't{int(team):03d}' for team in selected}
    target_bridges = set(prior_bridges)
    prior_bridges.update(project + f"c{net['network_id']:03d}" for net in before['custom_networks'].values())
    target_bridges.update(project + f"c{net['network_id']:03d}" for net in after['custom_networks'].values())
    def guest_on(value, bridges):
        devices = (value or {}).get('network_device', (value or {}).get('network_interface', []))
        return bool(devices) and all(device.get('bridge') in bridges for device in devices)
    def interfaces(value):
        return {item.get('name'): item.get('bridge') for item in (value or {}).get('network_interface', [])}
    blockers = []
    for resource in detail.get('resource_changes', []):
        change = resource.get('change', {})
        if resource.get('mode') == 'data' or change.get('actions') == ['no-op']:
            continue
        address = resource.get('address', '?')
        prior, target = change.get('before'), change.get('after')
        allowed = address in ('proxmox_sdn_applier.apply', 'proxmox_sdn_applier.finalizer', 'terraform_data.sdn_revision')
        guest = re.fullmatch(r'module\.(scenario_box|scenario_container)\["([^"\]]+)"\]\..+', address)
        router = re.fullmatch(r'module\.custom_router\["([^"\]]+)"\]\..+', address)
        vnet = re.fullmatch(r'proxmox_sdn_vnet\.custom\["([^"\]]+)"\]', address)
        if guest:
            field = 'scenario_boxes' if guest[1] == 'scenario_box' else 'scenario_containers'
            key = guest[2]
            allowed = (prior is None or key in before[field] and guest_on(prior, prior_bridges)) and (
                target is None or key in after[field] and guest_on(target, target_bridges))
        elif router:
            key = router[1]
            allowed = (prior is None or key in before['custom_networks'] and
                interfaces(prior).get('eth0') == project + f"t{int(before['custom_networks'][key]['owner_team_id']):03d}") and (
                target is None or key in after['custom_networks'] and
                interfaces(target).get('eth0') == project + f"t{int(after['custom_networks'][key]['owner_team_id']):03d}")
        elif vnet:
            key = vnet[1]
            allowed = (prior is None or key in before['custom_networks'] and prior.get('id') ==
                project + f"c{before['custom_networks'][key]['network_id']:03d}") and (
                target is None or key in after['custom_networks'] and target.get('id') ==
                project + f"c{after['custom_networks'][key]['network_id']:03d}")
        if not allowed:
            blockers.append(address)
    return sorted(set(blockers))


def review_projection(cfg, teams):
    """Include only recorded selected-team releases in placement preparation."""
    selected = list(map(str, teams))
    if (not selected or len(selected) != len(set(selected)) or
            any(team not in set(map(str, cfg.get('team_ids', []))) for team in selected)):
        raise ValueError('Choose configured ordinary teams without duplicates.')
    result = deepcopy(cfg)
    result['jump_boxes'] = {}
    for field in FIELDS:
        result[field] = {}
    for team in selected:
        record = cfg.get('release_rollouts', {}).get(team)
        if not record:
            raise ValueError(f'Team {team} has no declared release.')
        verify_owned(cfg, team, record['desired'])
        for field in FIELDS:
            result[field].update(deepcopy(record['desired']['resources'][field]))
    return result


def review_targets(cfg, teams):
    """Target recorded old and new release resources, including removals."""
    review_projection(cfg, teams)  # Validate current ownership before targeting.
    targets = {'proxmox_sdn_applier.apply', 'proxmox_sdn_applier.finalizer',
               'terraform_data.sdn_revision'}
    modules = {'scenario_boxes': 'scenario_box', 'scenario_containers': 'scenario_container',
               'custom_networks': 'custom_router'}
    for team in map(str, teams):
        record = cfg['release_rollouts'][team]
        for phase in ('applied', 'desired'):
            for field, rows in record.get(phase, {}).get('resources', {}).items():
                for key in rows:
                    targets.add(f'module.{modules[field]}[{json.dumps(key)}]')
                    if field == 'custom_networks':
                        targets.add(f'proxmox_sdn_vnet.custom[{json.dumps(key)}]')
    return sorted(targets)
