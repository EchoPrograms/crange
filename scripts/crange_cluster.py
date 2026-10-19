"""Privileged cluster refresh and unprivileged sanitized snapshot reader.

HTTP handlers only read the published snapshot. Credentialed discovery and
reservation queries run as fixed controller jobs. Cluster data is admin-only.
"""
import json
import hashlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from crange import ProxmoxAPI
from crange_placement import ReservationLedger, capacity_window
import crange_scheduling as scheduling


SNAPSHOT_NAME = 'cluster-inventory.json'


def expand_capacity_policy(cpu_ratio=5.0, memory_ratio=2.0, disk_ratio=2.0):
    if (type(cpu_ratio) not in (int,float) or not 1 <= cpu_ratio <= 6 or
            type(memory_ratio) not in (int,float) or not 1 <= memory_ratio <= 2 or
            type(disk_ratio) not in (int,float) or not 1 <= disk_ratio <= 2):
        raise ValueError('Commitment limits are outside the reviewed CPU 1–6, RAM 1–2, disk 1–2 bounds.')
    from crange_scheduling import CONFIG
    from crange_network_publish import policy_write_lock
    with policy_write_lock(CONFIG/'distribution-policy.json'):
        return _expand_capacity_policy(cpu_ratio,memory_ratio,disk_ratio)


def _expand_capacity_policy(cpu_ratio=5.0, memory_ratio=2.0, disk_ratio=2.0):
    """Set bounded administrator commitment ceilings without changing reserves."""
    import copy
    from crange_catalog import validate_catalog
    from crange_placement import capacity_window
    from crange import ProxmoxAPI
    from crange_scheduling import CONFIG, discover, read_settings
    policy, catalog = read_settings()
    validate_catalog(catalog)
    expected = {'cpu_overcommit_ratio': float(cpu_ratio),
                'memory_overcommit_ratio': float(memory_ratio),
                'local_linked_disk_overcommit_ratio': float(disk_ratio)}
    if (type(cpu_ratio) not in (int, float) or not 1 <= cpu_ratio <= 6 or
            type(memory_ratio) not in (int, float) or not 1 <= memory_ratio <= 2 or
            type(disk_ratio) not in (int, float) or not 1 <= disk_ratio <= 2):
        raise ValueError('Commitment limits are outside the reviewed CPU 1–6, RAM 1–2, disk 1–2 bounds.')
    policy_path = CONFIG / 'distribution-policy.json'
    policy_sha256 = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    initial_revision = 'mhl-cluster-cpu-reserve-20261008'
    if not isinstance(policy.get('revision'), str) or not policy['revision']:
        raise ValueError('Placement policy has no valid revision.')
    for node, settings in policy.get('nodes', {}).items():
        if any(key in settings for key in expected):
            raise ValueError(f'{node} has a node-specific commitment limit; stop for review.')
    baseline = {'cpu_overcommit_ratio': 3.5, 'memory_overcommit_ratio': 2.0,
                'local_linked_disk_overcommit_ratio': 1.5}
    if policy.get('revision') == initial_revision:
        if (policy_sha256 != 'bb1328bf74b86d5971333cd19590c011129f820b67b55fb444e5ffb649cce81b' or
                any(policy.get(key) != value for key, value in baseline.items())):
            raise ValueError('Protected placement policy differs from the reviewed baseline; stop for review.')
    if policy.get('revision') != initial_revision:
        current = {key: policy.get(key) for key in expected}
        if (current['cpu_overcommit_ratio'] is None or not 1 <= current['cpu_overcommit_ratio'] <= 6 or
                current['memory_overcommit_ratio'] is None or not 1 <= current['memory_overcommit_ratio'] <= 2 or
                current['local_linked_disk_overcommit_ratio'] is None or
                not 1 <= current['local_linked_disk_overcommit_ratio'] <= 2):
            raise ValueError('Existing capacity limits are outside the reviewed range; stop for review.')
    # Compute before/after capacity on the same fresh provider observation, so
    # the response makes the effect of the fixed controls reviewable.
    inventory = discover(ProxmoxAPI(), policy)
    def headroom(selected_policy):
        result = {}
        for name, node in inventory.get('nodes', {}).items():
            if not node.get('online'):
                continue
            budget = capacity_window(node, selected_policy, name)
            result[name] = {'cpu_cores': round(budget['cpu_cores'], 2),
                            'memory_mb': round(budget['memory_mb'], 2)}
            storage = node.get('storages', {}).get('local-lvm')
            if storage and storage.get('available', True) and storage.get('inventory_complete', True):
                disk = capacity_window(node, selected_policy, name, storage,
                                       selected_policy.get('clone_mode', 'linked'))
                result[name]['local_lvm_disk_gb'] = round(disk['disk_gb'], 2)
        return result
    before = headroom(policy)
    updated = copy.deepcopy(policy)
    updated.update(expected)
    def token(value):
        return str(int(value)) if int(value) == value else str(value).replace('.', 'p')
    updated['revision'] = 'capacity-v20261009-cpu' + token(cpu_ratio) + '-ram' + token(memory_ratio) + '-disk' + token(disk_ratio)
    # Host reserves and memory policy are intentionally carried through verbatim.
    if policy == updated:
        return {'state': 'already-current', 'revision': updated['revision'], 'limits': expected,
                'before_headroom': before, 'after_headroom': before,
                'policy_path': str(policy_path), 'policy_sha256': policy_sha256,
                'physical_disk_reserve_unchanged': True, 'memory_reserve_unchanged': True}
    after = headroom(updated)
    if hashlib.sha256(policy_path.read_bytes()).hexdigest() != policy_sha256:
        raise ValueError('Placement policy changed during capacity review; refresh and retry.')
    scheduling.atomic_json(policy_path, updated, mode=0o600)
    return {'state': 'updated', 'previous_revision': policy['revision'],
            'revision': updated['revision'], 'limits': expected,
            'before_headroom': before, 'after_headroom': after,
            'policy_path': str(policy_path), 'previous_policy_sha256': policy_sha256,
            'physical_disk_reserve_unchanged': True, 'ram_overcommit_unchanged': True,
            'allowed_nodes_unchanged': True, 'storage_approval_unchanged': True}


def set_memory_reserve(node_name, minimum_mb, fraction, expected_revision):
    """Admin policy edit only; leave guest sizes, ratios and storage intact."""
    import copy
    import uuid
    from crange import ProxmoxAPI
    from crange_scheduling import CONFIG, discover, read_settings
    from crange_placement import capacity_window
    from crange_network_publish import policy_write_lock
    if (node_name not in {f'proxmox{i}' for i in range(1,7)}
            or type(minimum_mb) is not int or not 2048 <= minimum_mb <= 32768
            or type(fraction) not in (int,float) or not .1 <= fraction <= .5):
        raise ValueError('Choose an enrolled node, at least 2 GB host RAM, and a 10–50% reserve.')
    path = CONFIG / 'distribution-policy.json'
    with policy_write_lock(path):
        policy, _ = read_settings()
        if policy.get('revision') != expected_revision:
            raise ValueError('Placement policy changed; refresh the cluster snapshot before saving reserves.')
        original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        inventory = discover(ProxmoxAPI(), policy)
        node = inventory.get('nodes', {}).get(node_name, {})
        if node_name not in policy.get('allowed_nodes', []) or not node.get('online'):
            raise ValueError('Memory reserves can be edited only for an enrolled online node.')
        physical = node.get('memory_mb', 0)
        if max(minimum_mb, physical * fraction) > physical * .5:
            raise ValueError('The requested host reserve exceeds half this node’s physical RAM.')
        before = capacity_window(node, policy, node_name)
        updated = copy.deepcopy(policy)
        settings = updated.setdefault('nodes', {}).setdefault(node_name, {})
        settings.update(memory_reserve_mb=minimum_mb, memory_reserve_fraction=float(fraction))
        after = capacity_window(node, updated, node_name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != original_hash:
            raise ValueError('Placement policy changed during reserve review; refresh and retry.')
        updated['revision'] = 'memory-reserve-' + uuid.uuid4().hex
        scheduling.atomic_json(path, updated, mode=0o600)
        return {'node':node_name,'revision':updated['revision'],
                'minimum_host_memory_gb':minimum_mb/1024,'host_memory_fraction':fraction,
                'before_headroom_gb':round(before['memory_mb']/1024,2),
                'after_headroom_gb':round(after['memory_mb']/1024,2),
                'guest_sizes_unchanged':True,'commitment_ratios_unchanged':True,
                'storage_policy_unchanged':True,'network_acceptance_unchanged':True}


def inventory_snapshot(*, admin=False):
    if not admin:
        raise PermissionError('Cluster inventory requires platform administrator access.')
    path = scheduling.DATA / SNAPSHOT_NAME
    if not path.exists():
        return {'captured_at': None, 'nodes': [], 'guests': [],
                'stale': True, 'freshness': {'stale': True, 'reason': 'Run the fixed cluster refresh job'},
                'automatic': {'enabled': False, 'blockers': ['Inventory has not been refreshed']}}
    snapshot = json.loads(path.read_text())
    error_path = scheduling.DATA / 'cluster-refresh-error.json'
    if error_path.exists():
        snapshot['refresh_error'] = json.loads(error_path.read_text())
    age = time.time() - snapshot['captured_at']
    snapshot['stale'] = age > snapshot['max_inventory_age_seconds'] or age < -5 or bool(snapshot.get('refresh_error'))
    snapshot['freshness'] = {'age_seconds': max(0, age),
        'stale': snapshot['stale']}
    return snapshot


def _refresh_inventory_snapshot(*, api=None):
    api = api or ProxmoxAPI()
    policy, catalog = scheduling.read_settings()
    raw = scheduling.discover(api, policy)
    scheduling.DATA.mkdir(parents=True, exist_ok=True, mode=0o750)
    ledger = ReservationLedger(scheduling.DATA / 'reservations.sqlite3')
    try: reservations = ledger.active_reservations()
    finally: ledger.close()
    nodes, guests, global_blockers = [], [], []
    replicas = {r.get('template_id'): (image_id, r) for image_id, image in catalog['images'].items()
                for r in image['replicas'] if r.get('template_id') is not None}
    try:
        media_status = json.loads(scheduling.LIBRARY_STATUS.read_text())
    except (OSError, ValueError):
        media_status = {}
    if not isinstance(media_status, dict):
        media_status = {}
    try:
        status_updated = datetime.fromisoformat(str(media_status.get('updated_at', '')).replace('Z', '+00:00'))
        if status_updated.tzinfo is None:
            status_updated = status_updated.replace(tzinfo=timezone.utc)
        media_manifest_fresh = 0 <= time.time() - status_updated.timestamp() <= 86400
    except (TypeError, ValueError, OverflowError):
        media_manifest_fresh = False
    media_files = {(row.get('content'), row.get('name')): row for row in media_status.get('files', [])
                   if isinstance(row, dict) and isinstance(row.get('name'), str)}
    # Proxmox content listings are cheap metadata calls. Cache each query for
    # this refresh; never hash large archives from the dashboard refresh path.
    content_cache = {}

    def archive_observation(node_name, replica, image):
        volid = replica.get('template_file_id', '')
        try:
            storage, path = volid.split(':', 1)
            content, name = path.split('/', 1)
            if content != 'vztmpl' or storage != replica.get('datastore_id'):
                return {'observed': False, 'reason': 'Catalog volume identity is inconsistent'}
            manifest = media_files.get((content, name), {})
            digest = image.get('artifact_sha256')
            if (not media_manifest_fresh or not digest or manifest.get('sha256') != digest or
                    replica.get('artifact_sha256') != digest or
                    type(manifest.get('size')) is not int or manifest['size'] <= 0):
                return {'observed': False, 'reason': 'Central checksum manifest does not verify this archive'}
            copies = {copy.get('storage'): copy for copy in manifest.get('copies', [])
                      if isinstance(copy, dict)}
            copy = copies.get(storage, {})
            if copy.get('sha256') != digest or copy.get('size') != manifest['size']:
                return {'observed': False, 'reason': 'Checksum manifest has no verified copy for this storage'}
            key = (node_name, storage, content)
            if key not in content_cache:
                try:
                    content_cache[key] = api.request('GET', f'/nodes/{node_name}/storage/{storage}/content',
                                                     {'content': content})
                except Exception:
                    content_cache[key] = None
            rows = content_cache[key]
            actual = next((row for row in rows or [] if row.get('volid') == volid), None)
            if not actual or actual.get('size') != manifest['size']:
                return {'observed': False, 'reason': 'Archive is absent or its size differs from the manifest'}
            return {'observed': True, 'reason': 'Volume, size and SHA256 manifest match',
                    'size': manifest['size'], 'sha256': digest}
        except (ValueError, AttributeError):
            return {'observed': False, 'reason': 'Catalog archive volume ID is invalid'}
    for name, node in sorted(raw['nodes'].items()):
        blockers = []
        if name not in policy.get('allowed_nodes', scheduling.DEFAULT_POLICY['allowed_nodes']):
            blockers.append('Node is excluded by placement policy')
        if node.get('storage_inventory_error'):
            blockers.append('Detailed datastore discovery failed; this node is unavailable for placement')
        if not node.get('online'): blockers.append('Node is offline')
        if node.get('draining'): blockers.append('Node is draining')
        if node.get('load', 0) > policy.get('max_load', 1): blockers.append('Observed host load exceeds admission limit')
        if not node.get('management_verified'): blockers.append('Range network/MTU/management acceptance is missing')
        pending = [r for r in reservations if r['node_name'] == name]
        memory_total, cpu_total = node.get('memory_mb', 0), node.get('cpu_cores', 0)
        ram_pending = sum(r['memory_mb'] for r in pending)
        cpu_pending = sum(r['cpu_cores'] for r in pending)
        budget = capacity_window(node, policy, name) if node.get('online') else None
        ram_reserve = budget['memory_reserve_mb'] if budget else 0
        cpu_capacity = budget['cpu_limit_cores'] if budget else 0
        memory_available = budget['memory_mb'] - ram_pending if budget else 0
        cpu_available = budget['cpu_cores'] - cpu_pending if budget else 0
        if memory_available <= 0: blockers.append('RAM headroom is exhausted')
        if cpu_available <= 0: blockers.append('CPU commitment headroom is exhausted')
        storages = []
        for sid, storage in sorted(node.get('storages', {}).items()):
            disk_pending = sum(r['disk_gb'] for r in pending if r['datastore_id'] == sid)
            mode = policy.get('clone_mode', 'linked')
            disk_budget = capacity_window(node, policy, name, storage, mode)
            disk_reserve = disk_budget['disk_reserve_gb']
            available = disk_budget['disk_gb'] - disk_pending
            storage_blockers = []
            if sid not in policy.get('allowed_storages', []): storage_blockers.append('Datastore is not approved for running guests')
            if not storage.get('available'): storage_blockers.append('Datastore is unavailable')
            if storage.get('inventory_complete') is False: storage_blockers.append('Datastore capacity/content/locality discovery is incomplete')
            if available <= 0: storage_blockers.append('Disk headroom is exhausted')
            if policy.get('clone_mode', 'linked') == 'linked' and (storage.get('shared') or not storage.get('snapshots_verified')):
                storage_blockers.append('Local linked-clone/snapshot acceptance is missing')
            storages.append({'datastore_id': sid, 'total_gb': storage['total_gb'],
                'free_gb': storage['free_gb'], 'available': storage.get('available', False),
                'shared': storage.get('shared', False),
                'used_gb': storage['total_gb'] - storage['free_gb'], 'committed_gb': storage['committed_gb'],
                'reserved_gb': disk_pending, 'host_reserve_gb': disk_reserve,
                'available_gb': max(0, available), 'commitment_limit_gb': disk_budget['disk_limit_gb'],
                'overcommit_ratio': disk_budget['disk_ratio'], 'clone_modes': storage.get('clone_modes', []),
                'eligibility': {'eligible': not storage_blockers, 'blockers': storage_blockers}})
        if not any(s['eligibility']['eligible'] for s in storages): blockers.append('No approved datastore has verified capacity')
        nodes.append({'node': name, 'online': node.get('online', False), 'load': node.get('load', 0),
            'host_memory_reserve_settings': {
                'minimum_mb':policy.get('nodes',{}).get(name,{}).get('memory_reserve_mb',policy.get('memory_reserve_mb',4096)),
                'fraction':policy.get('nodes',{}).get(name,{}).get('memory_reserve_fraction',policy.get('memory_reserve_fraction',.25))},
            'capacity': {'memory_mb': memory_total, 'cpu_cores': cpu_total,
                         'memory_commitment_limit_mb': budget['memory_limit_mb'] if budget else 0,
                         'cpu_commitment_limit_cores': cpu_capacity, 'storages': storages},
            'usage': {'memory_mb': node.get('observed_memory_mb', 0), 'cpu_cores': node.get('observed_cpu_cores'), 'cpu_load': node.get('load', 0)},
            'commitments': {'memory_mb': node.get('committed_memory_mb', 0), 'cpu_cores': node.get('committed_cpu_cores', 0)},
            'reservations': {'memory_mb': ram_pending, 'cpu_cores': cpu_pending},
            'host_reserves': {'memory_mb': ram_reserve,
                              'cpu_cores': budget['cpu_reserve_cores'] if budget else 0},
            'availability': {'memory_mb': max(0, memory_available), 'cpu_cores': max(0, cpu_available)},
            'guest_types': node.get('guest_types', []),
            'eligibility': {'eligible': not blockers, 'blockers': blockers,
                'guest_types': {kind: {'eligible': not blockers and kind in node.get('guest_types', []),
                    'blockers': blockers + ([] if kind in node.get('guest_types', []) else
                        ['Provider container operations have not passed acceptance' if kind == 'container' else 'Guest type unavailable'])}
                    for kind in ('vm', 'container')}},
            'verified_networks': node.get('verified_networks', []), 'guests': []})
    for vmid, guest in sorted(raw['guests'].items(), key=lambda row: int(row[0])):
        image_id, replica = replicas.get(int(vmid), (None, {}))
        template = bool(guest['template'])
        counts_compute = not template and guest.get('status') != 'stopped'
        row = {'vmid': int(vmid), 'node': guest['node_name'], 'kind': guest['kind'],
            'name': guest.get('name'), 'status': guest.get('status', 'unknown'), 'template': guest['template'], 'cache': bool(replica.get('cache_local')),
            'image_id': image_id, 'memory_mb': guest['memory_mb'] if counts_compute else 0,
            'cpu_cores': guest['cpu_cores'] if counts_compute else 0,
            'configured_memory_mb': guest['memory_mb'], 'configured_cpu_cores': guest['cpu_cores'],
            'datastores': guest['disks'], 'clone_modes_verified': replica.get('clone_modes_verified', [])}
        guests.append(row)
        for node in nodes:
            if node['node'] == row['node']: node['guests'].append(row)
    if not policy.get('enabled'): global_blockers.append('Administrator policy has not enabled automatic placement')
    if not any(n['eligibility']['eligible'] for n in nodes): global_blockers.append('No node passes current admission gates')
    image_distribution = []
    for image_id, image in sorted(catalog['images'].items()):
        locations = []
        for node_name in sorted(raw['nodes']):
            replicas_on_node = []
            for replica in image.get('replicas', []):
                if replica.get('node') != node_name:
                    continue
                template_id = replica.get('template_id')
                observed = raw['guests'].get(str(template_id), {}) if template_id is not None else {}
                archive = archive_observation(node_name, replica, image) if template_id is None else None
                actual_guest = observed if observed.get('template') and observed.get('node_name') == node_name else {}
                replicas_on_node.append({
                    'state': replica.get('state'), 'datastore_id': replica.get('datastore_id'),
                    'template_id': template_id, 'cache_local': bool(replica.get('cache_local')),
                    'observed': bool(actual_guest) if template_id is not None else bool(archive and archive['observed']),
                    'actual_datastores': sorted(actual_guest.get('disks', {})),
                    'observation_reason': ('Template VMID is absent or not a template on the recorded node'
                        if template_id is not None and not actual_guest else
                        archive.get('reason') if archive else 'Template exists on the recorded node'),
                    'observed_size': archive.get('size') if archive else None,
                    'observed_sha256': archive.get('sha256') if archive else None,
                    'clone_modes': replica.get('clone_modes_verified', []),
                })
            if image.get('source_type') == 'stock_archive' and not any(r.get('observed') for r in replicas_on_node):
                source_replica = next((r for r in image.get('replicas', []) if r.get('template_file_id')), None)
                if source_replica:
                    archive = archive_observation(node_name, source_replica, image)
                    replicas_on_node.append({
                        'state': 'uncataloged', 'datastore_id': source_replica.get('datastore_id'),
                        'template_id': None, 'template_file_id': source_replica.get('template_file_id'),
                        'cache_local': False, 'cataloged': False, 'observed': archive['observed'],
                        'observation_reason': archive['reason'], 'observed_size': archive.get('size'),
                        'observed_sha256': archive.get('sha256'), 'clone_modes': [],
                    })
            locations.append({'node': node_name, 'replicas': replicas_on_node})
        stock_manifest = False
        if image.get('source_type') == 'stock_archive':
            for replica in image.get('replicas', []):
                volid = replica.get('template_file_id', '')
                if ':vztmpl/' not in volid:
                    continue
                name = volid.split(':vztmpl/', 1)[1]
                manifest = media_files.get(('vztmpl', name), {})
                if (media_manifest_fresh and manifest.get('sha256') == image.get('artifact_sha256') and
                        type(manifest.get('size')) is int and manifest['size'] > 0 and
                        any(copy.get('sha256') == manifest['sha256'] and copy.get('size') == manifest['size']
                            for copy in manifest.get('copies', []) if isinstance(copy, dict))):
                    stock_manifest = True
                    break
        image_distribution.append({'image_id': image_id, 'kind': image.get('kind'),
            'disk_gb': image.get('disk_gb'), 'approved_projects': image.get('approved_projects', []),
            'archived': bool(stock_manifest or image.get('artifact', {}).get('complete_capture')),
            'source_type': image.get('source_type', 'published_template'),
            'locations': locations})
    ready_images = []
    for image in image_distribution:
        replicas = [replica for location in image['locations'] for replica in location['replicas']]
        has_observed_replica = any(replica['state'] == 'ready' and replica['observed'] for replica in replicas)
        central_vm_archive = image['kind'] == 'vm' and image['source_type'] != 'stock_archive' and image['archived']
        if image['approved_projects'] and (has_observed_replica or central_vm_archive):
            ready_images.append(image['image_id'])
    if not ready_images: global_blockers.append('No approved image version has a verified ready archive or local cache')
    cache_paths=set(policy.get('cache',policy).get('tested_paths',[]))
    snapshot = {'schema_version': 1, 'captured_at': raw['captured_at'], 'policy_revision': policy['revision'],
        'commitment_policy': {'cpu_overcommit_ratio': policy.get('cpu_overcommit_ratio', 1.0),
            'memory_overcommit_ratio': policy.get('memory_overcommit_ratio', 1.0),
            'local_linked_disk_overcommit_ratio': policy.get('local_linked_disk_overcommit_ratio', 1.0),
            'memory_reserve_fraction': policy.get('memory_reserve_fraction', .25),
            'memory_reserve_mb': policy.get('memory_reserve_mb', 4096),
            'disk_reserve_fraction': policy.get('disk_reserve_fraction', .2),
            'disk_reserve_gb': policy.get('disk_reserve_gb', 20)},
        'max_inventory_age_seconds': policy.get('max_inventory_age_seconds', 120), 'nodes': nodes, 'guests': guests,
        'images': image_distribution,
        'cache_capabilities': {'tested_paths':sorted(cache_paths),
                               'container_ready':{'local-linked-container','container-archive-restore'}<=cache_paths},
        'automatic': {'enabled': bool(policy.get('enabled')), 'policy_enabled': bool(policy.get('enabled')),
                      'operational_ready': not global_blockers, 'ready_image_versions': ready_images, 'blockers': global_blockers},
        'stale': False, 'freshness': {'stale': False, 'age_seconds': 0}}
    scheduling.atomic_json(scheduling.DATA / SNAPSHOT_NAME, snapshot, mode=0o640)
    return snapshot


def refresh_inventory_snapshot(*, api=None):
    """Retain last successful data and publish a bounded failure marker."""
    scheduling.DATA.mkdir(parents=True, exist_ok=True, mode=0o750)
    error_path = scheduling.DATA / 'cluster-refresh-error.json'
    try:
        result = _refresh_inventory_snapshot(api=api)
        error_path.unlink(missing_ok=True)
        return result
    except Exception as exc:
        scheduling.atomic_json(error_path, {'failed_at': time.time(), 'error_type': type(exc).__name__,
            'message': 'Cluster refresh failed; previous inventory is retained. Inspect the fixed worker job.'}, mode=0o640)
        raise


def configuration_bundle(inventory, catalog, acceptance, output_directory):
    """Write reviewed production settings from recorded live acceptance evidence.

    The generated policy stays disabled. Root enables only after separately
    reviewing passing mixed-guest deployment/network and plan/apply evidence.
    Existing controller settings are never overwritten by this review helper.
    """
    import copy
    from crange_catalog import validate_catalog
    validate_catalog(catalog)
    if not acceptance.get('job_id') or not acceptance.get('source_revision'):
        raise ValueError('A recorded acceptance worker job and source revision are required.')
    policy = copy.deepcopy(scheduling.DEFAULT_POLICY)
    policy['revision'] = 'accepted-' + acceptance['job_id']
    policy['enabled'] = False
    policy['cpu_overcommit_ratio'] = 3.5
    policy['memory_overcommit_ratio'] = 2.0
    policy['local_linked_disk_overcommit_ratio'] = 1.5
    policy['expected_cluster_nodes'] = [f'proxmox{i}' for i in range(1, 7)]
    policy['cache'] = {'isolated_bridge': acceptance.get('isolated_bridge'),
        'isolated_bridges': acceptance.get('isolated_bridges', []),
        'tested_paths': acceptance.get('tested_paths', [])}
    required_paths = {'local-linked-vm', 'local-linked-container',
                      'vm-archive-restore', 'container-archive-restore'}
    tested_paths = set(acceptance.get('tested_paths', []))
    if not required_paths <= tested_paths:
        raise ValueError('Acceptance must prove local linked clones and archive restore for VMs and containers.')
    approved_storage = acceptance.get('approved_storage', [])
    approved_backup_storage = acceptance.get('approved_backup_storage', [])
    if not approved_storage or not approved_backup_storage:
        raise ValueError('Acceptance must name tested guest and central backup storage pairs.')
    policy['approved_storage'] = approved_storage
    policy['approved_backup_storage'] = approved_backup_storage
    policy['tested_paths'] = sorted(tested_paths)
    policy['acceptance'] = {'job_id': acceptance['job_id'], 'source_revision': acceptance['source_revision']}
    policy['network_attestations'] = {}
    policy['nodes'] = {}
    for node, proof in acceptance.get('nodes', {}).items():
        if node not in inventory['nodes'] or not proof.get('evidence'):
            raise ValueError('Node capability acceptance requires actual inventory and evidence.')
        network = proof.get('network', {})
        required = scheduling.NETWORK_CHECKS
        if network.get('verified') and (not required <= set(network.get('passed_checks', [])) or
                network.get('mtu') != 1450 or not network.get('verified_networks')):
            raise ValueError('Network attestation lacks required end-to-end acceptance checks.')
        policy['network_attestations'][node] = {**network, 'zone': 'crange',
            'management_verified': bool(network.get('verified'))}
        policy['nodes'][node] = {'provider_container_verified': proof.get('provider_container_verified') is True,
            'snapshot_storages': list(proof.get('snapshot_storages', [])),
            'verified_features': list(proof.get('verified_features', [])),
            **proof.get('administrator_reserves', {})}
    for image in catalog['images'].values():
        if image.get('source_type') == 'stock_archive': continue
        artifact = image.get('artifact', {})
        if (not artifact.get('complete_capture') or not artifact.get('volume_id', '').startswith('crange-library:backup/') or
                not image.get('source_identity') or not image.get('disk_gb') or not image.get('memory_mb') or not image.get('cpu_cores')):
            raise ValueError('Published cache images require complete central artifacts, sizes and source identities.')
    directory = Path(output_directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, value in [('distribution-policy.json', policy), ('image-catalog.json', catalog)]:
        path = directory / name
        if path.exists(): raise ValueError('Review bundle exists; create a new immutable acceptance directory.')
        scheduling.atomic_json(path, value)
    return {'directory': str(directory), 'policy_revision': policy['revision'],
            'enabled': False, 'image_versions': sorted(catalog['images'])}
