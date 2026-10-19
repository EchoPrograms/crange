"""Fixed recovery of the protected P100 sealed-Windows local cache job."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from urllib.parse import quote

import crange_scheduling as scheduling
from crange import ProxmoxAPI
from crange_cache_ownership import cache_owned
from crange_cache_jobs import (_cache_probe_identity, _root, _settings,
                               select_probe_bridge, shared_reservation_validator)
from crange_distribution import (DistributionError, FileJournal, ProbeVerifier,
                                 _JournaledProbeAPI, catalog_transaction,
                                 publish_replica, required_capacity)
from crange_placement import ReservationLedger

IMAGE_ID = 'mhl-windows-server-2025-base-p100-v1'
ARTIFACT_SHA256 = '3c968282c7f4a494187981cbf7666e1e5e562f27206dfbd45cb89165e02e5b45'


def public_recovery_status():
    """Expose only fixed P100 recovery identifiers backed by a live reservation."""
    journal_dir = _root() / 'cache-journal'
    ledger = ReservationLedger(_root() / 'reservations.sqlite3')
    rows = []
    try:
        directory_stat = journal_dir.lstat()
        if (not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != os.geteuid() or
                directory_stat.st_mode & 0o077):
            return rows
        for path in sorted(journal_dir.glob('*.json')):
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                try:
                    file_stat = os.fstat(fd)
                    if (not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != os.geteuid() or
                            file_stat.st_nlink != 1 or file_stat.st_mode & 0o077 or file_stat.st_size > 1024 * 1024):
                        continue
                    with os.fdopen(fd, 'r', encoding='utf-8') as stream:
                        fd = -1
                        record = json.load(stream)
                finally:
                    if fd >= 0:
                        os.close(fd)
                job_id = record.get('job_id')
                if (not isinstance(job_id, str) or path.name != job_id + '.json' or
                        record.get('image_id') != IMAGE_ID or
                        record.get('artifact_sha256') != ARTIFACT_SHA256 or
                        record.get('state') not in ('verifying', 'failed-verification')):
                    continue
                reservation = record.get('reservation')
                saved = ledger.get_job(job_id)
                if (saved.get('state') != 'running' or saved.get('project_id') != 100 or
                        saved.get('allocations') != [reservation] or not isinstance(reservation, dict) or
                        reservation.get('image') != IMAGE_ID or
                        reservation.get('artifact_sha256') != ARTIFACT_SHA256):
                    continue
                node, storage = record.get('node'), record.get('datastore_id')
                template_id, probe_id = record.get('template_id'), record.get('probe_id')
                if (node not in {f'proxmox{i}' for i in range(1, 7)} or
                        storage not in ('local-lvm', 'ssd-lvm', 'hdd-lvm') or
                        type(template_id) is not int or type(probe_id) is not int):
                    continue
                resolved = reservation.get('resolved', {})
                if (resolved.get('node_name') != node or resolved.get('datastore_id') != storage):
                    continue
                rows.append({
                    'image_id': IMAGE_ID,
                    'source_job_id': job_id,
                    'state': record['state'],
                    'node': node,
                    'datastore_id': storage,
                    'template_id': template_id,
                    'probe_id': probe_id,
                    'reason': ('Verification stopped before completion; exact reservation is retained.'
                               if record['state'] == 'failed-verification' else
                               'Verification is incomplete; exact reservation is retained.'),
                })
            except (OSError, ValueError, TypeError, KeyError):
                continue
    finally:
        ledger.close()
    return rows


def _assert_task_success(api, node, upid):
    parts = str(upid).split(':')
    if len(parts) < 8 or parts[0] != 'UPID' or parts[1] != node:
        raise DistributionError('Cache recovery has a malformed or out-of-scope provider task.')
    status = api.request('GET', f'/nodes/{node}/tasks/{quote(upid, safe="")}/status')
    if status.get('status') != 'stopped' or status.get('exitstatus') != 'OK':
        raise DistributionError('A recorded cache provider task is active or did not finish successfully.')
    return status


def _resource(api, node, vmid, pool, status):
    rows = api.request('GET', '/cluster/resources', {'type': 'vm'})
    matches = [row for row in rows if str(row.get('vmid')) == str(vmid)]
    if len(matches) != 1:
        raise DistributionError(f'Exact owned cache guest {vmid} is missing or ambiguous.')
    row = matches[0]
    if row.get('type') != 'qemu' or row.get('node') != node or row.get('pool') != pool or row.get('status') != status:
        raise DistributionError(f'Exact owned cache guest {vmid} is not in the expected stopped state.')
    return row


def _load_locked_job(image_id, source_job_id):
    if (image_id != IMAGE_ID or not isinstance(source_job_id, str) or
            not re.fullmatch(r'cache-[a-zA-Z0-9-]{1,64}', source_job_id)):
        raise DistributionError('Choose a protected P100 Windows cache job identity.')
    journal_dir = _root() / 'cache-journal'
    try:
        dir_stat = journal_dir.lstat()
        if (not stat.S_ISDIR(dir_stat.st_mode) or dir_stat.st_uid != os.geteuid() or
                dir_stat.st_mode & 0o077):
            raise OSError('untrusted cache journal directory')
        path = journal_dir / (source_job_id + '.json')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            file_stat = os.fstat(fd)
            if (not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != os.geteuid() or
                    file_stat.st_nlink != 1 or file_stat.st_mode & 0o077 or file_stat.st_size > 1024 * 1024):
                raise OSError('untrusted cache journal')
            with os.fdopen(fd, 'r', encoding='utf-8') as stream:
                fd = -1
                record = json.load(stream)
        finally:
            if fd >= 0:
                os.close(fd)
    except (OSError, ValueError, TypeError) as exc:
        raise DistributionError('The selected protected cache job does not exist.') from exc
    policy, catalog = _settings()
    image = catalog.get('images', {}).get(IMAGE_ID)
    if (not image or image.get('artifact_sha256') != ARTIFACT_SHA256 or
            100 not in image.get('approved_projects', [])):
        raise DistributionError('Immutable P100 image approval or artifact checksum changed.')
    capture, profile, sealed = _cache_probe_identity(image)
    if not sealed or profile != 'windows':
        raise DistributionError('The published Windows image no longer has sealed-source provenance.')
    from crange_catalog import validate_sealed_windows_capture
    validate_sealed_windows_capture(capture)
    node, storage = record.get('node'), record.get('datastore_id')
    template_id, probe_id = record.get('template_id'), record.get('probe_id')
    if (record.get('job_id') != source_job_id or record.get('image_id') != IMAGE_ID or
            record.get('artifact_sha256') != ARTIFACT_SHA256 or record.get('kind') != 'qemu' or
            record.get('allocation_node') != node or
            node not in {f'proxmox{i}' for i in range(1, 7)} or
            type(template_id) is not int or type(probe_id) is not int or
            template_id < 100 or probe_id < 100 or template_id == probe_id):
        raise DistributionError('Protected cache job identity does not match the approved immutable P100 image.')
    settings = policy.get('cache', policy)
    if (not policy.get('enabled') or (node, storage) not in set(map(tuple, policy.get('approved_storage', []))) or
            f'vm-archive-restore' not in settings.get('tested_paths', []) or
            f'local-linked-vm' not in settings.get('tested_paths', [])):
        raise DistributionError('Current administrator policy no longer approves the reviewed cache path.')
    return policy, catalog, image, capture, record


def _validate_job(api, policy, image, journal, ledger, record, *, allow_cleanup=False):
    job_id, node, storage = record.get('job_id'), record.get('node'), record.get('datastore_id')
    template_id, probe_id = record.get('template_id'), record.get('probe_id')
    if (record.get('state') not in ('verifying', 'failed-verification') or
            record.get('image_id') != IMAGE_ID or record.get('artifact_sha256') != ARTIFACT_SHA256 or
            record.get('allocation_node') != node or storage not in ('local-lvm', 'ssd-lvm', 'hdd-lvm') or
            record.get('kind') != 'qemu' or type(template_id) is not int or type(probe_id) is not int):
        raise DistributionError('Protected cache journal is not the exact recoverable P100 generation.')
    if any(task.get('state') != 'confirmed-terminal' for task in record.get('provider_tasks', [])):
        raise DistributionError('A previous cache provider task is still uncertain; retain its live reservation.')
    saved = ledger.get_job(job_id)
    reservation = record.get('reservation')
    if (saved.get('state') != 'running' or saved.get('project_id') != 100 or
            saved.get('allocations') != [reservation] or not isinstance(reservation, dict) or
            reservation.get('image') != IMAGE_ID or reservation.get('artifact_sha256') != ARTIFACT_SHA256 or
            reservation.get('resolved', {}).get('node_name') != node or
            reservation.get('resolved', {}).get('datastore_id') != storage or
            not shared_reservation_validator(ledger, job_id, reservation, IMAGE_ID,
                                             ARTIFACT_SHA256)(reservation, node, storage,
                                                               required_capacity(image))):
        raise DistributionError('Exact live P100 cache reservation is missing or no longer covers this image.')
    pin_path = _root() / 'artifact-pins' / (job_id + '.json')
    try:
        pin = json.loads(pin_path.read_text())
        artifact = image.get('artifact') or {}
        if pin != {'volume_id': artifact.get('volume_id'), 'artifact_sha256': ARTIFACT_SHA256,
                   'job_id': job_id, 'state': 'active'}:
            raise ValueError('pin mismatch')
    except (OSError, ValueError) as exc:
        raise DistributionError('Protected artifact pin does not match the fixed image and job.') from exc
    inventory = api.request('GET', '/cluster/resources', {'type': 'vm'})
    present = set()
    proved_absent = {row.get('vmid') for row in record.get('cleanup_progress', [])
                     if row.get('absent') is True and row.get('inventory_complete') is True}
    for vmid, pool in ((template_id, 'crange-iac'), (probe_id, 'crange-probes')):
        rows = [row for row in inventory if str(row.get('vmid')) == str(vmid)]
        if not rows and allow_cleanup and vmid in proved_absent:
            continue
        if len(rows) != 1 or rows[0].get('type') != 'qemu' or rows[0].get('node') != node or rows[0].get('pool') != pool or rows[0].get('status') != 'stopped':
            raise DistributionError(f'Exact owned cache guest {vmid} is missing, ambiguous, or not stopped.')
        present.add(vmid)
        config = api.request('GET', f'/nodes/{node}/qemu/{vmid}/config')
        if config.get('lock') or not cache_owned(config, vmid, job_id):
            raise DistributionError(f'Exact cache guest {vmid} ownership/configuration changed.')
        if vmid == template_id and int(config.get('template', 0)) != 1:
            raise DistributionError('Exact cache base is not a stopped Proxmox template.')
        if vmid == probe_id and (int(config.get('template', 0)) != 0 or
                config.get('description') != f'CRANGE verification {job_id}' or
                'link_down=1' not in config.get('net0', '') or
                'bridge=' + select_probe_bridge(policy) not in config.get('net0', '')):
            raise DistributionError('Disposable probe configuration changed or is not isolated.')
        status = api.request('GET', f'/nodes/{node}/qemu/{vmid}/status/current')
        if status.get('status') != 'stopped':
            raise DistributionError('The exact cache template and probe must be stopped before recovery.')
    if any(r.get('job_id') == job_id or r.get('template_id') == template_id
           for r in image.get('replicas', [])):
        raise DistributionError('Catalog already references this partial generation; reconcile its exact publication.')
    for task in record.get('provider_tasks', []):
        _assert_task_success(api, node, task.get('upid'))
    if probe_id in present:
        pool = api.request('GET', '/pools/crange-probes')
        if not any(m.get('type') == 'qemu' and m.get('node') == node and str(m.get('vmid')) == str(probe_id)
                   for m in pool.get('members', [])):
            raise DistributionError('The existing probe is no longer in the dedicated verification pool.')
    return record, reservation, present


def resume(image_id, source_job_id, *, timeout=900):
    """Resume one exact stopped probe and publish only after fresh identity proof."""
    if type(timeout) is not int or not 60 <= timeout <= 1200:
        raise DistributionError('Cache recovery readiness timeout is outside the fixed safety bound.')
    policy, catalog, image, capture, source_record = _load_locked_job(image_id, source_job_id)
    job_id = source_record['job_id']
    node, storage = source_record['node'], source_record['datastore_id']
    template_id, probe_id = source_record['template_id'], source_record['probe_id']
    journal = FileJournal(_root() / 'cache-journal')
    ledger = ReservationLedger(_root() / 'reservations.sqlite3')
    api = ProxmoxAPI()
    try:
        with scheduling.node_locks([node], library=True), journal.allocation_lock():
            record, reservation, _present = _validate_job(api, policy, image, journal, ledger, source_record)
            if record.get('verification') or record.get('state') == 'failed-verification':
                raise DistributionError('This cache attempt already has a failed verifier result; clean up the owned partial guests.')
            bridge = select_probe_bridge(policy)
            verifier = ProbeVerifier(bridge, policy.get('cache', policy).get('isolated_bridges', []),
                                     capture, ct_exec=None, timeout=timeout, init_system='windows')
            wrapped = _JournaledProbeAPI(api, record, journal)
            result = verifier(wrapped, node, 'qemu', probe_id)
            if not isinstance(result, dict) or result.get('passed') is not True or result.get('identity_unique') is not True:
                record.update(state='failed-verification', verification=result if isinstance(result, dict) else {'passed': False})
                journal(dict(record))
                raise DistributionError('Existing disposable Windows clone did not pass sealed identity verification; fixed cleanup is available.')
            if not cache_owned(api.request('GET', f'/nodes/{node}/qemu/{probe_id}/config'), probe_id, job_id):
                raise DistributionError('Disposable probe ownership changed before cleanup.')
            upid = wrapped.request('DELETE', f'/nodes/{node}/qemu/{probe_id}', {'purge': 1})
            wrapped.task(node, upid, timeout=600)
            resources = api.request('GET', '/cluster/resources', {'type': 'vm'})
            if any(str(row.get('vmid')) == str(probe_id) for row in resources):
                raise DistributionError('Disposable probe deletion is not yet proven; retain the cache reservation.')
            replica = {'node': node, 'template_id': template_id, 'datastore_id': storage,
                       'state': 'ready', 'artifact_sha256': ARTIFACT_SHA256,
                       'verified_at': datetime.now(timezone.utc).isoformat(),
                       'verification': result, 'job_id': job_id,
                       'clone_modes': ['linked'], 'clone_modes_verified': ['linked'], 'cache_local': True}
            updated = publish_replica(catalog, IMAGE_ID, replica)
            record.update(state='ready', verification=result)
            journal(dict(record))
            catalog_transaction(scheduling.CONFIG / 'image-catalog.json', lambda _catalog: updated)
            inventory = scheduling.discover(api, policy)
            actual = inventory.get('guests', {}).get(str(template_id))
            if (not actual or actual.get('node_name') != node or not actual.get('template') or
                    str(probe_id) in inventory.get('guests', {})):
                raise DistributionError('Published cache generation failed its final live inventory reconciliation; reservation retained.')
            ledger.reconcile_job(job_id, operation_terminal=True, allocations_reconciled=True)
            (_root() / 'artifact-pins' / (job_id + '.json')).unlink(missing_ok=True)
            return {'image_id': IMAGE_ID, 'job_id': job_id, 'state': 'ready',
                    'node': node, 'storage': storage, 'template_id': template_id,
                    'probe_id': probe_id, 'artifact_sha256': ARTIFACT_SHA256,
                    'verification': result}
    finally:
        ledger.close()


def reconcile(image_id, source_job_id, *, action):
    """Remove only the exact failed partial P100 cache generation after ownership checks."""
    if action != 'cleanup':
        raise DistributionError('Choose the fixed cleanup action.')
    policy, catalog, image, _capture, source_record = _load_locked_job(image_id, source_job_id)
    job_id = source_record['job_id']
    node, template_id, probe_id = source_record['node'], source_record['template_id'], source_record['probe_id']
    journal = FileJournal(_root() / 'cache-journal')
    ledger = ReservationLedger(_root() / 'reservations.sqlite3')
    api = ProxmoxAPI()
    try:
        with scheduling.node_locks([node], library=True), journal.allocation_lock():
            record, _reservation, present = _validate_job(api, policy, image, journal, ledger, source_record, allow_cleanup=True)
            if any(r.get('job_id') == job_id or r.get('template_id') == template_id
                   for r in image.get('replicas', [])):
                raise DistributionError('Published cache generations cannot be removed by partial-job cleanup.')
            progress = record.setdefault('cleanup_progress', [])
            for vmid in (probe_id, template_id):
                if vmid not in present:
                    continue
                kind_config = api.request('GET', f'/nodes/{node}/qemu/{vmid}/config')
                if not cache_owned(kind_config, vmid, job_id):
                    raise DistributionError('Owned cache guest labels changed; cleanup refused.')
                upid = api.request('DELETE', f'/nodes/{node}/qemu/{vmid}', {'purge': 1})
                wrapped = _JournaledProbeAPI(api, record, journal)
                # The deletion request itself is journaled before task polling.
                if not isinstance(upid, str) or not upid.startswith('UPID:'):
                    raise DistributionError('Provider did not return a deletion task; retain reservation.')
                record.setdefault('provider_tasks', []).append({'path': f'/nodes/{node}/qemu/{vmid}',
                    'upid': upid, 'state': 'running'})
                journal(dict(record))
                wrapped.task(node, upid, timeout=900)
                resources = api.request('GET', '/cluster/resources', {'type': 'vm'})
                if any(str(row.get('vmid')) == str(vmid) for row in resources):
                    raise DistributionError('Owned cache guest remains after its terminal delete task; retain the reservation.')
                digest = hashlib.sha256(json.dumps(sorted((r.get('node'), r.get('type'), str(r.get('vmid')))
                        for r in resources), separators=(',', ':')).encode()).hexdigest()
                progress.append({'vmid': vmid, 'absent': True, 'inventory_complete': True,
                    'cluster_guest_inventory_sha256': digest,
                    'checked_at': datetime.now(timezone.utc).isoformat()})
                journal(dict(record))
            resources = api.request('GET', '/cluster/resources', {'type': 'vm'})
            if any(str(row.get('vmid')) in {str(template_id), str(probe_id)} for row in resources):
                raise DistributionError('Owned cache guests are still present; retain their reservation and retry reconciliation.')
            digest = hashlib.sha256(json.dumps(sorted((r.get('node'), r.get('type'), str(r.get('vmid')))
                    for r in resources), separators=(',', ':')).encode()).hexdigest()
            record.update(state='cleaned-owned', cleanup_evidence={'verified': True, 'node': node,
                'template_id': template_id, 'probe_id': probe_id, 'inventory_complete': True,
                'both_guests_absent': True, 'cluster_guest_inventory_sha256': digest,
                'provider_tasks_terminal': True, 'checked_at': datetime.now(timezone.utc).isoformat()})
            journal(dict(record))
            ledger.reconcile_job(job_id, operation_terminal=True, allocations_reconciled=True)
            (_root() / 'artifact-pins' / (job_id + '.json')).unlink(missing_ok=True)
            return {'image_id': IMAGE_ID, 'job_id': job_id, 'state': 'cleaned-owned',
                    'template_id': template_id, 'probe_id': probe_id, 'reservation_released': True}
    finally:
        ledger.close()
