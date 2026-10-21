"""Adopt native HA router locations without replacing the routed identity.

Only root-registered routers on the surviving shared store are eligible. HA is
the fencing authority; this module never starts, migrates or recreates a guest.
State writes use Terraform's locking and unforced lineage/serial validation.
"""
import copy
import hashlib
import json
from pathlib import Path
import re
import time
import uuid
import fcntl
import os
from contextlib import contextmanager

from crange_apply_scope import guest_key
from crange_placement import PlacementError

NODES = {f'proxmox{i}' for i in range(1, 7)}
STORAGE = 'crange-router-ha'


@contextmanager
def project_operation(project, *, blocking=True, root=None):
    """Serialize HA metadata adoption with normal project CLI operations."""
    if not re.fullmatch(r'[0-9]{1,3}', str(project)) or not 1 <= int(project) <= 255:
        raise PlacementError('Invalid project operation identity.')
    directory = Path(root or Path.home()/'.local/state/crange/project-operations')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(directory/f'p{int(project):03d}.lock',
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True


def creation_is_independent(attributes):
    clone = attributes.get('clone') or []
    return not clone or (len(clone) == 1 and clone[0].get('full') is True
                         and type(clone[0].get('vm_id')) is int)


def automatic_enabled():
    policy = Path('/etc/crange/router-ha/policy.json')
    if not policy.exists():
        return False
    for item in (policy, *policy.parents):
        info = item.stat()
        if item.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise PlacementError('Router HA policy must be protected from modification.')
    return json.loads(policy.read_text()).get('automatic_enrollment') is True


def enroll_created(path, plan_id, changes):
    """Automatically enroll only routers created by this completed apply."""
    if not automatic_enabled():
        return []
    reconcile_locations(path)
    keys = sorted({guest_key(row['address']) for row in changes
                   if row.get('actions') == ['create'] and str(guest_key(row['address']) or '').startswith('router:')})
    if not keys:
        return []
    from crange_scheduling import atomic_json
    pending = path/'.crange-router-ha-pending.json'
    atomic_json(pending, {'plan_id':plan_id,'changes':[
        row for row in changes if guest_key(row['address']) in keys]},0o640)
    import socket
    project = int(path.name.split('-')[-1])
    for key in keys:
        request = {'command':'router-ha-enroll','node':'proxmox4','project_id':project,
                   'router_key':key,'plan_id':plan_id}
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(330);client.connect('/run/crange-ct-read/socket')
            client.sendall(json.dumps(request).encode()+b'\n');raw=b''
            while not raw.endswith(b'\n') and len(raw)<=8192:
                part=client.recv(8193-len(raw))
                if not part:break
                raw+=part
        reply=json.loads(raw)
        value=json.loads(reply['value']) if set(reply)=={'value'} and isinstance(reply['value'],str) else None
        if not isinstance(value,dict) or value.get('phase')!='enrolled' or value.get('project_id')!=project or value.get('router_key')!=key:
            raise PlacementError('Router HA enrollment is incomplete; retain its operator journal and retry bootstrap configuration.')
    pending.unlink()
    return keys


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def normalized_snapshot(snapshot):
    """Terraform emits its check sets in a different order on repeated pulls."""
    result = copy.deepcopy(snapshot)
    checks = result.get('check_results')
    if isinstance(checks, list):
        for check in checks:
            if isinstance(check, dict) and isinstance(check.get('objects'), list):
                check['objects'].sort(key=digest)
        checks.sort(key=digest)
    return result


def address(resource, instance):
    result = (resource.get('module', '') + '.' if resource.get('module') else '')
    result += resource['type'] + '.' + resource['name']
    if 'index_key' in instance:
        result += '[' + json.dumps(instance['index_key'], separators=(',', ':')) + ']'
    return result


def container_states(snapshot):
    if (snapshot.get('version') != 4 or not snapshot.get('lineage') or
            type(snapshot.get('serial')) is not int or snapshot['serial'] < 0):
        raise PlacementError('Router adoption requires a versioned Terraform state snapshot.')
    result = {}
    for resource in snapshot.get('resources', []):
        if resource.get('mode') != 'managed' or resource.get('type') != 'proxmox_virtual_environment_container':
            continue
        for instance in resource.get('instances', []):
            key = address(resource, instance)
            if key in result or instance.get('deposed') or instance.get('status') == 'tainted':
                raise PlacementError('Ambiguous, deposed or tainted router state cannot be adopted.')
            result[key] = instance['attributes']
    return result


def relocated_snapshot(snapshot, moves):
    """Patch only verified CT node fields; preserve all other state verbatim.

    Authorization belongs to prove_moves(). Kept separate so the native state
    transaction can also be exercised on a disposable non-project container.
    Incrementing the serial by exactly one is essential: unforced Terraform push
    rejects a competing write with that same serial and different contents.
    """
    if not moves:
        raise PlacementError('Router adoption has no verified moves.')
    result = copy.deepcopy(snapshot)
    states = container_states(result)
    for key, move in moves.items():
        attributes = states.get(key, {})
        disks = attributes.get('disk') or []
        if (move.get('from') not in NODES or move.get('to') not in NODES or move['from'] == move['to'] or
                type(move.get('vmid')) is not int or move['vmid'] < 100 or
                str(attributes.get('id')) != str(move['vmid']) or attributes.get('vm_id') not in (None, move['vmid']) or
                attributes.get('node_name') != move['from'] or len(disks) != 1 or
                disks[0].get('datastore_id') != STORAGE):
            raise PlacementError('Router state identity, shared storage or prior location differs.')
        attributes['node_name'] = move['to']
    result['serial'] += 1
    return result


def config_identity(config):
    """Stable routed/disk identity, excluding counters and the movable node."""
    return {key: copy.deepcopy(config[key]) for key in sorted(config)
            if key in ('hostname', 'rootfs', 'unprivileged', 'tags') or re.fullmatch(r'net\d+', key)}


def fresh(row, now):
    try:
        return -5 <= now - float(row.get('timestamp', 0)) <= 60
    except (TypeError, ValueError):
        return False


def membership_identity(rows):
    return [{k: r.get(k) for k in ('vmid', 'type', 'node', 'status', 'name', 'pool')} for r in rows]


def candidate_matches(candidate, observed):
    """Native local state persistence can advance the supplied serial once more."""
    if type(observed.get('serial')) is not int or observed['serial'] not in (candidate['serial'], candidate['serial'] + 1):
        return False
    return ({k: v for k, v in normalized_snapshot(candidate).items() if k not in ('serial', 'terraform_version')} ==
            {k: v for k, v in normalized_snapshot(observed).items() if k not in ('serial', 'terraform_version')})


def placement_metadata(path, moves):
    file = path / 'crange-applied-placements.json'
    if not file.exists():
        return None, None
    before = json.loads(file.read_text())
    after = copy.deepcopy(before)
    for key, move in moves.items():
        resource_key = guest_key(key)
        if resource_key not in before:
            continue
        record = after[resource_key]
        if record.get('guest_id') != move['vmid'] or record.get('node_name') != move['from']:
            raise PlacementError('Router placement metadata changed; preserve its image binding for reconciliation.')
        record.update(node_name=move['to'], datastore_id=STORAGE)
    return before, after


def finish_metadata(path, transaction):
    """A crash after state push must not discard the router's image binding."""
    from crange_scheduling import atomic_json
    saved = transaction / 'placements-before.json'
    if not saved.exists():
        return
    before = json.loads(saved.read_text())
    after = json.loads((transaction / 'placements-after.json').read_text())
    file = path / 'crange-applied-placements.json'
    observed = json.loads(file.read_text())
    if observed == after:
        return
    if observed != before:
        raise PlacementError('Router state adopted, but placement metadata changed; retain its journal.')
    atomic_json(file, after, 0o640)


def refresh_storage(path, key, vmid, storage, *, tf=None, api=None):
    """Record a completed operator disk move without replacing its container.

    Terraform performs its normal locked refresh-only transaction. Creation
    metadata and every other guest must survive; an import is never used.
    """
    from crange import ProxmoxAPI, tf_run
    from crange_scheduling import atomic_json
    if storage != STORAGE or not str(guest_key(key) or '').startswith('router:'):
        raise PlacementError('Storage adoption is restricted to registered router addresses.')
    path = Path(path); tf = tf or tf_run; api = api or ProxmoxAPI()
    before = json.loads(tf(path, ['state','pull'], capture=True).stdout)
    attrs = container_states(before).get(key, {})
    if attrs.get('vm_id') != vmid or str(attrs.get('id')) != str(vmid) or not creation_is_independent(attrs):
        raise PlacementError('Router storage adoption lacks its original creation identity.')
    node = attrs.get('node_name'); project = int(path.name.split('-')[-1])
    config = api.request('GET', f'/nodes/{node}/lxc/{vmid}/config')
    if (not str(config.get('rootfs','')).startswith(f'{STORAGE}:{vmid}/') or
            f'p{project:03d}' not in str(config.get('tags','')).split(';') or config.get('lock')):
        raise PlacementError('Native shared router disk is not ready for state refresh.')
    metadata_file = path/'crange-applied-placements.json'
    metadata = json.loads(metadata_file.read_text())
    resource_key = guest_key(key)
    record = metadata.get(resource_key, {})
    if record.get('guest_id') != vmid or record.get('node_name') != node:
        raise PlacementError('Router image/placement binding differs; preserve its migration journal.')
    transaction = Path.home()/'.local/state/crange/router-storage-refresh'/path.name/(str(vmid)+'-'+digest(before)[:12])
    transaction.mkdir(parents=True, mode=0o700, exist_ok=False)
    atomic_json(transaction/'before.json', before)
    atomic_json(transaction/'placements-before.json', metadata)
    plan = transaction/'refresh.tfplan'
    tf(path, ['plan','-refresh-only','-input=false','-out',str(plan),'-target='+key], capture=True)
    detail = json.loads(tf(path, ['show','-json',str(plan)], capture=True).stdout)
    if any(r.get('change',{}).get('actions') != ['no-op'] for r in detail.get('resource_changes',[])):
        raise PlacementError('Router refresh-only plan contains resource mutations; preserve the journal.')
    atomic_json(transaction/'receipt.json', {'phase':'submitting-refresh','vmid':vmid})
    tf(path, ['apply','-input=false',str(plan)], capture=True)
    after = json.loads(tf(path, ['state','pull'], capture=True).stdout)
    current = container_states(after).get(key, {})
    disks = current.get('disk') or []
    def other_guests(snapshot):
        return {address(r,i):i for r in snapshot.get('resources',[]) if r.get('mode')=='managed' and
                r.get('type') in ('proxmox_virtual_environment_vm','proxmox_virtual_environment_container')
                for i in r.get('instances',[]) if address(r,i)!=key}
    if (after.get('lineage') != before['lineage'] or other_guests(after) != other_guests(before) or
            current.get('node_name') != node or current.get('vm_id') != vmid or len(disks)!=1 or
            disks[0].get('datastore_id') != STORAGE or disks[0].get('size') != attrs['disk'][0]['size'] or
            current.get('operating_system') != attrs.get('operating_system') or current.get('clone') != attrs.get('clone') or
            current.get('initialization') != attrs.get('initialization')):
        raise PlacementError('Router refresh changed an unrelated guest or creation identity; preserve its journal.')
    if json.loads(metadata_file.read_text()) != metadata:
        raise PlacementError('Router placement metadata changed during storage refresh; retain the journal.')
    metadata[resource_key]['datastore_id'] = STORAGE
    atomic_json(metadata_file, metadata, 0o640)
    receipt = {'phase':'verified','vmid':vmid,'node':node,'storage':STORAGE,'lineage_preserved':True,'unrelated_guests_unchanged':True}
    atomic_json(transaction/'receipt.json',receipt)
    return receipt


def prove_moves(snapshot, registry, api, *, now=None):
    now = time.time() if now is None else now
    project = registry.get('project_id')
    if (registry.get('schema_version') != 1 or type(project) is not int or not 1 <= project <= 255 or project in (60, 61) or
            registry.get('state_lineage') != snapshot.get('lineage') or not registry.get('routers')):
        raise PlacementError('Router HA registration or Terraform lineage differs.')
    states = container_states(snapshot)
    resources = api.request('GET', '/cluster/resources', {'type': 'vm'})
    nodes = {r['node']: r for r in api.request('GET', '/nodes')}
    moves = {}
    for key, registration in registry['routers'].items():
        resource_key = guest_key(key)
        attributes = states.get(key, {})
        vmid = registration.get('vmid')
        if (not resource_key or not resource_key.startswith('router:') or type(vmid) is not int or
                attributes.get('vm_id') not in (None, vmid) or str(attributes.get('id')) != str(vmid)):
            raise PlacementError('Registered router no longer matches the project state.')
        if not creation_is_independent(attributes):
            raise PlacementError('Linked router creation references cannot be adopted; router state retained.')
        actual = [r for r in resources if r.get('vmid') == vmid]
        if len(actual) != 1 or actual[0].get('type') != 'lxc' or actual[0].get('pool') != 'crange-iac':
            raise PlacementError('Registered router has no unique native container identity.')
        node = actual[0]['node']
        if node == attributes.get('node_name') and nodes.get(node, {}).get('status') == 'offline':
            # No state move is inferred while HA fences/recovers this node.
            # Unrelated lab work may proceed; protect_registered_plan forbids
            # touching these registered identities until explicitly retired.
            continue
        allowed = registration.get('eligible_nodes', [])
        if (not isinstance(allowed, list) or not allowed or len(set(allowed)) != len(allowed) or
                not set(allowed) <= NODES or node not in NODES or node not in allowed or
                (attributes.get('node_name') not in allowed and attributes.get('node_name') != registration.get('initial_node')) or
                nodes.get(node, {}).get('status') != 'online' or actual[0].get('status') != 'running'):
            raise PlacementError('Router recovery has not reached an enrolled online node.')
        config = api.request('GET', f'/nodes/{node}/lxc/{vmid}/config')
        tags = set(str(config.get('tags', '')).split(';'))
        if (not {f'p{project:03d}', 'terraform', 'network-infrastructure', 'range-lan'} <= tags or
                not str(config.get('rootfs', '')).startswith(STORAGE + ':') or
                digest(config_identity(config)) != registration.get('identity_sha256')):
            raise PlacementError('Recovered router network, ownership or shared disk identity differs.')
        ha = api.request('GET', '/cluster/ha/status/current')
        quorum = [r for r in ha if r.get('type') == 'quorum' and r.get('quorate') == 1]
        master = [r for r in ha if r.get('type') == 'master' and ' (active,' in r.get('status', '')]
        lrm = [r for r in ha if r.get('type') == 'lrm' and r.get('node') == node
               and ' (active,' in r.get('status', '')]
        service = [r for r in ha if r.get('sid') == f'ct:{vmid}']
        if (len(quorum) != 1 or len(master) != 1 or len(lrm) != 1 or len(service) != 1 or
                any(not fresh(r, now) for r in master + lrm) or nodes.get(master[0].get('node'), {}).get('status') != 'online' or
                service[0].get('node') != node or service[0].get('state') != 'started' or
                service[0].get('request_state') != 'started'):
            raise PlacementError('Native HA has not confirmed a fresh exclusive router assignment.')
        # Recheck authoritative membership and config after the HA observation.
        current = [r for r in api.request('GET', '/cluster/resources', {'type': 'vm'}) if r.get('vmid') == vmid]
        if (membership_identity(current) != membership_identity(actual) or
                digest(config_identity(api.request('GET', f'/nodes/{node}/lxc/{vmid}/config')))
                != registration['identity_sha256']):
            raise PlacementError('Router recovery changed during verification; review again.')
        # A returning original host must agree with the HA authority even if
        # Terraform still records that host. Never silently adopt an old copy.
        if node != attributes.get('node_name'):
            moves[key] = {'vmid': vmid, 'from': attributes['node_name'], 'to': node}
    return moves


def protect_registered_plan(path, detail):
    registration=Path('/etc/crange/router-ha')/(Path(path).name+'.json')
    if not registration.exists():return
    registry=json.loads(registration.read_text())
    for resource in detail.get('resource_changes',[]):
        if (resource.get('address') in registry.get('routers',{}) and
                resource.get('change',{}).get('actions') not in (['no-op'],['read'])):
            raise PlacementError('Native HA owns this router identity. Retire/review its registration before changing or deleting it.')


def reconcile_locations(path, *, api=None, tf=None, registry_path=None, journal_root=None):
    """Worker entry point. Missing root registration leaves ordinary projects alone."""
    from crange import ProxmoxAPI, tf_run
    from crange_scheduling import atomic_json
    path = Path(path)
    registration = Path(registry_path or Path('/etc/crange/router-ha') / (path.name + '.json'))
    if not registration.exists():
        return []
    project = int(path.name.split('-')[-1])
    stat = registration.stat()
    if stat.st_uid != 0 or stat.st_mode & 0o022 or registration.is_symlink():
        raise PlacementError('Router HA registration must be root-owned and protected from modification.')
    if registry_path is None:
        for parent in registration.parents:
            info = parent.stat()
            if info.st_uid != 0 or info.st_mode & 0o022 or parent.is_symlink():
                raise PlacementError('Router HA registration parents must be protected from replacement.')
    registry = json.loads(registration.read_text())
    if registry.get('project_id') != project:
        raise PlacementError('Router HA registration belongs to another project.')
    if registry.get('routers') == {}:
        return []
    tf = tf or tf_run
    api = api or ProxmoxAPI()
    pull = lambda: json.loads(tf(path, ['state', 'pull'], capture=True).stdout)
    before = pull()
    root = Path(journal_root or Path.home() / '.local/state/crange/router-ha-reconcile') / path.name
    if root.exists():
        for pending in sorted(root.glob('*/receipt.json')):
            receipt = json.loads(pending.read_text())
            if receipt.get('phase') == 'prepared':
                # No state push can occur before the durable submitting phase.
                # Keep the abandoned review but allow a fresh verified attempt.
                receipt['phase'] = 'aborted-before-submit'
                atomic_json(pending, receipt)
            if receipt.get('phase') not in ('verified', 'aborted-before-submit'):
                candidate = json.loads((pending.parent / 'candidate.json').read_text())
                if candidate_matches(candidate, before):
                    finish_metadata(path, pending.parent)
                    receipt['phase'] = 'verified'
                    atomic_json(pending, receipt)
                else:
                    raise PlacementError('Prior router state adoption is uncertain; inspect ' + str(pending))
    moves = prove_moves(before, registry, api)
    if not moves:
        return []
    candidate = relocated_snapshot(before, moves)
    metadata_before, metadata_after = placement_metadata(path, moves)
    transaction = root / (str(before['serial']) + '-' + digest(candidate)[:12] + '-' + uuid.uuid4().hex[:8])
    transaction.mkdir(mode=0o700, parents=True, exist_ok=False)
    atomic_json(transaction / 'before.json', before)
    atomic_json(transaction / 'candidate.json', candidate)
    if metadata_before is not None:
        atomic_json(transaction / 'placements-before.json', metadata_before)
        atomic_json(transaction / 'placements-after.json', metadata_after)
    receipt = {'phase': 'prepared', 'project_id': project, 'moves': moves,
               'before_sha256': digest(before), 'candidate_sha256': digest(candidate)}
    atomic_json(transaction / 'receipt.json', receipt)
    if (normalized_snapshot(pull()) != normalized_snapshot(before) or json.loads(registration.read_text()) != registry or
            prove_moves(before, registry, api) != moves):
        raise PlacementError('Router state, registration or HA assignment changed before adoption.')
    if metadata_before is not None and json.loads((path / 'crange-applied-placements.json').read_text()) != metadata_before:
        raise PlacementError('Router placement metadata changed before adoption; retain its journal.')
    receipt['phase'] = 'submitting'
    atomic_json(transaction / 'receipt.json', receipt)
    # Never use -force, disable locking, or alter another backend's lineage.
    tf(path, ['state', 'push', '-lock-timeout=30s', str(transaction / 'candidate.json')], capture=True)
    if not candidate_matches(candidate, pull()):
        raise PlacementError('Router state adoption needs reconciliation; retain its protected journal.')
    finish_metadata(path, transaction)
    receipt['phase'] = 'verified'
    atomic_json(transaction / 'receipt.json', receipt)
    return sorted(moves)


def retire_routers(path, vmids):
    """Fixed worker-to-root relay; accepts no commands, paths or shell text."""
    import socket
    project = int(path.name.split('-')[-1])
    registration = Path('/etc/crange/router-ha')/(path.name+'.json')
    if not registration.exists():return
    registry = json.loads(registration.read_text())
    wanted = sorted({r['vmid'] for r in registry['routers'].values()} & set(vmids))
    for vmid in wanted:
        request = {'command':'router-ha-retire','node':'proxmox4','project_id':project,'vmid':vmid}
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(65);client.connect('/run/crange-ct-read/socket')
            client.sendall(json.dumps(request).encode()+b'\n');raw=b''
            while not raw.endswith(b'\n') and len(raw)<=8192:
                part=client.recv(8193-len(raw))
                if not part:break
                raw+=part
        reply=json.loads(raw)
        result=json.loads(reply['value']) if set(reply)=={'value'} and isinstance(reply['value'],str) else None
        if result!={'state':'retired','project_id':project,'vmid':vmid}:
            raise PlacementError('Router HA retirement was not confirmed; retain the teardown plan.')
