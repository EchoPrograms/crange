"""Reviewed VM release capture; no commands or evidence supplied by callers."""
from __future__ import annotations
import json
import re
import time
from pathlib import Path

from crange_distribution import (DistributionError, PROBE_POOL, FileJournal,
    _owned, _require_probe_pool, _source_config_identity, _source_capacity,
    capture_source_identity, export_image, SSHVolumeHasher)
from crange_placement import ReservationLedger
from crange_scheduling import CONFIG, DATA, atomic_json, discover, node_locks, read_settings


def capture_policy(node, profile):
    policy, _ = read_settings()
    if not profile.get('agent_enabled'):
        raise DistributionError('Release capture requires a tested approved QGA-enabled VM profile; rebuild staging with that profile first.')
    if not policy.get('enabled') or node not in policy.get('allowed_nodes', []):
        raise DistributionError('Release capture node is not enabled by administrator policy.')
    bridge = policy.get('capture_isolated_bridge')
    if bridge not in policy.get('isolated_bridges', []) or not re.fullmatch(r'[A-Za-z0-9_-]{1,15}', str(bridge)):
        raise DistributionError('Configure an approved capture_isolated_bridge before capture.')
    storage = policy.get('capture_datastore')
    if (node, storage) not in set(map(tuple, policy.get('approved_storage', []))):
        raise DistributionError('Configure approved capture_datastore before capture.')
    backup = policy.get('capture_backup_storage')
    if (node, backup) not in set(map(tuple, policy.get('approved_backup_storage', []))):
        raise DistributionError('Configure approved capture_backup_storage before capture.')
    return policy, bridge, storage, backup


def capture_vm(api, *, project, node, source_id, name, description, slug, profile):
    """Clone a stopped staging VM, isolate before boot, capture real identity.

    Failure retains the exact journal, guest, and capacity reservation for review;
    no retry deletes or reuses an uncertain allocation.
    """
    policy, bridge, storage, backup = capture_policy(node, profile)
    job = f'capture-p{project:03d}-{slug}'
    identity_job, export_job, probe_job = job + '-identity', job + '-export', job + '-probe'
    journal = FileJournal(DATA / 'source-journal')
    receipt_path = DATA / 'capture-journal' / (job + '.json')
    ledger = ReservationLedger(DATA / 'reservations.sqlite3')
    try:
        with node_locks([node], library=True), journal.allocation_lock():
            if receipt_path.exists():
                raise DistributionError('Capture already allocated; reconcile protected capture journal before retry.')
            source = api.vm(node, source_id)
            if int(source.get('template', 0)) or api.status(node, source_id) != 'stopped':
                raise DistributionError('Staging source must be stopped before release cloning.')
            if not str(source.get('agent', '')).split(',')[0] in ('1', 'enabled=1'):
                raise DistributionError('Staging VM QGA is disabled; rebuild with a tested approved agent profile.')
            capacity = _source_capacity(source, 'qemu')
            backup_status = api.request('GET', f'/nodes/{node}/storage/{backup}/status')
            backup_config = api.request('GET', f'/storage/{backup}')
            if int(backup_config.get('shared', 0)) != 1:
                raise DistributionError('Capture backup storage must be administrator-configured shared central storage.')
            total, available = float(backup_status.get('total', 0)), float(backup_status.get('avail', 0))
            reserve = max(total * .20, 20 * 1024 ** 3)
            if (int(backup_status.get('active', 0)) != 1 or 'backup' not in str(backup_status.get('content', '')).split(',')
                    or total <= 0 or available - capacity['disk_gb'] * 1024 ** 3 < reserve):
                raise DistributionError('Central archive storage lacks capture space and reserved headroom.')
            source_sha = _source_config_identity(source, source_id)
            allocation = ledger.reserve_source_probe(project_id=project, job_id=job, owner='release-capture',
                source_ref=f'source:{node}:qemu:{source_id}', source_sha256=source_sha,
                node_name=node, datastore_id=storage, capacity=capacity,
                inventory=discover(api, policy), policy=policy)
            ledger.mark_running(job)
            # The general allocator avoids the source-probe worker's reserved band.
            vmid = int(api.request('GET', '/cluster/nextid'))
            if vmid < 100 or 20000 <= vmid < 20100:
                raise DistributionError('Capture allocator returned an invalid or probe-reserved VMID.')
            marker = f'crange-capture-{slug}'
            receipt = dict(job_id=job, source_id=source_id, node=node, template_id=vmid,
                reservation=allocation, marker=marker, state='allocated', provider_tasks=[])
            atomic_json(receipt_path, receipt)
            def task(method, path, fields):
                upid = api.request(method, path, fields)
                receipt['provider_tasks'].append(dict(upid=upid, path=path, state='running'))
                atomic_json(receipt_path, receipt)
                api.task(node, upid, timeout=1800)
                receipt['provider_tasks'][-1]['state'] = 'confirmed-terminal'
                atomic_json(receipt_path, receipt)
            task('POST', f'/nodes/{node}/qemu/{source_id}/clone', dict(newid=vmid, name=name,
                description=description, pool=PROBE_POOL, full=1, storage=storage))
            if _source_config_identity(api.vm(node, source_id), source_id) != source_sha or api.status(node, source_id) != 'stopped':
                raise DistributionError('Staging source changed during capture cloning; reservation retained.')
            base = f'/nodes/{node}/qemu/{vmid}'
            config = api.vm(node, vmid)
            fields = {'tags': f'crange-image;{marker};p{project:03d}', 'onboot': 0}
            for key, value in config.items():
                if re.fullmatch(r'net[0-9]+', key):
                    fields[key] = f'{str(value).split(",")[0]},bridge={bridge},link_down=1'
            api.request('PUT', base + '/config', fields)
            config = api.vm(node, vmid)
            _owned(config, marker)
            _require_probe_pool(api, node, 'qemu', vmid)
            for key, value in config.items():
                if re.fullmatch(r'net[0-9]+', key) and (f'bridge={bridge}' not in str(value).split(',') or 'link_down=1' not in str(value).split(',')):
                    raise DistributionError('Capture network isolation was not applied exactly.')
            task('POST', base + '/status/start', {})
            # Agent startup is asynchronous; commands are fixed and failure remains gated.
            deadline = time.monotonic() + 180
            while True:
                try:
                    identity = capture_source_identity(api, node=node, kind='vm', vmid=vmid,
                        job_id=identity_job, journal=journal)
                    break
                except (DistributionError, RuntimeError):
                    if time.monotonic() >= deadline:
                        # Only the exact, owned, terminal-started allocation may be stopped.
                        _require_probe_pool(api, node, 'qemu', vmid)
                        _owned(api.vm(node, vmid), marker)
                        task('POST', base + '/status/shutdown', {'timeout': 90, 'forceStop': 0})
                        raise
                    time.sleep(2)
            task('POST', base + '/status/shutdown', {'timeout': 90, 'forceStop': 0})
            if api.status(node, vmid) != 'stopped':
                raise DistributionError('Captured guest did not shut down cleanly.')
            task('POST', base + '/template', {})
            config = api.vm(node, vmid)
            if int(config.get('template', 0)) != 1 or _source_config_identity(config, vmid) != identity['source_config_sha256']:
                raise DistributionError('Captured template configuration changed after identity capture.')
            export_image(api, node=node, kind='vm', vmid=vmid, storage=backup,
                job_id=export_job, journal=journal, hash_volume=SSHVolumeHasher(),
                approved_backup_storage=map(tuple, policy['approved_backup_storage']), identity_record=identity)
            inventory = discover(api, policy)
            if str(vmid) not in inventory.get('guests', {}):
                raise DistributionError('Captured template absent from complete capacity inventory; reservation retained.')
            ledger.reconcile_job(job, operation_terminal=True, allocations_reconciled=True)
            receipt.update(state='captured', identity_job=identity_job, export_job=export_job, probe_job=probe_job)
            atomic_json(receipt_path, receipt)
        # Reuse the fixed worker's exact reservation, cleanup, and terminal-task checks.
        from crange_distribution import main
        main(['--catalog', str(CONFIG / 'image-catalog.json'), '--journal-dir', str(journal.directory),
              '--policy', str(CONFIG / 'distribution-policy.json'), 'source-probe',
              '--project', str(project), '--source-node', node, '--source-id', str(vmid),
              '--datastore-id', storage, '--isolated-bridge', bridge, '--job-id', probe_job,
              '--identity-record', str(journal.directory / (identity_job + '.json'))])
        receipt['state'] = 'verified'
        atomic_json(receipt_path, receipt)
        return vmid, {'export_job': export_job, 'probe_job': probe_job}
    finally:
        ledger.close()
