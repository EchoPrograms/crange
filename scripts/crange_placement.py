"""Deterministic conservative placement and a shared transactional capacity ledger.

Inventory memory is MB, storage GB, CPU logical cores; committed values include
ALL configured guests (including stopped guests). observed_memory_mb is normalized
non-reclaimable host use including platform overhead, not added to commitments.
"""
import copy
import json
import math
import os
import re
import sqlite3
import time
import uuid
from crange_catalog import validate_catalog


class PlacementError(ValueError):
    pass


def _number(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise PlacementError(f'{label}: finite number >= {minimum} required')
    return value


def capacity_window(node, policy, node_name, storage=None, clone_mode='linked'):
    """Configured commitment limits with a live physical-pressure gate.

    Linked local disks may promise more virtual space than the thin pool has,
    but current free space and a physical reserve still bound new writes.
    """
    settings = {**policy, **policy.get('nodes', {}).get(node_name, {})}
    ratios = (('cpu_overcommit_ratio', 3.5),
              ('memory_overcommit_ratio', 2.0),
              ('local_linked_disk_overcommit_ratio', 1.5))
    for field, maximum in ratios:
        value = _number(settings.get(field, 1), field, 1)
        if value > maximum:
            raise PlacementError(f'{field} exceeds the approved {maximum}:1 ceiling')
    memory = _number(node.get('memory_mb'), 'physical memory')
    cpu = _number(node.get('cpu_cores'), 'physical CPU')
    memory_reserve = max(memory * settings.get('memory_reserve_fraction', .25),
                         settings.get('memory_reserve_mb', 4096))
    cpu_limit = cpu * (1 - settings.get('cpu_reserve_fraction', .2)) * settings.get('cpu_overcommit_ratio', 1)
    memory_limit = memory * settings.get('memory_overcommit_ratio', 1) - memory_reserve
    observed_free = memory - memory_reserve - _number(node.get('observed_memory_mb'), 'observed memory')
    result = {'memory_limit_mb': memory_limit, 'cpu_limit_cores': cpu_limit,
              'memory_reserve_mb': memory_reserve,
              'cpu_reserve_cores': cpu * settings.get('cpu_reserve_fraction', .2),
              'memory_mb': min(memory_limit - _number(node.get('committed_memory_mb'), 'configured guest RAM'),
                               observed_free * settings.get('memory_overcommit_ratio', 1)),
              'cpu_cores': cpu_limit - _number(node.get('committed_cpu_cores'), 'configured guest CPU')}
    if storage is not None:
        total = _number(storage.get('total_gb'), 'storage capacity')
        free = _number(storage.get('free_gb'), 'storage free')
        reserve = max(total * settings.get('disk_reserve_fraction', .2), settings.get('disk_reserve_gb', 20))
        linked_local = (clone_mode == 'linked' and not storage.get('shared')
                        and storage.get('snapshots_verified'))
        ratio = settings.get('local_linked_disk_overcommit_ratio', 1) if linked_local else 1
        limit = total * ratio - reserve
        result.update(disk_limit_gb=limit, disk_reserve_gb=reserve,
                      disk_ratio=ratio,
                      disk_gb=min(limit - _number(storage.get('committed_gb'), 'storage commitments'),
                                  (free - reserve) * ratio))
    return result


def schedule_batch(requests, inventory, catalog, policy, reservations=(), now=None, known_existing_allocations=()):
    now = time.time() if now is None else now
    validate_catalog(catalog)
    for config in [policy] + list(policy.get('nodes', {}).values()):
        for key in ('memory_reserve_fraction', 'disk_reserve_fraction', 'cpu_reserve_fraction'):
            if key in config and not 0 <= _number(config[key], key) < 1:
                raise PlacementError(f'{key}: fraction must be below one')
        for key in ('memory_reserve_mb', 'disk_reserve_gb', 'cpu_overcommit_ratio',
                    'memory_overcommit_ratio', 'local_linked_disk_overcommit_ratio'):
            if key in config:
                _number(config[key], key, 1 if key.endswith('overcommit_ratio') else 0)
    if not policy.get('revision') or not policy.get('allowed_nodes'):
        raise PlacementError('policy revision and allowed_nodes required')
    age = now - _number(inventory.get('captured_at'), 'inventory captured_at')
    if age < -5 or age > policy.get('max_inventory_age_seconds', 120):
        raise PlacementError('stale capacity inventory; refresh before planning')
    for key in ('max_inventory_age_seconds', 'review_ttl_seconds', 'max_reviewed_plans'):
        if key in policy:
            _number(policy[key], key, 1)
    for key in ('preference_bonus', 'copy_overhead_fraction', 'max_load'):
        if key in policy:
            _number(policy[key], key)
    if policy.get('preference_bonus', .05) > .1 or policy.get('copy_overhead_fraction', .1) > 1:
        raise PlacementError('preference bonus <= .1 and copy overhead fraction <= 1 required')
    if any(not isinstance(n, str) or not n for n in policy['allowed_nodes']):
        raise PlacementError('allowed_nodes must contain node names')
    if policy.get('clone_mode', 'linked') not in ('linked', 'full'):
        raise PlacementError('clone_mode must be linked or explicit full fallback')
    nodes = copy.deepcopy(inventory.get('nodes', {}))
    pending = {name: {'memory_mb': 0, 'cpu_cores': 0, 'disks': {}} for name in nodes}
    known = {a['reservation_id']: a for a in known_existing_allocations}
    if len(known) != len(known_existing_allocations):
        raise PlacementError('duplicate confirmed reservation allocation')
    for reservation in reservations:
        if reservation['node_name'] not in pending:
            raise PlacementError('reservation references missing node')
        p = pending[reservation['node_name']]
        actual = known.get(reservation.get('reservation_id'), {})
        if actual and (actual.get('node_name') != reservation['node_name'] or actual.get('datastore_id') != reservation['datastore_id']):
            raise PlacementError('confirmed allocation identity does not match reservation')
        for key in ('memory_mb', 'cpu_cores'):
            p[key] += max(0, reservation[key] - _number(actual.get(key, 0), 'confirmed allocation '+key))
        storage = reservation['datastore_id']
        p['disks'][storage] = p['disks'].get(storage, 0) + max(0, reservation['disk_gb'] - _number(actual.get('disk_gb', 0), 'confirmed allocation disk_gb'))
    seen = set()
    prepared = []
    for request in requests:
        r = copy.deepcopy(request)
        key = r.get('resource_key')
        if not isinstance(key, str) or not key or key in seen:
            raise PlacementError('unique resource_key required')
        seen.add(key)
        for field in ('memory_mb', 'cpu_cores', 'disk_gb'):
            _number(r.get(field), field, 0.001)
        r['disk_gb'] += _number(r.get('copy_overhead_gb', r['disk_gb'] * policy.get('copy_overhead_fraction', .1)), 'copy overhead')
        image = catalog['images'].get(r.get('image'))
        if not image or image['kind'] != r.get('kind'):
            raise PlacementError(f'{key}: unknown image or incompatible guest kind')
        if '*' not in image['approved_projects'] and r.get('project_id') not in image['approved_projects']:
            raise PlacementError(f'{key}: image not approved for project')
        intent = r.get('placement', {})
        if not isinstance(intent, dict):
            raise PlacementError('placement must be an object')
        for field in ('preferred_node', 'pinned_node'):
            if intent.get(field) and intent[field] not in nodes:
                raise PlacementError(f'{field}: unknown node')
        if 'allowed_nodes' in intent and (not isinstance(intent['allowed_nodes'], list) or not intent['allowed_nodes'] or any(n not in nodes for n in intent['allowed_nodes'])):
            raise PlacementError('hard allowed_nodes must name known nodes')
        if intent.get('pinned_node') and intent['pinned_node'] not in intent.get('allowed_nodes', policy['allowed_nodes']):
            raise PlacementError('pin excluded by allowed-node policy')
        if r.get('cpu_type') == 'host' and not image.get('host_compatibility_feature'):
            raise PlacementError('host CPU requires image host_compatibility_feature')
        if intent.get('mode', 'auto') not in ('auto', 'pin'):
            raise PlacementError('placement mode must be auto or pin')
        if intent.get('mode') == 'pin' and not intent.get('pinned_node'):
            raise PlacementError('pin requires pinned_node')
        prepared.append((r, image))
    # Limits use existing project/platform commitments supplied by discovery, plus
    # pending plans and this batch; no replacement credit before actual deletion.
    for scope, limits in [('global', policy.get('global_limits', {}))] + [(str(p), v) for p, v in policy.get('project_quotas', {}).items()]:
        usage = inventory.get('quota_usage', {}).get(scope, {})
        for field, limit in limits.items():
            total = usage.get(field, 0) + sum(r.get(field, 0) for r, _ in prepared if scope == 'global' or str(r.get('project_id')) == scope)
            total += sum(max(0, r.get(field, 0) - known.get(r.get('reservation_id'), {}).get(field, 0)) for r in reservations if scope == 'global' or str(r.get('project_id')) == scope)
            if total > limit:
                raise PlacementError(f'{scope}: {field} quota exceeded')
    prepared.sort(key=lambda pair: (len(pair[1].get('replicas', [])), -pair[0]['memory_mb'], -pair[0]['disk_gb'], pair[0]['resource_key']))
    result = []
    for r, image in prepared:
        choices, rejected = [], {}
        intent = r.get('placement', {})
        for name in sorted(nodes):
            node, p = nodes[name], pending[name]
            try:
                allowed = intent.get('allowed_nodes', policy['allowed_nodes'])
                if name not in policy['allowed_nodes'] or name not in allowed or intent.get('pinned_node', name) != name:
                    raise PlacementError('excluded by hard node constraint')
                if not node.get('online') or node.get('draining'):
                    raise PlacementError('node offline or draining')
                if node.get('load', 0) > policy.get('max_load', 1):
                    raise PlacementError('observed workload load exceeds admission threshold')
                if image['architecture'] not in node.get('architectures', []) or r['kind'] not in node.get('guest_types', []):
                    raise PlacementError('guest architecture/type unsupported')
                required = set(image.get('required_features', [])) | set(r.get('required_features', []))
                if r.get('cpu_type') == 'host':
                    required.add(image['host_compatibility_feature'])
                if not required <= set(node.get('features', [])):
                    raise PlacementError('required CPU/firmware capability unavailable')
                networks = set(r.get('networks', []))
                if not networks <= set(node.get('networks', [])) or not networks <= set(node.get('verified_networks', [])) or not node.get('management_verified'):
                    raise PlacementError('network/MTU/management path not verified')
                node_window = capacity_window(node, policy, name)
                available_mem = node_window['memory_mb'] - p['memory_mb']
                available_cpu = node_window['cpu_cores'] - p['cpu_cores']
                if r['memory_mb'] > available_mem or r['cpu_cores'] > available_cpu:
                    raise PlacementError('insufficient RAM/CPU headroom')
                replicas = [x for x in image.get('replicas', []) if x['node'] == name and x['state'] == 'ready']
                if not replicas:
                    raise PlacementError('missing verified local image replica; run preparation job')
                for storage_id, storage in sorted(node.get('storages', {}).items()):
                    if storage_id not in policy.get('allowed_storages', []) or not storage.get('available') or r['kind'] not in storage.get('guest_types', []) :
                        continue
                    if r.get('requires_snapshots') and not storage.get('snapshots_verified'):
                        continue
                    if r.get('resolved_datastore_id') and storage_id != r['resolved_datastore_id']:
                        continue
                    matching = [x for x in replicas if (not r.get('resolved_template_id') or x.get('template_id') == r['resolved_template_id']) and (not r.get('resolved_template_file_id') or x.get('template_file_id') == r['resolved_template_file_id'])]
                    if not matching:
                        continue
                    mode = r.get('resolved_clone_mode') or policy.get('clone_mode', 'linked')
                    candidates = []
                    for candidate in matching:
                        candidate_mode = 'archive' if candidate.get('template_file_id') and not candidate.get('template_id') else mode
                        if candidate_mode == 'linked':
                            if storage.get('shared') or candidate.get('datastore_id') != storage_id or 'linked' not in candidate.get('clone_modes_verified', []) or 'linked' not in storage.get('clone_modes', []) or not storage.get('snapshots_verified'):
                                continue
                        elif candidate_mode == 'full' and 'full' not in storage.get('clone_modes', []):
                            continue
                        if r.get('resolved_clone_mode') and candidate_mode != r['resolved_clone_mode']:
                            continue
                        candidates.append((candidate, candidate_mode))
                    if not candidates:
                        continue
                    replica, clone_mode = sorted(candidates, key=lambda x: (x[0].get('template_id', 0), x[0].get('template_file_id', '')))[0]
                    available_disk = capacity_window(node, policy, name, storage, clone_mode)['disk_gb'] - p['disks'].get(storage_id, 0)
                    if r['disk_gb'] > available_disk:
                        continue
                    score = (r['memory_mb'] / max(available_mem, 1) + r['cpu_cores'] / max(available_cpu, .01) + r['disk_gb'] / max(available_disk, .01)) / 3 + node.get('load', 0) * .1 + node.get('provisioning_tasks', 0) * .1
                    if intent.get('preferred_node') == name:
                        score -= min(policy.get('preference_bonus', .05), .1)
                    choices.append((score, name, storage_id, replica, clone_mode, {'memory_mb': available_mem-r['memory_mb'], 'cpu_cores': available_cpu-r['cpu_cores'], 'disk_gb': available_disk-r['disk_gb']}))
                if not any(c[1] == name for c in choices):
                    raise PlacementError('no approved datastore has disk headroom and verified local linked cache; run reviewed cache preparation (or explicitly authorize full-clone policy)')
            except PlacementError as exc:
                rejected[name] = str(exc)
        if not choices:
            raise PlacementError(f"{r['resource_key']}: no eligible placement: {json.dumps(rejected, sort_keys=True)}")
        _, name, storage, replica, clone_mode, headroom = min(choices, key=lambda c: c[:3])
        pending[name]['memory_mb'] += r['memory_mb']
        pending[name]['cpu_cores'] += r['cpu_cores']
        pending[name]['disks'][storage] = pending[name]['disks'].get(storage, 0) + r['disk_gb']
        preferred = intent.get('preferred_node')
        reason = 'Lowest projected pressure among eligible nodes'
        if preferred == name:
            reason = 'Preferred node is eligible and has sufficient headroom'
        elif preferred:
            reason = f"Preferred {preferred}: {rejected.get(preferred, 'higher projected resource pressure')}; placed on {name}"
        reason += '; archive creation' if clone_mode == 'archive' else '; verified local linked clone' if clone_mode == 'linked' else '; explicit full-clone policy fallback'
        result.append({'resource_key': r['resource_key'], 'project_id': r.get('project_id'), 'image': r['image'], 'artifact_sha256': image['artifact_sha256'], 'resolved': {'node_name': name, 'datastore_id': storage, 'template_id': replica.get('template_id'), 'template_file_id': replica.get('template_file_id'), 'template_node': name, 'clone_mode': clone_mode, 'verified_local_cache': clone_mode == 'linked'}, 'resources': {k: r[k] for k in ('memory_mb', 'cpu_cores', 'disk_gb')}, 'headroom': headroom, 'reason': reason, 'policy_revision': policy['revision']})
    return sorted(result, key=lambda r: r['resource_key'])


class ReservationLedger:
    """SQLite is shared by every entry point; running jobs never auto-expire."""
    def __init__(self, path):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        os.chmod(self.path, 0o600)
        self.db.execute('PRAGMA busy_timeout=30000')
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY, project_id INTEGER, owner TEXT, state TEXT, expires REAL, heartbeat REAL, allocations TEXT)')

    def active_reservations(self, exclude_job_id=None):
        result = []
        for job, project, allocations in self.db.execute("SELECT job_id, project_id, allocations FROM jobs WHERE state != 'released'"):
            if job == exclude_job_id:
                continue
            for a in json.loads(allocations):
                result.append({'project_id': project, 'reservation_id': a['reservation_id'], 'resource_key': a['resource_key'], **a['resolved'], **a['resources']})
        return result

    def reserve_batch(self, requests, inventory, catalog, policy, project_id, job_id, owner, now=None, known_existing_allocations=()):
        now = time.time() if now is None else now
        if not requests or not isinstance(project_id, int) or isinstance(project_id, bool) or not 1 <= project_id <= 255 or not owner or not job_id or any(r.get('project_id') != project_id for r in requests):
            raise PlacementError('valid project, owner, job and nonempty single-project batch required')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.db.execute('SELECT 1 FROM jobs WHERE job_id=?', (job_id,)).fetchone():
                raise PlacementError('job already has reservations; reconcile instead of duplicating')
            count = self.db.execute("SELECT COUNT(*) FROM jobs WHERE project_id=? AND owner=? AND state='reviewed'", (project_id, owner)).fetchone()[0]
            if count >= policy.get('max_reviewed_plans', 2):
                raise PlacementError('outstanding reviewed-plan limit reached')
            placements = schedule_batch(requests, inventory, catalog, policy, self.active_reservations(), now, known_existing_allocations)
            for item in placements:
                item['reservation_id'] = str(uuid.uuid4())
            self.db.execute('INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)', (job_id, project_id, owner, 'reviewed', now + policy.get('review_ttl_seconds', 3600), now, json.dumps(placements, sort_keys=True)))
            self.db.execute('COMMIT')
            return placements
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def reserve_preparation(self, *, project_id, job_id, owner, image_id, catalog,
                            node_name, datastore_id, capacity, inventory, policy, now=None):
        """Admit cache base/probe/transfer capacity without asserting a ready replica.

        Remote discovery/identity checks happen before this short transaction.
        Existing cache/source ownership, artifact pins and node/library locks are
        the fixed worker's responsibility. No catalog entry is fabricated here.
        """
        now = time.time() if now is None else now
        validate_catalog(catalog)
        image = catalog['images'].get(image_id)
        if not image or ('*' not in image['approved_projects'] and project_id not in image['approved_projects']):
            raise PlacementError('cache image is not approved for this project')
        if isinstance(project_id, bool) or not isinstance(project_id, int) or not 1 <= project_id <= 255 or not owner or not job_id:
            raise PlacementError('valid project, owner and job required')
        if now - _number(inventory.get('captured_at'), 'inventory captured_at') > policy.get('max_inventory_age_seconds', 120) or inventory['captured_at'] > now+5:
            raise PlacementError('stale cache capacity inventory')
        node = inventory.get('nodes', {}).get(node_name, {})
        storage = node.get('storages', {}).get(datastore_id, {})
        if node_name not in policy.get('allowed_nodes', []) or not node.get('online') or node.get('draining') or node.get('load', 0) > policy.get('max_load', 1):
            raise PlacementError('cache destination node is unavailable or excluded')
        if datastore_id not in policy.get('allowed_storages', []) or not storage.get('available') or image['kind'] not in storage.get('guest_types', []) or image['architecture'] not in node.get('architectures', []) or image['kind'] not in node.get('guest_types', []):
            raise PlacementError('cache destination datastore or guest capability unavailable')
        mode = capacity.get('clone_mode', 'linked')
        if mode not in ('linked', 'full') or (mode == 'linked' and (storage.get('shared') or not storage.get('snapshots_verified') or 'linked' not in storage.get('clone_modes', []))):
            raise PlacementError('cache linked-clone datastore capability unverified')
        demand = {key: _number(capacity.get(key), 'cache '+key, .001) for key in ('memory_mb','cpu_cores','disk_gb')}
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.db.execute('SELECT 1 FROM jobs WHERE job_id=?', (job_id,)).fetchone():
                raise PlacementError('job already has reservations; reconcile instead of duplicating')
            count = self.db.execute("SELECT COUNT(*) FROM jobs WHERE project_id=? AND owner=? AND state='reviewed'",(project_id,owner)).fetchone()[0]
            if count >= policy.get('max_reviewed_plans',2):raise PlacementError('outstanding reviewed-plan limit reached')
            pending = self.active_reservations()
            override = {**policy, **policy.get('nodes', {}).get(node_name, {})}
            for key in ('memory_reserve_fraction','disk_reserve_fraction','cpu_reserve_fraction'):
                if not 0 <= _number(override.get(key, {'memory_reserve_fraction':.25,'disk_reserve_fraction':.2,'cpu_reserve_fraction':.2}[key]),key) < 1:
                    raise PlacementError('invalid cache reserve policy')
            window = capacity_window(node, policy, node_name, storage, mode)
            available = {key: window[key] for key in demand}
            for key in demand:
                available[key] -= sum(r[key] for r in pending if r['node_name']==node_name and (key!='disk_gb' or r['datastore_id']==datastore_id))
                if demand[key] > available[key]:raise PlacementError('cache preparation has insufficient '+key+' headroom')
            for scope,limits in [('global',policy.get('global_limits',{})),(str(project_id),policy.get('project_quotas',{}).get(str(project_id),{}))]:
                usage=inventory.get('quota_usage',{}).get(scope,{})
                for key,limit in limits.items():
                    if usage.get(key,0)+demand.get(key,0)+sum(r.get(key,0) for r in pending if scope=='global' or r['project_id']==project_id)>limit:
                        raise PlacementError('cache preparation '+scope+' quota exceeded')
            item={'operation':'cache_prepare','resource_key':'cache:'+image_id+':'+node_name+':'+datastore_id,
                  'project_id':project_id,'image':image_id,'artifact_sha256':image['artifact_sha256'],
                  'resolved':{'node_name':node_name,'datastore_id':datastore_id,'clone_mode':mode},
                  'resources':demand,'headroom':{key:available[key]-demand[key] for key in demand},
                  'policy_revision':policy['revision'],'reservation_id':str(uuid.uuid4()),
                  'reason':'Cache base, disposable verification and transfer capacity reserved'}
            self.db.execute('INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)',(job_id,project_id,owner,'reviewed',now+policy.get('review_ttl_seconds',3600),now,json.dumps([item],sort_keys=True)))
            self.db.execute('COMMIT')
            return item
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def reserve_source_probe(self, *, project_id, job_id, owner, source_ref,
                             source_sha256, node_name, datastore_id, capacity,
                             inventory, policy, now=None):
        """Reserve one direct, disposable source-verification clone.

        This reservation is deliberately independent of the immutable image
        catalog: it is for validating a captured source before publication.
        Its allocation records the exact source reference/config fingerprint.
        """
        now = time.time() if now is None else now
        if (isinstance(project_id, bool) or not isinstance(project_id, int) or
                not 1 <= project_id <= 255 or not owner or
                not isinstance(job_id, str) or not job_id or
                not isinstance(source_ref, str) or not source_ref or
                not re.fullmatch(r'[a-f0-9]{64}', str(source_sha256))):
            raise PlacementError('valid project, owner, job and captured source required')
        if (now - _number(inventory.get('captured_at'), 'inventory captured_at') >
                policy.get('max_inventory_age_seconds', 120) or inventory['captured_at'] > now + 5):
            raise PlacementError('stale source-probe capacity inventory')
        demand = {key: _number(capacity.get(key), 'source-probe ' + key, .001)
                  for key in ('memory_mb', 'cpu_cores', 'disk_gb')}
        self.db.execute('BEGIN IMMEDIATE')
        try:
            node = inventory.get('nodes', {}).get(node_name, {})
            storage = node.get('storages', {}).get(datastore_id, {})
            if (node_name not in policy.get('allowed_nodes', []) or not node.get('online') or
                    node.get('draining') or node.get('load', 0) > policy.get('max_load', 1)):
                raise PlacementError('source-probe node is unavailable or excluded')
            if (datastore_id not in policy.get('allowed_storages', []) or
                    not storage.get('available') or 'vm' not in storage.get('guest_types', []) or
                    'amd64' not in node.get('architectures', []) or 'vm' not in node.get('guest_types', [])):
                raise PlacementError('source-probe datastore or VM capability unavailable')
            if self.db.execute('SELECT 1 FROM jobs WHERE job_id=?', (job_id,)).fetchone():
                raise PlacementError('job already has reservations; reconcile instead of duplicating')
            count = self.db.execute("SELECT COUNT(*) FROM jobs WHERE project_id=? AND owner=? AND state='reviewed'", (project_id, owner)).fetchone()[0]
            if count >= policy.get('max_reviewed_plans', 2):
                raise PlacementError('outstanding reviewed-plan limit reached')
            window = capacity_window(node, policy, node_name, storage, 'full')
            pending = self.active_reservations()
            available = {
                'memory_mb': window['memory_mb'] - sum(r['memory_mb'] for r in pending if r['node_name'] == node_name),
                'cpu_cores': window['cpu_cores'] - sum(r['cpu_cores'] for r in pending if r['node_name'] == node_name),
                'disk_gb': window['disk_gb'] - sum(r['disk_gb'] for r in pending if r['node_name'] == node_name and r['datastore_id'] == datastore_id),
            }
            if any(demand[key] > available[key] for key in demand):
                raise PlacementError('source-probe has insufficient ' + next(k for k in demand if demand[k] > available[k]) + ' headroom')
            for scope, limits in [('global', policy.get('global_limits', {})),
                                  (str(project_id), policy.get('project_quotas', {}).get(str(project_id), {}))]:
                usage = inventory.get('quota_usage', {}).get(scope, {})
                for key, limit in limits.items():
                    reserved = sum(r.get(key, 0) for r in pending if scope == 'global' or r['project_id'] == project_id)
                    if usage.get(key, 0) + demand.get(key, 0) + reserved > limit:
                        raise PlacementError('source-probe ' + scope + ' quota exceeded')
            allocation = {'operation': 'source_probe', 'resource_key': 'source-probe:' + source_ref,
                'project_id': project_id, 'image': source_ref, 'artifact_sha256': source_sha256,
                'resolved': {'node_name': node_name, 'datastore_id': datastore_id, 'clone_mode': 'full'},
                'resources': demand, 'headroom': {key: available[key] - demand[key] for key in demand},
                'policy_revision': policy['revision'], 'reservation_id': str(uuid.uuid4()),
                'reason': 'Direct source acceptance clone and isolated disposable probe'}
            self.db.execute('INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)',
                (job_id, project_id, owner, 'reviewed', now + policy.get('review_ttl_seconds', 3600), now,
                 json.dumps([allocation], sort_keys=True)))
            self.db.execute('COMMIT')
            return allocation
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def validate_job(self, job_id, now=None):
        now = time.time() if now is None else now
        row = self.db.execute('SELECT state, expires, allocations FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        if not row or row[0] not in ('reviewed', 'running') or (row[0] == 'reviewed' and now > row[1]):
            raise PlacementError('reservation invalid or review expired; restage')
        return json.loads(row[2])

    def close(self):
        self.db.close()

    def get_job(self, job_id):
        row = self.db.execute('SELECT project_id, owner, state, expires, heartbeat, allocations FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        if not row:
            raise PlacementError('unknown operation')
        return dict(job_id=job_id, project_id=row[0], owner=row[1], state=row[2], expires=row[3], heartbeat=row[4], allocations=json.loads(row[5]))

    def status(self, job_id=None):
        if job_id is not None:
            return self.get_job(job_id)
        return [self.get_job(row[0]) for row in self.db.execute('SELECT job_id FROM jobs ORDER BY job_id').fetchall()]

    def _revalidate(self, job_id, requests, inventory, catalog, policy, now, known_existing_allocations):
        saved = self.validate_job(job_id, now)
        if any(a['policy_revision'] != policy.get('revision') for a in saved):
            raise PlacementError('placement policy changed; restage')
        original = {r['resource_key']: copy.deepcopy(r) for r in requests}
        if set(original) != {a['resource_key'] for a in saved}:
            raise PlacementError('requested resource set changed; restage')
        for a in saved:
            r = original[a['resource_key']]
            if r.get('project_id') != a['project_id'] or r.get('image') != a['image'] or catalog['images'].get(a['image'], {}).get('artifact_sha256') != a['artifact_sha256']:
                raise PlacementError('reserved project or immutable image identity changed')
            r['placement'] = {'mode': 'pin', 'pinned_node': a['resolved']['node_name']}
            r['resolved_clone_mode'] = a['resolved']['clone_mode']
            r['resolved_datastore_id'] = a['resolved']['datastore_id']
            r['resolved_template_id'] = a['resolved'].get('template_id')
            r['resolved_template_file_id'] = a['resolved'].get('template_file_id')
        # Remove only confirmed physical portions of this job from the fresh baseline:
        # the entire expected reservation is then admitted again, avoiding double
        # commitments while retaining its maximum demand after partial allocation.
        inventory = copy.deepcopy(inventory)
        by_reservation = {a['reservation_id']: a for a in saved}
        for actual in known_existing_allocations:
            expected = by_reservation.get(actual['reservation_id'])
            if not expected:
                continue
            resolved = expected['resolved']
            if actual.get('node_name') != resolved['node_name'] or actual.get('datastore_id') != resolved['datastore_id']:
                raise PlacementError('confirmed allocation differs from reviewed placement')
            node = inventory['nodes'][resolved['node_name']]
            for field, committed in [('memory_mb', 'committed_memory_mb'), ('cpu_cores', 'committed_cpu_cores')]:
                amount = _number(actual.get(field, 0), 'actual '+field)
                node[committed] = max(0, node[committed] - amount)
            # Observed memory requires measured resident use; configured RAM is
            # never assumed to equal residency or subtracted from host overhead.
            node['observed_memory_mb'] = max(0, node['observed_memory_mb'] - _number(actual.get('observed_memory_mb', 0), 'actual observed memory'))
            storage = node['storages'][resolved['datastore_id']]
            disk = _number(actual.get('disk_gb', 0), 'actual disk')
            storage['committed_gb'] = max(0, storage['committed_gb'] - disk)
            storage['free_gb'] = min(storage['total_gb'], storage['free_gb'] + disk)
            for scope in ('global', str(expected['project_id'])):
                usage = inventory.get('quota_usage', {}).get(scope, {})
                for field in ('memory_mb', 'cpu_cores', 'disk_gb'):
                    if field in usage:
                        usage[field] = max(0, usage[field] - actual.get(field, 0))
        fresh = schedule_batch(list(original.values()), inventory, catalog, policy, self.active_reservations(exclude_job_id=job_id), now, known_existing_allocations)
        for a, b in zip(saved, fresh):
            if a['resolved'] != b['resolved'] or a['resources'] != b['resources']:
                raise PlacementError('reviewed placement or resource request changed; restage')
        return saved

    def revalidate_job(self, job_id, requests, inventory, catalog, policy, now=None, known_existing_allocations=()):
        """Recheck the exact reviewed placements under the reservation writer lock."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            saved = self._revalidate(job_id, requests, inventory, catalog, policy, now, known_existing_allocations)
            self.db.execute('COMMIT')
            return saved
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def mark_running(self, job_id, now=None, *, requests=None, inventory=None, catalog=None, policy=None, known_existing_allocations=()):
        """Optional capability inputs atomically revalidate and claim an operation."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            state = self.get_job(job_id)['state']
            if state != 'reviewed':
                raise PlacementError('only a reviewed operation can be claimed once')
            self.validate_job(job_id, now)
            inputs = (requests, inventory, catalog, policy)
            if any(x is not None for x in inputs):
                if any(x is None for x in inputs):
                    raise PlacementError('all revalidation inputs required')
                self._revalidate(job_id, requests, inventory, catalog, policy, now, known_existing_allocations)
            self.db.execute("UPDATE jobs SET state='running', heartbeat=? WHERE job_id=?", (time.time() if now is None else now, job_id))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def purge_reviewed(self, job_id, *, operation_terminal, no_allocations_confirmed, provider_tasks_running=False, now=None):
        """Expiry alone does not release capacity; require explicit negative evidence."""
        now = time.time() if now is None else now
        self.db.execute('BEGIN IMMEDIATE')
        try:
            job = self.get_job(job_id)
            if job['state'] != 'reviewed' or job['expires'] >= now or not operation_terminal or not no_allocations_confirmed or provider_tasks_running:
                raise PlacementError('expired reviewed operation requires confirmed terminal/no-allocation evidence')
            self.db.execute("UPDATE jobs SET state='released' WHERE job_id=?", (job_id,))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def heartbeat(self, job_id, now=None):
        cursor = self.db.execute("UPDATE jobs SET heartbeat=? WHERE job_id=? AND state='running'", (time.time() if now is None else now, job_id))
        if cursor.rowcount != 1:
            raise PlacementError('operation not running')

    def reconcile_job(self, job_id, *, operation_terminal, allocations_reconciled, provider_tasks_running=False):
        if not operation_terminal or not allocations_reconciled or provider_tasks_running:
            raise PlacementError('retain reservation until operation terminal and actual allocations reconciled')
        cursor = self.db.execute("UPDATE jobs SET state='released' WHERE job_id=?", (job_id,))
        if cursor.rowcount != 1:
            raise PlacementError('unknown operation')
