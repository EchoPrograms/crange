"""Privileged cluster refresh and unprivileged sanitized snapshot reader.

HTTP handlers only read the published snapshot. Credentialed discovery and
reservation queries run as fixed controller jobs. Cluster data is admin-only.
"""
import json
import os
import time
from pathlib import Path

from crange import ProxmoxAPI
from crange_placement import ReservationLedger, capacity_window
import crange_scheduling as scheduling


SNAPSHOT_NAME = 'cluster-inventory.json'


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
        row = {'vmid': int(vmid), 'node': guest['node_name'], 'kind': guest['kind'],
            'name': guest.get('name'), 'status': guest.get('status', 'unknown'), 'template': guest['template'], 'cache': bool(replica.get('cache_local')),
            'image_id': image_id, 'memory_mb': guest['memory_mb'], 'cpu_cores': guest['cpu_cores'],
            'datastores': guest['disks'], 'clone_modes_verified': replica.get('clone_modes_verified', [])}
        guests.append(row)
        for node in nodes:
            if node['node'] == row['node']: node['guests'].append(row)
    if not policy.get('enabled'): global_blockers.append('Administrator policy has not enabled automatic placement')
    if not any(n['eligibility']['eligible'] for n in nodes): global_blockers.append('No node passes current admission gates')
    ready_images = [image_id for image_id, image in catalog['images'].items() if image.get('approved_projects') and
        any(r.get('state') == 'ready' and (r.get('template_file_id') or 'linked' in r.get('clone_modes_verified', [])) for r in image.get('replicas', []))]
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
                replicas_on_node.append({
                    'state': replica.get('state'), 'datastore_id': replica.get('datastore_id'),
                    'template_id': template_id, 'cache_local': bool(replica.get('cache_local')),
                    'observed': bool(observed.get('template') and observed.get('node_name') == node_name),
                    'clone_modes': replica.get('clone_modes_verified', []),
                })
            locations.append({'node': node_name, 'replicas': replicas_on_node})
        image_distribution.append({'image_id': image_id, 'kind': image.get('kind'),
            'disk_gb': image.get('disk_gb'), 'approved_projects': image.get('approved_projects', []),
            'archived': bool(image.get('artifact', {}).get('complete_capture')),
            'locations': locations})
    if not ready_images: global_blockers.append('No approved image version has a verified ready archive or local cache')
    snapshot = {'schema_version': 1, 'captured_at': raw['captured_at'], 'policy_revision': policy['revision'],
        'max_inventory_age_seconds': policy.get('max_inventory_age_seconds', 120), 'nodes': nodes, 'guests': guests,
        'images': image_distribution,
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
    policy['expected_cluster_nodes'] = [f'proxmox{i}' for i in range(1, 6)]
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
