"""Worker-only discovery and exact-plan placement. No HTTP-side scheduling."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from crange_catalog import validate_catalog, catalog_identity
from crange_placement import PlacementError, ReservationLedger

CONFIG = Path(os.environ.get('CRANGE_DISTRIBUTION_CONFIG', Path.home() / '.config/crange'))
DATA = Path(os.environ.get('CRANGE_DISTRIBUTION_DATA', '/var/lib/crange-distribution'))
KNOWN_NODES = {f'proxmox{i}' for i in range(1, 7)}
DEFAULT_POLICY = {'revision': 'conservative-v1', 'enabled': False,
                  # Proxmox6 is intentionally not enrolled until it joins the
                  # cluster and passes the same storage/network acceptance.
                  'allowed_nodes': [f'proxmox{i}' for i in range(1, 6)],
                  'allowed_storages': ['local-lvm', 'ssd-lvm', 'hdd-lvm'],
                  'max_inventory_age_seconds': 120, 'review_ttl_seconds': 3600,
                  'max_reviewed_plans': 2, 'max_load': 1.0,
                  'clone_mode': 'linked', 'copy_overhead_fraction': .1, 'nodes': {}, 'network_attestations': {}}
LIBRARY_STATUS = Path('/var/lib/crange-media-library/status.json')
ARCHIVE_STORAGES = ('crange-library', 'media-mirror1', 'media-mirror4', 'local')


def verified_archive_copies(catalog, status):
    """Add immutable catalog aliases for manifest-verified copies of stock archives.

    The copied rows remain bound to the same artifact hash and source node. The
    reservation then records the selected volume ID, and begin_apply checks that
    exact volume on that node before Terraform can create anything.
    """
    if not isinstance(status, dict) or not status.get('updated_at'):
        return catalog
    files = {(row.get('content'), row.get('name')): row for row in status.get('files', [])
             if isinstance(row, dict) and isinstance(row.get('name'), str)}
    result = copy.deepcopy(catalog)
    for image in result.get('images', {}).values():
        if image.get('source_type') != 'stock_archive' or image.get('kind') != 'container':
            continue
        digest = image.get('artifact_sha256')
        for replica in list(image.get('replicas', [])):
            source = replica.get('template_file_id', '')
            if not source or ':vztmpl/' not in source:
                continue
            name = source.split(':vztmpl/', 1)[1]
            row = files.get(('vztmpl', name), {})
            if (not isinstance(row.get('size'), (int, float)) or row['size'] <= 0 or
                    row.get('sha256') != digest):
                continue
            verified = {item.get('storage') for item in row.get('copies', [])
                        if isinstance(item, dict) and item.get('sha256') == digest}
            for storage in ARCHIVE_STORAGES:
                if storage not in verified:
                    continue
                volid = f'{storage}:vztmpl/{name}'
                if volid == source or any(r.get('node') == replica.get('node') and
                                          r.get('template_file_id') == volid
                                          for r in image.get('replicas', [])):
                    continue
                candidate = copy.deepcopy(replica)
                candidate.update(template_file_id=volid, datastore_id=storage,
                                 copy_verification={'storage': storage, 'sha256': digest})
                image.setdefault('replicas', []).append(candidate)
    return result


def read_settings():
    file = CONFIG / 'distribution-policy.json'
    policy = json.loads(file.read_text()) if file.exists() else copy.deepcopy(DEFAULT_POLICY)
    if not isinstance(policy, dict): raise PlacementError('Invalid distribution policy.')
    catalog_file = CONFIG / 'image-catalog.json'
    catalog = json.loads(catalog_file.read_text()) if catalog_file.exists() else {'schema_version': 1, 'images': {}}
    validate_catalog(catalog)
    try:
        status = json.loads(LIBRARY_STATUS.read_text())
    except (OSError, ValueError):
        status = {}
    catalog = verified_archive_copies(catalog, status)
    validate_catalog(catalog)
    return policy, catalog


def atomic_json(file, value, mode=0o600):
    file = Path(file); file.parent.mkdir(parents=True, exist_ok=True, mode=0o750 if mode == 0o640 else 0o700)
    temp = file.with_name(file.name + '.' + os.urandom(8).hex() + '.tmp')
    with temp.open('x') as stream:
        json.dump(value, stream, indent=2, sort_keys=True); stream.write('\n')
        stream.flush(); os.fsync(stream.fileno())
    temp.chmod(mode); temp.replace(file)


def placement_intent(node, preferred_node=''):
    nodes = KNOWN_NODES
    if node not in [*nodes, 'auto'] or preferred_node not in [*nodes, '']:
        raise PlacementError('Choose Automatic or an approved range node.')
    if node != 'auto' and preferred_node:
        raise PlacementError('A legacy node pin cannot also have a soft preference.')
    return ({'mode': 'auto', 'preferred_node': preferred_node} if node == 'auto'
            else {'mode': 'pin', 'pinned_node': node})


def intent_for(box, default_preferred_node=None):
    intent = copy.deepcopy(box.get('placement') or placement_intent(box.get('node_name', 'proxmox4')))
    if intent.get('mode') == 'auto' and 'preferred_node' not in intent and default_preferred_node:
        intent['preferred_node'] = default_preferred_node
    return intent


def _size_mb(value):
    match = re.search(r'(?:^|,)size=([0-9.]+)([KMGT])(?:,|$)', str(value))
    if not match: raise PlacementError('A guest disk has no defensible configured size.')
    return float(match[1]) * {'K': 1/1024, 'M': 1, 'G': 1024, 'T': 1024*1024}[match[2]]


def disk_allocations(config):
    result = {}
    for field, value in config.items():
        if not re.fullmatch(r'(?:scsi|sata|virtio|ide|efidisk|tpmstate|mp)\d+|rootfs', field): continue
        value = str(value)
        if 'media=cdrom' in value or ':cloudinit' in value or value.startswith('none'): continue
        volume = value.split(',')[0]
        if ':' not in volume or volume.startswith('/'): continue
        storage = volume.split(':')[0]
        result[storage] = result.get(storage, 0) + _size_mb(value) / 1024
    return result


NETWORK_CHECKS = frozenset({'vxlan_mtu', 'controller', 'guacamole', 'dns', 'internet_egress', 'team_isolation'})


def network_attestation_valid(attestation):
    """Evidence is node-specific; exact VNet membership remains mandatory."""
    return (attestation.get('verified') is True and attestation.get('mtu') == 1450
            and attestation.get('zone') == 'crange'
            and NETWORK_CHECKS <= set(attestation.get('passed_checks', [])))


def _api_flag(value):
    return value in (True, 1, '1')


def provider_container_placement_verified(policy, node_name):
    """Require both pinned-provider and node-specific disposable CT acceptance.

    A linked-cache proof only covers storage/backing semantics. It cannot prove
    Terraform's CT create/config network options work on the destination node.
    Keep the legacy boolean insufficient; admission needs a structured record
    tied to provider version, node API version, and exact acceptance job.
    """
    if policy.get('provider_version') != '0.104.0':
        return False
    proof = policy.get('nodes', {}).get(node_name, {}).get('provider_container_acceptance')
    if not isinstance(proof, dict):
        return False
    return (proof.get('provider_version') == '0.104.0'
            and proof.get('node') == node_name
            and isinstance(proof.get('node_version'), str)
            and proof['node_version'].startswith('pve-manager/')
            and proof.get('disposable_terraform_ct') is True
            and proof.get('network_config_verified') is True
            and isinstance(proof.get('job_id'), str) and bool(proof['job_id']))


def discover(api, policy, planned_networks=()):
    """Account every visible guest, including stopped guests and non-range guests.

    VM.Audit on all guests is required. Host memory.used is deliberately treated
    as a conservative upper bound on non-reclaimable use; it is NOT added to
    configured guest RAM. Template disks count, template CPU/RAM do not.
    """
    nodes = {n['node']: n for n in api.request('GET', '/nodes')}
    guests = api.request('GET', '/cluster/resources', {'type': 'vm'})
    try:
        cluster_storages = api.request('GET', '/cluster/resources', {'type': 'storage'})
    except Exception:
        # Detailed per-node storage discovery still gates every eligible node.
        cluster_storages = []
    # Node storage listings expose audit-safe status/content/type. The global
    # configuration index requires Datastore.Allocate on some PVE versions.
    storage_defs = {}
    vnets = {v['vnet'] for v in api.request('GET', '/cluster/sdn/vnets') if v.get('zone') == 'crange'}
    result = {'captured_at': time.time(), 'nodes': {}, 'guests': {}, 'quota_usage': {'global': {}}}
    # Placement exclusions must never hide existing commitments or quota use.
    discovery_nodes = (set(nodes) | set(policy['allowed_nodes']) |
                       set(policy.get('expected_cluster_nodes', [])) |
                       {guest['node'] for guest in guests})
    for name in sorted(discovery_nodes):
        if name not in nodes or nodes[name].get('status') != 'online':
            result['nodes'][name] = {'online': False, 'memory_mb': 0, 'cpu_cores': 0,
                'committed_memory_mb': 0, 'committed_cpu_cores': 0,
                'observed_memory_mb': 0, 'management_verified': False,
                'verified_networks': [], 'guest_types': [], 'storages': {}}
            # Cluster resources retains configured maxima for offline guests.
            # Charge these against global/project quotas; never offer the node
            # for placement. Missing maxima make the inventory incomplete.
            for guest in (g for g in guests if g['node'] == name):
                template = bool(int(guest.get('template', 0)))
                if not template and any(guest.get(key) is None for key in ('maxmem', 'maxcpu', 'maxdisk')):
                    raise PlacementError(f'Cannot account workloads on offline node {name}; configured maxima are missing.')
                memory = float(guest.get('maxmem', 0)) / 2**20
                cores = float(guest.get('maxcpu', 0))
                disk = float(guest.get('maxdisk', 0)) / 2**30
                if not template and (memory <= 0 or cores <= 0):
                    raise PlacementError(f'Cannot account workloads on offline node {name}; configured CPU/RAM maxima are invalid.')
                if not template:
                    result['nodes'][name]['committed_memory_mb'] += memory
                    result['nodes'][name]['committed_cpu_cores'] += cores
                    for scope in ['global', *re.findall(r'(?:^|;)p(\d{3})(?:;|$)', str(guest.get('tags', '')))]:
                        scope = str(int(scope)) if scope != 'global' else scope
                        usage = result['quota_usage'].setdefault(scope, {})
                        for key, amount in (('memory_mb', memory), ('cpu_cores', cores), ('disk_gb', disk)):
                            usage[key] = usage.get(key, 0) + amount
                result['guests'][str(guest['vmid'])] = {'node_name': name, 'kind': guest['type'],
                    'template': template, 'name': guest.get('name'), 'status': 'offline',
                    'tags': guest.get('tags', ''), 'memory_mb': memory, 'cpu_cores': cores,
                    'disks': {'offline': disk}}
            continue
        status = api.request('GET', f'/nodes/{name}/status')
        storage_summary = [row for row in cluster_storages if row.get('node') == name]
        storage_detail = name in policy['allowed_nodes']
        storage_error = False
        if storage_detail:
            try:
                storage_rows = api.request('GET', f'/nodes/{name}/storage')
            except Exception:
                storage_rows = storage_summary
                storage_detail = False
                storage_error = True
        else:
            storage_rows = storage_summary
        cpu = int(status['cpuinfo']['cpus'])
        attestation = policy.get('network_attestations', {}).get(name, {})
        fresh = network_attestation_valid(attestation)
        networks = vnets | {'craccess', 'crtrans'}
        if fresh: networks |= set(planned_networks)
        verified_networks = (set(attestation.get('verified_networks', [])) & networks) if fresh else set()
        node = {'online': True, 'draining': policy.get('nodes', {}).get(name, {}).get('draining', False),
                'memory_mb': status['memory']['total']/2**20, 'cpu_cores': cpu,
                'committed_memory_mb': 0, 'committed_cpu_cores': 0,
                'observed_memory_mb': status['memory']['used']/2**20,
                'load': float(status.get('loadavg', [0])[0])/cpu,
                'observed_cpu_cores': float(status['cpu']) * cpu if status.get('cpu') is not None else None,
                'architectures': ['amd64'], 'guest_types': ['vm'] + (['container'] if provider_container_placement_verified(policy, name) else []),
                'features': policy.get('nodes', {}).get(name, {}).get('verified_features', []),
                'networks': sorted(networks), 'verified_networks': sorted(verified_networks),
                'management_verified': fresh and attestation.get('management_verified') is True,
                'storage_inventory_error': storage_error,
                'storages': {}}
        for row in storage_rows:
            sid = row['storage']; definition = storage_defs.get(sid, {})
            content = str(row.get('content', definition.get('content', ''))).split(',')
            total_bytes = row.get('total', row.get('maxdisk', 0))
            used_bytes = row.get('disk', 0)
            fallback_free = max(0, total_bytes - used_bytes) if isinstance(total_bytes, (int, float)) and isinstance(used_bytes, (int, float)) else 0
            free_bytes = row.get('avail', fallback_free)
            valid_capacity = (isinstance(total_bytes, (int, float)) and not isinstance(total_bytes, bool)
                and isinstance(free_bytes, (int, float)) and not isinstance(free_bytes, bool)
                and math.isfinite(total_bytes) and math.isfinite(free_bytes)
                and total_bytes > 0 and 0 <= free_bytes <= total_bytes)
            locality_known = row.get('shared') in (False, True, 0, 1, '0', '1')
            # Unknown locality must never authorize a local linked clone.
            shared = _api_flag(row['shared']) if locality_known else True
            complete = (storage_detail and valid_capacity and 'avail' in row
                and locality_known and bool(set(content) & {'images', 'rootdir'}))
            snapshots = complete and not shared and sid in policy.get('nodes', {}).get(name, {}).get('snapshot_storages', [])
            node['storages'][sid] = {'available': complete and _api_flag(row.get('active')) and _api_flag(row.get('enabled', 1)),
                'total_gb': total_bytes/2**30 if valid_capacity else 0,
                'free_gb': free_bytes/2**30 if valid_capacity else 0,
                'inventory_complete': complete, 'locality_known': locality_known,
                'committed_gb': 0, 'shared': shared, 'guest_types': (['vm'] if 'images' in content else []) +
                (['container'] if 'rootdir' in content else []),
                'clone_modes': ['full', 'linked'] if snapshots else ['full'],
                'snapshots_verified': snapshots}
        result['nodes'][name] = node
    for guest in guests:
        name = guest['node']
        if name not in result['nodes'] or not result['nodes'][name].get('online'): continue
        kind = guest['type']; vmid = int(guest['vmid'])
        config = api.request('GET', f'/nodes/{name}/{kind}/{vmid}/config')
        node = result['nodes'][name]; template = bool(int(config.get('template', 0)))
        memory = float(config.get('memory', 0)); cores = float(config.get('cores', 1))*float(config.get('sockets', 1))
        disks = disk_allocations(config)
        if not template:
            node['committed_memory_mb'] += memory; node['committed_cpu_cores'] += cores
        for sid, amount in disks.items():
            if sid in node['storages']: node['storages'][sid]['committed_gb'] += amount
        result['guests'][str(vmid)] = {'node_name': name, 'kind': kind, 'template': template,
            'name': config.get('name', config.get('hostname')), 'status': guest.get('status', 'unknown'), 'tags': config.get('tags', ''),
            'memory_mb': memory, 'cpu_cores': cores, 'disks': disks}
        if not template:
            for scope in ['global', *re.findall(r'(?:^|;)p(\d{3})(?:;|$)', str(config.get('tags', '')))]:
                scope = str(int(scope)) if scope != 'global' else scope
                usage = result['quota_usage'].setdefault(scope, {})
                usage['memory_mb'] = usage.get('memory_mb', 0) + memory
                usage['cpu_cores'] = usage.get('cpu_cores', 0) + cores
                usage['disk_gb'] = usage.get('disk_gb', 0) + sum(disks.values())
    return result


def applied_resources(path):
    from crange import tf_run
    if not (path / 'terraform.tfstate').exists(): return {}
    state = json.loads(tf_run(path, ['show', '-json'], capture=True).stdout)
    result = {}
    def visit(module):
        for resource in module.get('resources', []):
            if resource.get('type') not in ('proxmox_virtual_environment_vm', 'proxmox_virtual_environment_container'): continue
            address = resource['address']; values = resource['values']
            prefix = address.split('.proxmox_virtual_environment_')[0]
            patterns = [('module.project_router', 'router:project'), ('module.staging_router', 'router:staging')]
            for start, key in patterns:
                if prefix == start: result[key] = values
            for module_name, kind in [('team_router','router:team'),('custom_router','router:custom'),('jumpbox','jumpbox'),('scenario_box','vm'),('scenario_container','container')]:
                match = re.fullmatch(r'module\.'+module_name+r'\["([^"\\]+)"\]', prefix)
                if match: result[kind+':'+match[1]] = values
        for child in module.get('child_modules', []): visit(child)
    visit(state.get('values', {}).get('root_module', {}))
    return result


def declarations(path, cfg):
    from crange import templates_for, container_templates_for
    vm_templates = templates_for(path); ct_templates = container_templates_for(path)
    rows = []
    def append(key, kind, box, reference, networks):
        image = box.get('image') or reference.get('image_id')
        rows.append({'resource_key': key, 'kind': kind, 'box': box, 'reference': reference,
                     'image': image, 'networks': networks,
                     'default_preferred_node': 'proxmox4' if key.startswith('jumpbox:') else None})
    router_node = cfg.get('router_node', 'proxmox4')
    router = {'node_name': router_node, 'placement': cfg.get('router_placement') or placement_intent(router_node), 'cores':1,'memory_mb':512}
    reference = {'image_id': cfg.get('router_image', 'debian-13-router-v1')}
    p = f'p{int(cfg["project_id"]):03d}'
    append('router:project','container',router,reference,['crtrans',p+'tr'])
    append('router:staging','container',router,reference,[p+'tr',p+'t254'])
    for team in cfg['team_ids']: append('router:team:'+str(team),'container',router,reference,[p+'tr',p+f't{int(team):03d}'])
    for key, box in cfg.get('custom_networks', {}).items():
        append('router:custom:'+key,'container',box,reference,[p+f't{int(box["owner_team_id"]):03d}',p+f'c{int(box["network_id"]):03d}'])
    for kind, field, approved in [('jumpbox','jump_boxes',vm_templates),('vm','scenario_boxes',vm_templates),('container','scenario_containers',ct_templates)]:
        for key, box in cfg.get(field, {}).items():
            ref = approved.get(box.get('template','approved-base'), {})
            nid = int(box.get('network_id',box['team_id']))
            network = p+('c' if box.get('network_key') else 't')+f'{nid:03d}'
            append(kind+':'+key,'container' if kind=='container' else 'vm',box,ref,[network])
    return rows


def template_profile(path, row):
    try:
        base = json.loads((path / 'template-profile.json').read_text())
    except (OSError, json.JSONDecodeError):
        base = {}
    return {**base, **(row['reference'].get('profile') or {})}


def freeze(row, live, previous=None):
    """Keep actual placements regardless of deleted preferences or new defaults."""
    source = (previous or {}).get('resolved') or previous or {}
    values = {key:copy.deepcopy(source[key]) for key in ('node_name','datastore_id','template_id','template_node','template_file_id','clone_mode','verified_local_cache') if key in source}
    values['node_name'] = live['node_name']
    clone = live.get('clone') or []
    if clone:
        values.update(template_id=clone[0].get('vm_id'), template_node=clone[0].get('node_name') or live['node_name'],
                      clone_mode='full' if clone[0].get('full') else 'linked')
    disks = live.get('disk') or live.get('disk_device') or []
    if disks: values['datastore_id'] = disks[0]['datastore_id']
    initialization = live.get('operating_system') or []
    if initialization and initialization[0].get('template_file_id'):
        values['template_file_id'] = initialization[0]['template_file_id']
        values['clone_mode'] = 'archive'
    values.setdefault('datastore_id','local-lvm')
    return {'resource_key': row['resource_key'], 'resolved': values, 'reason':'Existing guest placement retained', 'existing':True, 'image': row.get('image') or ''}


def prepare(path, cfg, plan_id, owner, api=None):
    from crange import ProxmoxAPI, validate_project_quota
    validate_project_quota(path,cfg)
    policy,catalog = read_settings()
    if not policy.get('enabled'):
        boxes = [box for field in ('jump_boxes', 'scenario_boxes', 'scenario_containers', 'custom_networks') for box in cfg.get(field, {}).values()]
        if cfg.get('router_node') == 'auto' or any(intent_for(box).get('mode') == 'auto' for box in boxes):
            raise PlacementError('Automatic distribution is not enabled: publish verified images and complete network acceptance first.')
        return None
    rows = declarations(path,cfg)
    DATA.mkdir(parents=True,exist_ok=True,mode=0o700)
    existing=applied_resources(path); placements=[];requests=[]
    record_file = path / 'crange-applied-placements.json'
    applied_record = json.loads(record_file.read_text()) if record_file.exists() else {}
    for row in rows:
        key=row['resource_key']
        if key in existing:
            live = existing[key]
            current_cpu = int(live.get('cpu', [{}])[0].get('cores', live.get('cores', 1)))
            current_memory = int(live.get('memory', [{}])[0].get('dedicated', live.get('memory_mb', 512)))
            desired = row['box']
            clone = live.get('clone') or []
            previous = applied_record.get(key, {})
            # Approval aliases may advance for future guests. Existing applied
            # resources change image only through an explicit desired-state edit.
            replacement = (previous.get('declared_template') is not None and previous['declared_template'] != desired.get('template')) or bool(desired.get('image') and previous.get('image_id') and desired['image'] != previous['image_id'])
            if not replacement and previous.get('image_id'):
                row = copy.deepcopy(row)
                row['image'] = previous['image_id']
            elif not replacement and not desired.get('image'):
                # A legacy physical source is not proof of the alias's current
                # immutable version; retain it without inventing that identity.
                row = copy.deepcopy(row)
                row['image'] = None
            if not replacement and int(desired.get('cores', current_cpu)) <= current_cpu and int(desired.get('memory_mb', current_memory)) <= current_memory:
                frozen = freeze(row,live,previous)
                frozen['declared_template'] = desired.get('template')
                placements.append(frozen);continue
            row = copy.deepcopy(row)
            row['box']['placement'] = {'mode': 'pin', 'pinned_node': live['node_name']}
            row['box']['node_name'] = live['node_name']
            if not replacement:row['retained'] = freeze(row, live, previous)['resolved']
        image=catalog['images'].get(row['image'])
        if not image:raise PlacementError(f'{key}: image has no verified logical version. Run image publication/preparation before planning.')
        size=image.get('disk_gb')
        if not isinstance(size,(int,float)) or isinstance(size,bool) or size<=0:raise PlacementError('Published image disk allocation is missing.')
        box=row['box'];profile=template_profile(path,row)
        requests.append({'resource_key':key,'project_id':int(cfg['project_id']),
            'kind':row['kind'],'image':row['image'],'placement':intent_for(box,row.get('default_preferred_node')),'networks':row['networks'],
            'memory_mb':int(box.get('memory_mb',512 if row['kind']=='container' else 4096)),
            'cpu_cores':int(box.get('cores',1 if row['kind']=='container' else 2)),
            'cpu_type':profile.get('cpu_type','host') if row['kind']=='vm' else 'host',
            'required_features':list(profile.get('required_features',[])),
            'disk_gb':max(size,8 if row['kind']=='container' else size)})
        if row.get('retained'):
            for field in ('datastore_id','template_id','template_file_id','clone_mode'):
                if row['retained'].get(field) is not None:requests[-1]['resolved_'+field]=row['retained'][field]
    inventory=discover(api or ProxmoxAPI(),policy,{n for row in rows for n in row['networks']})
    from crange_proxmox_compat import reviewed_identity
    provider_compat = reviewed_identity()
    ledger=ReservationLedger(DATA/'reservations.sqlite3')
    try:
        if requests:placements+=ledger.reserve_batch(requests,inventory,catalog,policy,int(cfg['project_id']),plan_id,owner)
    finally:
        ledger.close()
    for placement in placements:
        row = next(row for row in rows if row['resource_key'] == placement['resource_key'])
        placement['declared_template'] = row['box'].get('template')
    return {'provider_compat': provider_compat, 'plan_id':plan_id,'placements':placements,'requests':requests,'policy_revision':policy['revision'],
            'policy_sha256':hashlib.sha256(json.dumps(policy,sort_keys=True).encode()).hexdigest(),
            'catalog_sha256':catalog_identity(catalog),'reservation_ids':[p['reservation_id'] for p in placements if p.get('reservation_id')]}


def tf_overrides(prepared):
    return {'resolved_placements': {p['resource_key']:{**p['resolved'], 'image_id': p.get('image', ''), **({'disk_gb': next(r['disk_gb'] for r in prepared['requests'] if r['resource_key'] == p['resource_key'])} if p.get('resources') else {})} for p in prepared['placements']}}


def begin_apply(path, prepared, api=None):
    from crange import ProxmoxAPI
    policy,catalog=read_settings()
    if policy.get('enabled') and not prepared:
        raise PlacementError('This plan predates capacity reservations; review a fresh plan.')
    if not prepared:return
    from crange_proxmox_compat import reviewed_identity
    if reviewed_identity() != prepared.get('provider_compat', {'enabled': False}):
        raise PlacementError('Proxmox compatibility adapter changed after review; restage.')
    if hashlib.sha256(json.dumps(policy,sort_keys=True).encode()).hexdigest()!=prepared['policy_sha256']:
        raise PlacementError('Placement policy changed after review; restage.')
    if catalog_identity(catalog)!=prepared['catalog_sha256']:
        raise PlacementError('Published image catalog changed after review; restage.')
    api=api or ProxmoxAPI()
    networks={n for request in prepared['requests'] for n in request['networks']}
    inventory=discover(api,policy,networks)
    for placement in prepared['placements']:
        resolved = placement['resolved']
        node = inventory['nodes'].get(resolved['node_name'], {})
        if not node.get('online') or node.get('draining'):
            raise PlacementError('Reviewed node is offline or draining; restage.')
        if not node.get('storages', {}).get(resolved['datastore_id'], {}).get('available'):
            raise PlacementError('Reviewed datastore is unavailable; restage.')
        if placement.get('existing'):continue
        image=catalog['images'][placement['image']]
        if image.get('source_type')=='stock_archive':
            media=api.request('GET',f'/nodes/{resolved["node_name"]}/storage/{resolved["template_file_id"].split(":")[0]}/content',{'content':'vztmpl'})
            if not any(m.get('volid')==resolved['template_file_id'] and m.get('size',0)>0 for m in media):
                raise PlacementError('Reviewed OS archive is unavailable; restage.')
        else:
            kind='qemu' if image['kind']=='vm' else 'lxc'
            actual=api.request('GET',f'/nodes/{resolved["template_node"]}/{kind}/{resolved["template_id"]}/config')
            if not int(actual.get('template',0)) or 'crange-image' not in str(actual.get('tags','')).split(';'):
                raise PlacementError('Reviewed image replica identity is no longer valid; restage.')
    if prepared['requests']:
        ledger=ReservationLedger(DATA/'reservations.sqlite3')
        try:
            ledger.mark_running(prepared['plan_id'],requests=prepared['requests'],inventory=inventory,catalog=catalog,policy=policy)
        finally:ledger.close()


def finish_apply(path, prepared, *, succeeded, api=None):
    """Failed/uncertain applies retain reservations until explicit reconciliation."""
    if not prepared:return
    from crange import ProxmoxAPI
    api=api or ProxmoxAPI()
    if not succeeded:return
    actual=applied_resources(path);record={}
    for placement in prepared['placements']:
        key=placement['resource_key']
        if key not in actual:raise PlacementError('Provider allocation missing after apply; reservations retained.')
        values=actual[key];resolved=placement['resolved']
        if values['node_name']!=resolved['node_name']:raise PlacementError('Applied node differs from reviewed placement; reservations retained.')
        record[key]={**resolved,'guest_id':int(values['vm_id']),'image_id':placement.get('image',''), 'declared_template':placement.get('declared_template')}
        kind='qemu' if key.startswith(('vm:','jumpbox:')) else 'lxc'
        identity=api.request('GET',f'/nodes/{resolved["node_name"]}/{kind}/{record[key]["guest_id"]}/config')
        if f'p{int(path.name.split("-")[-1]):03d}' not in str(identity.get('tags','')).split(';'):
            raise PlacementError('Applied guest ownership mismatch; reservations retained.')
    atomic_json(path/'crange-applied-placements.json',record,0o640)
    if prepared['requests']:
        for node in {p['resolved']['node_name'] for p in prepared['placements']}:
            ids = {int(v['guest_id']) for v in record.values() if v['node_name'] == node}
            if any(int(task.get('id') or 0) in ids for task in api.request('GET',f'/nodes/{node}/tasks',{'running':1}) if str(task.get('id') or '').isdigit()):
                raise PlacementError('Provider tasks still running; reservations retained for reconciliation.')
        ledger=ReservationLedger(DATA/'reservations.sqlite3')
        try:ledger.reconcile_job(prepared['plan_id'],operation_terminal=True,allocations_reconciled=True)
        finally:ledger.close()


def abandon_plan(prepared):
    """Planning never provisions guests; release only this operation's preview."""
    if not prepared or not prepared.get('requests'):return
    ledger=ReservationLedger(DATA/'reservations.sqlite3')
    try:ledger.reconcile_job(prepared['plan_id'],operation_terminal=True,allocations_reconciled=True)
    finally:ledger.close()


def print_review(prepared):
    if not prepared:return
    print('\nPlacement review:')
    for row in prepared['placements']:
        r=row['resolved'];print(f'  {row["resource_key"]}: {r["node_name"]} / {r["datastore_id"]}; {row["reason"]}')
        if not row.get('existing'):
            print(f'    image={row["image"]} reservation={row["reservation_id"]}; remaining headroom={json.dumps(row["headroom"],sort_keys=True)}')


@contextmanager
def node_locks(nodes, library=False):
    DATA.mkdir(parents=True,exist_ok=True,mode=0o700); streams=[]
    try:
        for name in sorted(set(nodes)):
            if name not in KNOWN_NODES:raise PlacementError('Invalid provisioning node lock.')
            stream=(DATA/(name+'.lock')).open('a');fcntl.flock(stream,fcntl.LOCK_EX);streams.append(stream)
        if library:
            stream=(DATA/'library.lock').open('a');fcntl.flock(stream,fcntl.LOCK_EX);streams.append(stream)
        yield
    finally:
        for stream in reversed(streams):fcntl.flock(stream,fcntl.LOCK_UN);stream.close()


def required_preparations(path, cfg, *, api=None):
    """Pure cache target planning; no remote image or shared reservation mutation.

    Capacity trials include the cache base/probe/transfer and full guest growth.
    The worker re-admits each selected fill atomically immediately before copying.
    """
    import tempfile
    from crange import ProxmoxAPI
    from crange_distribution import required_capacity
    policy, catalog = read_settings()
    if not policy.get('enabled'):
        raise PlacementError('Automatic distribution is not enabled; complete cache/network acceptance first.')
    api = api or ProxmoxAPI()
    rows = declarations(path, cfg)
    existing = applied_resources(path)
    record_file = path / 'crange-applied-placements.json'
    prior = json.loads(record_file.read_text()) if record_file.exists() else {}
    new = []
    for row in rows:
        key = row['resource_key']; previous = prior.get(key, {})
        explicit_change = previous.get('declared_template') is not None and previous['declared_template'] != row['box'].get('template')
        explicit_change |= bool(row['box'].get('image') and previous.get('image_id') and row['box']['image'] != previous['image_id'])
        if key in existing and not explicit_change:
            continue
        new.append(row)
    inventory = discover(api, policy, {n for row in rows for n in row['networks']})
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    ledger = ReservationLedger(DATA/'reservations.sqlite3')
    try:
        pending = ledger.active_reservations()
    finally:
        ledger.close()
    working = copy.deepcopy(inventory)
    def consume(node_name, storage_id, resources):
        node=working['nodes'][node_name];storage=node['storages'][storage_id]
        node['committed_memory_mb'] += resources['memory_mb']
        node['observed_memory_mb'] += resources['memory_mb']
        node['committed_cpu_cores'] += resources['cpu_cores']
        storage['committed_gb'] += resources['disk_gb'];storage['free_gb'] -= resources['disk_gb']
    for reservation in pending:
        if reservation['node_name'] in working['nodes'] and reservation['datastore_id'] in working['nodes'][reservation['node_name']]['storages']:
            consume(reservation['node_name'],reservation['datastore_id'],reservation)
    fills=[];planned_caches=set()
    for row in sorted(new,key=lambda r:(r['image'] or '',r['resource_key'])):
        image=catalog['images'].get(row['image'])
        if not image:raise PlacementError(f'{row["resource_key"]}: publish an approved immutable image before staging.')
        if isinstance(image.get('disk_gb'),bool) or not isinstance(image.get('disk_gb'),(int,float)) or not 0 < image['disk_gb'] < float('inf'):
            raise PlacementError('Published image disk allocation is missing.')
        box=row['box'];intent=intent_for(box,row.get('default_preferred_node'));candidates=[];failures={}
        demand={'memory_mb':int(box.get('memory_mb',512 if row['kind']=='container' else 4096)),
                'cpu_cores':int(box.get('cores',1 if row['kind']=='container' else 2)),
                'disk_gb':image['disk_gb']*(1+policy.get('copy_overhead_fraction',.1))}
        for node_name,node in sorted(working['nodes'].items()):
            if row['kind'] not in node.get('guest_types', []):continue
            if intent.get('pinned_node',node_name)!=node_name or node_name not in intent.get('allowed_nodes',policy['allowed_nodes']):continue
            if not node.get('management_verified') or not set(row['networks'])<=set(node.get('verified_networks',[])):continue
            if not set(image.get('required_features',[]))<=set(node.get('features',[])):continue
            for storage_id,storage in sorted(node.get('storages',{}).items()):
                replicas=[r for r in image.get('replicas',[]) if r['state']=='ready' and r['node']==node_name and (r.get('template_file_id') or (r['datastore_id']==storage_id and 'linked' in r.get('clone_modes_verified',[]) and storage.get('snapshots_verified') and not storage.get('shared')))]
                already=(row['image'],node_name,storage_id) in planned_caches
                cache_demand={'memory_mb':0,'cpu_cores':0,'disk_gb':0}
                if not replicas and not already:
                    source=next((r for r in image.get('replicas',[]) if r['state']=='ready' and r.get('template_id')),None)
                    config=api.request('GET',f'/nodes/{source["node"]}/{"qemu" if row["kind"]=="vm" else "lxc"}/{source["template_id"]}/config') if source else {}
                    cache_demand=required_capacity(image,source_config=config)
                total={key:demand[key]+cache_demand[key] for key in demand}
                profile=template_profile(path,row)
                if row['kind']=='vm' and profile.get('cpu_type','host')=='host':
                    feature=image.get('host_compatibility_feature')
                    if not feature or feature not in node.get('features',[]):
                        failures[node_name+'/'+storage_id]='Host CPU compatibility is not verified on this node';continue
                try:
                    with tempfile.TemporaryDirectory(prefix='crange-cache-capacity-') as directory:
                        trial=ReservationLedger(Path(directory)/'trial.sqlite3')
                        try:
                            allocation=trial.reserve_preparation(project_id=int(cfg['project_id']),job_id='trial',owner='stage-preview',image_id=row['image'],catalog=catalog,node_name=node_name,datastore_id=storage_id,capacity={**total,'clone_mode':'linked' if not replicas or not replicas[0].get('template_file_id') else 'full'},inventory=working,policy=policy)
                        finally:trial.close()
                except PlacementError as exc:
                    failures[node_name+'/'+storage_id]=str(exc);continue
                score=sum(total[k]/max(allocation['headroom'][k]+total[k],.01) for k in total)/3
                if intent.get('preferred_node')==node_name:score-=min(policy.get('preference_bonus',.05),.1)
                candidates.append((score,node_name,storage_id,not replicas and not already,cache_demand,total))
        if not candidates:raise PlacementError(f'{row["resource_key"]}: no safe cache-plus-guest target: {json.dumps(failures,sort_keys=True)}')
        _,node_name,storage_id,missing,cache_demand,total=min(candidates,key=lambda x:x[:3])
        consume(node_name,storage_id,total)
        if missing:
            fills.append({'image':row['image'],'node':node_name,'storage':storage_id,'additional_capacity':demand,'resource_keys':[row['resource_key']]})
            planned_caches.add((row['image'],node_name,storage_id))
        elif (row['image'],node_name,storage_id) in planned_caches:
            fill=next(f for f in fills if (f['image'],f['node'],f['storage'])==(row['image'],node_name,storage_id))
            for key in demand:fill['additional_capacity'][key]+=demand[key]
            fill['resource_keys'].append(row['resource_key'])
    return fills
