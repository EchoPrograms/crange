"""Fixed worker bridge for shared-ledger local image caches and reviewed GC.

Lock order: sorted node locks, library flock, catalog transaction, allocation
journal lock, durable artifact pin. Pins do not reacquire library/catalog locks.
These functions run only in the privileged worker, never the HTTP process.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
import uuid

from crange import ProxmoxAPI
from crange_distribution import (DistributionError, FileJournal, ProbeVerifier,
    ReplicaWorker, SSHContainerExec, SSHVolumeHasher, catalog_transaction, required_capacity,
    shared_reservation_validator, UnixContainerBackingVerifier)
from crange_image_cache import CacheError, build_eviction_plan, apply_eviction_plan
from crange_placement import ReservationLedger
import crange_scheduling as scheduling


def _settings():
    policy, catalog = scheduling.read_settings()
    if not policy.get('enabled'):
        raise DistributionError('Automatic distribution is disabled by administrator policy.')
    return policy, catalog


def acceptance_policy(path, node_name, datastore_id):
    """Load an administrator-owned, one-node cache-only acceptance policy."""
    path = Path(path)
    stat = path.stat()
    if path.is_symlink() or stat.st_uid != 0 or stat.st_mode & 0o022:
        raise DistributionError('Acceptance policy must be root-owned and not writable by others.')
    policy = json.loads(path.read_text())
    if (not policy.get('enabled') or policy.get('allowed_nodes') != [node_name] or
            policy.get('approved_storage') != [[node_name, datastore_id]] or
            policy.get('nodes', {}).get(node_name, {}).get('container_cache_preparation_allowed') is not True):
        raise DistributionError('Acceptance policy must approve only this exact isolated CT cache destination.')
    return policy


def _root():
    scheduling.DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    return scheduling.DATA


@contextmanager
def artifact_pin(volume, digest, job_id):
    if not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', job_id):
        raise DistributionError('Invalid artifact pin job identity.')
    path = _root() / 'artifact-pins' / (job_id + '.json')
    scheduling.atomic_json(path, {'volume_id': volume, 'artifact_sha256': digest,
                                 'job_id': job_id, 'state': 'active'})
    # Failed operations retain the pin until owned reconciliation.
    try:
        yield
    except Exception:
        raise
    else:
        path.unlink()


def _ready(image, node, storage):
    return [r for r in image.get('replicas', []) if r.get('cache_local') and r.get('node') == node
            and r.get('datastore_id') == storage and r.get('state') == 'ready'
            and 'linked' in r.get('clone_modes_verified', [])]


def select_probe_bridge(policy):
    """Choose only an administrator-approved isolated bridge before allocation."""
    settings = policy.get('cache', policy)
    if not isinstance(settings, dict):
        raise DistributionError('Cache probe bridge policy is invalid.')
    approved = settings.get('isolated_bridges', [])
    if (not isinstance(approved, list) or not approved or
            any(not isinstance(item, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,15}', item)
                for item in approved)):
        raise DistributionError('Cache preparation requires an approved isolated bridge.')
    configured = settings.get('isolated_bridge')
    if configured is not None:
        if configured not in approved:
            raise DistributionError('Configured cache probe bridge is not in isolated_bridges.')
        return configured
    if len(approved) == 1:
        return approved[0]
    raise DistributionError('Set isolated_bridge when policy approves multiple isolated_bridges.')


def _prove_absent_cache_allocation(api, node, template_id, created_at):
    """Require complete PVE inventory and task evidence before clearing an absent allocation."""
    expected = {f'proxmox{i}' for i in range(1, 6)}
    nodes = api.request('GET', '/nodes')
    if ({row.get('node') for row in nodes} != expected or
            any(row.get('status') != 'online' for row in nodes)):
        raise DistributionError('Cannot reconcile allocation while cluster nodes are unavailable.')
    resources = api.request('GET', '/cluster/resources', {'type': 'vm'})
    if any(str(row.get('vmid')) == str(template_id) for row in resources):
        raise DistributionError('Allocated cache guest exists; owned repair is required.')
    identity = sorted((row.get('node'), row.get('type'), str(row.get('vmid'))) for row in resources)
    inventory_hash = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
    active = api.request('GET', f'/nodes/{node}/tasks', {'source': 'active', 'limit': 1000})
    if len(active) >= 1000:
        raise DistributionError('Active task enumeration may be truncated; retain reservation.')
    if any(str(task.get('id')) == str(template_id) for task in active):
        raise DistributionError('A provider task for the cache allocation is still active.')
    history = api.request('GET', f'/nodes/{node}/tasks', {'source': 'all', 'limit': 1000})
    if len(history) >= 1000:
        raise DistributionError('Provider task history may be truncated; retain reservation.')
    allocation_types = {'qmcreate', 'qmrestore', 'qmclone', 'vzcreate', 'vzrestore', 'vzclone'}
    if any(str(task.get('id')) == str(template_id) and task.get('type') in allocation_types and
           float(task.get('starttime', 0)) >= created_at - 1 for task in history):
        raise DistributionError('A provider allocation task exists; reconcile its exact terminal result.')
    return {'verified': True, 'node': node, 'template_id': int(template_id),
            'inventory_complete': True, 'target_absent': True,
            'active_task_scan_complete': True, 'history_scan_complete': True,
            'cluster_guest_inventory_sha256': inventory_hash,
            'active_task_count': len(active), 'history_task_count': len(history),
            'verified_at': time.time()}


def prepare_cache(*, project_id, image_id, node_name, datastore_id='local-lvm',
                  additional_capacity=None, owner='cli', job_id=None, acceptance_policy_path=None):
    """Admit cache/probe plus optional imminent guest demand, then verify local base.

    Failure retains reservations, provider IDs/tasks and artifact pin. Success
    discovers actual allocations before releasing only this preparation demand.
    The caller must immediately obtain the guest deployment's reviewed plan.
    """
    job_id = job_id or 'cache-' + uuid.uuid4().hex
    if not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', job_id):
        raise DistributionError('Invalid cache preparation job ID.')
    policy, _ = _settings()
    if acceptance_policy_path is not None:
        if additional_capacity:
            raise DistributionError('Acceptance cache preparation cannot include guest deployment demand.')
        policy = acceptance_policy(acceptance_policy_path, node_name, datastore_id)
    api = ProxmoxAPI()
    journal = FileJournal(_root() / 'cache-journal')
    ledger = ReservationLedger(_root() / 'reservations.sqlite3')
    catalog_path = scheduling.CONFIG / 'image-catalog.json'
    result = {}
    try:
        with scheduling.node_locks([node_name], library=True):
            def update(catalog):
                image = catalog['images'].get(image_id)
                if not image or ('*' not in image['approved_projects'] and project_id not in image['approved_projects']):
                    raise DistributionError('Image is not approved for the selected project.')
                if acceptance_policy_path is not None and image['kind'] != 'container':
                    raise DistributionError('Acceptance cache policy is restricted to container images.')
                ready = _ready(image, node_name, datastore_id)
                if ready:
                    if len(ready) != 1: raise DistributionError('Ambiguous cache generation; reconcile.')
                    kind = 'qemu' if image['kind'] == 'vm' else 'lxc'
                    config = api.request('GET', f'/nodes/{node_name}/{kind}/{ready[0]["template_id"]}/config')
                    if int(config.get('template', 0)) != 1 or config.get('lock') or (
                            f'crange-job-{ready[0]["job_id"]}' not in str(config.get('tags', '')).split(';') or
                            'crange-image' not in str(config.get('tags', '')).split(';')):
                        raise DistributionError('Existing cache changed; owned repair required.')
                    result.update(ready[0], reused=True)
                    return catalog
                bridge = select_probe_bridge(policy)
                for file in journal.directory.glob('*.json'):
                    previous = json.loads(file.read_text())
                    if ((previous.get('image_id'), previous.get('node'), previous.get('datastore_id')) ==
                            (image_id, node_name, datastore_id) and previous.get('state') not in
                            ('evicted', 'cleaned-owned', 'reconciled-absent')):
                        raise DistributionError('Cache fill already exists as ' + previous['job_id'] + '; attach/reconcile its exact generation.')
                settings = policy.get('cache', policy)
                source_identity = image.get('source_identity') or image.get('verification', {}).get('source_identity')
                if not source_identity: raise DistributionError('Captured source identity evidence is missing.')
                verify = ProbeVerifier(bridge, settings.get('isolated_bridges', []), source_identity,
                                       ct_exec=SSHContainerExec(), init_system=image.get('init_system', 'systemd'))
                artifact = image.get('artifact', {})
                if (not artifact.get('volume_id', '').startswith('crange-library:backup/') or
                        not artifact.get('source_id') or not artifact.get('node') or not artifact.get('complete_capture')):
                    raise DistributionError('Verified complete central artifact provenance is missing.')
                if not {f'local-linked-{image["kind"]}', f'{image["kind"]}-archive-restore'}.issubset(settings.get('tested_paths', [])):
                    raise DistributionError('Cache linked-clone/restore capabilities have not passed acceptance.')
                demand = required_capacity(image)
                total = dict(demand)
                for key in ('memory_mb', 'cpu_cores', 'disk_gb'):
                    extra = (additional_capacity or {}).get(key, 0)
                    if isinstance(extra, bool) or not isinstance(extra, (int, float)) or not math.isfinite(extra) or extra < 0:
                        raise DistributionError('Invalid trusted staging capacity demand.')
                    total[key] += extra
                inventory = scheduling.discover(api, policy)
                allocation = ledger.reserve_preparation(project_id=project_id, job_id=job_id,
                    owner=owner, image_id=image_id, catalog=catalog, node_name=node_name,
                    datastore_id=datastore_id, capacity=total, inventory=inventory, policy=policy)
                ledger.mark_running(job_id)
                validator = shared_reservation_validator(ledger, job_id, allocation,
                                                         image_id, image['artifact_sha256'])
                archive = {'state': 'exported', 'source_id': artifact.get('source_id'),
                    'node': artifact.get('node'), 'volume_id': artifact.get('volume_id'),
                    'complete_capture': artifact.get('complete_capture'),
                    'artifact_sha256': image['artifact_sha256']}
                worker = ReplicaWorker(api, journal, verify,
                    approved_storage=[(node_name, datastore_id)],
                    tested_paths=settings.get('tested_paths', []),
                    allocation_lock=journal.allocation_lock, hash_volume=SSHVolumeHasher(),
                    reservation_validator=validator, backing_verifier=UnixContainerBackingVerifier())
                with artifact_pin(artifact.get('volume_id'), image['artifact_sha256'], job_id):
                    updated = worker.prepare(catalog, image_id, source_node=artifact.get('node'),
                        source_id=artifact.get('source_id'), node=node_name, datastore_id=datastore_id,
                        job_id=job_id, archive=archive, reservation=allocation)
                result.update(_ready(updated['images'][image_id], node_name, datastore_id)[0], reused=False)
                return updated
            catalog_transaction(catalog_path, update)
            if not result.get('reused'):
                inventory = scheduling.discover(api, policy)
                vmid = str(result['template_id'])
                actual = inventory['guests'].get(vmid)
                if not actual or not actual.get('template') or actual['node_name'] != node_name:
                    raise DistributionError('Cache allocation reconciliation is incomplete; reservation retained.')
                record = json.loads((journal.directory / (job_id + '.json')).read_text())
                if (record.get('state') != 'ready' or str(record.get('probe_id')) in inventory['guests'] or
                        any(t['state'] != 'confirmed-terminal' for t in record.get('provider_tasks', []))):
                    raise DistributionError('Provider tasks remain uncertain; reservation retained.')
                ledger.reconcile_job(job_id, operation_terminal=True, allocations_reconciled=True)
        return {'job_id': job_id, 'image_id': image_id, **result}
    finally:
        ledger.close()


# Reviewed host-only code: argv subprocesses, no guest command or raw shell input.
_BACKING_SCAN = r'''
import json, os, subprocess, sys
volumes=json.load(sys.stdin)
def call(argv):
    return subprocess.run(argv,check=True,capture_output=True,text=True,timeout=120).stdout
rows=json.loads(call(['lvs','--reportformat','json','-a','-o','lv_path,vg_name,lv_name,origin']))['report'][0]['lv']
by_path={os.path.realpath(r['lv_path']):r for r in rows if r.get('lv_path')}
by_name={(r['vg_name'],r['lv_name']):r for r in rows}
paths={v:call(['pvesm','path',v]).strip() for v in volumes}
identity={os.path.realpath(p):v for v,p in paths.items()}
result={}; vg_storage={}
for volume,path in paths.items():
    sid=volume.split(':')[0]; chain=[volume]; actual=os.path.realpath(path)
    if path.startswith('/dev/'):
        if actual not in by_path: raise RuntimeError('unresolved logical volume')
        row=by_path[actual]; vg_storage[row['vg_name']]=sid; seen=set()
        while row.get('origin','').strip():
            origin=row['origin'].strip(' []'); key=(row['vg_name'],origin)
            if key in seen or key not in by_name: raise RuntimeError('unresolved origin')
            seen.add(key); chain.append(sid+':'+origin); row=by_name[key]
    else:
        frames=json.loads(call(['qemu-img','info','--force-share','--backing-chain','--output=json',path]))
        if not isinstance(frames,list): frames=[frames]
        for frame in frames:
            filename=frame.get('filename')
            if not filename: raise RuntimeError('missing backing filename')
            chain.append(identity.get(os.path.realpath(filename),filename))
    result[volume]=sorted(set(chain))
physical=[]
for row in rows:
    if row.get('origin','').strip() and row['vg_name'] in vg_storage:
        sid=vg_storage[row['vg_name']]; current=row; seen=set(); chain=[]
        while current.get('origin','').strip():
            origin=current['origin'].strip(' []'); key=(current['vg_name'],origin)
            if key in seen or key not in by_name: raise RuntimeError('unresolved physical origin')
            seen.add(key); chain.append(sid+':'+origin); current=by_name[key]
        physical.append({'physical_volume':sid+':'+row['lv_name'],'backing_volumes':chain})
print(json.dumps({'volumes':result,'physical_dependencies':physical,'complete':True}))
'''


class SSHBackingInspector:
    def __init__(self, run=None): self.run = run or subprocess.run

    def __call__(self, node, volumes):
        if node not in {f'proxmox{i}' for i in range(1, 7)} or any(
                not re.fullmatch(r'[a-zA-Z0-9_-]+:[a-zA-Z0-9_/.-]+', v) for v in volumes):
            raise CacheError('Unsupported provider volume identity; backing proof incomplete.')
        result = self.run(['ssh', '-o', 'BatchMode=yes', node,
                          'python3 -c ' + shlex.quote(_BACKING_SCAN)],
                         input=json.dumps(sorted(set(volumes))), check=True,
                         capture_output=True, text=True, timeout=600)
        mapping = json.loads(result.stdout)
        if (mapping.get('complete') is not True or set(mapping.get('volumes', {})) != set(volumes) or
                any(not isinstance(v, list) or not v for v in mapping['volumes'].values()) or
                not isinstance(mapping.get('physical_dependencies'), list)):
            raise CacheError('Host backing inspector did not cover every provider volume.')
        return mapping


def _volumes(config):
    volumes = []
    for key, value in config.items():
        if not re.fullmatch(r'(?:scsi|sata|virtio|ide|efidisk|tpmstate|mp)\d+|rootfs|unused\d+', key): continue
        volume = str(value).split(',')[0]
        if 'media=cdrom' in str(value) or volume.startswith('none'): continue
        if ':' not in volume or volume.startswith('/'):
            raise CacheError('External/unmanaged volume prevents complete dependency evidence.')
        volumes.append(volume)
    return sorted(set(volumes))


def collect_cache_evidence(api, catalog, image_id, node_name, template_id, *,
                           ledger, journal, policy, backing_inspector=None, hasher=None):
    """Fresh complete cluster/native-snapshot/LVM+QCOW2 origin evidence.

    A partial API/SSH result raises, never turns inaccessible guests into empty
    dependency lists. Both current and pending configs, every native snapshot and
    all homelab guests are inspected. Unsupported stores fail closed.
    """
    inspector = backing_inspector or SSHBackingInspector()
    hasher = hasher or SSHVolumeHasher()
    observed = time.time()
    permissions = api.request('GET', '/access/permissions', {'path': '/vms'})
    if not permissions.get('/vms', {}).get('VM.Audit'):
        raise CacheError('Cluster-wide VM.Audit is required to prove complete inventory.')
    nodes = api.request('GET', '/nodes')
    expected_nodes = set(policy.get('expected_cluster_nodes', [f'proxmox{i}' for i in range(1, 6)]))
    if set(n['node'] for n in nodes) != expected_nodes or any(n.get('status') != 'online' for n in nodes):
        raise CacheError('Every cluster node must be reachable for complete backing evidence.')
    resources = api.request('GET', '/cluster/resources', {'type': 'vm'})
    identity = lambda rows: sorted((r['node'], r['type'], int(r['vmid'])) for r in rows)
    guests, snapshots, operations, configs, pending_configs, snapshot_configs, node_volumes = [], [], [], {}, {}, {}, {}
    for resource in resources:
        node, kind, vmid = resource['node'], resource['type'], int(resource['vmid'])
        if kind not in ('qemu', 'lxc'): raise CacheError('Unsupported cluster guest type.')
        base = f'/nodes/{node}/{kind}/{vmid}'
        current = api.request('GET', base + '/config', {'current': 1})
        pending = api.request('GET', base + '/config')
        configs[(node, vmid)] = current
        pending_configs[(node, vmid)] = pending
        allvolumes = sorted(set(_volumes(current) + _volumes(pending)))
        row = {'node': node, 'vmid': vmid, 'backing_volumes': allvolumes, 'status': resource.get('status')}
        guests.append(row)
        if current.get('lock') or pending.get('lock'):
            operations.append({'node': node, 'template_id': vmid, 'state': 'provider-lock'})
        names = api.request('GET', base + '/snapshot')
        for snap in names:
            if snap['name'] == 'current': continue
            snapconfig = api.request('GET', base + '/config', {'snapshot': snap['name']})
            snapshot_configs[(node, vmid, snap['name'])] = snapconfig
            volumes = _volumes(snapconfig)
            snapshots.append({'node': node, 'vmid': vmid, 'name': snap['name'], 'backing_volumes': volumes})
            allvolumes.extend(volumes)
        node_volumes.setdefault(node, set()).update(allvolumes)
    for node in nodes:
        tasks = api.request('GET', f'/nodes/{node["node"]}/tasks', {'source': 'active', 'limit': 1000})
        if len(tasks) >= 1000: raise CacheError('Active task enumeration may be truncated.')
        operations.extend({'node': node['node'], 'state': 'running', 'upid': task['upid']} for task in tasks)
    # Include unattached provider volumes: an orphan thin child still owns its base.
    for node in nodes:
        name = node['node']
        for storage in api.request('GET', f'/nodes/{name}/storage'):
            if not storage.get('active'):
                raise CacheError('Unavailable datastore prevents complete origin inventory.')
            if not {'images', 'rootdir'}.intersection(str(storage.get('content', '')).split(',')):
                continue
            content = api.request('GET', f'/nodes/{name}/storage/{storage["storage"]}/content')
            for row in content:
                if row.get('content') in ('images', 'rootdir'):
                    node_volumes.setdefault(name, set()).add(row['volid'])
    for node, volumes in node_volumes.items():
        report = inspector(node, list(volumes))
        if report.get('complete') is not True:
            raise CacheError('Physical backing inventory is incomplete.')
        mapping = report['volumes']
        configured = set(v for row in guests + snapshots if row['node'] == node for v in row['backing_volumes'])
        physical = report['physical_dependencies']
        physical += [{'physical_volume': volume, 'backing_volumes': chain}
                     for volume, chain in mapping.items() if volume not in configured and len(chain) > 1]
        guests.extend({'node': node, 'vmid': 'physical:' + row['physical_volume'],
                       'backing_volumes': row['backing_volumes']} for row in physical)
        for row in guests + snapshots:
            if row['node'] == node and not str(row['vmid']).startswith('physical:'):
                row['backing_volumes'] = sorted(set(v for volume in row['backing_volumes'] for v in mapping[volume]))
    # Detect concurrent config/snapshot/guest changes during slow host inspection.
    if identity(api.request('GET', '/cluster/resources', {'type': 'vm'})) != identity(resources):
        raise CacheError('Cluster guest inventory changed during backing inspection; retry review.')
    for resource in resources:
        key = (resource['node'], int(resource['vmid']))
        current = api.request('GET', f'/nodes/{resource["node"]}/{resource["type"]}/{resource["vmid"]}/config', {'current': 1})
        if _volumes(current) != _volumes(configs[key]) or current.get('lock') != configs[key].get('lock'):
            raise CacheError('Guest volume/lock identity changed during inspection.')
        pending = api.request('GET', f'/nodes/{resource["node"]}/{resource["type"]}/{resource["vmid"]}/config')
        if _volumes(pending) != _volumes(pending_configs[key]) or pending.get('lock') != pending_configs[key].get('lock'):
            raise CacheError('Pending guest disk identity changed during inspection.')
        native = api.request('GET', f'/nodes/{resource["node"]}/{resource["type"]}/{resource["vmid"]}/snapshot')
        expected = sorted(s['name'] for s in snapshots if s['node'] == key[0] and s['vmid'] == key[1])
        if sorted(s['name'] for s in native if s['name'] != 'current') != expected:
            raise CacheError('Native snapshots changed during inspection.')
        for name in expected:
            snapconfig = api.request('GET', f'/nodes/{resource["node"]}/{resource["type"]}/{resource["vmid"]}/config', {'snapshot': name})
            if _volumes(snapconfig) != _volumes(snapshot_configs[(key[0], key[1], name)]):
                raise CacheError('Native snapshot disks changed during inspection.')

    reservations = []
    for job in ledger.status():
        if job['state'] == 'released': continue
        reservations.extend({'image_id': allocation.get('image'), 'template_id': allocation['resolved'].get('template_id'),
                             'state': job['state'], 'job_id': job['job_id']} for allocation in job['allocations'])
    for file in journal.directory.glob('*.json'):
        record = json.loads(file.read_text())
        if record.get('state') not in ('ready', 'evicted', 'cleaned-owned', 'reconciled-absent'):
            operations.append({'node': record.get('node'), 'image_id': record.get('image_id'),
                               'template_id': record.get('template_id'), 'state': 'uncertain-journal'})
    pins = list(policy.get('local_critical_pins', []))
    candidate_config = configs.get((node_name, template_id))
    if not candidate_config: raise CacheError('Candidate absent from complete provider inventory.')
    artifact = catalog['images'][image_id]['artifact']
    digest = hasher(node_name, artifact['volume_id'])
    proof = {'volume_id': artifact['volume_id'], 'artifact_sha256': digest,
             'verified_at': time.time(), 'verified': digest == catalog['images'][image_id]['artifact_sha256']}
    evidence = {'observed_at': observed, 'cluster_complete': True, 'snapshots_complete': True,
        'backing_complete': True, 'reservations_complete': True, 'operations_complete': True,
        'pins_complete': True, 'candidate': {'node': node_name, 'template_id': template_id,
                                           'volumes': _volumes(candidate_config)},
        'guests': guests, 'snapshots': snapshots, 'reservations': reservations,
        'operations': operations, 'pins': pins, 'central_verification': proof}
    evidence['revision'] = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    return evidence


def review_cache_gc(*, image_id, node_name, template_id, owner='cli'):
    policy, catalog = _settings()
    api = ProxmoxAPI()
    journal = FileJournal(_root() / 'cache-journal')
    ledger = ReservationLedger(_root() / 'reservations.sqlite3')
    try:
        with scheduling.node_locks(policy['allowed_nodes'], library=True):
            evidence = collect_cache_evidence(api, catalog, image_id, node_name, template_id,
                ledger=ledger, journal=journal, policy=policy)
            plan = build_eviction_plan(catalog, image_id, node_name, template_id, evidence)
            plan_id = plan['plan_hash']
            scheduling.atomic_json(_root() / 'cache-gc-plans' / (plan_id + '.json'),
                                   {'plan': plan, 'owner': owner, 'state': 'reviewed'})
            return plan_id
    finally: ledger.close()


def apply_cache_gc(plan_id):
    if not re.fullmatch(r'[a-f0-9]{64}', plan_id): raise CacheError('Invalid reviewed cache GC plan ID.')
    path = _root() / 'cache-gc-plans' / (plan_id + '.json')
    review = json.loads(path.read_text())
    if review['state'] != 'reviewed': raise CacheError('GC plan is already completed or uncertain.')
    policy, _ = _settings()
    api = ProxmoxAPI()
    journal = FileJournal(_root() / 'cache-journal')
    ledger = ReservationLedger(_root() / 'reservations.sqlite3')
    plan = review['plan']
    try:
        with scheduling.node_locks(policy['allowed_nodes'], library=True):
            def update(catalog):
                with journal.allocation_lock():
                    def refresh():
                        return catalog, collect_cache_evidence(api, catalog, plan['image_id'],
                            plan['node'], plan['template_id'], ledger=ledger, journal=journal, policy=policy)
                    review['state'] = 'applying'
                    scheduling.atomic_json(path, review)
                    from contextlib import nullcontext
                    updated = apply_eviction_plan(api, plan, refresh=refresh, hash_volume=SSHVolumeHasher(),
                        journal=journal, lock=nullcontext,
                        artifact_pin=lambda volume, digest: artifact_pin(volume, digest, 'gc-' + plan_id[:24]))
                    return updated
            catalog_transaction(scheduling.CONFIG / 'image-catalog.json', update)
            review['state'] = 'completed'
            scheduling.atomic_json(path, review)
            return {'plan_id': plan_id, 'state': 'completed', 'template_id': plan['template_id']}
    finally: ledger.close()


def _reconcile_absent_cache_job(job_id, record, *, catalog, api, journal, ledger):
    """Release only an allocated cache request proven absent from PVE and its task history."""
    if (record.get('job_id') != job_id or record.get('state') not in ('allocated', 'reconciled-absent') or
            record.get('provider_tasks', []) != [] or record.get('probe_id') is not None):
        raise DistributionError('Only a task-free allocated cache request can be reconciled as absent.')
    node, vmid = record.get('node'), record.get('template_id')
    if (node not in {f'proxmox{i}' for i in range(1, 6)} or
            isinstance(vmid, bool) or not isinstance(vmid, int) or vmid < 100 or
            record.get('allocation_node') != node or record.get('datastore_id') != 'local-lvm' or
            record.get('kind') != 'qemu'):
        raise DistributionError('Allocated cache request identity is incomplete or unsupported.')
    image_id = record.get('image_id')
    image = catalog.get('images', {}).get(image_id)
    artifact = image.get('artifact', {}) if image else {}
    if (not image or record.get('artifact_sha256') != image.get('artifact_sha256') or
            record.get('source_node') != artifact.get('node') or
            record.get('source_id') != artifact.get('source_id') or
            any(r.get('node') == node and int(r.get('template_id', -1)) == vmid
                for r in image.get('replicas', []))):
        raise DistributionError('Catalog still references the allocated cache identity.')
    reservation = record.get('reservation')
    saved = ledger.get_job(job_id)
    if (saved.get('state') not in ('running', 'released') or saved.get('project_id') not in range(1, 256) or
            saved.get('allocations') != [reservation] or not isinstance(reservation, dict) or
            reservation.get('image') != image_id or reservation.get('artifact_sha256') != image['artifact_sha256'] or
            reservation.get('resolved', {}).get('node_name') != node or
            reservation.get('resolved', {}).get('datastore_id') != record.get('datastore_id')):
        raise DistributionError('Reservation ledger does not exactly own the allocated cache request.')
    pin_path = _root() / 'artifact-pins' / (job_id + '.json')
    if (saved.get('state') == 'released' and record.get('state') == 'reconciled-absent' and
            record.get('absence_verified') is True and isinstance(record.get('absence_evidence'), dict) and
            not pin_path.exists()):
        return {'job_id': job_id, 'state': 'released', 'template_id': vmid, 'allocation_absent': True}
    try:
        pin = json.loads(pin_path.read_text())
    except (OSError, ValueError) as exc:
        raise DistributionError('Exact artifact pin is missing or unreadable; retain reservation.') from exc
    artifact = image.get('artifact', {})
    if pin != {'volume_id': artifact.get('volume_id'), 'artifact_sha256': image['artifact_sha256'],
               'job_id': job_id, 'state': 'active'}:
        raise DistributionError('Artifact pin does not match the exact cache request.')
    journal_path = journal.directory / (job_id + '.json')
    try:
        created_at = float(record.get('created_at', journal_path.stat().st_mtime))
    except (OSError, TypeError, ValueError) as exc:
        raise DistributionError('Cache request creation time is unavailable; retain reservation.') from exc
    evidence = _prove_absent_cache_allocation(api, node, vmid, created_at)
    if record.get('state') == 'allocated':
        record.update(state='reconciled-absent', absence_verified=True, absence_evidence=evidence)
        journal(record)
    elif (record.get('absence_verified') is not True or
          not isinstance(record.get('absence_evidence'), dict)):
        raise DistributionError('Existing absent-allocation receipt is incomplete; retain reservation.')
    if saved['state'] == 'running':
        ledger.reconcile_job(job_id, operation_terminal=True, allocations_reconciled=True)
    pin_path.unlink()
    return {'job_id': job_id, 'state': 'released', 'template_id': vmid, 'allocation_absent': True}


def reconcile_cache_job(job_id):
    """Release a completed published cache fill after fresh task/allocation checks.

    Partial or untracked provider requests require reviewed ownership repair;
    this helper never guesses that an absent VM means a timed-out POST is terminal.
    """
    if not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', job_id): raise DistributionError('Invalid cache job identity.')
    policy, catalog = _settings()
    api = ProxmoxAPI()
    journal = FileJournal(_root() / 'cache-journal')
    record = json.loads((journal.directory / (job_id + '.json')).read_text())
    node = record['node']
    ledger = ReservationLedger(_root() / 'reservations.sqlite3')
    try:
        with scheduling.node_locks([node], library=True), journal.allocation_lock():
            if record.get('state') in ('allocated', 'reconciled-absent'):
                return _reconcile_absent_cache_job(job_id, record, catalog=catalog, api=api,
                                                   journal=journal, ledger=ledger)
            if record.get('state') != 'ready':
                raise DistributionError('Partial cache job needs reviewed owned repair; reservations retained.')
            published = [r for r in catalog['images'][record['image_id']]['replicas']
                         if r.get('job_id') == job_id and r.get('template_id') == record['template_id']]
            if len(published) != 1: raise DistributionError('Verified generation not published; reservation retained.')
            for task in record.get('provider_tasks', []):
                path = task.get('path', '')
                task_node = task.get('node') or path.split('/')[2]
                from urllib.parse import quote
                status = api.request('GET', f'/nodes/{task_node}/tasks/{quote(task["upid"], safe="")}/status')
                if status.get('status') != 'stopped':
                    raise DistributionError('Provider operation remains active or uncertain.')
            inventory = scheduling.discover(api, policy)
            actual = inventory['guests'].get(str(record['template_id']))
            if (not actual or actual['node_name'] != node or not actual.get('template') or
                    f'crange-job-{job_id}' not in actual.get('tags', '').split(';') or
                    str(record.get('probe_id')) in inventory['guests']):
                raise DistributionError('Actual cache/probe ownership reconciliation is incomplete.')
            ledger.reconcile_job(job_id, operation_terminal=True, allocations_reconciled=True)
            pin = _root() / 'artifact-pins' / (job_id + '.json')
            pin.unlink(missing_ok=True)
            return {'job_id': job_id, 'state': 'released', 'template_id': record['template_id']}
    finally: ledger.close()


def resume_cache_template(job_id):
    """Resume one terminal archive restore after proving its exact owned template."""
    if not re.fullmatch(r'cache-[a-zA-Z0-9-]{1,64}', job_id):
        raise DistributionError('Invalid cache job identity.')
    policy, catalog = _settings()
    api = ProxmoxAPI()
    journal = FileJournal(_root() / 'cache-journal')
    ledger = ReservationLedger(_root() / 'reservations.sqlite3')
    journal_path = journal.directory / (job_id + '.json')
    pin_path = _root() / 'artifact-pins' / (job_id + '.json')
    try:
        with scheduling.node_locks(['proxmox2'], library=True), journal.allocation_lock():
            record = json.loads(journal_path.read_text())
            image_id = record.get('image_id')
            image = catalog.get('images', {}).get(image_id)
            if (record.get('job_id') != job_id or record.get('state') != 'converting-template' or
                    record.get('node') != 'proxmox2' or record.get('allocation_node') != 'proxmox2' or
                    record.get('datastore_id') != 'local-lvm' or record.get('kind') != 'qemu' or
                    not isinstance(record.get('template_id'), int) or not image or
                    record.get('artifact_sha256') != image.get('artifact_sha256')):
                raise DistributionError('Cache journal is not the exact resumable P2 restore.')
            artifact = image.get('artifact', {})
            if (artifact.get('node') != record.get('source_node') or
                    str(artifact.get('source_id')) != str(record.get('source_id')) or
                    artifact.get('complete_capture') is not True or
                    not artifact.get('volume_id', '').startswith('crange-library:backup/')):
                raise DistributionError('Immutable source artifact does not match the cache journal.')
            replicas = [r for r in image.get('replicas', [])
                        if r.get('node') == 'proxmox2' and str(r.get('template_id')) == str(record['template_id'])]
            if replicas:
                raise DistributionError('Catalog already references the partial cache allocation.')
            saved = ledger.get_job(job_id)
            reservation = record.get('reservation')
            if (saved.get('state') != 'running' or saved.get('allocations') != [reservation] or
                    reservation.get('image') != image_id or
                    reservation.get('artifact_sha256') != image['artifact_sha256'] or
                    reservation.get('resolved', {}).get('node_name') != 'proxmox2' or
                    reservation.get('resolved', {}).get('datastore_id') != 'local-lvm'):
                raise DistributionError('Exact running cache reservation is missing or changed.')
            if (not shared_reservation_validator(ledger, job_id, reservation, image_id,
                                                 image['artifact_sha256'])(
                    reservation, 'proxmox2', 'local-lvm', required_capacity(image))):
                raise DistributionError('Cache reservation does not cover the reviewed image capacity.')
            try:
                pin = json.loads(pin_path.read_text())
            except (OSError, ValueError) as exc:
                raise DistributionError('Exact artifact pin is missing; retain reservation.') from exc
            if pin != {'volume_id': artifact['volume_id'], 'artifact_sha256': image['artifact_sha256'],
                       'job_id': job_id, 'state': 'active'}:
                raise DistributionError('Artifact pin does not match this restored cache job.')
            if not record.get('provider_tasks') or any(t.get('state') != 'confirmed-terminal'
                                                       for t in record['provider_tasks']):
                raise DistributionError('Restore task is not recorded as confirmed terminal.')
            from urllib.parse import quote
            for task in record['provider_tasks']:
                task_node = task.get('node') or task.get('path', '').split('/')[2]
                upid = str(task.get('upid', ''))
                if (task_node != 'proxmox2' or
                        f':qmrestore:{record["template_id"]}:' not in upid):
                    raise DistributionError('Journal task is not the exact P2 VM restore operation.')
                status = api.request('GET', f'/nodes/{task_node}/tasks/{quote(upid, safe="")}/status')
                if status.get('status') != 'stopped' or status.get('exitstatus') != 'OK':
                    raise DistributionError('Recorded restore task has not completed successfully.')
            vmid = record['template_id']
            resources = api.request('GET', '/cluster/resources', {'type': 'vm'})
            matches = [r for r in resources if str(r.get('vmid')) == str(vmid)]
            if (len(matches) != 1 or matches[0].get('type') != 'qemu' or
                    matches[0].get('node') != 'proxmox2' or matches[0].get('pool') != 'crange-iac' or
                    matches[0].get('status') != 'stopped'):
                raise DistributionError('VMID is not the exact stopped P2 cache allocation in crange-iac.')
            active = api.request('GET', '/nodes/proxmox2/tasks', {'source': 'active', 'limit': 1000})
            if len(active) >= 1000 or any(str(t.get('id')) == str(vmid) for t in active):
                raise DistributionError('A provider task for the restored cache VM is active or ambiguous.')
            config = api.request('GET', f'/nodes/proxmox2/qemu/{vmid}/config')
            marker = f'crange-job-{job_id}'
            tags = set(str(config.get('tags', '')).split(';'))
            scsi_parts = config.get('scsi0', '').split(',')
            configured_disks = {k for k in config if re.fullmatch(r'(?:scsi|sata|virtio|ide)\d+', k)}
            if ('crange-image' not in tags or marker not in tags or config.get('lock') or
                    int(config.get('template', 0)) != 1 or
                    configured_disks != {'scsi0', 'ide2'} or
                    not scsi_parts or scsi_parts[0] != f'local-lvm:base-{vmid}-disk-0' or
                    'size=20G' not in scsi_parts[1:] or
                    config.get('ide2', '').split(',', 1)[0] != f'local-lvm:vm-{vmid}-cloudinit' or
                    'bridge=' + select_probe_bridge(policy) not in config.get('net0', '') or
                    'link_down=1' not in config.get('net0', '')):
                raise DistributionError('Restored VM configuration does not match the owned stopped template.')
            snapshots = api.request('GET', f'/nodes/proxmox2/qemu/{vmid}/snapshot')
            if [s.get('name') for s in snapshots] != ['current']:
                raise DistributionError('Unexpected snapshot state on the partial cache template.')
            pool = api.request('GET', '/pools/crange-iac')
            if not any(m.get('type') == 'qemu' and m.get('node') == 'proxmox2' and
                       str(m.get('vmid')) == str(vmid) for m in pool.get('members', [])):
                raise DistributionError('Exact restored template pool membership cannot be confirmed.')
            if image.get('kind') != 'vm' or image.get('disk_gb') != 20:
                raise DistributionError('Restored disk size does not match the reviewed P2 cache image.')
            settings = policy.get('cache', policy)
            bridge = select_probe_bridge(policy)
            source_identity = image.get('source_identity') or image.get('verification', {}).get('source_identity')
            if (not source_identity or not {'local-linked-vm', 'vm-archive-restore'}.issubset(
                    settings.get('tested_paths', []))):
                raise DistributionError('Reviewed identity and P2 clone/restore acceptance are required.')
            verify = ProbeVerifier(bridge, settings.get('isolated_bridges', []),
                source_identity,
                ct_exec=SSHContainerExec(), init_system=image.get('init_system', 'systemd'))
            allocation = reservation
            validator = shared_reservation_validator(ledger, job_id, allocation,
                                                       image_id, image['artifact_sha256'])
            worker = ReplicaWorker(api, journal, verify,
                approved_storage=[('proxmox2', 'local-lvm')],
                tested_paths=settings.get('tested_paths', []),
                allocation_lock=journal.allocation_lock,
                reservation_validator=validator, backing_verifier=UnixContainerBackingVerifier())
            # The existing restore already created a template; resume only performs
            # disposable identity verification, exact probe cleanup, then publish.
            updated = worker.resume_template(catalog, image_id, record=record)
            catalog_transaction(scheduling.CONFIG / 'image-catalog.json', lambda _catalog: updated)
            inventory = scheduling.discover(api, policy)
            actual = inventory['guests'].get(str(vmid))
            if (not actual or actual.get('node_name') != 'proxmox2' or not actual.get('template') or
                    str(record.get('probe_id')) in inventory['guests'] or record.get('state') != 'ready' or
                    any(t.get('state') != 'confirmed-terminal' for t in record.get('provider_tasks', []))):
                raise DistributionError('Resumed cache verification is incomplete; reservation retained.')
            ledger.reconcile_job(job_id, operation_terminal=True, allocations_reconciled=True)
            pin_path.unlink()
            return {'job_id': job_id, 'state': 'ready', 'template_id': vmid}
    finally:
        ledger.close()
