"""Quiesce exactly reviewed retiring guests before their replacement reuses IPs."""
import json
import re
from pathlib import Path


def retirements(cfg, teams, detail):
    from crange import CrangeError, NODES
    selected = set(map(str, teams))
    project = int(cfg['project_id'])
    owned = {}
    for team in selected:
        record = cfg.get('release_rollouts', {}).get(team, {})
        applied = record.get('applied') or {}
        for field, module, kind, prefix in (
                ('scenario_boxes', 'scenario_box', 'qemu', 'vm:'),
                ('scenario_containers', 'scenario_container', 'lxc', 'container:')):
            for key, box in applied.get('resources', {}).get(field, {}).items():
                if (str(box.get('team_id')) != team or box.get('release_scope') != 'team'
                        or box.get('release_id') != applied.get('release_id')):
                    raise CrangeError('Retiring release ownership differs from its applied record.')
                resource_type = 'vm' if kind == 'qemu' else 'container'
                address = f'module.{module}[{json.dumps(key)}].proxmox_virtual_environment_{resource_type}.scenario'
                owned[address] = (box, record.get('applied_endpoints', {}).get(prefix + key), kind, team)
    targets = []
    for resource in detail.get('resource_changes', []):
        change = resource.get('change', {})
        if 'delete' not in change.get('actions', []) or resource.get('address') not in owned:
            continue
        box, endpoint, kind, team = owned[resource['address']]
        before = change.get('before') or {}
        network = int(box.get('network_id', team))
        bridge = f'p{project:03d}{"c" if box.get("network_key") else "t"}{network:03d}'
        name = bridge + '-' + box['name']
        guest_id = before.get('vm_id')
        node = before.get('node_name')
        initialization = before.get('initialization') or []
        planned_name = (before.get('name') if kind == 'qemu' else
                        initialization[0].get('hostname') if isinstance(initialization, list)
                        and len(initialization) == 1 and isinstance(initialization[0], dict) else None)
        if (not isinstance(endpoint, dict) or type(guest_id) is not int or guest_id < 100
                or guest_id != endpoint.get('id') or node not in NODES
                or node != endpoint.get('node_name')
                or planned_name != name
                or endpoint.get('address') != f'10.{project}.{network}.{box["ip_last_octet"]}'):
            raise CrangeError('Retiring guest differs from the exact reviewed applied endpoint.')
        targets.append({'resource': resource['address'], 'id': guest_id, 'node': node,
                        'kind': kind, 'name': name, 'bridge': bridge, 'project': project})
    if len({row['id'] for row in targets}) != len(targets):
        raise CrangeError('Retiring guest identities are ambiguous.')
    return sorted(targets, key=lambda row: row['id'])


def _validate(api, targets):
    from crange import CrangeError
    resources = api.request('GET', '/cluster/resources', {'type': 'vm'})
    if not isinstance(resources, list):
        raise CrangeError('Live retiring-guest inventory is unavailable.')
    for target in targets:
        matches = [row for row in resources if str(row.get('vmid')) == str(target['id'])]
        if (len(matches) != 1 or matches[0].get('node') != target['node']
                or matches[0].get('type') != target['kind'] or matches[0].get('pool') != 'crange-iac'):
            raise CrangeError('Retiring guest has moved or is outside recorded ownership.')
        base = f'/nodes/{target["node"]}/{target["kind"]}/{target["id"]}'
        config = api.request('GET', base + '/config')
        status = api.request('GET', base + '/status/current')
        networks = [value for key, value in config.items() if re.fullmatch(r'net[0-9]+', key)]
        if (config.get('name', config.get('hostname')) != target['name']
                or config.get('lock') or int(config.get('template', 0))
                or f'p{target["project"]:03d}' not in str(config.get('tags', '')).split(';')
                or status.get('status') not in ('running', 'stopped') or not networks
                or any('bridge=' + target['bridge'] not in str(net).split(',') for net in networks)):
            raise CrangeError('Retiring guest generation, network or power state is unsafe to stop.')


def quiesce(api, targets, receipt_path, plan_id):
    """All ownership checks precede writes; resume recorded tasks, never replay POST."""
    from crange import CrangeError
    from crange_scheduling import atomic_json
    if not targets:
        return
    if not re.fullmatch(r'[a-f0-9]{32}', plan_id):
        raise CrangeError('Retiring guests require an exact reviewed plan.')
    receipt_path = Path(receipt_path)
    receipt = {'plan_id': plan_id, 'targets': targets, 'steps': {}, 'state': 'validated'}
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if receipt.get('plan_id') != plan_id or receipt.get('targets') != targets:
            raise CrangeError('Retirement receipt differs from the exact reviewed guests.')
    _validate(api, targets)
    own_tasks = {step.get('upid') for step in receipt['steps'].values() if step.get('upid')}
    ids = {str(target['id']) for target in targets}
    for node in sorted({target['node'] for target in targets}):
        active = api.request('GET', f'/nodes/{node}/tasks', {'source': 'active', 'limit': 1000})
        if (not isinstance(active, list) or len(active) >= 1000
                or any(str(task.get('id')) in ids and task.get('upid') not in own_tasks for task in active)):
            raise CrangeError('An active or uncertain provider task references a retiring guest.')
    atomic_json(receipt_path, receipt, 0o600)
    print(f'Stopping {len(targets)} reviewed old-release guests before replacement; selected environments will be offline until configuration and health checks finish.', flush=True)
    for target in targets:
        key = str(target['id'])
        base = f'/nodes/{target["node"]}/{target["kind"]}/{target["id"]}'
        step = receipt['steps'].get(key, {})
        if step.get('upid'):
            api.task(target['node'], step['upid'], timeout=180)
        elif step.get('state') == 'submitting':
            raise CrangeError('Prior retiring-guest stop submission is ambiguous; retain the receipt and reconcile its provider task.')
        elif api.request('GET', base + '/status/current').get('status') != 'stopped':
            step = {'state': 'submitting'}
            receipt['steps'][key] = step
            atomic_json(receipt_path, receipt, 0o600)
            step['upid'] = api.request('POST', base + '/status/stop', {})
            atomic_json(receipt_path, receipt, 0o600)
            api.task(target['node'], step['upid'], timeout=180)
        if api.request('GET', base + '/status/current').get('status') != 'stopped':
            raise CrangeError('Old-release guest has not stopped; no replacement apply started.')
        step['state'] = 'stopped'
        receipt['steps'][key] = step
        atomic_json(receipt_path, receipt, 0o600)
        print(f'Confirmed retiring guest stopped: {target["name"]} ({target["id"]}).', flush=True)
    # Fresh all-guest validation closes the gap before Terraform creates peers.
    _validate(api, targets)
    if any(api.request('GET', f'/nodes/{row["node"]}/{row["kind"]}/{row["id"]}/status/current').get('status') != 'stopped' for row in targets):
        raise CrangeError('A retiring guest restarted; replacement apply is blocked.')
    receipt['state'] = 'stopped'
    atomic_json(receipt_path, receipt, 0o600)
