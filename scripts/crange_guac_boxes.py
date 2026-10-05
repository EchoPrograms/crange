"""Fixed power and snapshot actions for applied project-owned guests."""
from __future__ import annotations

import ipaddress
import json
import re

FIELDS = {'jumpbox': 'jump_boxes', 'vm': 'scenario_boxes', 'container': 'scenario_containers'}
POWER_ACTIONS = {'start', 'shutdown', 'stop', 'restart'}
SNAPSHOT_ACTIONS = {'list', 'create', 'restore', 'delete'}


def resource_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'(jumpbox|vm|container):[a-z][a-z0-9_.-]{0,63}', value):
        raise ValueError('Choose a deployed project box.')
    return value


def snapshot_name(value, *, required=False):
    if value == '' and not required:
        return value
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', value):
        raise ValueError('Snapshot name must be 1–40 lowercase letters, digits or hyphens, starting with a letter.')
    return value


def resolve(project, key):
    from crange import CrangeError, ProxmoxAPI, json_config, output_json, project_dir
    kind, name = resource_key(key).split(':', 1)
    path = project_dir(str(project))
    cfg = json_config(path)
    field = FIELDS[kind]
    box = cfg.get(field, {}).get(name)
    live = output_json(path).get(field, {}).get('value', {}).get(name)
    if not box or not live or not live.get('id') or not live.get('address'):
        raise CrangeError('Choose a deployed box in this project.')
    network = cfg.get('custom_networks', {}).get(box.get('network_key', ''))
    network_id = int(network['network_id']) if network else int(box['team_id'])
    vnet = f'p{int(project):03d}{"c" if network else "t"}{network_id:03d}'
    expected = f'{vnet}-jump-{int(box["jump_number"]):02d}' if kind == 'jumpbox' else f'{vnet}-{box["name"]}'
    address = ipaddress.IPv4Address(live['address'])
    if address not in ipaddress.IPv4Network(f'10.{int(project)}.{network_id}.0/24'):
        raise CrangeError('Applied box address is outside its declared subnet.')
    node = live.get('node_name', box['node_name'])
    if node not in {f'proxmox{i}' for i in range(1, 7)}:
        raise CrangeError('Applied box has no verified physical node; refresh its applied outputs.')
    vmid = int(live['id'])
    api = ProxmoxAPI()
    actual = (api.container if kind == 'container' else api.vm)(node, vmid)
    if actual.get('name', actual.get('hostname')) != expected or int(actual.get('template', 0)):
        raise CrangeError('Applied box identity does not match this project; refusing operation.')
    return path, cfg, box, api, kind, node, vmid, str(address)


def power(project, key, action):
    from crange import CrangeError, save_config, validate_project_quota
    if action not in POWER_ACTIONS:
        raise CrangeError('Unsupported power action.')
    path, cfg, box, api, kind, node, vmid, address = resolve(project, key)
    status = (api.container_status if kind == 'container' else api.status)(node, vmid)
    if action in ('shutdown', 'stop', 'restart') and status == 'running':
        kind_path = 'lxc' if kind == 'container' else 'qemu'
        endpoint = 'stop' if action == 'stop' else 'shutdown'
        fields = {'overrule-shutdown': 1} if action == 'stop' else {'timeout': 90, 'forceStop': 0}
        upid = api.request('POST', f'/nodes/{node}/{kind_path}/{vmid}/status/{endpoint}', fields)
        api.task(node, upid, timeout=180)
        status = (api.container_status if kind == 'container' else api.status)(node, vmid)
        if status != 'stopped':
            raise CrangeError('Box did not stop.' if action == 'stop' else 'Box did not stop cleanly.')
    if action in ('start', 'restart') and status != 'running':
        validate_project_quota(path, cfg)
        (api.container_start if kind == 'container' else api.start)(node, vmid)
        status = (api.container_status if kind == 'container' else api.status)(node, vmid)
        if status != 'running':
            raise CrangeError('Box did not start.')
    box['started'] = status == 'running'
    save_config(path, cfg)
    print(f'{key}: {status}.')


def snapshots(project, key, action, name=''):
    from crange import CrangeError, save_config
    if action not in SNAPSHOT_ACTIONS:
        raise CrangeError('Unsupported snapshot action.')
    snapshot_name(name, required=action != 'list')
    path, cfg, box, api, kind, node, vmid, _address = resolve(project, key)
    kind_path = 'lxc' if kind == 'container' else 'qemu'
    base = f'/nodes/{node}/{kind_path}/{vmid}/snapshot'
    listed = api.request('GET', base)
    if not isinstance(listed, list):
        raise CrangeError('Proxmox returned an invalid snapshot list.')
    records = {str(item.get('name')): item for item in listed if item.get('name') not in ('current', None)}
    if action == 'list':
        print(json.dumps({'box': key, 'snapshots': [
            {'name': item['name'], 'description': item.get('description', ''),
             'created': item.get('snaptime')}
            for item in sorted(records.values(), key=lambda x: x['name'])]}, sort_keys=True))
        return
    if action == 'create':
        if name in records:
            raise CrangeError('A snapshot with that name already exists.')
        if len(records) >= 20:
            raise CrangeError('This box has reached the 20-snapshot limit.')
        upid = api.request('POST', base, {'snapname': name, 'description': 'CRANGE user snapshot'})
    else:
        if name not in records:
            raise CrangeError('Snapshot does not exist on this box.')
        target = base + '/' + name
        upid = api.request('POST' if action == 'restore' else 'DELETE',
                           target + ('/rollback' if action == 'restore' else ''),
                           {} if action == 'restore' else None)
    api.task(node, upid, timeout=900)
    if action == 'restore':
        status = (api.container_status if kind == 'container' else api.status)(node, vmid)
        box['started'] = status == 'running'
        save_config(path, cfg)
    print(f'{key}: snapshot {name} {"restored" if action == "restore" else "created" if action == "create" else "deleted"}.')
