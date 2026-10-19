"""Recover completed, journaled archive restores without submitting another restore."""
import json
import re
from urllib.parse import quote

import crange_cache_jobs as jobs
from crange_cache_ownership import cache_description, cache_tags


def public_status():
    """Admin display hints only; execution independently validates every claim."""
    rows = []
    try:
        _,catalog=jobs._settings()
    except Exception:
        catalog={'images':{}}
    for path in sorted((jobs._root() / 'cache-journal').glob('cache-*.json')):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        snapshot_probe=(record.get('state')=='verifying' and record.get('kind')=='qemu' and
            type(record.get('probe_id')) is int and
            catalog.get('images',{}).get(record.get('image_id'),{}).get('source_identity',{}).get('identity_mode')=='windows-scenario-snapshot-v1')
        pending_probe=(record.get('state')=='verifying' and record.get('kind')=='qemu'
                       and type(record.get('probe_id')) is int
                       and (bool(record.get('probe_candidate')) or len(record.get('provider_tasks', [])) == 4))
        if ((not snapshot_probe and not pending_probe and (record.get('state') not in ('allocated', 'converting-template') or record.get('probe_id') is not None)) or
                record.get('job_id') != path.stem or
                not re.fullmatch(r'cache-[a-zA-Z0-9-]{1,64}', path.stem)):
            continue
        rows.append({**{key: record.get(key) for key in
                     ('job_id', 'image_id', 'node', 'datastore_id', 'template_id', 'kind', 'state')}, 'snapshot_probe':snapshot_probe})
    return rows


def _resume(api, ledger, journal, policy, catalog, record):
    fail = jobs.DistributionError
    job, node, storage, vmid = (record.get(k) for k in
                                ('job_id', 'node', 'datastore_id', 'template_id'))
    image_id = record.get('image_id')
    image = catalog.get('images', {}).get(image_id)
    pending_probe=(record.get('state')=='verifying' and record.get('kind')=='qemu'
                   and type(record.get('probe_id')) is int
                       and (bool(record.get('probe_candidate')) or len(record.get('provider_tasks', [])) == 4))
    if (not isinstance(job, str) or not re.fullmatch(r'cache-[a-zA-Z0-9-]{1,64}', job) or
            (not pending_probe and (record.get('state') not in ('allocated', 'converting-template') or record.get('probe_id') is not None)) or
            type(vmid) is not int or vmid < 100 or not image or
            node not in policy.get('allowed_nodes', []) or
            [node, storage] not in policy.get('approved_storage', []) or
            record.get('allocation_node') != node or
            record.get('artifact_sha256') != image.get('artifact_sha256')):
        raise fail('This cache journal is not an eligible completed archive restore.')
    kind = 'qemu' if image.get('kind') == 'vm' else 'lxc' if image.get('kind') == 'container' else None
    if kind is None or record.get('kind') != kind:
        raise fail('Cache restore kind differs from the immutable image.')
    artifact = image.get('artifact', {})
    if (artifact.get('complete_capture') is not True or
            not artifact.get('volume_id', '').startswith('crange-library:backup/') or
            (record.get('source_node'), str(record.get('source_id'))) !=
            (artifact.get('node'), str(artifact.get('source_id')))):
        raise fail('Cache restore source differs from its complete immutable archive.')
    if any(replica.get('job_id') == job or
           (replica.get('node') == node and str(replica.get('template_id')) == str(vmid))
           for replica in image.get('replicas', [])):
        raise fail('Catalog already references this cache allocation; use reconciliation.')
    saved = ledger.get_job(job)
    reservation = record.get('reservation')
    if (not isinstance(reservation, dict) or saved.get('state') != 'running' or
            saved.get('allocations') != [reservation] or
            reservation.get('operation') != 'cache_prepare' or reservation.get('image') != image_id or
            reservation.get('artifact_sha256') != image['artifact_sha256'] or
            reservation.get('resolved', {}).get('node_name') != node or
            reservation.get('resolved', {}).get('datastore_id') != storage or
            (saved.get('project_id') not in image.get('approved_projects', []) and
             '*' not in image.get('approved_projects', []))):
        raise fail('Exact running cache admission is missing or changed.')
    validator = jobs.shared_reservation_validator(ledger, job, reservation, image_id, image['artifact_sha256'])
    from crange_distribution import validate_probe_candidate
    candidate = validate_probe_candidate(image, record.get('probe_candidate'))
    if not validator(reservation, node, storage, jobs.required_capacity(image, probe_candidate=candidate)):
        raise fail('Retained reservation does not cover cache verification.')
    pin = json.loads((jobs._root() / 'artifact-pins' / (job + '.json')).read_text())
    if pin != {'job_id': job, 'state': 'active', 'volume_id': artifact['volume_id'],
               'artifact_sha256': image['artifact_sha256']}:
        raise fail('Exact immutable archive pin is missing or changed.')
    tasks = record.get('provider_tasks', [])
    operation = 'qmrestore' if kind == 'qemu' else 'vzrestore'
    failed_machine_probe = pending_probe and len(tasks) == 4
    if (len(tasks) != (4 if failed_machine_probe else 2 if pending_probe else 1) or tasks[0].get('node') != node or
            not re.fullmatch(rf'UPID:{re.escape(node)}:[A-Fa-f0-9]+:[A-Fa-f0-9]+:[A-Fa-f0-9]+:{operation}:{vmid}:[^:\r\n]+:',
                             str(tasks[0].get('upid', '')))):
        raise fail('Journal does not identify exactly one owned archive restore task.')
    status = api.request('GET', f'/nodes/{node}/tasks/{quote(tasks[0]["upid"], safe="")}/status')
    if status.get('status') != 'stopped' or status.get('exitstatus') != 'OK':
        raise fail('Archive restore is still active, failed, or unconfirmed; reservation retained.')
    if pending_probe:
        last=tasks[1]
        if (record.get('probe_pool')!='crange-probes' or last.get('node')!=node or
                not re.fullmatch(rf'UPID:{re.escape(node)}:[A-Fa-f0-9]+:[A-Fa-f0-9]+:[A-Fa-f0-9]+:qmclone:{vmid}:[^:\r\n]+:',str(last.get('upid','')))):
            raise fail('Retained probe has no exact pre-start clone history.')
        clone=api.request('GET',f'/nodes/{node}/tasks/{quote(last["upid"],safe="")}/status')
        if clone.get('status')!='stopped' or clone.get('exitstatus')!='OK':
            raise fail('Probe clone is not confirmed successful; no new clone submitted.')
    if failed_machine_probe:
        # Admit only the exact failed start and successful defensive stop. A
        # generic failed boot, foreign VMID or active task cannot be retried.
        probe_id = record['probe_id']
        for task, operation in zip(tasks[2:], ('qmstart', 'qmstop')):
            expected_path = f'/nodes/{node}/qemu/{probe_id}/status/' + ('start' if operation == 'qmstart' else 'stop')
            if (task.get('node') not in (None, node) or task.get('path') != expected_path or
                    not re.fullmatch(rf'UPID:{re.escape(node)}:[A-Fa-f0-9]+:[A-Fa-f0-9]+:[A-Fa-f0-9]+:{operation}:{probe_id}:[^:\r\n]+:', str(task.get('upid', '')))):
                raise fail('Probe recovery is outside the exact failed-machine-start history.')
            observed = api.request('GET', f'/nodes/{node}/tasks/{quote(task["upid"], safe="")}/status')
            if observed.get('status') != 'stopped':
                raise fail('Probe recovery task is active or uncertain.')
            error = str(observed.get('exitstatus', ''))
            if operation == 'qmstart':
                pattern = r"Installed QEMU version '[0-9.]+' is too old to run machine type '(pc-(?:q35|i440fx)-[0-9.]+(?:\+pve[0-9]+)?)', please upgrade node '" + re.escape(node) + r"'\.?"
                match = re.fullmatch(pattern, error)
                if not match:
                    raise fail('Only a verified unsupported QEMU machine start is recoverable here.')
                failed_machine = match[1]
            elif error != 'OK':
                raise fail('Failed probe stop is not confirmed successful.')
    active = api.request('GET', f'/nodes/{node}/tasks', {'source': 'active', 'limit': 1000})
    if len(active) >= 1000 or any(str(t.get('id')) in {str(vmid),str(record.get('probe_id'))} for t in active):
        raise fail('An active or ambiguous provider task still references this allocation.')
    resources = api.request('GET', '/cluster/resources', {'type': 'vm'})
    actual = [row for row in resources if str(row.get('vmid')) == str(vmid)]
    if (len(actual) != 1 or actual[0].get('node') != node or actual[0].get('type') != kind or
            actual[0].get('pool') != 'crange-iac' or actual[0].get('status') != 'stopped'):
        raise fail('Restored allocation is not the exact stopped range-owned guest.')
    config = api.request('GET', f'/nodes/{node}/{kind}/{vmid}/config')
    name = 'name' if kind == 'qemu' else 'hostname'
    tags = set(config.get('tags', '').split(';'))
    markers = {tag for tag in tags if tag.startswith(('crange-cache-', 'crange-job-'))}
    if (config.get(name) != f'crange-replica-{vmid}' or
            (record.get('state') in ('converting-template','verifying') and not jobs.cache_owned(config, vmid, job)) or
            not {'crange-image', f'crange-cache-{vmid}'} <= tags or
            markers != {f'crange-cache-{vmid}'} or config.get('lock') or
            int(config.get('template', 0)) != 1 or any(re.fullmatch(r'(?:unused|hostpci|usb)\d+|args|hookscript', key) for key in config)):
        raise fail('Restored template ownership, lock, or hardware differs; no adoption performed.')
    bridge = jobs.select_probe_bridge(policy)
    networks = [value for key, value in config.items() if re.fullmatch(r'net\d+', key)]
    if not networks or any('bridge=' + bridge not in value.split(',') for value in networks):
        raise fail('Restored template is not on the approved isolated probe bridge.')
    # A retained read-only ISO is not a guest-writable cache volume. Generated
    # cloud-init volumes still require exact local VMID ownership below.
    def shared_readonly_iso(value):
        parts=str(value).split(',')
        return ('media=cdrom' in parts and bool(re.fullmatch(
            r'(?:crange-library|media-mirror1|media-mirror4):iso/[A-Za-z0-9][A-Za-z0-9_.+-]*\.iso',parts[0])))
    disks = [(key, value.split(',')[0]) for key, value in config.items()
             if re.fullmatch(r'(?:scsi|sata|virtio|ide|efidisk|tpmstate)\d+|rootfs|mp\d+', key)
             and not shared_readonly_iso(value)]
    volumes = [(key, value) for key, value in disks if value not in ('none', '')]
    if (not volumes or not any(re.fullmatch(r'(?:scsi|sata|virtio|ide)\d+|rootfs', key) for key, _ in volumes) or
            any(not re.fullmatch(re.escape(storage) + rf':(?:base-{vmid}-disk-\d+|vm-{vmid}-cloudinit)', value)
                for _, value in volumes)):
        raise fail('Restored template disks are not exclusively owned local cache volumes.')
    settings = policy.get('cache', policy)
    required = {f'local-linked-{image["kind"]}', f'{image["kind"]}-archive-restore'}
    if not required <= set(settings.get('tested_paths', [])):
        raise fail('Cache restore and linked-clone paths have not passed platform acceptance.')
    if pending_probe:
        probe_id=record['probe_id']
        rows=[row for row in resources if str(row.get('vmid'))==str(probe_id)]
        probe=api.request('GET',f'/nodes/{node}/qemu/{probe_id}/config')
        status=api.request('GET',f'/nodes/{node}/qemu/{probe_id}/status/current')
        inherited=jobs.cache_owned(probe,vmid,job)
        own=jobs.cache_owned(probe,probe_id,job)
        if (probe_id==vmid or len(rows)!=1 or rows[0].get('node')!=node or rows[0].get('pool')!='crange-probes'
                or rows[0].get('type')!='qemu' or rows[0].get('status')!='stopped' or status.get('status')!='stopped'
                or probe.get('name')!=f'crange-probe-{probe_id}' or probe.get('lock') or int(probe.get('template',0))
                or not (inherited or own)):
            raise fail('Retained pre-start probe is not its exact stopped owned clone.')
        probe_disks=[str(value).split(',')[0] for key,value in probe.items()
                     if re.fullmatch(r'(?:scsi|sata|ide|virtio|tpmstate|efidisk)[0-9]+',key)
                     and not shared_readonly_iso(value) and str(value).split(',')[0]!='none']
        if not probe_disks or any(not re.fullmatch(re.escape(storage)+rf':vm-{probe_id}-(?:disk-[0-9]+|cloudinit)',v) for v in probe_disks):
            raise fail('Retained probe volumes differ from its admitted local allocation.')
        if failed_machine_probe:
            if str(probe.get('machine', '')).split(',')[0].removeprefix('type=') not in (failed_machine, re.sub(r'\+pve[0-9]+$', '', failed_machine)):
                raise fail('Failed machine type no longer matches the exact owned stopped probe.')
            from crange_capture import _portable_capture_machine
            # Capability check happens before any write. ProbeVerifier applies
            # the same fixed baseline immediately before the isolated boot.
            _portable_capture_machine(api, node, probe)
        api.request('PUT',f'/nodes/{node}/qemu/{probe_id}/config',
                    {'tags':cache_tags({},probe_id),'description':f'CRANGE verification {job}'})
        tasks[1]['state']='confirmed-terminal'
    identity, init_system, _ = jobs._cache_probe_identity(image)
    verify = jobs.ProbeVerifier(bridge, settings.get('isolated_bridges', []), identity,
                               ct_exec=jobs.SSHContainerExec(api=api), init_system=init_system)
    worker = jobs.ReplicaWorker(api, journal, verify, approved_storage=[(node, storage)],
        tested_paths=settings.get('tested_paths', []), linked_storages=jobs.proved_linked_storage(policy, node, storage),
        reservation_validator=validator, backing_verifier=jobs.UnixContainerBackingVerifier(api=api), probe_candidate=candidate)
    # The original task supplied the VMID/name/tags. Only after all fresh checks
    # may the interrupted post-restore metadata step be completed. Never restore,
    # convert, or boot this template; the worker verifies a new disposable clone.
    tasks[0]['state'] = 'confirmed-terminal'
    journal(record)
    api.request('PUT', f'/nodes/{node}/{kind}/{vmid}/config',
                {'tags': cache_tags(config, vmid), 'description': cache_description(job, image_id)})
    if not pending_probe:record['state'] = 'converting-template'
    journal(record)
    return worker.resume_template(catalog, image_id, record=record)


def resume(job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r'cache-[a-zA-Z0-9-]{1,64}', job_id):
        raise jobs.DistributionError('Choose a protected cache restore job.')
    policy, _ = jobs._settings()
    journal = jobs.FileJournal(jobs._root() / 'cache-journal')
    path = journal.directory / (job_id + '.json')
    record = json.loads(path.read_text())
    node = record.get('node')
    if node not in policy.get('allowed_nodes', []):
        raise jobs.DistributionError('Cache node is outside current platform policy.')
    ledger = jobs.ReservationLedger(jobs._root() / 'reservations.sqlite3')
    try:
        with jobs.scheduling.node_locks([node], library=True), journal.allocation_lock():
            record = json.loads(path.read_text())
            if record.get('job_id') != job_id or record.get('node') != node:
                raise jobs.DistributionError('Cache journal identity changed before recovery.')
            jobs.catalog_transaction(jobs.scheduling.CONFIG / 'image-catalog.json',
                lambda catalog: _resume(jobs.ProxmoxAPI(), ledger, journal, policy, catalog, record))
    finally:
        ledger.close()
    reconciled = jobs.reconcile_cache_job(job_id)
    return {**reconciled, 'state': 'ready', 'reservation': 'released'}
