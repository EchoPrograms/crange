"""Short-lived read-only cluster observations shared by interactive status views.

Authorization is checked by callers. Never reuse these display observations to
approve a power operation, deployment, image deletion or placement.
"""
import copy
import json
import re
import threading
import time

_CACHE = {'expires':0, 'value':None}
_LOCK = threading.Lock()


def observe(api=None):
    if api is not None:
        return _observe(api)
    with _LOCK:
        if _CACHE['expires'] > time.monotonic():
            return copy.deepcopy(_CACHE['value'])
        from crange import ProxmoxAPI
        client = ProxmoxAPI()
        client.request_timeout = 3
        client.read_attempts = 1
        value = _observe(client)
        _CACHE.update(expires=time.monotonic()+3, value=value)
        return copy.deepcopy(value)


def _observe(api):
    captured = time.time()
    expected = {f'proxmox{i}' for i in range(1,7)}
    try:
        nodes = api.request('GET','/nodes')
        membership = api.request('GET','/cluster/status')
        resources = api.request('GET','/cluster/resources',{'type':'vm'})
        if not all(isinstance(value,list) for value in (nodes,membership,resources)):
            raise ValueError('Incomplete live cluster observation.')
        members = {row['name']:row for row in membership if row.get('type')=='node' and row.get('name')}
        by_name = {row['node']:row for row in nodes if row.get('node')}
        result = []
        for name in sorted(expected | set(by_name) | set(members)):
            node, member = by_name.get(name,{}), members.get(name,{})
            if node.get('status')=='offline' or member.get('online') in (0,False):
                online = False
            elif node.get('status')=='online' and member.get('online') in (1,True):
                online = True
            else:
                online = None
            result.append({'node':name,'online':online,'observed_at':captured})
        return {'captured_at':captured,'nodes':result,'resources':resources,'error':None}
    except Exception:
        # A failed read provides no current evidence of either power state.
        return {'captured_at':captured,'nodes':[{'node':name,'online':None,'observed_at':captured} for name in sorted(expected)],
                'resources':[],'error':'Live cluster status is unavailable.'}


def box_states(rows, *, observation=None):
    observation = observe() if observation is None else observation
    online = {row['node']:row['online'] for row in observation['nodes']}
    resources = observation['resources']
    result = []
    for row in rows:
        kind = 'lxc' if row['kind']=='container' else 'qemu'
        matches = [r for r in resources if type(row.get('guest_id')) is int and r.get('vmid')==row['guest_id'] and
                   r.get('type')==kind and r.get('name')==row.get('guest_name') and
                   r.get('pool')=='crange-iac' and f'p{row["project"]:03d}' in str(r.get('tags','')).split(';') and not r.get('template')]
        state, error = 'unknown', observation.get('error') or 'Applied guest identity could not be verified.'
        node = row.get('node')
        if len(matches)==1:
            resource = matches[0]
            node = resource.get('node')
            if online.get(resource.get('node')) is False:
                state,error = 'offline','The Proxmox host is offline.'
            elif online.get(resource.get('node')) is True and resource.get('status') in ('running','stopped'):
                state,error = resource['status'],None
            else:
                error = 'Current host or guest power status is unavailable.'
        result.append({**row,'node':node,'state':state,'power_error':error,'observed_at':observation['captured_at']})
    return result


def project_box_states(path, cfg, *, observation=None):
    """Display declared boxes against recorded identities and one live inventory.

    Read local state directly: this status path must never run Terraform or
    contact every individual guest, especially when a node is unavailable.
    """
    captured = time.time()
    applied, state_error = {}, None
    try:
        snapshot = json.loads((path / 'terraform.tfstate').read_text())
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('resources', []), list):
            raise ValueError('Invalid Terraform snapshot.')
        for resource in snapshot.get('resources', []):
            match = re.fullmatch(r'module\.(jumpbox|scenario_box|scenario_container)\["([^"\\]+)"\]', resource.get('module', ''))
            if not match or resource.get('mode') == 'data':
                continue
            kind = {'jumpbox':'jumpbox','scenario_box':'vm','scenario_container':'container'}[match[1]]
            expected = 'proxmox_virtual_environment_container' if kind == 'container' else 'proxmox_virtual_environment_vm'
            if resource.get('type') != expected:
                continue
            applied.setdefault(f'{kind}:{match[2]}', []).extend(resource.get('instances', []))
    except FileNotFoundError:
        pass  # A new project has declarations but no applied guests yet.
    except (OSError, ValueError, TypeError, AttributeError):
        state_error = 'Recorded deployment status is unavailable.'

    project = int(cfg['project_id'])
    rows, result = [], {}
    for kind, field in (('jumpbox','jump_boxes'),('vm','scenario_boxes'),('container','scenario_containers')):
        for name, box in (cfg.get(field) or {}).items():
            key = f'{kind}:{name}'
            instances = applied.get(key, [])
            if state_error or not instances:
                result[key] = {'state':'unknown' if state_error else 'undeployed',
                               'node':None,'power_error':state_error,'observed_at':captured}
                continue
            current = [item for item in instances if not item.get('deposed') and item.get('status') != 'tainted']
            if len(current) != 1:
                result[key] = {'state':'unknown','node':None,
                               'power_error':'Recorded guest identity is ambiguous.','observed_at':captured}
                continue
            attrs = current[0].get('attributes') or {}
            guest_id = attrs.get('vm_id') or attrs.get('id')
            try:
                guest_id = int(guest_id) if not isinstance(guest_id, bool) else None
            except (ValueError, TypeError):
                guest_id = None
            network = (cfg.get('custom_networks') or {}).get(box.get('network_key', ''))
            network_id = int(network['network_id']) if network else int(box['team_id'])
            vnet = f'p{project:03d}{"c" if network else "t"}{network_id:03d}'
            expected_name = f'{vnet}-jump-{int(box.get("jump_number", 1)):02d}' if kind == 'jumpbox' else f'{vnet}-{box["name"]}'
            rows.append({'project':project,'kind':kind,'resource_key':key,'guest_id':guest_id,
                         'guest_name':expected_name,'node':attrs.get('node_name')})
    if rows:
        observation = observe() if observation is None else observation
        for row in box_states(rows, observation=observation):
            result[row['resource_key']] = {k:row.get(k) for k in ('state','node','power_error','observed_at')}
        captured = observation['captured_at']
    return {'boxes':result,'observed_at':captured,'error':state_error or (observation or {}).get('error')}


def overlay_nodes(snapshot, observation):
    """Live reachability does not make old capacity figures current."""
    value = copy.deepcopy(snapshot)
    status = {row['node']:row for row in observation['nodes']}
    for node in value.get('nodes',[]):
        live = status.get(node['node'],{'online':None})
        node['online'] = live['online']
        node['status_observed_at'] = observation['captured_at']
        if live['online'] is not True:
            node.setdefault('eligibility',{}).update(eligible=False)
            node['eligibility'].setdefault('blockers',[]).append('Host offline' if live['online'] is False else 'Live host status unavailable')
    value['node_status_observed_at'] = observation['captured_at']
    value['node_status_error'] = observation.get('error')
    return value
