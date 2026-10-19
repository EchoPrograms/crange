"""Reviewed local image cache eviction. Native backing chains/snapshots stay local.

Central backup artifacts are separate recoverable exports. Their existence never
makes a live local backing chain removable. Evidence adapters must enumerate the
entire cluster and native snapshots, including non-range guests and stopped VMs.
"""
import copy
import hashlib
import json
import re
import time

from crange_catalog import validate_catalog, verified_local_cache_storage
from crange_cache_ownership import cache_owned


class CacheError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _candidate(catalog, image_id, node, template_id):
    validate_catalog(catalog)
    image = catalog['images'][image_id]
    matches = [r for r in image['replicas'] if r.get('template_id') == template_id and r['node'] == node]
    if len(matches) != 1:
        raise CacheError('Exact local replica identity is missing or ambiguous.')
    replica = matches[0]
    if replica.get('state') != 'ready' or not replica.get('cache_local') or not verified_local_cache_storage(replica):
        raise CacheError('Only a verified owned local cache replica may be evicted.')
    artifact = image.get('artifact', {})
    if (artifact.get('format') != 'vzdump' or not artifact.get('complete_capture') or
            not artifact.get('volume_id', '').startswith('crange-library:backup/') or
            artifact.get('artifact_sha256', image['artifact_sha256']) != image['artifact_sha256']):
        raise CacheError('A complete recoverable central artifact is required.')
    return image, replica, artifact


def _dependencies(image_id, node, template_id, evidence, now, max_age=120):
    required = ('cluster_complete', 'snapshots_complete', 'backing_complete',
                'reservations_complete', 'operations_complete', 'pins_complete')
    if any(evidence.get(field) is not True for field in required):
        raise CacheError('Complete cluster, backing, snapshot and operation evidence is required.')
    if not evidence.get('revision') or now - evidence.get('observed_at', 0) > max_age or evidence.get('observed_at', now + 1) > now + 5:
        raise CacheError('Dependency evidence is stale or has no revision.')
    candidate = evidence.get('candidate', {})
    if candidate.get('node') != node or candidate.get('template_id') != template_id or not candidate.get('volumes'):
        raise CacheError('Candidate volume/backing identity evidence is missing.')
    volumes = set(candidate['volumes'])
    def references(row):
        return (row.get('origin_template_id') == template_id or template_id in row.get('origin_vmids', []) or
                bool(volumes.intersection(row.get('backing_volumes', []))) or
                any(re.search(rf'(?:^|[/:-])base-{template_id}-', str(value)) for value in row.get('backing_volumes', [])))
    blockers = []
    for guest in evidence.get('guests', []):
        if guest.get('vmid') == template_id and guest.get('node') == node:
            continue
        if references(guest): blockers.append(f'guest {guest.get("node")}/{guest.get("vmid")}')
    for snapshot in evidence.get('snapshots', []):
        if references(snapshot) or (snapshot.get('vmid') == template_id and snapshot.get('node') == node):
            blockers.append(f'native snapshot {snapshot.get("vmid")}/{snapshot.get("name")}')
    for reservation in evidence.get('reservations', []):
        if reservation.get('state') not in ('released', 'cancelled', 'reconciled-terminal') and (
                reservation.get('image_id', reservation.get('image')) == image_id or
                reservation.get('template_id') == template_id):
            blockers.append('reviewed or running reservation')
    for pin in evidence.get('pins', []):
        if pin.get('image_id') == image_id and pin.get('node', node) == node:
            blockers.append('retained local critical pin')
    for operation in evidence.get('operations', []):
        if operation.get('state') not in ('confirmed-terminal', 'reconciled-terminal') and (
                operation.get('node') == node or operation.get('image_id') == image_id or
                operation.get('template_id') == template_id):
            blockers.append('provider operation is not confirmed idle')
    if blockers: raise CacheError('Local cache is still required: ' + ', '.join(blockers))
    return candidate


def build_eviction_plan(catalog, image_id, node, template_id, evidence, *, now=None):
    now = time.time() if now is None else now
    image, replica, artifact = _candidate(catalog, image_id, node, template_id)
    candidate = _dependencies(image_id, node, template_id, evidence, now)
    proof = evidence.get('central_verification', {})
    if (proof.get('artifact_sha256') != image['artifact_sha256'] or
            proof.get('volume_id') != artifact['volume_id'] or
            proof.get('verified') is not True or now - proof.get('verified_at', 0) > 120 or
            proof.get('verified_at', now + 6) > now + 5):
        raise CacheError('Fresh actual central artifact checksum evidence is required.')
    plan = {'operation': 'evict-local-image-cache', 'schema_version': 1,
            'image_id': image_id, 'node': node, 'template_id': template_id,
            'kind': image['kind'], 'artifact_sha256': image['artifact_sha256'],
            'central_volume_id': artifact['volume_id'], 'replica_identity': _digest(replica),
            'candidate_volumes': sorted(candidate['volumes']), 'evidence_revision': evidence['revision'],
            'created_at': now, 'review_expires_at': now + 900}
    plan['plan_hash'] = _digest(plan)
    return plan


def apply_eviction_plan(api, plan, *, refresh, hash_volume, journal, lock,
                        artifact_pin, now=None):
    """Fixed admin worker deletion; caller must provide the reviewed exact plan.

    refresh() returns (catalog, complete evidence). lock() protects allocations and
    all local lifecycle operations. artifact_pin(volume,hash) prevents concurrent
    central artifact GC. Actual checksum is read while this pin is held.
    """
    now = time.time() if now is None else now
    identity = dict(plan)
    claimed = identity.pop('plan_hash', None)
    if claimed != _digest(identity) or plan.get('operation') != 'evict-local-image-cache' or now > plan['review_expires_at']:
        raise CacheError('Eviction review is invalid or expired.')
    with lock(), artifact_pin(plan['central_volume_id'], plan['artifact_sha256']):
        catalog, evidence = refresh()
        image, replica, artifact = _candidate(catalog, plan['image_id'], plan['node'], plan['template_id'])
        candidate = _dependencies(plan['image_id'], plan['node'], plan['template_id'], evidence, now)
        if (_digest(replica) != plan['replica_identity'] or
                sorted(candidate['volumes']) != plan['candidate_volumes'] or
                image['artifact_sha256'] != plan['artifact_sha256'] or artifact['volume_id'] != plan['central_volume_id']):
            raise CacheError('Reviewed image/backing identity changed; restage eviction.')
        node, vmid = plan['node'], plan['template_id']
        kind = 'qemu' if plan['kind'] == 'vm' else 'lxc'
        config = api.request('GET', f'/nodes/{node}/{kind}/{vmid}/config')
        if (int(config.get('template', 0)) != 1 or config.get('lock') or
                not cache_owned(config, vmid, replica.get('job_id'))):
            raise CacheError('Candidate template ownership, lock or identity changed.')
        actual_volumes = sorted(str(value).split(',')[0] for field, value in config.items()
            if re.fullmatch(r'(?:scsi|sata|virtio|ide|efidisk|tpmstate|mp)\d+|rootfs|unused\d+', field)
            and ':' in str(value).split(',')[0] and 'media=cdrom' not in str(value))
        if actual_volumes != plan['candidate_volumes']:
            raise CacheError('Actual provider volumes changed; restage eviction.')
        status = api.request('GET', f'/nodes/{node}/{kind}/{vmid}/status/current')
        if status.get('status') != 'stopped': raise CacheError('Cache template is not stopped.')
        if hash_volume(node, artifact['volume_id']) != image['artifact_sha256']:
            raise CacheError('Central artifact checksum changed; eviction refused.')
        record = {'job_id': 'cache-gc-' + claimed[:24], 'state': 'evicting', 'plan': plan,
                  'image_id': plan['image_id'], 'node': node, 'template_id': vmid,
                  'datastore_id': replica['datastore_id'], 'artifact_sha256': image['artifact_sha256']}
        journal(record)
        task = api.request('DELETE', f'/nodes/{node}/{kind}/{vmid}', {'purge': 1})
        record['upid'] = task
        journal(dict(record))
        api.task(node, task, timeout=1800)
        result = copy.deepcopy(catalog)
        result['images'][plan['image_id']]['replicas'] = [r for r in result['images'][plan['image_id']]['replicas']
                                                        if r.get('template_id') != vmid]
        record['state'] = 'evicted'
        journal(record)
        if callable(getattr(journal, 'confirm_eviction', None)):
            journal.confirm_eviction(replica['job_id'], vmid, claimed, task)
        return validate_catalog(result)


def activate_cached_version(catalog, image_id, node, *, current=None):
    """Return a selection only after new version is locally linked-clone verified."""
    validate_catalog(catalog)
    image = catalog['images'][image_id]
    replicas = [r for r in image['replicas'] if r['node'] == node and
                r.get('cache_local') and r.get('state') == 'ready' and
                'linked' in r.get('clone_modes_verified', []) and verified_local_cache_storage(r)]
    if len(replicas) != 1: raise CacheError('Prepare exactly one verified local linked-clone base first.')
    return {'image_id': image_id, 'node': node, 'template_id': replicas[0]['template_id'],
            'previous': copy.deepcopy(current), 'clone_mode': 'linked'}
