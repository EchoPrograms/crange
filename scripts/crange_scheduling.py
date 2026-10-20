"""Worker-only discovery and exact-plan placement. No HTTP-side scheduling."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import math
import os
import re
import socket
import time
from contextlib import contextmanager
from pathlib import Path

from crange_catalog import validate_catalog, catalog_identity
from crange_placement import PlacementError, ReservationLedger, workload_node_allowed

CONFIG = Path(os.environ.get('CRANGE_DISTRIBUTION_CONFIG', Path.home() / '.config/crange'))
DATA = Path(os.environ.get('CRANGE_DISTRIBUTION_DATA', '/var/lib/crange-distribution'))
KNOWN_NODES = {f'proxmox{i}' for i in range(1, 7)}
DEFAULT_POLICY = {'revision': 'conservative-v1', 'enabled': False,
                  # Cluster membership alone does not admit placements: each node
                  # still requires storage/provider/network acceptance.
                  'allowed_nodes': [f'proxmox{i}' for i in range(1, 7)],
                  'expected_cluster_nodes': sorted(KNOWN_NODES),
                  'router_only_nodes': ['proxmox1'],
                  'allowed_storages': ['local-lvm', 'ssd-lvm', 'hdd-lvm', 'crange-thin', 'local-hdd'],
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


def _offline_config_snapshot(node, kind, vmid):
    """Ask the fixed root-owned P4 relay for one allowlisted sanitized snapshot."""
    if node not in KNOWN_NODES or kind not in ('lxc', 'qemu') or type(vmid) is not int:
        raise PlacementError('Offline config snapshot identity is invalid.')
    request = {'node': 'proxmox4', 'guest_node': node, 'kind': kind, 'vmid': vmid,
               'command': 'offline-config-snapshot'}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(65)
            client.connect('/run/crange-ct-read/socket')
            client.sendall(json.dumps(request, separators=(',', ':')).encode() + b'\n')
            data = b''
            while not data.endswith(b'\n') and len(data) <= 8192:
                chunk = client.recv(8193 - len(data))
                if not chunk:
                    break
                data += chunk
    except OSError as exc:
        raise PlacementError('Fixed offline config snapshot relay is unavailable.') from exc
    if len(data) > 8192 or not data.endswith(b'\n'):
        raise PlacementError('Fixed offline config snapshot response is invalid.')
    try:
        response = json.loads(data)
        snapshot = json.loads(response['value']) if set(response) == {'value'} else None
    except (ValueError, TypeError, KeyError):
        snapshot = None
    if not isinstance(snapshot, dict):
        raise PlacementError('Fixed offline config snapshot was denied or malformed.')
    captured = snapshot.get('captured_at')
    digest = snapshot.get('config_sha256')
    tags = snapshot.get('tags')
    capacity_inventory = snapshot.get('scope') == 'cluster-capacity'
    if (snapshot.get('guest_node') != node or snapshot.get('kind') != kind or
            snapshot.get('vmid') != vmid or snapshot.get('status') != 'offline' or
            type(snapshot.get('template')) is not bool or
            type(snapshot.get('memory_bytes')) is not int or snapshot['memory_bytes'] < 0 or
            isinstance(snapshot.get('cpu_cores'), bool) or
            not isinstance(snapshot.get('cpu_cores'), (int, float)) or
            not math.isfinite(snapshot['cpu_cores']) or snapshot['cpu_cores'] < 0 or
            type(snapshot.get('disk_bytes')) is not int or snapshot['disk_bytes'] <= 0 or
            not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest) or
            not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags) or
            (not capacity_inventory and (not tags or not (('p100' in tags) ^ ('crange-image' in tags)))) or
            not isinstance(captured, (int, float)) or isinstance(captured, bool) or
            not math.isfinite(captured) or captured > time.time() + 5 or time.time() - captured > 120):
        raise PlacementError('Fixed offline config snapshot failed identity, freshness, or limit validation.')
    return snapshot


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


def bootstrap_node_candidates(policy):
    """Nodes whose existing provider and zone proofs can host a router-only bootstrap."""
    return [node for node in policy.get('allowed_nodes', [])
            if provider_container_placement_verified(policy, node)
            and network_attestation_valid(policy.get('network_attestations', {}).get(node, {}))]


def _bootstrap_planned_networks(inventory, policy, requests, node_name=None, router_nodes=None):
    """Mark planned-VNet eligibility only inside a router-only bootstrap review.

    This is not an acceptance record. The project VNets are created by the
    reviewed foundation plan and remain ineligible to ordinary placement until
    exact per-VNet acceptance is separately recorded.
    """
    candidates = bootstrap_node_candidates(policy)
    if not candidates or (router_nodes is None and node_name not in candidates):
        raise PlacementError('Bootstrap requires an enrolled node with provider and zone acceptance.')
    expected = set()
    project_ids = set()
    for request in requests:
        if request.get('kind') != 'container' or not str(request.get('resource_key', '')).startswith('router:'):
            raise PlacementError('Bootstrap plans may contain only project router containers.')
        pid = request.get('project_id')
        if type(pid) is not int or not 1 <= pid <= 255 or pid in (60, 61):
            raise PlacementError('Bootstrap project ID is invalid.')
        project_ids.add(pid)
        expected.update(request.get('networks', []))
        intent = request.get('placement', {})
        if isinstance(router_nodes, list):
            if not set(router_nodes) <= set(candidates) or intent.get('mode') != 'auto':
                raise PlacementError('Every bootstrap router must use reviewed, enrolled candidate nodes.')
        else:
            expected_node = (router_nodes or {}).get(request.get('resource_key')) if router_nodes is not None else node_name
            if expected_node not in candidates or intent.get('mode') != 'pin' or intent.get('pinned_node') != expected_node:
                raise PlacementError('Every bootstrap router must be pinned to its reviewed, enrolled node.')
    if len(project_ids) != 1 or not expected:
        raise PlacementError('Bootstrap must contain one project and its router networks.')
    project = next(iter(project_ids))
    prefix = f'p{project:03d}'
    if any(net != 'crtrans' and not re.fullmatch(rf'{prefix}(?:tr|t(?:00[1-9]|0[1-9][0-9]|1[0-9]{{2}}|2[0-4][0-9]|254))', net)
           for net in expected):
        raise PlacementError('Bootstrap network set is outside the project router topology.')
    if isinstance(router_nodes, list):
        allowed_nodes = set(router_nodes)
        if not allowed_nodes or not allowed_nodes <= set(candidates):
            raise PlacementError('Bootstrap candidate nodes no longer have current provider and zone acceptance.')
        for request in requests:
            intent = request.get('placement', {})
            if intent.get('mode') != 'auto' or set(intent.get('allowed_nodes', [])) != allowed_nodes:
                raise PlacementError('Every bootstrap router must use the exact reviewed candidate-node set.')
        selected_nodes = allowed_nodes
    else:
        selected_nodes = set((router_nodes or {}).values()) or {node_name}
    available_nodes = set()
    for selected in selected_nodes:
        node = inventory.get('nodes', {}).get(selected)
        if not node or not node.get('online') or node.get('draining'):
            if isinstance(router_nodes, list):
                # An automatic candidate is an option, not a dependency. The
                # scheduler still rejects it; exact pinned targets must fail.
                continue
            raise PlacementError('Bootstrap node is unavailable.')
        available_nodes.add(selected)
    if not available_nodes:
        raise PlacementError('No reviewed bootstrap candidate is available.')
    # Keep planning eligibility distinct from live, exact-VNet acceptance. The
    # enrolled zone proof plus project-only resource allowlist permits crtrans
    # for router foundation planning, but this never records acceptance.
    for selected in available_nodes:
        inventory['nodes'][selected]['bootstrap_planned_networks'] = sorted(expected)
    return inventory


def _custom_foundation_networks(inventory, policy, requests, cfg, keys):
    """Grant transient router-only eligibility behind an accepted parent LAN."""
    allowed = {'router:custom:' + key for key in keys}
    if not allowed or {row['resource_key'] for row in requests} != allowed:
        raise PlacementError('Custom network foundation must contain only its exact new router requests.')
    candidates = set(bootstrap_node_candidates(policy))
    project = int(cfg['project_id'])
    for request in requests:
        key = request['resource_key'].removeprefix('router:custom:')
        net = cfg['custom_networks'][key]
        parent = f'p{project:03d}t{int(net["owner_team_id"]):03d}'
        downstream = f'p{project:03d}c{int(net["network_id"]):03d}'
        if (request.get('kind') != 'container' or request.get('project_id') != project
                or set(request.get('networks', [])) != {parent, downstream}):
            raise PlacementError('Custom network foundation request differs from its declaration.')
        for name, node in inventory.get('nodes', {}).items():
            if (name in candidates and node.get('online') and not node.get('draining')
                    and node.get('management_verified') and parent in node.get('verified_networks', [])
                    and parent in node.get('networks', [])):
                node.setdefault('bootstrap_planned_networks', []).extend([parent, downstream])
    return inventory


def discover(api, policy, planned_networks=()):
    """Account visible guest disks and active or unknown-status guest CPU/RAM.

    VM.Audit on all guests is required. Host memory.used is deliberately treated
    as a conservative upper bound on non-reclaimable use; it is NOT added to
    configured guest RAM. Stopped guests and templates retain disk commitments
    but contribute no CPU/RAM. Unknown guest state is counted conservatively.
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
                template = bool(int(guest.get('template') or 0))
                maxima = {key: guest.get(key) for key in ('maxmem', 'maxcpu', 'maxdisk')}
                if maxima['maxdisk'] is None or (not template and any(value is None for value in maxima.values())):
                    # /cluster/resources omits configured maxima for guests on
                    # an offline node. Read its quorum-backed shared config
                    # first: retrying the unavailable node's API for every
                    # guest can stall discovery for minutes. Never infer from a name,
                    # Terraform declaration, or zero usage counters.
                    snapshot = None
                    try:
                        snapshot = _offline_config_snapshot(name, guest['type'], int(guest['vmid']))
                    except PlacementError as snapshot_error:
                        # Compatibility for deployments without the fixed relay.
                        # A denied or malformed relay response must fail closed.
                        if str(snapshot_error) != 'Fixed offline config snapshot relay is unavailable.':
                            raise
                        try:
                            config = api.request('GET', f"/nodes/{name}/{guest['type']}/{guest['vmid']}/config")
                        except Exception:
                            raise snapshot_error from None
                        if not isinstance(config, dict):
                            raise PlacementError(f'Cannot account workloads on offline node {name}; guest {guest["vmid"]} config is invalid.')
                        template = bool(int(config.get('template', template)))
                        if maxima['maxmem'] is None and config.get('memory') is not None:
                            maxima['maxmem'] = float(config['memory']) * 2**20
                        if maxima['maxcpu'] is None and config.get('cores') is not None:
                            maxima['maxcpu'] = float(config['cores']) * float(config.get('sockets', 1))
                        if maxima['maxdisk'] is None:
                            configured_disks = disk_allocations(config)
                            if configured_disks:
                                maxima['maxdisk'] = sum(configured_disks.values()) * 2**30
                    else:
                        template = snapshot['template']
                        if maxima['maxmem'] is None and snapshot['memory_bytes']:
                            maxima['maxmem'] = snapshot['memory_bytes']
                        if maxima['maxcpu'] is None and snapshot['cpu_cores']:
                            maxima['maxcpu'] = snapshot['cpu_cores']
                        if maxima['maxdisk'] is None:
                            maxima['maxdisk'] = snapshot['disk_bytes']
                missing_values = [key for key, value in maxima.items()
                                  if value is None and (not template or key == 'maxdisk')]
                if missing_values:
                    missing = ', '.join(missing_values)
                    raise PlacementError(f'Cannot account workloads on offline node {name}; configured maxima are missing ({missing}; guest {guest["vmid"]}).')
                memory = float(maxima['maxmem'] or 0) / 2**20
                cores = float(maxima['maxcpu'] or 0)
                disk = float(maxima['maxdisk'] or 0) / 2**30
                if (not math.isfinite(disk) or disk < 0 or
                        (not template and (not math.isfinite(memory) or not math.isfinite(cores) or
                                           memory <= 0 or cores <= 0))):
                    raise PlacementError(f'Cannot account workloads on offline node {name}; configured CPU/RAM/disk maxima are invalid (guest {guest["vmid"]}).')
                counts_compute = not template and guest.get('status') != 'stopped'
                if counts_compute:
                    result['nodes'][name]['committed_memory_mb'] += memory
                    result['nodes'][name]['committed_cpu_cores'] += cores
                for scope in ['global', *re.findall(r'(?:^|;)p(\d{3})(?:;|$)', str(guest.get('tags', '')))]:
                    scope = str(int(scope)) if scope != 'global' else scope
                    usage = result['quota_usage'].setdefault(scope, {})
                    for key, amount in (('memory_mb', memory if counts_compute else 0),
                                        ('cpu_cores', cores if counts_compute else 0), ('disk_gb', disk)):
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
                # ISO/template-only NFS mirrors can be offline independently of
                # usable guest storage. PVE filters before probing stores when
                # content is supplied; do not wait for irrelevant media mounts.
                storage_rows = api.request('GET', f'/nodes/{name}/storage', {'content': 'images,rootdir'})
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
    # Configuration reads dominate larger clusters and can outlive the inventory
    # freshness window when serialized. Bound read concurrency to the gateway's
    # normal four slots. Mutations and dependent follow-ups remain sequential.
    online_guests = [g for g in guests if result['nodes'].get(g['node'], {}).get('online')]
    with ThreadPoolExecutor(max_workers=4) as readers:
        config_reads = {str(g['vmid']): readers.submit(api.request, 'GET',
            f'/nodes/{g["node"]}/{g["type"]}/{int(g["vmid"])}/config') for g in online_guests}
    for guest in guests:
        name = guest['node']
        if name not in result['nodes'] or not result['nodes'][name].get('online'): continue
        kind = guest['type']; vmid = int(guest['vmid'])
        try:
            config = config_reads[str(vmid)].result()
        except Exception as exc:
            # A VMID can disappear between the cluster listing and config read
            # during a reviewed cancel/recreate. Ignore only a confirmed stale
            # listing; a live or moved VM, or an inconclusive refresh, fails closed.
            from crange import CrangeError
            if not isinstance(exc, CrangeError) or 'failed with HTTP 500.' not in str(exc):
                raise
            refreshed = api.request('GET', '/cluster/resources', {'type': 'vm'})
            if not isinstance(refreshed, list):
                raise PlacementError('Could not verify a guest disappeared after a config API error.') from exc
            if any(int(item.get('vmid', -1)) == vmid for item in refreshed):
                raise
            continue
        node = result['nodes'][name]; template = bool(int(config.get('template', 0)))
        memory = float(config.get('memory', 0)); cores = float(config.get('cores', 1))*float(config.get('sockets', 1))
        disks = disk_allocations(config)
        counts_compute = not template and guest.get('status') != 'stopped'
        if counts_compute:
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
                usage['memory_mb'] = usage.get('memory_mb', 0) + (memory if counts_compute else 0)
                usage['cpu_cores'] = usage.get('cpu_cores', 0) + (cores if counts_compute else 0)
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
        if key.startswith('router:'):
            box = {**box, 'disk_gb': box.get('disk_gb', 4)}
        image = box.get('image') or reference.get('image_id')
        rows.append({'resource_key': key, 'kind': kind, 'box': box, 'reference': reference,
                     'image': image, 'networks': networks,
                     'default_preferred_node': 'proxmox4' if key.startswith('jumpbox:') else None})
    router_node = cfg.get('router_node', 'proxmox4')
    router_nodes = cfg.get('bootstrap_router_nodes', {})
    def router_for(key):
        if cfg.get('bootstrap_candidate_nodes'):
            candidates = list(cfg['bootstrap_candidate_nodes'])
            return {'node_name': candidates[0], 'placement': {'mode': 'auto', 'allowed_nodes': candidates},
                    'cores':1,'memory_mb':512}
        node = router_nodes.get(key, router_node)
        return {'node_name': node, 'placement': placement_intent(node), 'cores':1,'memory_mb':512}
    reference = {'image_id': cfg.get('router_image', 'debian-12-router-v1')}
    p = f'p{int(cfg["project_id"]):03d}'
    append('router:project','container',router_for('router:project'),reference,['crtrans',p+'tr'])
    append('router:staging','container',router_for('router:staging'),reference,[p+'tr',p+'t254'])
    for team in cfg['team_ids']:
        key = 'router:team:'+str(team)
        append(key,'container',router_for(key),reference,[p+'tr',p+f't{int(team):03d}'])
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


def requested_cpu_type(kind, profile):
    """Only QEMU guests can select a host CPU model; LXC uses the host kernel."""
    return profile.get('cpu_type', 'host') if kind == 'vm' else None


def freeze(row, live, previous=None):
    """Keep actual placements regardless of deleted preferences or new defaults."""
    source = (previous or {}).get('resolved') or previous or {}
    values = {key:copy.deepcopy(source[key]) for key in ('node_name','datastore_id','template_id','template_node','template_file_id','clone_mode','verified_local_cache') if key in source}
    values['node_name'] = live['node_name']
    if live.get('machine'):
        values['machine'] = live['machine']
    if row.get('kind') == 'vm' and live.get('boot_order'):
        # Advancing a base profile must not reboot existing guests merely to
        # remove an empty installer drive from their boot order.
        values['boot_order'] = copy.deepcopy(live['boot_order'])
    clone = live.get('clone') or []
    if clone:
        values.update(template_id=clone[0].get('vm_id'), template_node=clone[0].get('node_name') or live['node_name'],
                      clone_mode='full' if clone[0].get('full') else 'linked')
    disks = live.get('disk') or live.get('disk_device') or []
    if disks: values['datastore_id'] = disks[0]['datastore_id']
    if row['resource_key'].startswith('router:'):
        size = disks[0].get('size') if disks else None
        if isinstance(size,bool) or not isinstance(size,(int,float)) or not math.isfinite(size) or size <= 0:
            raise PlacementError('Cannot preserve the existing router disk size from Terraform state.')
        values['disk_gb'] = size
    initialization = live.get('operating_system') or []
    if initialization and initialization[0].get('template_file_id'):
        values['template_file_id'] = initialization[0]['template_file_id']
        values['clone_mode'] = 'archive'
    values.setdefault('datastore_id','local-lvm')
    return {'resource_key': row['resource_key'], 'resolved': values, 'reason':'Existing guest placement retained', 'existing':True, 'image': row.get('image') or ''}


def requested_disk_gb(row, image):
    size = image['disk_gb']
    if row['resource_key'].startswith('router:'):
        # Stock tar archives have no fixed disk layout. Captured filesystems
        # retain their source allocation; existing disks are always pinned.
        default = row.get('retained', {}).get('disk_gb', row['box'].get('disk_gb', 4))
        return default if image.get('source_type') == 'stock_archive' else max(size, default)
    return max(size, 8 if row['kind'] == 'container' else size)


def reviewed_bootstrap_replacements(path, project_id, owner, bootstrap_node, requests, policy, ledger, router_nodes=None):
    """Find only an identical, still-reviewed router bootstrap preview to replace."""
    if not bootstrap_node or not requests or any(not r['resource_key'].startswith('router:') for r in requests):
        return []
    requested_networks = sorted({network for request in requests for network in request.get('networks', [])})
    request_by_key = {request['resource_key']: request for request in requests}
    replacements = []
    for manifest in (Path(path) / '.crange-plans').glob('*.json'):
        try:
            record = json.loads(manifest.read_text())
            plan_id = record.get('plan_id')
            if (not record.get('bootstrap') or record.get('applied_at') or
                    record.get('bootstrap_node') != bootstrap_node or
                    (isinstance(router_nodes, dict) and record.get('bootstrap_router_nodes') != router_nodes) or
                    (isinstance(router_nodes, list) and record.get('bootstrap_nodes') != router_nodes) or
                    sorted(record.get('bootstrap_networks', [])) != requested_networks or
                    plan_id != manifest.stem):
                continue
            saved = ledger.get_job(plan_id)
        except (OSError, json.JSONDecodeError, PlacementError):
            continue
        if (saved['project_id'] != project_id or saved['owner'] != owner or saved['state'] != 'reviewed'):
            continue
        allocations = saved['allocations']
        plan_placements = record.get('placements', [])
        if ({a.get('resource_key') for a in allocations} != set(request_by_key) or
                {p.get('resource_key') for p in plan_placements} != set(request_by_key) or
                {a.get('reservation_id') for a in allocations} != {p.get('reservation_id') for p in plan_placements}):
            continue
        allocation_by_key = {a['resource_key']: a for a in allocations}
        exact = True
        for key, request in request_by_key.items():
            allocation = allocation_by_key[key]
            expected_resources = {
                'memory_mb': request['memory_mb'],
                'cpu_cores': request['cpu_cores'],
                'disk_gb': request['disk_gb'] + request.get('copy_overhead_gb', request['disk_gb'] * policy.get('copy_overhead_fraction', 0.1)),
            }
            expected_node = (router_nodes or {}).get(key, bootstrap_node) if isinstance(router_nodes, dict) else None
            allowed_node = allocation.get('resolved', {}).get('node_name') in router_nodes if isinstance(router_nodes, list) else True
            if (request.get('kind') != 'container' or request.get('image') != allocation.get('image') or
                    (expected_node is not None and allocation.get('resolved', {}).get('node_name') != expected_node) or
                    not allowed_node or
                    allocation.get('resources') != expected_resources):
                exact = False
                break
        if exact:
            replacements.append(plan_id)
    return replacements


def purge_expired_guest_reviews(path, cfg, owner, api, ledger, *, now=None, review_directory=None):
    """Retire expired unapplied guest reviews only after negative provider evidence.

    Cache preparations and running applies are never expired by this sweep.
    Changed declarations, existing guests and active provider tasks retain capacity.
    """
    now=time.time() if now is None else now
    path=Path(path);project=int(cfg['project_id']);released=[]
    config=path/'crange.auto.tfvars.json'
    if not config.exists():return released
    digest=hashlib.sha256(config.read_bytes()).hexdigest()
    declared={row['resource_key']:row for row in declarations(path,cfg)}
    applied=applied_resources(path)
    directory=review_directory or Path.home()/'.local/share/crange/plans'/f'project-{project:03d}'
    for manifest in Path(directory).glob('*.json'):
        try:
            record=json.loads(manifest.read_text());plan_id=record.get('plan_id')
            if (not re.fullmatch(r'[a-f0-9]{32}',str(plan_id)) or manifest.stem!=plan_id or
                    record.get('project')!=project or record.get('applied_at') or
                    record.get('inputs_sha256')!=digest or now-float(record['created'])<=3600):continue
            saved=ledger.get_job(plan_id);allocations=saved['allocations']
            if (saved['state']!='reviewed' or saved['project_id']!=project or saved['owner']!=owner or
                    saved['expires']>=now or not allocations):continue
            names=set();nodes=set();placements={r['resource_key']:r for r in record.get('placements',[])}
            for item in allocations:
                key=item.get('resource_key');row=declared.get(key)
                if (not row or key in applied or not key.startswith(('vm:','container:','jumpbox:')) or
                        key not in placements or placements[key].get('existing') or
                        item.get('image')!=row['image']):raise ValueError('Not an unused guest review.')
                resolved=item['resolved'];node=resolved['node_name']
                if any(placements[key]['resolved'].get(k)!=resolved.get(k) for k in ('node_name','datastore_id')):
                    raise ValueError('Reservation differs from retained review.')
                box=row['box'];network=int(box.get('network_id',box['team_id']))
                suffix=(f"jump-{int(box['jump_number']):02d}" if key.startswith('jumpbox:') else box['name'])
                names.add(f"p{project:03d}{'c' if box.get('network_key') else 't'}{network:03d}-{suffix}")
                nodes.add(node)
            with node_locks(sorted(nodes),library=True):
                # A running apply changes ledger state before any provider create.
                if ledger.get_job(plan_id)['state']!='reviewed' or hashlib.sha256(config.read_bytes()).hexdigest()!=digest:continue
                guests=api.request('GET','/cluster/resources',{'type':'vm'})
                if not isinstance(guests,list) or any(g.get('name') in names for g in guests):continue
                tasks=[api.request('GET',f'/nodes/{node}/tasks',{'source':'active','limit':1000}) for node in sorted(nodes)]
                if any(not isinstance(rows,list) or rows for rows in tasks):continue
                ledger.purge_reviewed(plan_id,operation_terminal=True,no_allocations_confirmed=True,
                                      provider_tasks_running=False,now=now)
                record.update(reservation_state='released',reservation_released_at=now,
                              reservation_release_reason='expired_guest_review_no_guests_or_provider_tasks')
                atomic_json(manifest,record,0o600)
                summary=path/'.crange-plans'/manifest.name
                if summary.exists():
                    public=json.loads(summary.read_text());public.update({k:record[k] for k in
                        ('reservation_state','reservation_released_at','reservation_release_reason')})
                    atomic_json(summary,public,0o640)
                released.append(plan_id)
        except (OSError,ValueError,TypeError,KeyError,PlacementError):
            continue
    return released


def purge_expired_bootstrap_reviews(path, project_id, owner, api, ledger, now=None):
    """Release only expired, unapplied bootstrap reviews with negative PVE evidence."""
    now = time.time() if now is None else now
    root = Path(path)
    if any((root / name).exists() for name in ('terraform.tfstate', 'terraform.tfstate.backup', '.terraform.tfstate.lock.info')):
        return []
    manifests = root / '.crange-plans'
    if not manifests.is_dir():
        return []
    try:
        resources = api.request('GET', '/cluster/resources', {'type': 'vm'})
        if not isinstance(resources, list):
            return []
    except Exception:
        return []
    prefix = f'p{int(project_id):03d}'
    project_guests = [resource for resource in resources
                      if prefix in str(resource.get('tags', '')).split(';')
                      or str(resource.get('name', '')).startswith(prefix + '-')]
    if project_guests:
        return []
    released = []
    for manifest in manifests.glob('*.json'):
        try:
            record = json.loads(manifest.read_text())
            plan_id = record.get('plan_id')
            if (not record.get('bootstrap') or record.get('applied_at') or
                    plan_id != manifest.stem or now - float(record.get('created', now)) <= 3600):
                continue
            saved = ledger.get_job(plan_id)
        except (OSError, ValueError, TypeError, json.JSONDecodeError, PlacementError):
            continue
        if (saved['project_id'] != project_id or saved['owner'] != owner or saved['state'] != 'reviewed'
                or saved['expires'] >= now):
            continue
        allocations = saved['allocations']
        if (not allocations or any(not str(item.get('resource_key', '')).startswith('router:') for item in allocations)
                or any(item.get('resolved', {}).get('node_name') !=
                       record.get('bootstrap_router_nodes', {}).get(item.get('resource_key'), record.get('bootstrap_node'))
                       for item in allocations)):
            continue
        nodes = {item['resolved']['node_name'] for item in allocations}
        try:
            active_tasks = []
            for node in sorted(nodes):
                tasks = api.request('GET', f'/nodes/{node}/tasks', {'source': 'active', 'limit': 1000})
                if not isinstance(tasks, list):
                    raise ValueError('invalid task response')
                active_tasks.extend(tasks)
        except Exception:
            continue
        if active_tasks:
            continue
        try:
            ledger.purge_reviewed(plan_id, operation_terminal=True, no_allocations_confirmed=True,
                                  provider_tasks_running=False, now=now)
        except PlacementError:
            continue
        record.update(reservation_state='released', reservation_released_at=now,
                      reservation_release_reason='expired_bootstrap_review_no_project_guests_or_provider_tasks')
        atomic_json(manifest, record, 0o640)
        released.append(plan_id)
    return released


def prepare(path, cfg, plan_id, owner, api=None, *, bootstrap_node=None, bootstrap_nodes=None, network_foundation=None):
    from crange import ProxmoxAPI, validate_project_quota
    validate_project_quota(path,cfg)
    from crange_router_ha import reconcile_locations
    reconcile_locations(path, api=api)
    policy,catalog = read_settings()
    if not policy.get('enabled'):
        boxes = [box for field in ('jump_boxes', 'scenario_boxes', 'scenario_containers', 'custom_networks') for box in cfg.get(field, {}).values()]
        if cfg.get('router_node') == 'auto' or any(intent_for(box).get('mode') == 'auto' for box in boxes):
            raise PlacementError('Automatic distribution is not enabled: publish verified images and complete network acceptance first.')
        return None
    rows = declarations(path,cfg)
    DATA.mkdir(parents=True,exist_ok=True,mode=0o700)
    existing=applied_resources(path); placements=[];requests=[]
    if network_foundation:
        keys = {'router:custom:' + key for key in network_foundation}
        if (keys & set(existing) or cfg.get('jump_boxes') or cfg.get('scenario_boxes')
                or cfg.get('scenario_containers')):
            raise PlacementError('Network foundation must create only undeployed custom routers, without scenario guests.')
        rows = [row for row in rows if row['resource_key'] in existing or row['resource_key'] in keys]
        if not {'router:project', 'router:staging'} <= set(existing):
            raise PlacementError('Create the base project foundation before adding custom networks.')
    record_file = path / 'crange-applied-placements.json'
    applied_record = json.loads(record_file.read_text()) if record_file.exists() else {}
    for row in rows:
        key=row['resource_key']
        if key in existing:
            live = existing[key]
            if network_foundation:
                frozen = freeze(row, live, applied_record.get(key, {}))
                frozen['declared_template'] = row['box'].get('template')
                placements.append(frozen)
                continue
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
            'cpu_type':requested_cpu_type(row['kind'], profile),
            'required_features':list(profile.get('required_features',[])),
            'disk_gb':requested_disk_gb(row,image)})
        if row.get('retained'):
            for field in ('datastore_id','template_id','template_file_id','clone_mode'):
                if row['retained'].get(field) is not None:requests[-1]['resolved_'+field]=row['retained'][field]
    api=api or ProxmoxAPI()
    inventory=discover(api,policy,{n for row in rows for n in row['networks']})
    if bootstrap_node:
        if (cfg.get('jump_boxes') or cfg.get('custom_networks') or cfg.get('scenario_boxes') or
                cfg.get('scenario_containers')):
            raise PlacementError('Router bootstrap is restricted to an empty project with no scenario guests.')
        inventory=_bootstrap_planned_networks(inventory,policy,requests,bootstrap_node,bootstrap_nodes)
    if network_foundation:
        inventory=_custom_foundation_networks(inventory,policy,requests,cfg,network_foundation)
    from crange_proxmox_compat import reviewed_identity
    provider_compat = reviewed_identity()
    ledger=ReservationLedger(DATA/'reservations.sqlite3')
    replacements=[]
    expired_bootstrap_reviews=[]
    try:
        if requests:
            expired_bootstrap_reviews=purge_expired_bootstrap_reviews(path,int(cfg['project_id']),owner,api,ledger)
            if bootstrap_node:
                replacements=reviewed_bootstrap_replacements(path,int(cfg['project_id']),owner,bootstrap_node,requests,policy,ledger,bootstrap_nodes)
            placements+=ledger.reserve_batch(requests,inventory,catalog,policy,int(cfg['project_id']),plan_id,owner,
                bootstrap=bool(bootstrap_node or network_foundation),replace_reviewed_job_ids=replacements)
    finally:
        ledger.close()
    for placement in placements:
        row = next(row for row in rows if row['resource_key'] == placement['resource_key'])
        placement['declared_template'] = row['box'].get('template')
    bootstrap_router_nodes = {p['resource_key']:p['resolved']['node_name'] for p in placements
                              if str(p.get('resource_key','')).startswith('router:')} if bootstrap_node else None
    return {'provider_compat': provider_compat, 'plan_id':plan_id,'placements':placements,'requests':requests,'policy_revision':policy['revision'],
            'policy_sha256':hashlib.sha256(json.dumps(policy,sort_keys=True).encode()).hexdigest(),
            'catalog_sha256':catalog_identity(catalog),'reservation_ids':[p['reservation_id'] for p in placements if p.get('reservation_id')],
            **({'bootstrap_node':bootstrap_node} if bootstrap_node else {}),
            **({'bootstrap_nodes':bootstrap_nodes,'bootstrap_router_nodes':bootstrap_router_nodes} if bootstrap_node else {}),
            **({'network_foundation':list(network_foundation)} if network_foundation else {}),
            **({'replaced_reviewed_plan_ids':replacements} if replacements else {}),
            **({'purged_expired_bootstrap_plan_ids':expired_bootstrap_reviews} if expired_bootstrap_reviews else {})}


def tf_overrides(prepared):
    return {'resolved_placements': {p['resource_key']:{**p['resolved'], 'image_id': p.get('image', ''), **({'disk_gb': next(r['disk_gb'] for r in prepared['requests'] if r['resource_key'] == p['resource_key'])} if p.get('resources') else {})} for p in prepared['placements']}}


def begin_apply(path, prepared, api=None):
    from crange import ProxmoxAPI
    from crange_router_ha import reconcile_locations
    if reconcile_locations(path, api=api):
        raise PlacementError('Native HA relocated a router since review; its identity is preserved. Review a fresh plan.')
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
    if prepared.get('apply_scope'):
        from crange_apply_scope import verify_untouched
        verify_untouched(prepared, applied_resources(path))
    api=api or ProxmoxAPI()
    networks={n for request in prepared['requests'] for n in request['networks']}
    inventory=discover(api,policy,networks)
    if prepared.get('bootstrap_node'):
        inventory=_bootstrap_planned_networks(inventory,policy,prepared['requests'],prepared['bootstrap_node'],prepared.get('bootstrap_nodes'))
    if prepared.get('network_foundation'):
        from crange import json_config
        cfg = json_config(path)
        if any('router:custom:' + key in applied_resources(path) for key in prepared['network_foundation']):
            raise PlacementError('A reviewed custom router already exists; restage instead of applying an old foundation plan.')
        inventory=_custom_foundation_networks(inventory,policy,prepared['requests'],cfg,prepared['network_foundation'])
    for identity in prepared.get('apply_scope', {}).get('removed_guests', {}).values():
        node = inventory['nodes'].get(identity['node_name'], {})
        if not node.get('online') or node.get('draining'):
            raise PlacementError('Reviewed deletion node is offline or draining; restage.')
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
            ledger.mark_running(prepared['plan_id'],requests=prepared['requests'],inventory=inventory,catalog=catalog,policy=policy,
                                bootstrap=bool(prepared.get('bootstrap_node') or prepared.get('network_foundation')))
        finally:ledger.close()


def _wait_for_applied_tasks(api, node, ids, *, timeout=90, sleep=None, clock=None):
    """Wait only for this reviewed allocation; unrelated guests cannot block it."""
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    deadline = clock() + timeout
    while True:
        tasks = api.request('GET', f'/nodes/{node}/tasks', {'source': 'active', 'limit': 1000})
        if not isinstance(tasks, list):
            raise PlacementError('Could not verify whether applied project guests still have provider tasks.')
        if not any(int(task.get('id') or 0) in ids for task in tasks if str(task.get('id') or '').isdigit()):
            return
        if clock() >= deadline:
            raise PlacementError('Provider tasks still running; reservations retained for reconciliation.')
        sleep(2)


def finish_apply(path, prepared, *, succeeded, api=None):
    """Failed/uncertain applies retain reservations until explicit reconciliation."""
    if not prepared:return
    from crange import ProxmoxAPI
    api=api or ProxmoxAPI()
    if not succeeded:return
    actual=applied_resources(path)
    from crange_apply_scope import verify_untouched
    verify_untouched(prepared, actual)
    if any(key in actual for key in prepared.get('apply_scope', {}).get('removed_guests', {})):
        raise PlacementError('Reviewed guest deletion is incomplete; reservations retained.')
    record_file=path/'crange-applied-placements.json'
    previous=json.loads(record_file.read_text()) if record_file.exists() else {}
    record={key:value for key,value in previous.items() if key in actual
            and value.get('guest_id') == int(actual[key]['vm_id'])
            and value.get('node_name') == actual[key]['node_name']}
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
        affected = {request['resource_key'] for request in prepared['requests']}
        nodes = {record[key]['node_name'] for key in affected}
        for node in nodes:
            ids = {int(record[key]['guest_id']) for key in affected if record[key]['node_name'] == node}
            _wait_for_applied_tasks(api, node, ids)
        ledger=ReservationLedger(DATA/'reservations.sqlite3')
        try:ledger.reconcile_job(prepared['plan_id'],operation_terminal=True,allocations_reconciled=True)
        finally:ledger.close()


def abandon_plan(prepared):
    """Planning never provisions guests; release only this operation's preview."""
    if not prepared or not prepared.get('requests'):return
    ledger=ReservationLedger(DATA/'reservations.sqlite3')
    try:ledger.reconcile_job(prepared['plan_id'],operation_terminal=True,allocations_reconciled=True)
    finally:ledger.close()


def reconcile_applied_reservations(path, *, api=None):
    """Release only retained apply allocations proved present in provider state.

    Never infer completion from age or a missing worker. Exact saved placements,
    current Terraform identities and terminal provider tasks are all required.
    """
    from crange import ProxmoxAPI
    api = api or ProxmoxAPI()
    path = Path(path)
    project = int(path.name.split('-')[-1])
    ledger = ReservationLedger(DATA / 'reservations.sqlite3')
    released = []
    try:
        for manifest in sorted((path / '.crange-plans').glob('*.json')):
            try:
                saved = ledger.get_job(manifest.stem)
            except PlacementError:
                continue
            if saved['project_id'] != project or saved['state'] != 'running':
                continue
            record = json.loads(manifest.read_text())
            placements = {item['resource_key']: item for item in record.get('placements', [])}
            allocations = saved['allocations']
            if (record.get('plan_id') != manifest.stem or not allocations or
                    any(item.get('operation') for item in allocations) or
                    any(placements.get(item['resource_key'], {}).get('reservation_id') != item['reservation_id']
                        or placements[item['resource_key']]['resolved'] != item['resolved']
                        for item in allocations)):
                raise PlacementError('Retained apply reservation differs from its saved placement manifest.')
            # Reuse the normal apply completion gate: all saved allocations must
            # exist at their reviewed node in Terraform state, have project
            # ownership in PVE, and have no active provider task.
            retained = record['placements']
            if not record.get('apply_scope'):
                # Historical full-project manifests also list unrelated frozen
                # guests. Reconcile only this job's exact allocated resources;
                # their original immutable source must match provider state.
                retained = [placements[item['resource_key']] for item in allocations]
                actual = applied_resources(path)
                for item in retained:
                    values = actual.get(item['resource_key'], {})
                    resolved = item['resolved']
                    disks = values.get('disk') or values.get('disk_device') or []
                    clone = values.get('clone') or []
                    os_config = values.get('operating_system') or []
                    mode = resolved.get('clone_mode')
                    source_matches = (
                        mode in ('linked', 'full') and resolved.get('template_id') is not None and len(clone) == 1
                        and clone[0].get('vm_id') == resolved['template_id']
                        and (clone[0].get('node_name') or values.get('node_name')) == resolved.get('template_node')
                        and bool(clone[0].get('full')) == (mode == 'full')) or (
                        mode == 'archive' and bool(resolved.get('template_file_id')) and len(os_config) == 1
                        and os_config[0].get('template_file_id') == resolved['template_file_id'])
                    storage_matches = bool(disks) and all(
                        disk.get('datastore_id') == resolved.get('datastore_id') for disk in disks)
                    if not disks and mode in ('linked', 'full') and item['resource_key'].startswith(('vm:', 'jumpbox:')):
                        # The pinned provider leaves disk=[] for cloned VMs
                        # whose disks were inherited rather than declared.
                        # Prove their actual storage; never fill it from intent.
                        node, vmid = values.get('node_name'), values.get('vm_id')
                        if node == resolved.get('node_name') and type(vmid) is int:
                            config = api.request('GET', f'/nodes/{node}/qemu/{vmid}/config')
                            allocated = disk_allocations(config)
                            storage_matches = bool(allocated) and set(allocated) == {resolved.get('datastore_id')}
                    if not source_matches or not storage_matches:
                        raise PlacementError('Retained allocation source/storage differs from applied state; reservations retained.')
            prepared = {'plan_id': manifest.stem, 'placements': retained,
                        'requests': allocations, **({'apply_scope': record['apply_scope']} if 'apply_scope' in record else {})}
            finish_apply(path, prepared, succeeded=True, api=api)
            record.update(reservation_state='released', reservation_reconciled_at=time.time(),
                          reservation_reconciliation='exact_applied_state_and_terminal_provider_tasks')
            atomic_json(manifest, record, 0o640)
            released.append(manifest.stem)
        return released
    finally:
        ledger.close()


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
    from crange_distribution import required_capacity, preparation_peak_capacity
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
    if not new:
        return []
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
        # capacity_window expresses observed headroom in configured-RAM
        # units using the approved ratio. Reserve each queued guest once,
        # matching the ledger's nominal subtraction from that window.
        settings = {**policy, **policy.get('nodes', {}).get(node_name, {})}
        ratio = settings.get('memory_overcommit_ratio', 1)
        node['observed_memory_mb'] += resources['memory_mb'] / ratio
        node['committed_cpu_cores'] += resources['cpu_cores']
        storage['committed_gb'] += resources['disk_gb'];storage['free_gb'] -= resources['disk_gb']
    for reservation in pending:
        if reservation['node_name'] in working['nodes'] and reservation['datastore_id'] in working['nodes'][reservation['node_name']]['storages']:
            consume(reservation['node_name'],reservation['datastore_id'],reservation)
    fills=[];planned_caches=set();cache_demands={}
    for row in sorted(new,key=lambda r:(r['image'] or '',r['resource_key'])):
        image=catalog['images'].get(row['image'])
        if not image:raise PlacementError(f'{row["resource_key"]}: publish an approved immutable image before staging.')
        if isinstance(image.get('disk_gb'),bool) or not isinstance(image.get('disk_gb'),(int,float)) or not 0 < image['disk_gb'] < float('inf'):
            raise PlacementError('Published image disk allocation is missing.')
        box=row['box'];intent=intent_for(box,row.get('default_preferred_node'));candidates=[];failures={};excluded_nodes={}
        demand={'memory_mb':int(box.get('memory_mb',512 if row['kind']=='container' else 4096)),
                'cpu_cores':int(box.get('cores',1 if row['kind']=='container' else 2)),
                'disk_gb':image['disk_gb']*(1+policy.get('copy_overhead_fraction',.1))}
        for node_name,node in sorted(working['nodes'].items()):
            reasons=[]
            if not node.get('online'):reasons.append('node-offline')
            if node.get('draining'):reasons.append('node-draining')
            if node_name not in policy.get('allowed_nodes',[]):reasons.append('node-excluded-by-policy')
            if not workload_node_allowed(policy, node_name, row['resource_key'], row['kind']):
                reasons.append('node-reserved-for-routers')
            if intent.get('pinned_node',node_name)!=node_name or node_name not in intent.get('allowed_nodes',policy['allowed_nodes']):
                reasons.append('placement-intent-excludes-node')
            if row['kind'] not in node.get('guest_types',[]):reasons.append('guest-type-not-accepted')
            if not node.get('management_verified'):reasons.append('node-management-not-verified')
            network_eligibility=set(node.get('verified_networks',[])) | set(node.get('bootstrap_planned_networks',[]))
            missing_networks=set(row['networks'])-network_eligibility
            if missing_networks:reasons.append('required-networks-not-verified:'+','.join(sorted(missing_networks)))
            missing_features=set(image.get('required_features') or [])-set(node.get('features',[]))
            if missing_features:reasons.append('required-image-features-not-verified:'+','.join(sorted(missing_features)))
            if reasons:
                excluded_nodes[node_name]=reasons
                continue
            for storage_id,storage in sorted(node.get('storages',{}).items()):
                replicas=[r for r in image.get('replicas',[]) if r['state']=='ready' and r['node']==node_name and (r.get('template_file_id') or (r['datastore_id']==storage_id and 'linked' in r.get('clone_modes_verified',[]) and storage.get('snapshots_verified') and not storage.get('shared')))]
                already=(row['image'],node_name,storage_id) in planned_caches
                cache_demand={'memory_mb':0,'cpu_cores':0,'disk_gb':0}
                if not replicas and not already:
                    if row['image'] not in cache_demands:
                        config={}
                        if not (image.get('probe_resources') or (image.get('memory_mb') and image.get('cpu_cores'))):
                            source=next((r for r in image.get('replicas',[]) if r['state']=='ready'
                                and r.get('template_id') and working['nodes'].get(r['node'],{}).get('online')),None)
                            if source:
                                config=api.request('GET',f'/nodes/{source["node"]}/{"qemu" if row["kind"]=="vm" else "lxc"}/{source["template_id"]}/config')
                        # Published sizing is sufficient for an archived image;
                        # its original template host need not still be online.
                        cache_demands[row['image']]=required_capacity(image,source_config=config)
                    cache_demand=cache_demands[row['image']]
                peak = preparation_peak_capacity(cache_demand, demand)
                total = {key: peak[key] for key in demand}
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
                    reason=str(exc)
                    prefix='cache preparation has insufficient '
                    if reason.startswith(prefix):
                        key=reason[len(prefix):].split()[0]
                        if key in total:
                            from crange_placement import capacity_window
                            mode='linked' if not replicas or not replicas[0].get('template_file_id') else 'full'
                            window=capacity_window(node,policy,node_name,storage,mode)
                            label='guest + cache' if any(cache_demand.values()) else 'guest'
                            units={'memory_mb':'MiB RAM','cpu_cores':'CPU cores','disk_gb':'GiB disk'}
                            reason=(f'{label} preparation needs {total[key]:g} {units[key]}; '
                                    f'available {max(0,window[key]):.1f} '
                                    f'(guest={demand[key]:g}, cache={cache_demand[key]:g})')
                            if key=='memory_mb':
                                reason+=(f'; configured={node["committed_memory_mb"]:.1f} MiB, '
                                         f'observed={node["observed_memory_mb"]:.1f} MiB, '
                                         f'host reserve={window["memory_reserve_mb"]:.1f} MiB; '
                                         'queued guests and live reservations already accounted')
                    failures[node_name+'/'+storage_id]=reason;continue
                score=sum(total[k]/max(allocation['headroom'][k]+total[k],.01) for k in total)/3
                if intent.get('preferred_node')==node_name:score-=min(policy.get('preference_bonus',.05),.1)
                candidates.append((score,node_name,storage_id,not replicas and not already,cache_demand,total))
        if not candidates:
            detail={'node_exclusions':excluded_nodes,'storage_or_capacity_failures':failures}
            raise PlacementError(f'{row["resource_key"]}: no safe cache-plus-guest target: {json.dumps(detail,sort_keys=True)}')
        # A larger empty pool on the same host does not justify another
        # archive copy while a verified (or already planned) cache can fit.
        # Cross-node load balancing and safe capacity fallbacks still apply.
        reusable_nodes={candidate[1] for candidate in candidates if not candidate[3]}
        candidates=[candidate for candidate in candidates
                    if not candidate[3] or candidate[1] not in reusable_nodes]
        _,node_name,storage_id,missing,cache_demand,total=min(candidates,key=lambda x:x[:3])
        # Verification and transfer finish before guests are deployed. The
        # published base remains, but disposable probe CPU/RAM/disks do not.
        steady = dict(demand)
        if missing:
            steady['disk_gb'] += cache_demand['base_disk_gb']
        consume(node_name,storage_id,steady)
        if missing:
            fills.append({'image':row['image'],'node':node_name,'storage':storage_id,'additional_capacity':demand,'resource_keys':[row['resource_key']]})
            planned_caches.add((row['image'],node_name,storage_id))
        elif (row['image'],node_name,storage_id) in planned_caches:
            fill=next(f for f in fills if (f['image'],f['node'],f['storage'])==(row['image'],node_name,storage_id))
            for key in demand:fill['additional_capacity'][key]+=demand[key]
            fill['resource_keys'].append(row['resource_key'])
    return fills
