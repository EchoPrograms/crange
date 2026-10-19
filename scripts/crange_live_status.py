"""Short-lived read-only cluster observations shared by interactive status views.

Authorization is checked by callers. Never reuse these display observations to
approve a power operation, deployment, image deletion or placement.
"""
import copy
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
        if len(matches)==1:
            resource = matches[0]
            if online.get(resource.get('node')) is False:
                state,error = 'offline','The Proxmox host is offline.'
            elif online.get(resource.get('node')) is True and resource.get('status') in ('running','stopped'):
                state,error = resource['status'],None
            else:
                error = 'Current host or guest power status is unavailable.'
        result.append({**row,'state':state,'power_error':error,'observed_at':observation['captured_at']})
    return result


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
