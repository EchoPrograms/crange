"""Fixed, auditable image preparation jobs; never invoked by placement previews.

The worker supplies an API, durable journal and a reviewed verification function.
Cross-node VM copy and captured-CT restore stay disabled until administrators record
provider/API acceptance. Nothing here creates or reconfigures library storage.
"""
from __future__ import annotations

import hashlib
import math
import contextlib
import fcntl
import json
import subprocess
import shlex
import os
import tempfile
import re
import stat
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from crange_catalog import publish_replica, validate_catalog
from crange_cache_ownership import cache_owned, cache_tags, cache_description


class DistributionError(ValueError):
    pass


class GuestAgentPermissionError(DistributionError):
    """A QEMU Guest Agent request was explicitly rejected with HTTP 403."""


def artifact_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def publish_artifact(source: Path, library: Path, expected_sha256: str) -> Path:
    """Publish an administrator-exported artifact without replacing any version.

    The library directory must already be provisioned/reviewed. Streaming a backup
    export is permitted; copying live guest disks/configuration is never a caller
    of this operation. Checksum-addressed publication is atomic and read-only.
    """
    if not re.fullmatch(r'[a-f0-9]{64}', expected_sha256):
        raise DistributionError('Invalid published artifact checksum.')
    if not library.is_dir():
        raise DistributionError('Reviewed library directory does not exist.')
    destination = library / expected_sha256
    fd, temporary = tempfile.mkstemp(prefix='.publish-', dir=library)
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, 'wb') as output, source.open('rb') as input_file:
            for chunk in iter(lambda: input_file.read(1024 * 1024), b''):
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != expected_sha256:
            raise DistributionError('Export checksum does not match publication.')
        os.chmod(temporary, 0o440)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.is_symlink() or artifact_checksum(destination) != expected_sha256:
                raise DistributionError('Existing artifact identity is inconsistent.')
        return destination
    finally:
        Path(temporary).unlink(missing_ok=True)


PROBE_POOL = 'crange-probes'


def _require_probe_pool(api, node, kind, vmid):
    """Never use guest execution against ordinary range/template resources."""
    try:
        pool = api.request('GET', '/pools/' + PROBE_POOL)
        members = pool.get('members', [])
        present = any(member.get('type') == kind and member.get('node') == node
                      and str(member.get('vmid')) == str(vmid) for member in members)
    except Exception as exc:
        raise DistributionError('Cannot verify dedicated probe pool membership.') from exc
    if not present:
        raise DistributionError('Identity execution requires dedicated crange-probes pool membership.')


def _owned(config: dict, marker: str | None = None) -> None:
    tags = set(str(config.get('tags', '')).split(';'))
    if 'crange-image' not in tags or (marker and marker not in tags):
        raise DistributionError('Image is not owned by this distribution job.')


class _JournaledProbeAPI:
    """Record asynchronous probe start/stop tasks before waiting on them."""
    def __init__(self, api, record, journal):
        self.api, self.record, self.journal = api, record, journal

    def request(self, method, path, fields=None):
        result = self.api.request(method, path, fields)
        if method != 'GET' and isinstance(result, str) and result.startswith('UPID:'):
            self.record.setdefault('provider_tasks', []).append({'path': path, 'upid': result, 'state': 'running'})
            self.journal(dict(self.record))
        return result

    def task(self, node, upid, timeout=900):
        self.api.task(node, upid, timeout=timeout)
        for task in self.record.get('provider_tasks', []):
            if task.get('upid') == upid: task['state'] = 'confirmed-terminal'
        self.journal(dict(self.record))

    def record_evidence(self, evidence):
        """Persist protected verifier evidence before a caller parses it."""
        rows = self.record.setdefault('verification_evidence', [])
        rows.append(evidence)
        self.journal(dict(self.record))


class ReplicaWorker:
    """Journal reservations before submission; retain partial jobs on timeout.

    journal(record) must commit durable ownership; verification(api,node,kind,id)
    must run reviewed disposable-clone readiness and identity checks and return
    {passed: true, identity_unique: true, ...}. It cannot be supplied by GUI input.
    """
    def __init__(self, api, journal, verify, *, approved_storage, tested_paths=(), allocation_lock=None, hash_volume=None, reservation_validator=None, backing_verifier=None, linked_storages=(), probe_candidate=None):
        self.probe_candidate = probe_candidate
        self.backing_verifier = backing_verifier
        self.api, self.journal, self.verify = api, journal, verify
        self.allocation_lock = allocation_lock
        self.hash_volume = hash_volume
        self.reservation_validator = reservation_validator
        self._active_record = None
        self.approved_storage = set(approved_storage)
        self.tested_paths = set(tested_paths)
        self.linked_storages = {(n,s) for n,s in self.approved_storage if s=="local-lvm"} | (set(linked_storages) & self.approved_storage)

    def _config(self, node, kind, vmid):
        return self.api.request('GET', f'/nodes/{node}/{kind}/{vmid}/config')

    def _task(self, node, method, path, fields):
        print(f'Submitting cache operation: {method} {path}', flush=True)
        result = self.api.request(method, path, fields)
        if self._active_record is not None:
            self._active_record.setdefault('provider_tasks', []).append({'node': node, 'upid': result, 'state': 'running'})
            self.journal(dict(self._active_record))
        self.api.task(node, result, timeout=1800)
        if self._active_record is not None:
            self._active_record['provider_tasks'][-1]['state'] = 'confirmed-terminal'
            self.journal(dict(self._active_record))

    def prepare(self, catalog, image_id, **kwargs):
        with self.allocation_lock() if self.allocation_lock else contextlib.nullcontext():
            return self._prepare(catalog, image_id, **kwargs)

    def _prepare(self, catalog, image_id, *, source_node, source_id,
                node, datastore_id, job_id, archive=None, clone_mode="linked", reservation=None):
        if not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', job_id):
            raise DistributionError('Invalid distribution job ID.')
        if node not in {f'proxmox{i}' for i in range(1, 7)} or source_node not in {f'proxmox{i}' for i in range(1, 7)}:
            raise DistributionError('Unapproved node.')
        if (node, datastore_id) not in self.approved_storage:
            raise DistributionError('Destination datastore is not approved.')
        validate_catalog(catalog)
        image = catalog['images'][image_id]
        kind = 'qemu' if image['kind'] == 'vm' else 'lxc'
        reusable = [r for r in image['replicas'] if r.get('cache_local') and
                    r['node'] == node and r['datastore_id'] == datastore_id and
                    r.get('state') == 'ready' and clone_mode in r.get('clone_modes_verified', [])]
        if len(reusable) > 1:
            raise DistributionError('Ambiguous cache generation; reconcile before preparation.')
        if reusable:
            config = self._config(node, kind, reusable[0]['template_id'])
            if not cache_owned(config, reusable[0]['template_id'], reusable[0].get('job_id')):
                raise DistributionError('Image is not owned by this distribution job.')
            if int(config.get('template', 0)) != 1 or config.get('lock'):
                raise DistributionError('Published cache generation changed; fixed repair required.')
            return catalog
        if clone_mode not in ('linked', 'full'):
            raise DistributionError('Invalid clone verification mode.')
        if clone_mode == 'linked' and ((node, datastore_id) not in self.linked_storages or
                f'local-linked-{image["kind"]}' not in self.tested_paths):
            raise DistributionError('Local linked clone capability has not passed acceptance.')
        if archive:
            artifact = image.get('artifact', {})
            if (archive.get('state') != 'exported' or not archive.get('complete_capture') or
                    archive.get('source_id') != source_id or archive.get('node') != source_node or
                    artifact.get('source_id') != source_id or artifact.get('node') != source_node or
                    archive.get('volume_id') != artifact.get('volume_id') or
                    archive.get('artifact_sha256') != image['artifact_sha256']):
                raise DistributionError('Archive provenance does not match the immutable image catalog.')
            expression = r'[a-zA-Z0-9_-]+:backup/vzdump-' + ('qemu' if kind == 'qemu' else 'lxc') + r'-[a-zA-Z0-9_.-]+'
            if not re.fullmatch(expression, archive.get('volume_id', '')):
                raise DistributionError('Invalid captured backup volume.')
            if f'{image["kind"]}-archive-restore' not in self.tested_paths:
                raise DistributionError('Target-local archive restore has not passed acceptance.')
            print(f'Verifying immutable archive checksum from {node} before cache allocation.', flush=True)
            if self.hash_volume is None or self.hash_volume(node, archive['volume_id']) != image['artifact_sha256']:
                raise DistributionError('Target-accessible central artifact checksum verification failed.')
            source = {'tags': 'crange-image'}
            matching = []
        else:
            source = self._config(source_node, kind, int(source_id))
            _owned(source)
            if int(source.get('template', 0)) != 1:
                raise DistributionError('Source must be an immutable owned template.')
            matching = [r for r in image['replicas'] if r['node'] == source_node and
                        r.get('template_id') == source_id and r['state'] == 'ready' and
                        r['artifact_sha256'] == image['artifact_sha256']]
            if not matching:
                raise DistributionError('Source is not a verified replica of this image.')
            if source_node != node:
                raise DistributionError('Cross-node cache fill requires a verified central complete backup; no source-local temporary copy.')
        capacity = required_capacity(image, clone_mode=clone_mode,
                                     source_config=None if archive else source,
                                     probe_candidate=self.probe_candidate)
        if (self.reservation_validator is None or
                not self.reservation_validator(reservation, node, datastore_id, capacity)):
            raise DistributionError('A valid shared-ledger cache preparation reservation is required.')
        record = dict(job_id=job_id, image_id=image_id, node=node,
                      datastore_id=datastore_id, state='allocated', kind=kind,
                      source_node=source_node, source_id=source_id,
                      artifact_sha256=image['artifact_sha256'], reservation=reservation,
                      created_at=datetime.now(timezone.utc).timestamp())
        if self.probe_candidate:
            record['probe_candidate'] = dict(self.probe_candidate)
        vmid = int(self.api.request('GET', '/cluster/nextid'))
        owned_tags = cache_tags(source, vmid)
        record['template_id'] = vmid
        record['allocation_node'] = node
        self.journal(dict(record))
        self._active_record = record
        # A task failure/timeout leaves ownership journal intact: reconciliation,
        # not blind retry or deletion, decides whether allocation occurred.
        if archive:
            fields = {'vmid': vmid, 'storage': datastore_id, 'pool': 'crange-iac',
                      'unique': 1, 'tags': owned_tags}
            if kind == 'qemu':
                fields.update(archive=archive['volume_id'], name=f'crange-replica-{vmid}')
            else:
                fields.update(ostemplate=archive['volume_id'], restore=1, unprivileged=1,
                              hostname=f'crange-replica-{vmid}')
            self._task(node, 'POST', f'/nodes/{node}/{kind}', fields)
        else:
            self._task(node, 'POST', f'/nodes/{node}/{kind}/{source_id}/clone',
                       {'newid': vmid, 'full': 1, 'storage': datastore_id,
                        'pool': 'crange-iac',
                        'name' if kind == 'qemu' else 'hostname': f'crange-replica-{vmid}',
                        'description': f'CRANGE distribution {job_id} image {image_id}'})
        self.api.request('PUT', f'/nodes/{node}/{kind}/{vmid}/config',
                         {'tags': owned_tags, 'description': cache_description(job_id, image_id)})
        record['state'] = 'converting-template'
        self.journal(dict(record))
        if kind == 'qemu':
            current = self._config(node, kind, vmid)
            if int(current.get('template', 0)) != 1:
                self._task(node, 'POST', f'/nodes/{node}/{kind}/{vmid}/template', {})
        else:
            import time
            current = self._config(node, kind, vmid)
            upid = (self.api.request('POST', f'/nodes/{node}/lxc/{vmid}/template', {})
                    if int(current.get('template', 0)) != 1 else None)
            if upid is not None:
                record.setdefault('provider_tasks', []).append({'node': node, 'upid': upid, 'state': 'running'})
                self.journal(dict(record))
                self.api.task(node, upid, timeout=600)
                record['provider_tasks'][-1]['state'] = 'confirmed-terminal'
                self.journal(dict(record))
            deadline = time.monotonic() + 600
            while int(self._config(node, kind, vmid).get('template', 0)) != 1:
                if time.monotonic() >= deadline:
                    raise DistributionError('Container cache conversion is unconfirmed; reconcile journal.')
                time.sleep(2)

        return self._verify_and_publish(catalog, image_id, image, kind, node, datastore_id,
                                        job_id, vmid, clone_mode, owned_tags, record)

    def resume_template(self, catalog, image_id, *, record, clone_mode='linked'):
        """Continue an exactly owned restored template through probe and publication."""
        validate_catalog(catalog)
        image = catalog['images'][image_id]
        kind = 'qemu' if image['kind'] == 'vm' else 'lxc'
        if (record.get('state') not in ('converting-template', 'verifying') or
                record.get('image_id') != image_id or
                record.get('kind') != kind or clone_mode not in ('linked', 'full')):
            raise DistributionError('Cache journal is not an eligible restored-template resume.')
        node, datastore_id, job_id, vmid = (record.get('node'), record.get('datastore_id'),
                                            record.get('job_id'), record.get('template_id'))
        if (not isinstance(vmid, int) or (node, datastore_id) not in self.approved_storage or
                record.get('allocation_node') != node or
                record.get('artifact_sha256') != image.get('artifact_sha256')):
            raise DistributionError('Cache resume identity is incomplete or outside approved scope.')
        self._active_record = record
        owned_tags = cache_tags({}, vmid)
        return self._verify_and_publish(catalog, image_id, image, kind, node, datastore_id,
                                        job_id, vmid, clone_mode, owned_tags, record)

    def _verify_and_publish(self, catalog, image_id, image, kind, node, datastore_id,
                            job_id, vmid, clone_mode, owned_tags, record):
        config = self._config(node, kind, vmid)
        if not cache_owned(config, vmid, job_id):
            raise DistributionError('Image is not owned by this distribution job.')
        if int(config.get('template', 0)) != 1:
            raise DistributionError('Replica conversion did not produce a template.')
        if kind == 'lxc' and clone_mode == 'linked' and self.backing_verifier is None:
            raise DistributionError('Linked CT cache publication requires physical backing verification.')
        if record.get('state')=='verifying':
            probe_id=record.get('probe_id')
            if type(probe_id) is not int or probe_id==vmid:
                raise DistributionError('Retained probe identity is incomplete.')
            config=self._config(node,kind,probe_id)
            status=self.api.request('GET',f'/nodes/{node}/{kind}/{probe_id}/status/current')
            if not cache_owned(config,probe_id,job_id) or config.get('lock') or status.get('status')!='stopped':
                raise DistributionError('Retained probe is not the exact stopped owned clone.')
        else:
            probe_id = int(self.api.request('GET', '/cluster/nextid'))
            record.update(state='verifying', probe_id=probe_id, probe_pool=PROBE_POOL)
            self.journal(dict(record))
            self._task(node, 'POST', f'/nodes/{node}/{kind}/{vmid}/clone',
                       {'newid': probe_id, 'full': int(clone_mode == 'full'),
                        **({'storage': datastore_id} if clone_mode == 'full' else {}),
                        'pool': PROBE_POOL, 'description': f'CRANGE verification {job_id}',
                        'name' if kind == 'qemu' else 'hostname': f'crange-probe-{probe_id}'})
        probe_config = {'tags': cache_tags({}, probe_id),
                        'description': f'CRANGE verification {job_id}'}
        candidate = validate_probe_candidate(image, record.get('probe_candidate'))
        if candidate or image.get('probe_resources'):
            profile = candidate or image['probe_resources']
            probe_config.update(memory=profile['memory_mb'], cores=profile['cpu_cores'])
            if kind == 'qemu':
                probe_config['sockets'] = 1
                # A fixed verification size can be below the source's minimum
                # balloon size. Keep this disposable guest at its admitted RAM.
                probe_config['balloon'] = 0
                if config.get('vcpus') is not None:
                    probe_config['vcpus'] = min(int(config['vcpus']), profile['cpu_cores'])
            record['probe_resources'] = dict(profile)
            self.journal(dict(record))
        self.api.request('PUT', f'/nodes/{node}/{kind}/{probe_id}/config', probe_config)
        if kind == 'lxc' and clone_mode == 'linked':
            proof = self.backing_verifier(node, vmid, probe_id)
            if not isinstance(proof, dict) or proof.get('verified') is not True:
                raise DistributionError('Linked CT cache clone backing relation is unverified.')
            record['linked_backing'] = proof
            self.journal(dict(record))
        print(f'Verifying disposable {kind} clone {probe_id} on {node}; the cache template remains stopped.', flush=True)
        result = self.verify(_JournaledProbeAPI(self.api, record, self.journal), node, kind, probe_id)
        if not isinstance(result, dict) or result.get('passed') is not True or result.get('identity_unique') is not True:
            record['state'] = 'failed-verification'
            self.journal(dict(record))
            raise DistributionError('Disposable clone readiness/identity verification failed.')
        # Probe deletion is guarded and must complete before publication.
        if not cache_owned(self._config(node, kind, probe_id), probe_id, job_id):
            raise DistributionError('Probe is not owned by this distribution job.')
        self._task(node, 'DELETE', f'/nodes/{node}/{kind}/{probe_id}', {'purge': 1})
        replica = dict(node=node, template_id=vmid, datastore_id=datastore_id,
                       state='ready', artifact_sha256=image['artifact_sha256'],
                       verified_at=datetime.now(timezone.utc).isoformat(),
                       verification=result, job_id=job_id, clone_modes=[clone_mode], clone_modes_verified=[clone_mode], cache_local=True)
        if datastore_id != 'local-lvm' and (node,datastore_id) in self.linked_storages:
            replica['storage_verification']={'node':node,'datastore_id':datastore_id,
                                             'local':True,'snapshots_verified':True}
        updated = publish_replica(catalog, image_id, replica)
        if candidate:
            # This candidate becomes approved only after actual readiness,
            # independent guest identity and confirmed disposable-probe deletion.
            updated['images'][image_id]['probe_resources'] = {
                **candidate, 'verified': True,
                'evidence': f'Fresh isolated clone boot, unique identity and cleanup: {job_id}; '
                            f'archive {image["artifact_sha256"]} on {node}/{datastore_id}'}
        record['state'] = 'ready'
        self.journal(dict(record))
        return updated


def reserve_probe_storage(ledger, *, datastore_id, auto_storage=False, **scope):
    """Reserve a full VM probe on preferred or another approved local store."""
    from crange_placement import PlacementError
    if not auto_storage:
        return ledger.reserve_source_probe(datastore_id=datastore_id, **scope)
    if scope.get('kind') != 'vm' or scope.get('clone_mode') != 'full':
        raise PlacementError('Automatic probe storage requires a full VM copy.')
    node, policy, inventory = scope['node_name'], scope['policy'], scope['inventory']
    approved = set(map(tuple, policy.get('approved_storage', [])))
    if (node, datastore_id) not in approved:
        raise PlacementError('Preferred probe storage is not approved.')
    stores = inventory.get('nodes', {}).get(node, {}).get('storages', {})
    candidates = [datastore_id] + sorted(storage for storage, details in stores.items()
        if storage != datastore_id and (node, storage) in approved and
        storage in policy.get('allowed_storages', []) and details.get('shared') is False)
    failures = {}
    for storage in candidates:
        try:
            return ledger.reserve_source_probe(datastore_id=storage, **scope)
        except PlacementError as error:
            # Node, quota, identity and existing-allocation errors must never be
            # bypassed by choosing another disk. Failed reservations roll back.
            reason = str(error)
            if not (reason.startswith('source-probe has insufficient disk_gb headroom') or
                    reason.startswith('source-probe datastore or guest capability unavailable')):
                raise
            failures[storage] = reason
    raise PlacementError('No approved local probe datastore has headroom: ' + json.dumps(failures, sort_keys=True))


class SourceProbeWorker:
    """Verify one captured source by cloning it into the isolated probe pool."""
    def __init__(self, api, journal, verify, reservation_validator, backing_verifier=None):
        self.backing_verifier = backing_verifier
        self.api, self.journal, self.verify = api, journal, verify
        self.reservation_validator = reservation_validator

    def run(self, *, source_node, source_id, datastore_id, job_id, capture,
            reservation):
        if (source_node not in {f'proxmox{i}' for i in range(1, 7)} or
                type(source_id) is not int or source_id < 100 or
                not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', job_id)):
            raise DistributionError('Invalid source-probe scope.')
        kind = _source_guest_type(capture)
        _require_probe_pool(self.api, source_node, kind, source_id)
        base = f'/nodes/{source_node}/{kind}/{source_id}'
        source = self.api.request('GET', base + '/config')
        _owned(source)
        if (int(source.get('template', 0)) != 1 or
                self.api.request('GET', base + '/status/current').get('status') != 'stopped' or
                _source_config_identity(source, source_id) != capture.get('source_config_sha256')):
            raise DistributionError('Captured source template identity changed or is not stopped.')
        capacity = _source_capacity(source, kind)
        clone_mode = reservation.get('resolved', {}).get('clone_mode', 'full')
        if clone_mode not in ('full', 'linked') or (clone_mode == 'linked' and
                (kind != 'lxc' or self.backing_verifier is None or
                 not str(source.get('rootfs', '')).startswith(datastore_id + ':'))):
            raise DistributionError('Linked source probes require an exact local CT source and backing verifier.')
        source_ref = f'source:{source_node}:{kind}:{source_id}'
        if (self.reservation_validator is None or not self.reservation_validator(
                reservation, source_node, datastore_id, capacity)):
            raise DistributionError('A valid shared-ledger source-probe reservation is required.')
        marker = f'crange-job-{job_id}'
        vmid = _free_probe_vmid(self.api)
        record = {'job_id': job_id, 'image_id': source_ref, 'node': source_node,
                  'datastore_id': datastore_id, 'state': 'allocated', 'kind': 'vm' if kind == 'qemu' else 'container',
                  'source_node': source_node, 'source_id': source_id,
                  'source_config_sha256': capture['source_config_sha256'],
                  'artifact_sha256': capture['source_config_sha256'],
                  'source_capture_job_id': capture['job_id'],
                  'reservation': reservation, 'probe_id': vmid,
                  'probe_pool': PROBE_POOL, 'probe_marker': marker}
        self.journal(dict(record))
        clone_fields = {'newid': vmid, 'full': int(clone_mode == 'full'),
                        **({'storage': datastore_id} if clone_mode == 'full' else {}),
                        'pool': PROBE_POOL, 'name' if kind == 'qemu' else 'hostname': f'crange-source-probe-{vmid}',
                        'description': f'CRANGE source verification {job_id}'}
        upid = self.api.request('POST', base + '/clone', clone_fields)
        record.setdefault('provider_tasks', []).append(
            {'path': base + '/clone', 'upid': upid, 'state': 'running'})
        self.journal(dict(record))
        self.api.task(source_node, upid, timeout=1800)
        record['provider_tasks'][-1]['state'] = 'confirmed-terminal'
        self.journal(dict(record))
        probe_path = f'/nodes/{source_node}/{kind}/{vmid}'
        probe = self.api.request('GET', probe_path + '/config')
        _owned(probe)
        if int(probe.get('template', 0)) == 1:
            raise DistributionError('Disposable source clone unexpectedly remains a template.')
        tags = set(str(probe.get('tags', '')).split(';')) | {'crange-image', marker}
        self.api.request('PUT', probe_path + '/config', {'tags': ';'.join(sorted(tags))})
        if clone_mode == 'linked':
            proof = self.backing_verifier(source_node, source_id, vmid)
            if not isinstance(proof, dict) or proof.get('verified') is not True:
                raise DistributionError('Linked source-probe backing relation is unverified.')
            record['linked_backing'] = proof
        record['state'] = 'verifying'
        self.journal(dict(record))
        result = self.verify(_JournaledProbeAPI(self.api, record, self.journal),
                             source_node, kind, vmid)
        if not isinstance(result, dict) or result.get('passed') is not True or result.get('identity_unique') is not True:
            record['state'] = 'failed-verification'
            self.journal(dict(record))
            raise DistributionError('Source clone readiness/identity verification failed.')
        current = self.api.request('GET', probe_path + '/config')
        _owned(current, marker)
        if self.api.request('GET', probe_path + '/status/current').get('status') != 'stopped':
            raise DistributionError('Verified source probe did not stop cleanly.')
        _require_probe_pool(self.api, source_node, kind, vmid)
        delete_upid = self.api.request('DELETE', probe_path, {'purge': 1})
        record.setdefault('provider_tasks', []).append(
            {'path': probe_path, 'upid': delete_upid, 'state': 'running'})
        self.journal(dict(record))
        self.api.task(source_node, delete_upid, timeout=1800)
        record['provider_tasks'][-1]['state'] = 'confirmed-terminal'
        if source != self.api.request('GET', base + '/config') or self.api.request('GET', base + '/status/current').get('status') != 'stopped':
            raise DistributionError('Source template changed during clone verification.')
        record.update(state='source-probed', verification=result)
        self.journal(dict(record))
        return record

    def resume(self, *, source_node, source_id, datastore_id, job_id, capture,
               reservation, task_upid):
        """Attach a task UPID to one exact allocated clone and finish it safely."""
        path = self.journal.directory / (job_id + '.json')
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise DistributionError('Source-probe journal is missing or unreadable.') from exc
        kind = _source_guest_type(capture)
        source_ref = f'source:{source_node}:{kind}:{source_id}'
        if (record.get('state') != 'allocated' or record.get('job_id') != job_id or
                record.get('source_node') != source_node or record.get('source_id') != source_id or
                record.get('source_config_sha256') != capture.get('source_config_sha256') or
                record.get('source_capture_job_id') != capture.get('job_id') or
                record.get('image_id') != source_ref or record.get('datastore_id') != datastore_id or
                record.get('reservation') != reservation or
                not re.fullmatch(r'UPID:[^:]+:[0-9A-F]+:[0-9A-F]+:[0-9A-F]+:[^:]+:[^:]+:[^:]*:',
                                 str(task_upid), re.IGNORECASE)):
            raise DistributionError('Existing source-probe journal does not match the exact task and reservation.')
        _require_probe_pool(self.api, source_node, kind, source_id)
        base = f'/nodes/{source_node}/{kind}/{source_id}'
        source = self.api.request('GET', base + '/config')
        _owned(source)
        if (int(source.get('template', 0)) != 1 or
                self.api.request('GET', base + '/status/current').get('status') != 'stopped' or
                _source_config_identity(source, source_id) != capture.get('source_config_sha256')):
            raise DistributionError('Captured source template identity changed or is not stopped.')
        capacity = _source_capacity(source, kind)
        clone_mode = reservation.get('resolved', {}).get('clone_mode', 'full')
        if clone_mode not in ('full', 'linked') or (clone_mode == 'linked' and
                (kind != 'lxc' or self.backing_verifier is None or
                 not str(source.get('rootfs', '')).startswith(datastore_id + ':'))):
            raise DistributionError('Linked source probes require an exact local CT source and backing verifier.')
        if self.reservation_validator is None or not self.reservation_validator(reservation, source_node, datastore_id, capacity):
            raise DistributionError('A valid shared-ledger source-probe reservation is required.')
        vmid = int(record.get('probe_id', 0))
        probe_path = f'/nodes/{source_node}/{kind}/{vmid}'
        probe = self.api.request('GET', probe_path + '/config')
        marker = f'crange-job-{job_id}'
        if (int(probe.get('template', 0)) == 1 or
                probe.get('name' if kind == 'qemu' else 'hostname') != f'crange-source-probe-{vmid}' or
                str(probe.get('description','')).rstrip('\r\n') != f'CRANGE source verification {job_id}' or
                'crange-image' not in str(probe.get('tags', '')).split(';') or
                self.api.request('GET', probe_path + '/status/current').get('status') != 'stopped'):
            raise DistributionError('Existing source-probe guest identity or stopped state is not exact.')
        _require_probe_pool(self.api, source_node, kind, vmid)
        networks = [value for key, value in probe.items() if re.fullmatch(r'net[0-9]+', key)]
        if not networks or any(
                not re.search(r'(?:^|,)bridge=' + re.escape(self.verify.bridge) + r'(?:,|$)', str(value)) or
                'link_down=1' not in str(value) for value in networks):
            raise DistributionError('Existing source-probe guest is not fully disconnected on the approved bridge.')
        recorded_tasks = record.setdefault('provider_tasks', [])
        clone_task = {'path': base + '/clone', 'upid': task_upid, 'state': 'running'}
        if recorded_tasks:
            if (len(recorded_tasks) != 1 or
                    any(recorded_tasks[0].get(key) != value for key, value in clone_task.items()
                        if key != 'state') or
                    recorded_tasks[0].get('state') not in ('running', 'confirmed-terminal')):
                raise DistributionError('Existing source-probe provider task is outside the exact clone-resume scope.')
        else:
            recorded_tasks.append(clone_task)
            self.journal(dict(record))
        self.api.task(source_node, task_upid, timeout=1800)
        record['provider_tasks'][-1]['state'] = 'confirmed-terminal'
        self.journal(dict(record))
        tags = set(str(probe.get('tags', '')).split(';')) | {'crange-image', marker}
        self.api.request('PUT', probe_path + '/config', {'tags': ';'.join(sorted(tags))})
        if clone_mode == 'linked':
            proof = self.backing_verifier(source_node, source_id, vmid)
            if not isinstance(proof, dict) or proof.get('verified') is not True:
                raise DistributionError('Linked source-probe backing relation is unverified.')
            record['linked_backing'] = proof
        record['state'] = 'verifying'
        self.journal(dict(record))
        wrapped = _JournaledProbeAPI(self.api, record, self.journal)
        result = self.verify(wrapped, source_node, kind, vmid)
        if not isinstance(result, dict) or result.get('passed') is not True or result.get('identity_unique') is not True:
            record['state'] = 'failed-verification'
            self.journal(dict(record))
            raise DistributionError('Source clone readiness/identity verification failed.')
        current = self.api.request('GET', probe_path + '/config')
        _owned(current, marker)
        if self.api.request('GET', probe_path + '/status/current').get('status') != 'stopped':
            raise DistributionError('Verified source probe did not stop cleanly.')
        _require_probe_pool(self.api, source_node, kind, vmid)
        upid = wrapped.request('DELETE', probe_path, {'purge': 1})
        wrapped.task(source_node, upid, timeout=1800)
        if source != self.api.request('GET', base + '/config') or self.api.request('GET', base + '/status/current').get('status') != 'stopped':
            raise DistributionError('Source template changed during clone verification.')
        record.update(state='source-probed', verification=result)
        self.journal(dict(record))
        return record


def find_source_clone_task(api, node, source_id, target_id, kind='qemu'):
    """Find the unique terminal PVE clone task whose log created this VMID."""
    import urllib.parse
    tasks = api.request('GET', f'/nodes/{node}/tasks')
    matches = []
    for task in tasks:
        if task.get('type') != ('qmclone' if kind == 'qemu' else 'vzclone') or str(task.get('id')) != str(source_id):
            continue
        upid = task.get('upid')
        if not isinstance(upid, str):
            continue
        lines = api.request('GET', f'/nodes/{node}/tasks/{urllib.parse.quote(upid, safe="")}/log')
        content = '\n'.join(str(line.get('t', '')) for line in lines)
        if (f'vm-{target_id}-disk' in content and
                (f'base-{source_id}-disk' in content or f'vm-{source_id}-cloudinit' in content)):
            matches.append((task, upid))
    if len(matches) != 1 or matches[0][0].get('status') != 'OK':
        raise DistributionError('Cannot identify one successful exact source-clone task.')
    api.task(node, matches[0][1], timeout=120)
    return matches[0][1]


def reconcile_source_probe(api, journal, ledger, job_id):
    """Release a task-free allocation only with fresh exact absence evidence."""
    if not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', job_id):
        raise DistributionError('Invalid source-probe reconciliation job.')
    path = journal.directory / (job_id + '.json')
    record = json.loads(path.read_text())
    reservation = record.get('reservation')
    saved = ledger.get_job(job_id)
    node, source_id, probe_id = record.get('node'), record.get('source_id'), record.get('probe_id')
    kind = _source_guest_type(record)
    source_ref = f'source:{node}:{kind}:{source_id}'
    if (record.get('job_id') != job_id or record.get('state') not in ('allocated', 'reconciled-absent') or
            record.get('provider_tasks') or record.get('upid') or
            node not in {f'proxmox{i}' for i in range(1, 7)} or record.get('source_node') != node or
            type(probe_id) is not int or not 20000 <= probe_id < 20100 or type(source_id) is not int or
            saved.get('owner') != 'cli' or saved.get('state') not in ('running', 'released') or
            saved.get('allocations') != [reservation] or not isinstance(reservation, dict) or
            reservation.get('operation') != 'source_probe' or reservation.get('image') != source_ref or
            reservation.get('project_id') != saved.get('project_id') or record.get('image_id') != source_ref or
            reservation.get('artifact_sha256') != record.get('source_config_sha256') or
            reservation.get('resolved', {}).get('node_name') != node or
            reservation.get('resolved', {}).get('datastore_id') != record.get('datastore_id')):
        raise DistributionError('Exact task-free source-probe ledger and journal ownership required.')
    if saved['state'] == 'released':
        if record.get('state') != 'reconciled-absent' or record.get('absence_verified') is not True:
            raise DistributionError('Released source-probe lacks an exact absence receipt.')
        return record
    _require_probe_pool(api, node, kind, source_id)
    config = api.request('GET', f'/nodes/{node}/{kind}/{source_id}/config')
    _owned(config)
    if int(config.get('template', 0)) != 1 or _source_config_identity(config, source_id) != record.get('source_config_sha256'):
        raise DistributionError('Captured source changed; retain reservation.')
    from crange_cache_jobs import _prove_absent_cache_allocation
    created_at = float(record.get('created_at', path.stat().st_mtime))
    evidence = _prove_absent_cache_allocation(api, node, probe_id, created_at)
    # Clone task IDs refer to the source, so a target-only task scan is insufficient.
    for source in ('active', 'all'):
        tasks = api.request('GET', f'/nodes/{node}/tasks', {'source': source, 'limit': 1000})
        if len(tasks) >= 1000 or any(str(t.get('id')) == str(source_id) and
                t.get('type') in ('qmclone', 'vzclone') and
                (source == 'active' or float(t.get('starttime', 0)) >= created_at - 1) for t in tasks):
            raise DistributionError('Source clone task exists or history is incomplete; retain reservation.')
    evidence['source_clone_task_scan_complete'] = True
    record.update(state='reconciled-absent', absence_verified=True, absence_evidence=evidence)
    journal(record)
    ledger.reconcile_job(job_id, operation_terminal=True, allocations_reconciled=True)
    return record


def _source_guest_type(capture):
    kind = capture.get('kind', 'vm')
    if kind not in ('vm', 'container'):
        raise DistributionError('Unsupported source-probe guest kind.')
    return 'qemu' if kind == 'vm' else 'lxc'


def _source_capture_profile(capture):
    if isinstance(capture, dict) and capture.get('provenance_type') == 'sealed-windows-source-v1':
        from crange_catalog import validate_sealed_windows_capture
        validate_sealed_windows_capture(capture)
        if capture.get('state') != 'sealed-source-captured':
            raise DistributionError('Sealed Windows source record has an invalid capture state.')
        return 'windows'
    identity = capture.get('source_identity', {}) if isinstance(capture, dict) else {}
    if identity.get('os_family') == 'windows':
        if (capture.get('os_family') != 'windows' or 'init_system' in capture or
                capture.get('readiness') != 'windows-ready'):
            raise DistributionError('Windows identity record is not bound to the Windows capture profile.')
        return 'windows'
    init_system = capture.get('init_system') if isinstance(capture, dict) else None
    expected = 'running' if init_system == 'systemd' else 'default'
    if init_system not in ('systemd', 'openrc') or capture.get('readiness') != expected:
        raise DistributionError('Identity record is not bound to a supported guest capture profile.')
    return init_system


def _require_worker_sealed_record(record, supplied_path):
    """Require sealed evidence arguments to name the exact protected worker journal."""
    from crange_catalog import validate_sealed_windows_capture
    from crange_scheduling import DATA
    validate_sealed_windows_capture(record)
    expected = Path(DATA) / 'source-journal' / 'p100-sealed-windows-8912.json'
    path = Path(supplied_path)
    try:
        st = expected.lstat()
        supplied = path.resolve(strict=True)
        trusted = json.loads(expected.read_text())
    except (OSError, ValueError) as exc:
        raise DistributionError('Fixed worker-journaled P100 sealed source proof is required.') from exc
    if (path.is_symlink() or supplied != expected.resolve() or not stat.S_ISREG(st.st_mode) or
            stat.S_IMODE(st.st_mode) != 0o600 or trusted != record):
        raise DistributionError('Sealed source proof does not match the protected worker journal.')


def _source_capacity(config, kind):
    if kind == 'qemu':
        return _qemu_capacity(config)
    if str(config.get('unprivileged')) != '1':
        raise DistributionError('Source container must be unprivileged.')
    if any(re.fullmatch(r'mp[0-9]+', str(k)) or str(k).startswith(('lxc.', 'dev')) for k in config):
        raise DistributionError('Source container mounts/devices are unsupported for isolated probes.')
    rootfs = str(config.get('rootfs', ''))
    if not re.match(r'[a-zA-Z0-9_-]+:[a-zA-Z0-9_/.-]+,', rootfs):
        raise DistributionError('Source container root filesystem must be a managed storage volume.')
    return _qemu_capacity({'memory': config.get('memory'), 'cores': config.get('cores'), 'scsi0': rootfs})


def _qemu_capacity(config):
    """Return conservative full-clone plus write allowance from PVE config."""
    try:
        memory = int(config['memory'])
        cpu = int(config['cores']) * int(config.get('sockets', 1))
    except (KeyError, TypeError, ValueError) as exc:
        raise DistributionError('Source VM has no defensible RAM/CPU allocation.') from exc
    disk_bytes = 0
    for key, value in config.items():
        if not re.fullmatch(r'(?:scsi|sata|virtio|ide)[0-9]+', key) or ',media=cdrom' in str(value):
            continue
        match = re.search(r'(?:^|,)size=([0-9]+(?:\.[0-9]+)?)([KMGT])(?:i?B)?(?:,|$)', str(value), re.I)
        if not match:
            raise DistributionError('Source VM disk size is not available for safe capacity reservation.')
        amount = float(match.group(1))
        scale = {'K': 1024, 'M': 1024**2, 'G': 1024**3, 'T': 1024**4}[match.group(2).upper()]
        disk_bytes += amount * scale
    if disk_bytes <= 0:
        raise DistributionError('Source VM has no measurable disk for safe capacity reservation.')
    return {'memory_mb': memory, 'cpu_cores': cpu,
            'disk_gb': disk_bytes / (1024**3) * 1.1}


def _verified_system_disk(config, source_id):
    """Bind the boot-selected OS disk and its exact size to the source config."""
    match = re.search(r'(?:^|,)order=([^,]+)', str(config.get('boot') or ''))
    if not match:
        raise DistributionError('VM image has no authoritative Proxmox boot-disk order.')
    from crange_proxmox_disk import managed_volume, size_bytes
    for candidate in match.group(1).split(';'):
        if not re.fullmatch(r'(?:scsi|sata|virtio|ide)[0-9]+', candidate):
            continue
        value = config.get(candidate)
        if not isinstance(value, str) or 'media=cdrom' in value or ':cloudinit' in value:
            continue
        try:
            managed_volume(value)
            size = size_bytes(value) / 1024**3
        except ValueError as exc:
            raise DistributionError('Boot-selected VM system disk is not a verified managed volume.') from exc
        if not math.isfinite(size) or size <= 0:
            raise DistributionError('Boot-selected VM system disk size is invalid.')
        return {'interface': candidate, 'disk_gb': size,
                'source_config_sha256': _source_config_identity(config, int(source_id))}
    raise DistributionError('VM image boot order does not identify a managed operating-system disk.')


def _free_probe_vmid(api):
    """Choose and assert an unused ID outside the legacy/low VMID range."""
    resources = api.request('GET', '/cluster/resources', {'type': 'vm'})
    used = {int(item['vmid']) for item in resources if item.get('vmid') is not None}
    for candidate in range(20000, 20100):
        if candidate in used:
            continue
        confirmed = int(api.request('GET', '/cluster/nextid', {'vmid': candidate}))
        if confirmed == candidate:
            return candidate
    raise DistributionError('No unused source-probe VMID in the reviewed allocation band.')


def shared_reservation_validator(ledger, job_id, allocation, image_id, artifact_sha256):
    """Bind a worker operation to its exact running shared-ledger reservation."""
    def validate(proof, node, storage, required):
        saved = ledger.validate_job(job_id)
        return (len(saved) == 1 and saved[0] == allocation and proof == allocation and
                allocation.get('image') == image_id and
                allocation.get('artifact_sha256') == artifact_sha256 and
                allocation.get('resolved', {}).get('node_name') == node and
                allocation.get('resolved', {}).get('datastore_id') == storage and
                all(allocation.get('resources', {}).get(key, 0) >= required[key]
                    for key in ('memory_mb', 'cpu_cores', 'disk_gb')))
    return validate


class FileJournal:
    """Controller-owned durable journal and cooperative allocation lock."""
    _INITIAL_STATES = {'identity-captured', 'sealed-source-captured', 'exporting', 'allocated', 'evicting'}
    _MUTABLE_FIELDS = {'state', 'provider_tasks', 'upid', 'eviction', 'verification_evidence', 'cleanup_progress'}
    _TRANSITIONS = {
        'identity-captured': {'identity-captured'},
        'sealed-source-captured': {'sealed-source-captured'},
        'exporting': {'exporting', 'exported'},
        'exported': {'exported'},
        'allocated': {'allocated', 'converting-template', 'verifying', 'reconciled-absent', 'cancelled-uncertain'},
        'converting-template': {'converting-template', 'verifying', 'failed-verification', 'cancelled-uncertain'},
        'replacing-probe': {'replacing-probe','verifying'},
        'verifying': {'replacing-probe', 'verifying', 'ready', 'source-probed', 'failed-verification', 'cancelled-uncertain', 'cleaned-owned'},
        'failed-verification': {'failed-verification', 'cleaned-owned'},
        'cleaned-owned': {'cleaned-owned'},
        'source-probed': {'source-probed'},
        'ready': {'ready', 'evicted'},
        'evicting': {'evicting', 'evicted', 'cancelled-uncertain'},
        'evicted': {'evicted'},
        'cancelled-uncertain': {'cancelled-uncertain'},
        'reconciled-absent': {'reconciled-absent'},
    }

    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    @classmethod
    def _validate_update(cls, previous, incoming):
        if previous.get('job_id') != incoming.get('job_id'):
            raise DistributionError('Journal update cannot change job identity.')
        old_state, new_state = previous.get('state'), incoming.get('state')
        if new_state not in cls._TRANSITIONS.get(old_state, set()):
            raise DistributionError('Journal state transition is not permitted.')
        if old_state == 'verifying' and new_state == 'replacing-probe':
            replacement=incoming.get('probe_replacement',{})
            if (previous.get('kind')!='qemu' or previous.get('probe_replacement') or
                    type(previous.get('probe_id')) is not int or
                    set(replacement)!={'probe_id','requested_at','reason'} or
                    replacement.get('probe_id')!=previous['probe_id'] or
                    replacement.get('reason')!='guest-agent-verification-failed' or
                    not isinstance(replacement.get('requested_at'),str) or
                    not any(row.get('type') in ('qga-timeout','qga-exec-error')
                            for row in previous.get('verification_evidence',[]))):
                raise DistributionError('Probe replacement requires exact failed verification and a single immutable attempt.')
        for key, value in previous.items():
            if key in cls._MUTABLE_FIELDS:
                continue
            if key not in incoming or incoming[key] != value:
                raise DistributionError('Journal update changed or removed immutable job identity.')
        if 'upid' in previous and incoming.get('upid') != previous['upid']:
            raise DistributionError('Journal provider task identity cannot be changed.')
        if 'upid' in incoming and not re.fullmatch(
                r'UPID:[^:]+:[0-9A-F]+:[0-9A-F]+:[0-9A-F]+:[^:]+:[^:]+:[^:]*:',
                str(incoming['upid']), re.IGNORECASE):
            raise DistributionError('Journal provider task ID is invalid.')
        old_tasks, new_tasks = previous.get('provider_tasks', []), incoming.get('provider_tasks', [])
        if not isinstance(old_tasks, list) or not isinstance(new_tasks, list) or len(new_tasks) < len(old_tasks):
            raise DistributionError('Journal provider task history cannot be removed.')
        for task in new_tasks:
            if (not isinstance(task, dict) or
                    not re.fullmatch(r'UPID:[^:]+:[0-9A-F]+:[0-9A-F]+:[0-9A-F]+:[^:]+:[^:]+:[^:]*:',
                                     str(task.get('upid', '')), re.IGNORECASE) or
                    task.get('state') not in {'running', 'confirmed-terminal'}):
                raise DistributionError('Journal provider task history is malformed.')
        for index, old_task in enumerate(old_tasks):
            new_task = new_tasks[index]
            if not isinstance(old_task, dict) or not isinstance(new_task, dict):
                raise DistributionError('Journal provider task history is malformed.')
            if any(new_task.get(key) != value for key, value in old_task.items() if key != 'state'):
                raise DistributionError('Journal provider task identity cannot be changed.')
            allowed = {'running': {'running', 'confirmed-terminal'},
                       'confirmed-terminal': {'confirmed-terminal'}}
            if new_task.get('state') not in allowed.get(old_task.get('state'), set()):
                raise DistributionError('Journal provider task state cannot move backwards.')
        old_evidence = previous.get('verification_evidence', [])
        new_evidence = incoming.get('verification_evidence', [])
        if (not isinstance(old_evidence, list) or not isinstance(new_evidence, list) or
                len(new_evidence) < len(old_evidence) or new_evidence[:len(old_evidence)] != old_evidence or
                any(not isinstance(item, dict) or item.get('type') not in {
                    'qga-exec', 'qga-exec-error', 'qga-status', 'qga-status-error', 'qga-timeout'}
                    for item in new_evidence)):
            raise DistributionError('Protected verification evidence must remain append-only.')
        old_progress = previous.get('cleanup_progress', [])
        new_progress = incoming.get('cleanup_progress', [])
        if (not isinstance(old_progress, list) or not isinstance(new_progress, list) or
                len(new_progress) < len(old_progress) or new_progress[:len(old_progress)] != old_progress or
                any(not isinstance(item, dict) or type(item.get('vmid')) is not int or
                    item.get('absent') is not True or item.get('inventory_complete') is not True or
                    not re.fullmatch(r'[a-f0-9]{64}', str(item.get('cluster_guest_inventory_sha256', '')))
                    for item in new_progress)):
            raise DistributionError('Owned cache cleanup progress must remain append-only and evidenced.')
        if new_state == 'exported':
            if (not incoming.get('volume_id') or
                    not re.fullmatch(r'[a-zA-Z0-9_.-]+:backup/vzdump-(?:qemu|lxc)-[a-zA-Z0-9_.-]+',
                                     str(incoming.get('volume_id'))) or
                    not re.fullmatch(r'[a-f0-9]{64}', str(incoming.get('artifact_sha256', ''))) or
                    incoming.get('verified') is not True or incoming.get('complete_capture') is not True or
                    not incoming.get('upid')):
                raise DistributionError('Completed export requires one verified artifact and terminal UPID.')
        if new_state == 'reconciled-absent':
            evidence = incoming.get('absence_evidence')
            failed_restore = (incoming.get('kind')=='lxc' and len(old_tasks)==len(new_tasks)==1 and
                isinstance(evidence,dict) and evidence.get('local_volumes_absent') is True and
                evidence.get('failed_restore_upid')==new_tasks[0].get('upid') and
                re.fullmatch(rf"UPID:{re.escape(str(incoming.get('node')))}:[A-Fa-f0-9]+:[A-Fa-f0-9]+:[A-Fa-f0-9]+:vzrestore:{incoming.get('template_id')}:[^:\r\n]+:",str(new_tasks[0].get('upid',''))) and
                re.fullmatch(rf"unable to restore CT {incoming.get('template_id')} - unable to activate storage 'crange-library' - directory '/mnt/pve/crange-library' does not exist or is unreachable\.?",str(evidence.get('failed_restore_exitstatus',''))) and
                new_tasks[0].get('state')=='confirmed-terminal')
            if (old_state != 'allocated' or ((old_tasks or new_tasks) and not failed_restore) or
                    incoming.get('absence_verified') is not True or not isinstance(evidence, dict) or
                    evidence.get('verified') is not True or evidence.get('inventory_complete') is not True or
                    evidence.get('target_absent') is not True or
                    evidence.get('active_task_scan_complete') is not True or
                    evidence.get('history_scan_complete') is not True or
                    evidence.get('node') != incoming.get('node') or
                    evidence.get('template_id') != incoming.get('probe_id' if incoming.get('reservation', {}).get('operation') == 'source_probe' else 'template_id') or
                    (incoming.get('reservation', {}).get('operation') == 'source_probe' and evidence.get('source_clone_task_scan_complete') is not True) or
                    not re.fullmatch(r'[a-f0-9]{64}', str(evidence.get('cluster_guest_inventory_sha256', '')))):
                raise DistributionError('Absent allocation reconciliation requires task-free allocation evidence.')
        if new_state == 'source-probed':
            evidence = incoming.get('verification')
            if (not new_tasks or any(task.get('state') != 'confirmed-terminal' for task in new_tasks) or
                    not isinstance(evidence, dict) or evidence.get('passed') is not True or
                    evidence.get('identity_unique') is not True):
                raise DistributionError('Source-probed state requires verified identity and terminal provider tasks.')
        if new_state == 'cleaned-owned':
            evidence = incoming.get('cleanup_evidence')
            if (not isinstance(evidence, dict) or evidence.get('verified') is not True or
                    evidence.get('node') != incoming.get('node') or
                    evidence.get('template_id') != incoming.get('template_id') or
                    evidence.get('probe_id') != incoming.get('probe_id') or
                    evidence.get('inventory_complete') is not True or
                    evidence.get('both_guests_absent') is not True or
                    evidence.get('provider_tasks_terminal') is not True or
                    not re.fullmatch(r'[a-f0-9]{64}', str(evidence.get('cluster_guest_inventory_sha256', ''))) or
                    any(task.get('state') != 'confirmed-terminal' for task in new_tasks)):
                raise DistributionError('Owned cache cleanup requires terminal tasks and complete absence evidence.')
        if 'eviction' in previous and incoming.get('eviction') != previous['eviction']:
            raise DistributionError('Journal eviction receipt cannot be changed.')

    @contextlib.contextmanager
    def allocation_lock(self):
        with (self.directory / '.allocation.lock').open('a') as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def confirm_eviction(self, job_id, template_id, plan_hash, upid):
        """Called only after owned exact-generation provider deletion completed."""
        if not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', job_id):
            raise DistributionError('Invalid evicted cache job ID.')
        record_path = self.directory / f'{job_id}.json'
        if not record_path.exists():
            raise DistributionError('Evicted generation journal is missing; reconcile catalog.')
        record = json.loads(record_path.read_text())
        if record.get('template_id') != template_id:
            raise DistributionError('Eviction receipt does not match the prepared generation.')
        record.update(state='evicted', eviction={'plan_hash': plan_hash, 'upid': upid,
                                              'provider_terminal': True})
        self(record)

    def __call__(self, record):
        job_id = record['job_id']
        if not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', job_id):
            raise DistributionError('Invalid journal ID.')
        destination = self.directory / f'{job_id}.json'
        if record['state'] == 'allocated' and record.get('image_id'):
            key = (record['image_id'], record['node'], record['datastore_id'])
            for entry in self.directory.glob('*.json'):
                if entry == destination:
                    continue
                try:
                    other = json.loads(entry.read_text())
                except (OSError, ValueError) as exc:
                    raise DistributionError('Uncertain cache journal; reconcile before allocation.') from exc
                if ((other.get('image_id'), other.get('node'), other.get('datastore_id')) == key and
                        other.get('state') not in ('reconciled-absent', 'cleaned-owned', 'evicted')):
                    raise DistributionError('Cache generation has an existing inflight/uncertain job; reconcile or attach.')
        if destination.exists():
            try:
                previous = json.loads(destination.read_text())
            except (OSError, ValueError) as exc:
                raise DistributionError('Existing journal is unreadable; reconcile before updating.') from exc
            self._validate_update(previous, record)
        elif record.get('state') not in self._INITIAL_STATES:
            raise DistributionError('Unknown initial journal state.')
        fd, temporary = tempfile.mkstemp(dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(record, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            directory_fd = os.open(self.directory, os.O_DIRECTORY)
            try: os.fsync(directory_fd)
            finally: os.close(directory_fd)
        finally:
            Path(temporary).unlink(missing_ok=True)


def _source_config_identity(config, vmid):
    # PVE changes these metadata fields when stopping/converting to a template.
    stable = {key: value for key, value in config.items() if key not in {'template', 'lock', 'digest'}}
    # Template conversion renames only this VM's disk volume prefix from
    # vm-<id>-disk-N to base-<id>-disk-N. File-backed datastores include the
    # VMID directory and format suffix (for example 109/base-109-disk-0.raw);
    # normalize those exact paths too while preserving datastore, directory,
    # disk number, format, size/options, and every other config field.
    volume = re.compile(
        rf'(?P<storage>[A-Za-z0-9_.-]+:)(?P<directory>{int(vmid)}/)?'
        rf'(?:vm|base)-{int(vmid)}-disk-(?P<number>[0-9]+)'
        r'(?P<format>\.(?:raw|qcow2|vmdk))?(?=,|$)')
    for key, value in stable.items():
        if isinstance(value, str):
            stable[key] = volume.sub(
                lambda match: f'{match["storage"]}{match["directory"] or ""}'
                f'vm-{int(vmid)}-disk-{match["number"]}{match["format"] or ""}', value)
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _sealed_windows_source_volumes(config):
    """Normalize only 8912's two exact managed raw volumes across PVE template conversion."""
    expected = {'crange-library:8912/vm-8912-disk-0.raw',
                'crange-library:8912/vm-8912-disk-1.raw'}
    normalized = set()
    volume_pattern = re.compile(r'crange-library:8912/(?:vm|base)-8912-disk-([01])\.raw')
    for key in ('sata0', 'sata1', 'scsi0', 'scsi1', 'virtio0', 'virtio1', 'efidisk0'):
        value = config.get(key)
        if not isinstance(value, str):
            continue
        volume = value.split(',', 1)[0]
        match = volume_pattern.fullmatch(volume)
        if match:
            normalized.add(f'crange-library:8912/vm-8912-disk-{match.group(1)}.raw')
    return normalized == expected


def _fixed_guest_read(api, node, guest_type, vmid, command, ct_exec, timeout):
    if guest_type == 'lxc':
        if ct_exec is None: raise DistributionError('Fixed CT exec adapter is unavailable.')
        return ct_exec(node, vmid, command).strip()
    import time
    deadline = time.monotonic() + timeout
    # These commands are fixed read-only collectors. Agent exec can briefly
    # fail with 500 during boot even after get-osinfo succeeds. A bounded retry
    # is safe here; mutating guest initialization uses its separate PID journal.
    while True:
        try:
            response = api.request('POST', f'/nodes/{node}/qemu/{vmid}/agent/exec', {'command': command})
            break
        except RuntimeError as error:
            if getattr(error.__cause__, 'code', None) != 500 or time.monotonic() >= deadline:
                raise
            time.sleep(2)
    while time.monotonic() < deadline:
        result = api.request('GET', f'/nodes/{node}/qemu/{vmid}/agent/exec-status', {'pid': int(response['pid'])})
        if result.get('exited'):
            if int(result.get('exitcode', -1)) != 0:
                # Identify the fixed collector without copying guest output or
                # command arguments (which may contain identity data) into jobs.
                collector = Path(command[0]).name
                detail = ''
                if command == ['/usr/bin/systemctl', 'is-system-running']:
                    collector += ' is-system-running'
                    state = str(result.get('out-data', '')).strip()
                    if state in {'initializing', 'starting', 'running', 'degraded',
                                 'maintenance', 'stopping', 'offline', 'unknown'}:
                        detail = f'; state: {state}'
                error = DistributionError(
                    f'Source identity collector {collector} failed '
                    f'(exit {int(result.get("exitcode", -1))}{detail}).')
                if command == ['/usr/bin/systemctl', 'is-system-running'] and detail:
                    error.collector_state = state
                raise error
            return str(result.get('out-data', '')).strip()
        time.sleep(1)
    raise DistributionError('Source identity guest-agent check timed out.')


def _guest_readiness(read, init_system):
    if init_system == 'systemd':
        state=read(['/usr/bin/systemctl','is-system-running'])
        if state=='degraded':
            error=DistributionError('Source guest is not ready for identity capture.')
            error.collector_state='degraded'
            raise error
        return state
    # OpenRC installations differ on whether rc-status is linked into /bin or
    # /sbin. Try only these fixed executable paths; never invoke a shell.
    failure = None
    for command in (['/bin/rc-status', '--runlevel'], ['/sbin/rc-status', '--runlevel']):
        try:
            return read(command)
        except Exception as exc:
            failure = exc
    raise DistributionError('OpenRC readiness command failed.') from failure


def normalize_isolated_network_wait(api, node, kind, vmid, bridge, *, ct_exec=None):
    """Clear only a known offline wait failure on an owned disposable VM.

    This does not mask/disable a service or permit general degraded readiness.
    The caller must subsequently observe normal readiness and collect identity.
    """
    if kind not in ('qemu','lxc') or (kind=='lxc' and ct_exec is None):
        return None
    _require_probe_pool(api, node, kind, vmid)
    base = f'/nodes/{node}/{kind}/{vmid}'
    config = api.request('GET', base + '/config')
    _owned(config)
    nics = [str(value).split(',') for key, value in config.items()
            if re.fullmatch(r'net[0-9]+', key)]
    if (not nics or int(config.get('template', 0)) or
            any(f'bridge={bridge}' not in nic or 'link_down=1' not in nic for nic in nics) or
            api.request('GET', base + '/status/current').get('status') != 'running'):
        raise DistributionError('Offline readiness repair requires an owned running disconnected probe.')
    command = ['/usr/bin/systemctl', '--failed', '--no-pager', '--no-legend']
    output = _fixed_guest_read(api, node, kind, vmid, command, ct_exec, 30)
    rows = [line.strip() for line in output.splitlines() if line.strip()]
    unit = 'systemd-networkd-wait-online.service'
    if len(rows) != 1 or not re.fullmatch(
            rf'(?:\S+\s+)?{re.escape(unit)}\s+loaded\s+failed\s+failed(?:\s+.*)?', rows[0]):
        return None
    if kind=='lxc':
        result=ct_exec.reset_network_wait(node,vmid)
        if result.get('reset') is not True:
            return None
        return {'unit':unit,'action':'reset-failed','bridge':bridge,
                'all_nics_disconnected':True,'services_disabled':False}
    response = api.request('POST', base + '/agent/exec', {
        'command': ['/usr/bin/systemctl', 'reset-failed', unit]})
    import time
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = api.request('GET', base + '/agent/exec-status', {'pid': int(response['pid'])})
        if result.get('exited'):
            if int(result.get('exitcode', -1)) != 0:
                raise DistributionError('Offline network-wait status reset failed.')
            return {'unit': unit, 'action': 'reset-failed', 'bridge': bridge,
                    'all_nics_disconnected': True, 'services_disabled': False}
        time.sleep(1)
    raise DistributionError('Offline network-wait status reset observation timed out.')


def _windows_guest_identity(read):
    """Collect fixed Windows identity/readiness fields through QEMU Guest Agent."""
    script = r"""$ErrorActionPreference='Stop';$os=Get-CimInstance Win32_OperatingSystem;$cs=Get-CimInstance Win32_ComputerSystem;$admin=Get-CimInstance Win32_UserAccount|Where-Object {$_.LocalAccount -and $_.Name -eq 'Administrator'}|Select-Object -First 1;$guid=(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography').MachineGuid;$imageState=(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Setup\State').ImageState;$setup=Get-ItemProperty 'HKLM:\SYSTEM\Setup';$install=(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').InstallationType;$ga=Get-Service QEMU-GA;$term=Get-Service TermService;$winrm=Get-Service WinRM;$rdp=@(Get-NetFirewallRule -DisplayGroup 'Remote Desktop' -Enabled True|Get-NetFirewallAddressFilter|Select-Object -ExpandProperty RemoteAddress -Unique);[ordered]@{os_caption=$os.Caption;installation_type=$install;os_version=$os.Version;os_build=[string]$os.BuildNumber;computer_name=$cs.Name;part_of_domain=[bool]$cs.PartOfDomain;machine_guid=$guid;local_administrator_sid=$admin.SID;machine_sid=($admin.SID -replace '-500$','');image_state=$imageState;oobe_in_progress=$setup.OOBEInProgress;system_setup_in_progress=$setup.SystemSetupInProgress;setup_type=$setup.SetupType;qemu_guest_agent=($ga.Status -eq 'Running');qemu_guest_agent_automatic=($ga.StartType -eq 'Automatic');term_service_running=($term.Status -eq 'Running');term_service_automatic=($term.StartType -eq 'Automatic');winrm_service_running=($winrm.Status -eq 'Running');winrm_service_automatic=($winrm.StartType -eq 'Automatic');rdp_allowed_sources=$rdp}|ConvertTo-Json -Compress"""
    raw = read([r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
                '-NoProfile', '-NonInteractive', '-Command', script])
    try:
        identity = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise DistributionError('Windows source identity response is not valid JSON.') from exc
    if not isinstance(identity, dict):
        raise DistributionError('Windows source identity response is not an object.')
    identity['os_family'] = 'windows'
    from crange_catalog import validate_source_identity
    validate_source_identity(identity)
    return identity


def capture_source_identity(api, *, node, kind, vmid, job_id, journal, ct_exec=None, timeout=60, init_system='systemd', snapshot_project=None):
    """Read fixed identity commands before reviewed stop/template conversion.

    This never starts, stops or reconfigures a guest and cannot boot a template.
    The trusted worker journals the result; GUI parameters cannot supply commands.
    """
    from crange_catalog import validate_source_identity
    if (node not in {f'proxmox{i}' for i in range(1, 7)} or kind not in ('vm', 'container')
            or type(vmid) is not int or vmid < 100 or not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', job_id)
            or init_system not in ('systemd', 'openrc', 'windows') or
            (init_system == 'windows' and kind != 'vm') or not 1 <= timeout <= 180):
        raise DistributionError('Invalid source identity capture scope.')
    guest_type = 'qemu' if kind == 'vm' else 'lxc'
    base = f'/nodes/{node}/{guest_type}/{vmid}'
    _require_probe_pool(api, node, guest_type, vmid)
    config = api.request('GET', base + '/config')
    _owned(config)
    if int(config.get('template', 0)) or api.request('GET', base + '/status/current').get('status') != 'running':
        raise DistributionError('Identity capture requires an owned running guest before template conversion.')
    read = lambda command: _fixed_guest_read(api, node, guest_type, vmid, command, ct_exec, timeout)
    if init_system == 'windows':
        if snapshot_project is not None:
            if type(snapshot_project) is not int or not 1<=snapshot_project<=255 or f'p{snapshot_project:03d}' not in str(config.get('tags','')).split(';'):
                raise DistributionError('Scenario snapshot project does not match the owned capture tags.')
            from crange_windows_snapshot import collect
            identity=collect(read,config.get('vmgenid'),snapshot_project)
        else: identity = _windows_guest_identity(read)
        readiness = 'windows-ready'
    else:
        readiness = _guest_readiness(read, init_system)
        if readiness != ('running' if init_system == 'systemd' else 'default'):
            raise DistributionError('Source guest is not ready for identity capture.')
        identity = {'machine_id': read(['/bin/cat', '/etc/machine-id']),
                    'ssh_host_key': read(['/bin/cat', '/etc/ssh/ssh_host_ed25519_key.pub'])}
        validate_source_identity(identity)
    if config != api.request('GET', base + '/config') or api.request('GET', base + '/status/current').get('status') != 'running':
        raise DistributionError('Source changed during identity capture; repeat capture.')
    record = {'state': 'identity-captured', 'job_id': job_id, 'node': node, 'kind': kind,
              'source_id': vmid, 'source_config_sha256': _source_config_identity(config, vmid),
              'source_identity': identity, 'readiness': readiness,
              'captured_at': datetime.now(timezone.utc).isoformat()}
    if init_system == 'windows':
        record['os_family'] = 'windows'
    else:
        record['init_system'] = init_system
    journal(record)
    return record


def _readonly_template_backup_repair(api, *, node, vmid, job_id, upid, status, config):
    """Prove this exact terminal SATA/IDE template failure has a host-side fix."""
    fields = upid.split(':') if isinstance(upid, str) else []
    if (not re.fullmatch(r'capture-p[0-9]+-[a-f0-9]{12}-export', job_id)
            or len(fields) < 9 or fields[0] != 'UPID' or fields[1] != node
            or fields[5:7] != ['vzdump', str(vmid)]
            or any(not re.fullmatch('[a-fA-F0-9]+', value) for value in fields[2:5])
            or status.get('upid') != upid or status.get('node') != node
            or status.get('type') != 'vzdump' or str(status.get('id')) != str(vmid)
            or status.get('status') != 'stopped' or status.get('exitstatus') != 'job errors'
            or int(config.get('template', 0)) != 1 or config.get('lock')):
        raise DistributionError('Failed template export requires manual reconciliation.')
    disks = [str(value).split(',')[0] for key, value in config.items()
             if re.fullmatch(r'(sata|ide)[0-9]+', key)
             and re.search(r':base-' + str(vmid) + r'-disk-[0-9]+$', str(value).split(',')[0])]
    if not disks:
        raise DistributionError('Failed export is not the reviewed read-only template case.')
    log_path = f'/nodes/{node}/tasks/{urllib.parse.quote(upid, safe="")}/log'
    lines = []
    for offset in range(0, 1000, 50):
        page = api.request('GET', log_path, {'start': offset, 'limit': 50})
        if not isinstance(page, list):
            raise DistributionError('Failed export task log is unavailable.')
        lines.extend(str(row.get('t', '')) for row in page)
        if len(page) < 50:
            break
    text = '\n'.join(lines)
    if (not all(marker in text for marker in (
            'starting kvm to execute backup task', 'The device is not writable: Permission denied',
            f'Backup of VM {vmid} failed', f'CRANGE export {job_id}'))
            or not any("'" + disk + "'" in text for disk in disks)):
        raise DistributionError('Failed export does not match the reviewed template backup regression.')
    packages = api.request('GET', f'/nodes/{node}/apt/versions')
    matches = [row for row in packages if row.get('Package') == 'qemu-server'] if isinstance(packages, list) else []
    if len(matches) != 1 or matches[0].get('CurrentState') != 'Installed':
        raise DistributionError('Template backup package is not confirmed fully installed.')
    # PVE reports the available candidate as Version, and installed as OldVersion.
    # An available update is not evidence that the broken package was repaired.
    version = matches[0].get('OldVersion', matches[0].get('Version', ''))
    measured = re.fullmatch(r'([0-9]+)\.([0-9]+)\.([0-9]+)(?:[+~-].*)?', str(version))
    if not measured or tuple(map(int, measured.groups())) < (9, 0, 18):
        raise DistributionError('Prepare native template backups on this node before retrying export.')
    return {'reason': 'readonly-template-backup-package-repaired', 'node': node,
            'qemu_server_version': version, 'failed_upid': upid}


def export_image(api, *, node, kind, vmid, storage, job_id, journal,
                 hash_volume, approved_backup_storage, identity_record=None):
    """Export an owned stopped guest/template via vzdump, including config.

    hash_volume(node, volume_id) is a trusted fixed transport adapter, never a
    user-provided command. Returns the actual SHA256 of the exported file.
    """
    approved_backup_storage = tuple(approved_backup_storage)
    if (node, storage) not in set(approved_backup_storage) or kind not in ('vm', 'container'):
        raise DistributionError('Unapproved backup destination or image kind.')
    if not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', job_id):
        raise DistributionError('Invalid export job ID.')
    guest_type = 'qemu' if kind == 'vm' else 'lxc'
    config = api.request('GET', f'/nodes/{node}/{guest_type}/{int(vmid)}/config')
    _owned(config)
    if int(config.get('template', 0)) != 1:
        raise DistributionError('Export publication requires an immutable owned template.')
    from crange_scheduling import disk_allocations
    disk_gb = sum(disk_allocations(config).values())
    if disk_gb <= 0:
        raise DistributionError('Image export has no defensible configured disk allocation.')
    status = api.request('GET', f'/nodes/{node}/{guest_type}/{int(vmid)}/status/current')
    if status.get('status') != 'stopped':
        raise DistributionError('Published source must be stopped before export.')
    # Do not publish an incomplete captured CT with external/excluded mounts.
    if kind == 'container':
        for key, value in config.items():
            if re.fullmatch(r'mp[0-9]+', key):
                volume = str(value).split(',')[0]
                if ':' not in volume or ',backup=1' not in str(value):
                    raise DistributionError('Captured CT has an excluded/external mount.')
        if any(str(k).startswith(('lxc.mount', 'dev')) for k in config):
            raise DistributionError('Captured CT external mounts/devices are unsupported.')
    from crange_catalog import validate_source_identity, validate_sealed_windows_capture
    identity_record = identity_record if isinstance(identity_record, dict) else {}
    sealed_capture = identity_record.get('provenance_type') == 'sealed-windows-source-v1'
    windows_capture = sealed_capture or identity_record.get('source_identity', {}).get('os_family') == 'windows'
    if sealed_capture:
        validate_sealed_windows_capture(identity_record)
        from crange_scheduling import DATA
        trusted_record = Path(DATA) / 'source-journal' / 'p100-sealed-windows-8912.json'
        try:
            record_stat = trusted_record.lstat()
            trusted = json.loads(trusted_record.read_text())
        except (OSError, ValueError) as exc:
            raise DistributionError('Fixed worker-journaled P100 sealed source proof is required.') from exc
        if (trusted_record.is_symlink() or not stat.S_ISREG(record_stat.st_mode) or
                stat.S_IMODE(record_stat.st_mode) != 0o600 or trusted != identity_record):
            raise DistributionError('Sealed source proof does not match the protected worker journal.')
        expected_tags = {'crange-image', 'crange-capture-windows-server2025-locale-20261008',
                         'mhl-windows-sealed-locale'}
        if (node != 'proxmox4' or int(vmid) != 8912 or
                set(str(config.get('tags', '')).split(';')) != expected_tags or
                not _sealed_windows_source_volumes(config) or
                identity_record.get('source_config_sha256') != _source_config_identity(config, vmid) or
                status.get('status') != 'stopped'):
            raise DistributionError('Fresh sealed Windows source freeze does not match the reviewed P100 candidate.')
        profile_valid = identity_record.get('source_state') == 'stopped-never-booted'
    else:
        profile_valid = (
            identity_record.get('os_family') == 'windows' and
            'init_system' not in identity_record and identity_record.get('readiness') == 'windows-ready'
            if windows_capture else
            identity_record.get('init_system') in ('systemd', 'openrc') and
            identity_record.get('readiness') == ('running' if identity_record.get('init_system') == 'systemd' else 'default')
        )
    if (identity_record.get('state') != 'identity-captured'
            and not sealed_capture
            or (identity_record.get('node'), identity_record.get('kind'), identity_record.get('source_id')) != (node, kind, vmid)
            or identity_record.get('source_config_sha256') != _source_config_identity(config, vmid)
            or not profile_valid):
        raise DistributionError('Matching worker-captured source identity is required before export.')
    if not sealed_capture:
        from crange_windows_snapshot import validate_captured_identity
        validate_captured_identity(identity_record.get('source_identity'))
    content_path = f'/nodes/{node}/storage/{storage}/content'
    journal_dir = getattr(journal, 'directory', None)
    if not isinstance(journal_dir, (str, Path)):
        journal_dir = None
    elif isinstance(journal_dir, str):
        journal_dir = Path(journal_dir)
    prior_path = journal_dir / f'{job_id}.json' if journal_dir is not None else None
    if prior_path is not None and prior_path.exists():
        prior = json.loads(prior_path.read_text())
        expected = {
            'job_id': job_id, 'node': node, 'kind': kind, 'source_id': vmid,
            'storage': storage, 'disk_gb': disk_gb,
            'identity_capture': dict(identity_record),
            'memory_mb': int(config.get('memory', 0)),
            'cpu_cores': int(config.get('cores', 0)),
        }
        if not sealed_capture:
            expected['source_identity'] = dict(identity_record['source_identity'])
        if not windows_capture:
            expected['init_system'] = identity_record['init_system']
        else:
            expected['os_family'] = 'windows'
        if kind == 'vm':
            expected['system_disk'] = _verified_system_disk(config, vmid)
        if any(prior.get(key) != value for key, value in expected.items()):
            raise DistributionError('Existing export journal does not match this exact captured template.')
        if prior.get('state') == 'exported':
            digest = hash_volume(node, prior.get('volume_id', ''))
            if digest != prior.get('artifact_sha256'):
                raise DistributionError('Previously exported archive checksum changed; retain journal for review.')
            return prior
        if prior.get('state') == 'exporting' and prior.get('upid'):
            # Recovery after a worker interruption is bound to the already
            # submitted backup task. Never submit a second vzdump for this job.
            status = api.request('GET', f'/nodes/{node}/tasks/{urllib.parse.quote(prior["upid"], safe="")}/status')
            if status.get('status') == 'running':
                print('Resuming observation of the existing export task; no new backup submitted.', flush=True)
                api.task(node, prior['upid'], timeout=1800)
                status = api.request('GET', f'/nodes/{node}/tasks/{urllib.parse.quote(prior["upid"], safe="")}/status')
            if (kind == 'vm' and status.get('status') == 'stopped' and
                    status.get('exitstatus') == 'unexpected status' and
                    re.fullmatch(r'capture-p[0-9]+-[a-f0-9]{12}-export', job_id)):
                # A host reboot may leave an owned stopped template backup-locked.
                # The root relay independently verifies boot epoch, exact terminal
                # task, pool, tags, config hash and absence of active guest backups.
                contents = api.request('GET', content_path)
                before = set(prior.get('content_before', []))
                if any(x.get('content') == 'backup' and str(x.get('vmid')) == str(vmid)
                       and x.get('volid') not in before for x in contents):
                    raise DistributionError('Interrupted export has a registered archive; retain for review.')
                suffix = __import__('hashlib').sha256(prior['upid'].encode()).hexdigest()[:12]
                history = prior_path.with_name(prior_path.stem+'-failed-'+suffix+'.json')
                if history.exists(): raise DistributionError('Interrupted export history already exists; retain for review.')
                proof = recover_interrupted_export_lock(node, vmid, job_id, prior['upid'], config)
                prior.update(recovery=proof)
                journal(prior)
                prior_path.rename(history)
                print('Interrupted export confirmed terminal after host reboot; preserving partial files and retrying the same immutable template.', flush=True)
                return export_image(api,node=node,kind=kind,vmid=vmid,storage=storage,job_id=job_id,
                    journal=journal,hash_volume=hash_volume,approved_backup_storage=approved_backup_storage,
                    identity_record=identity_record)
            if (status.get('status')=='stopped' and str(status.get('exitstatus','')).startswith('403 Permission check failed')
                    and 'VM.Backup' in str(status.get('exitstatus',''))):
                # This explicit permission failure is terminal, not an observation
                # timeout. Retry only if it produced no new archive for this guest.
                contents=api.request('GET',content_path)
                prior_contents=set(prior.get('content_before',[]))
                if any(item.get('content')=='backup' and str(item.get('vmid'))==str(vmid)
                       and item.get('volid') not in prior_contents for item in contents):
                    raise DistributionError('Failed export left an archive; reconcile it before retry.')
                suffix=__import__('hashlib').sha256(prior['upid'].encode()).hexdigest()[:12]
                history=prior_path.with_name(prior_path.stem+'-failed-'+suffix+'.json')
                if history.exists():raise DistributionError('Failed export history already exists; reconcile before retry.')
                prior_path.rename(history)
                print('Prior export permission failure is confirmed terminal with no new archive; retrying after permission repair.',flush=True)
                return export_image(api,node=node,kind=kind,vmid=vmid,storage=storage,job_id=job_id,
                    journal=journal,hash_volume=hash_volume,approved_backup_storage=approved_backup_storage,
                    identity_record=identity_record)
            if kind == 'vm' and status.get('status') == 'stopped' and status.get('exitstatus') == 'job errors':
                proof = _readonly_template_backup_repair(api, node=node, vmid=vmid, job_id=job_id,
                    upid=prior['upid'], status=status, config=config)
                contents = api.request('GET', content_path)
                before = set(prior.get('content_before', []))
                if any(item.get('content') == 'backup' and str(item.get('vmid')) == str(vmid)
                       and item.get('volid') not in before for item in contents):
                    raise DistributionError('Failed template export left an archive; retain it for review.')
                suffix = __import__('hashlib').sha256(prior['upid'].encode()).hexdigest()[:12]
                history = prior_path.with_name(prior_path.stem+'-failed-'+suffix+'.json')
                if history.exists():
                    raise DistributionError('Failed template export history already exists; retain for review.')
                prior['recovery'] = proof
                journal(prior)
                prior_path.rename(history)
                print('Read-only template backup regression repaired; preserving failed task history and retrying the same immutable template.', flush=True)
                return export_image(api,node=node,kind=kind,vmid=vmid,storage=storage,job_id=job_id,
                    journal=journal,hash_volume=hash_volume,approved_backup_storage=approved_backup_storage,
                    identity_record=identity_record)
            if status.get('status') != 'stopped' or status.get('exitstatus') != 'OK':
                raise DistributionError('Prior export task is not confirmed successful; retain it for reconciliation.')
            log_path = f'/nodes/{node}/tasks/{urllib.parse.quote(prior["upid"], safe="")}/log'
            log_rows = []
            for offset in range(0, 10000, 50):
                page = api.request('GET', log_path, {'start': offset, 'limit': 50})
                if not isinstance(page, list):
                    raise DistributionError('Export task log has an invalid response; retain journal for review.')
                log_rows.extend(page)
                if len(page) < 50:
                    break
            else:
                raise DistributionError('Export task log exceeded the fixed recovery limit.')
            lines = [str(row.get('t', '')) for row in log_rows if isinstance(row, dict)]
            archive_paths = set()
            expected_prefix = f'/mnt/pve/{storage}/dump/'
            for line in lines:
                match = re.search(r"creating vzdump archive '([^']+)'", line)
                if match and match.group(1).startswith(expected_prefix):
                    archive_paths.add(match.group(1))
            if len(archive_paths) != 1 or not any(line.strip() == 'TASK OK' for line in lines):
                raise DistributionError('Successful export task log does not identify exactly one completed archive.')
            archive_path = archive_paths.pop()
            filename = archive_path[len(expected_prefix):]
            extension = 'vma' if kind == 'vm' else 'tar'
            if not re.fullmatch(rf'vzdump-{"qemu" if kind == "vm" else "lxc"}-{int(vmid)}-[0-9_-]{{10,24}}\.{extension}\.zst', filename):
                raise DistributionError('Export task archive name does not match the exact source guest.')
            volume_id = f'{storage}:backup/{filename}'
            digest = hash_volume(node, volume_id)
            if not re.fullmatch(r'[a-f0-9]{64}', str(digest)):
                raise DistributionError('Recovered archive transport returned no valid checksum.')
            prior.update(state='exported', volume_id=volume_id, artifact_sha256=digest,
                         verified=True, complete_capture=True,
                         recovered_from_terminal_task=True)
            journal(prior)
            return prior
        raise DistributionError('Existing export journal is not resumable; retain it for reconciliation.')
    before = {item['volid'] for item in api.request('GET', content_path)}
    record = dict(job_id=job_id, node=node, kind=kind, source_id=vmid,
                  storage=storage, state='exporting', disk_gb=disk_gb,
                  content_before=sorted(before),
                  identity_capture=dict(identity_record),
                  memory_mb=int(config.get('memory', 0)), cpu_cores=int(config.get('cores', 0)))
    if kind == 'vm':
        record['system_disk'] = _verified_system_disk(config, vmid)
    if not sealed_capture:
        record['source_identity'] = dict(identity_record['source_identity'])
    if windows_capture:
        record['os_family'] = 'windows'
    else:
        record['init_system'] = identity_record['init_system']
    journal(record)
    upid = api.request('POST', f'/nodes/{node}/vzdump',
        {'vmid': vmid, 'storage': storage, 'mode': 'stop', 'compress': 'zstd',
         'remove': 0, 'stdexcludes': 0, 'notes-template': f'CRANGE export {job_id}'})
    record['upid'] = upid
    journal(dict(record))
    api.task(node, upid, timeout=3600)
    matches = [item['volid'] for item in api.request('GET', content_path)
               if item['volid'] not in before and item.get('content') == 'backup'
               and int(item.get('vmid', -1)) == int(vmid)]
    if len(matches) != 1:
        raise DistributionError('Cannot uniquely identify exported backup; reconcile journal.')
    digest = hash_volume(node, matches[0])
    if not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise DistributionError('Backup transport returned no valid checksum.')
    record.update(state='exported', volume_id=matches[0], artifact_sha256=digest,
                  verified=True, complete_capture=True)
    journal(record)
    return record


def recover_interrupted_export_lock(node, vmid, job_id, upid, config):
    import socket
    slug = job_id.rsplit('-',2)[1]
    request = {'command':'capture-export-unlock','node':node,'vmid':vmid,
               'marker':'crange-capture-'+slug,'upid':upid,
               'config_sha256':_source_config_identity(config,vmid)}
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
        client.settimeout(65);client.connect('/run/crange-ct-read/socket')
        client.sendall(json.dumps(request).encode()+b'\n');raw=b''
        while not raw.endswith(b'\n') and len(raw)<8192:
            part=client.recv(8192-len(raw))
            if not part:break
            raw+=part
    response=json.loads(raw)
    proof=json.loads(response['value']) if set(response)=={'value'} and isinstance(response['value'],str) else {}
    if (proof.get('state')!='unlocked' or proof.get('upid')!=upid or proof.get('vmid')!=vmid or
            proof.get('node')!=node or proof.get('config_sha256')!=request['config_sha256'] or
            proof.get('partial_files_preserved') is not True):
        raise DistributionError('Fixed interrupted export recovery was rejected.')
    return proof


class SSHVolumeHasher:
    """Hash central archives at the library, or node-local volumes on their host."""
    def __init__(self, run=None, library_host='10.60.0.30', key_path=None):
        self.run = run or subprocess.run
        self.library_host = library_host
        self.key_path = key_path or os.path.expanduser('~/.ssh/id_ed25519_range')

    def __call__(self, node, volume_id):
        if node not in {f'proxmox{i}' for i in range(1, 7)} or not re.fullmatch(
                r'[a-zA-Z0-9_-]+:(backup|vztmpl)/[a-zA-Z0-9_.-]+', volume_id):
            raise DistributionError('Invalid artifact transport identity.')
        if volume_id.startswith('crange-library:'):
            kind, filename = volume_id.split(':', 1)[1].split('/', 1)
            directory = 'dump' if kind == 'backup' else 'template/cache'
            path = f'/srv/crange-library/{directory}/{filename}'
            result = self.run(['ssh', '-i', self.key_path, '-o', 'IdentitiesOnly=yes',
                               '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                               'root@' + self.library_host,
                               'sha256sum -- ' + shlex.quote(path)],
                              check=True, capture_output=True, text=True, timeout=3600)
            digest = result.stdout.split()
            if not digest or not re.fullmatch(r'[a-f0-9]{64}', digest[0]):
                raise DistributionError('Library returned no valid artifact checksum.')
            return digest[0]
        command = 'pvesm path ' + shlex.quote(volume_id)
        result = self.run(['ssh', '-o', 'BatchMode=yes', node, command],
                          check=True, capture_output=True, text=True, timeout=60)
        path = result.stdout.strip()
        if not path.startswith('/') or '\n' in path:
            raise DistributionError('Storage returned invalid artifact path.')
        result = self.run(['ssh', '-o', 'BatchMode=yes', node,
                          'sha256sum -- ' + shlex.quote(path)],
                         check=True, capture_output=True, text=True, timeout=3600)
        return result.stdout.split()[0]


def publish_exported_image(catalog, image_id, export, *, architecture,
                           source_revision, approved_projects, datastore_id,
                           verification):
    """Publish a newly exported approved source under a new logical version.

    Source verification must come from existing reviewed template acceptance;
    export checksums do not assert guest readiness or clone identity by themselves.
    """
    from crange_catalog import publish_image
    if (export.get('state') != 'exported' or not export.get('complete_capture') or
            verification.get('passed') is not True or not verification.get('evidence')):
        raise DistributionError('Complete export and source verification evidence required.')
    from crange_catalog import validate_source_identity, validate_sealed_windows_capture
    capture = export.get('identity_capture', {})
    sealed_capture = capture.get('provenance_type') == 'sealed-windows-source-v1'
    windows_capture = sealed_capture or export.get('source_identity', {}).get('os_family') == 'windows'
    if sealed_capture:
        validate_sealed_windows_capture(capture)
        from crange_catalog import SEALED_WINDOWS_IMAGE_ID
        if image_id != SEALED_WINDOWS_IMAGE_ID:
            raise DistributionError('The sealed Project 100 Windows source has one fixed immutable image ID.')
        if normalize_projects(approved_projects) != [100]:
            raise DistributionError('The sealed P100 Windows source can be approved only for Project 100.')
        profile_valid = capture.get('source_identity') is None and 'source_identity' not in export
        expected_verification = sealed_windows_verification(capture)
        if verification != expected_verification:
            raise DistributionError('Sealed Windows publication requires the fixed protected two-clone verification record.')
    else:
        from crange_windows_snapshot import validate_captured_identity
        validate_captured_identity(export.get('source_identity'))
        profile_valid = (
            capture.get('os_family') == 'windows' and 'init_system' not in capture
            and 'init_system' not in export and capture.get('readiness') == 'windows-ready'
            if windows_capture else
            capture.get('init_system') in ('systemd', 'openrc')
            and export.get('init_system') == capture.get('init_system')
            and capture.get('readiness') == ('running' if capture.get('init_system') == 'systemd' else 'default')
        )
    if ((capture.get('state') != 'identity-captured' and not sealed_capture) or
            (not capture.get('job_id') and not sealed_capture)
            or (not sealed_capture and capture.get('source_identity') != export.get('source_identity'))
            or not profile_valid
            or (capture.get('node'), capture.get('kind'), capture.get('source_id')) != (export['node'], export['kind'], export['source_id'])
            or not re.fullmatch(r'[a-f0-9]{64}', capture.get('source_config_sha256', ''))):
        raise DistributionError('Export is missing bound source identity capture provenance.')
    projects = normalize_projects(approved_projects)
    disk_gb = export.get('disk_gb')
    if not isinstance(disk_gb, (int, float)) or isinstance(disk_gb, bool) or not math.isfinite(disk_gb) or disk_gb <= 0:
        raise DistributionError('Published image requires a defensible configured disk size.')
    definition = {
        'kind': export['kind'], 'architecture': architecture, 'disk_gb': disk_gb,
        'memory_mb': export.get('memory_mb'), 'cpu_cores': export.get('cpu_cores'),
        'identity_capture': dict(capture),
        'artifact_sha256': export['artifact_sha256'],
        'artifact': {'volume_id': export['volume_id'], 'node': export['node'],
                     'format': 'vzdump', 'complete_capture': True, 'source_id': export['source_id']},
        'source_revision': source_revision, 'verified': True,
        'approved_projects': projects, 'verification': verification,
        'replicas': [{'node': export['node'], 'template_id': int(export['source_id']),
                      'datastore_id': datastore_id, 'state': 'ready',
                      'artifact_sha256': export['artifact_sha256'],
                      'verified_at': datetime.now(timezone.utc).isoformat(),
                      'verification': verification}]}
    if export.get('kind') == 'vm':
        system_disk = export.get('system_disk')
        if (not isinstance(system_disk, dict) or system_disk.get('source_config_sha256') != capture.get('source_config_sha256')
                or not re.fullmatch(r'(?:scsi|sata|virtio|ide)[0-9]+', str(system_disk.get('interface', '')))
                or not isinstance(system_disk.get('disk_gb'), (int, float)) or isinstance(system_disk.get('disk_gb'), bool)
                or not math.isfinite(system_disk['disk_gb']) or system_disk['disk_gb'] <= 0
                or system_disk['disk_gb'] > disk_gb):
            raise DistributionError('VM publication requires a boot-selected system disk bound to the captured source config.')
        definition['system_disk'] = dict(system_disk)
    if windows_capture:
        definition['os_family'] = 'windows'
        if sealed_capture:
            definition['display_name'] = 'Windows Server 2025 Desktop Experience'
            definition['windows_profile'] = 'mhl-server2025-desktop-experience'
    else:
        definition['init_system'] = capture['init_system']
    if not sealed_capture:
        definition['source_identity'] = dict(export['source_identity'])
    return publish_image(catalog, image_id, definition)


def sealed_windows_verification(capture):
    """Derive publication evidence from the protected, validated clone proof."""
    from crange_catalog import validate_sealed_windows_capture
    validate_sealed_windows_capture(capture)
    return {'passed': True, 'evidence': {
        'type': 'sealed-windows-independent-clone-proof',
        'protected_bundle_sha256': capture['protected_bundle_sha256'],
        'probe_vmids': [probe['vmid'] for probe in capture['probes']],
        'machine_guids': [probe['identity']['machine_guid'] for probe in capture['probes']],
        'machine_sids': [probe['identity']['machine_sid'] for probe in capture['probes']],
        'readiness': 'unattended-windows-ready',
        'profile': 'Windows Server 2025 Desktop Experience'}}


def disconnected_container_nic(value,index,bridge):
    """Keep a captured bind address without a gateway or physical connectivity."""
    import ipaddress
    fields=dict(part.split('=',1) for part in str(value).split(',') if '=' in part)
    address=fields.get('ip','manual')
    if address not in ('manual','dhcp'):
        try:
            parsed=ipaddress.IPv4Interface(address)
        except ValueError:
            raise DistributionError('Captured container address is invalid.') from None
        if not any(parsed.ip in ipaddress.IPv4Network(net) for net in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')):
            raise DistributionError('Captured container address must be private.')
        address=str(parsed)
    else:address='manual'
    return f'name=eth{index},bridge={bridge},ip={address},ip6=manual,link_down=1,type=veth'


class ProbeVerifier:
    """Fixed guest checks on a disconnected administrator-approved bridge.

    Source identities must be collected during reviewed capture, not invented.
    Windows guests use QEMU Agent-collected MachineGuid and local SID values;
    Linux guests use machine-id and Ed25519 host keys. CT exec remains fixed.
    """
    def __init__(self, isolated_bridge, approved_bridges, source_identity,
                 *, ct_exec=None, timeout=None, init_system="systemd"):
        if isolated_bridge not in approved_bridges or not re.fullmatch(r'[a-zA-Z0-9_-]{1,15}', isolated_bridge):
            raise DistributionError('Probe bridge is not approved.')
        sealed_windows = (isinstance(source_identity, dict) and
                          source_identity.get('provenance_type') == 'sealed-windows-source-v1')
        windows_source = sealed_windows or source_identity.get('os_family') == 'windows'
        if windows_source:
            if sealed_windows:
                from crange_catalog import validate_sealed_windows_capture
                validate_sealed_windows_capture(source_identity)
            else:
                from crange_catalog import validate_source_identity
                from crange_windows_snapshot import validate_captured_identity
                validate_captured_identity(source_identity)
            if init_system != 'windows':
                raise DistributionError('Windows source requires the Windows guest profile.')
        elif not source_identity.get('machine_id') or not source_identity.get('ssh_host_key'):
            raise DistributionError('Captured source identity evidence is required.')
        elif init_system not in ('systemd', 'openrc'):
            raise DistributionError('Unsupported reviewed guest readiness profile.')
        self.init_system = init_system
        self.bridge, self.source = isolated_bridge, source_identity
        self.sealed_windows = sealed_windows
        # Windows restore boots can include filesystem/AD recovery before QGA starts.
        # Keep the wait bounded without shortening those boots to Linux's limit.
        self.ct_exec = ct_exec
        self.timeout = (600 if init_system == 'windows' else 180) if timeout is None else timeout

    def __call__(self, api, node, kind, vmid):
        import time
        _require_probe_pool(api, node, kind, vmid)
        config = api.request('GET', f'/nodes/{node}/{kind}/{vmid}/config')
        _owned(config)
        networks = [key for key in config if re.fullmatch(r'net[0-9]+', key)]
        fields = {}
        for key in networks:
            if kind == 'qemu':
                model_mac = str(config[key]).split(',')[0]
                fields[key] = f'{model_mac},bridge={self.bridge},link_down=1'
            else:
                fields[key] = disconnected_container_nic(config[key],key[3:],self.bridge)
        fields['onboot'] = 0
        if kind == 'qemu':
            # Restored immutable archives may pin a newer source-node QEMU.
            # Normalize only the isolated disposable probe, never its parent.
            model = str(config.get('machine', '')).split(',')[0].removeprefix('type=')
            version = re.fullmatch(r'pc-(?:q35|i440fx)-([0-9]+)\.([0-9]+)(?:\+pve[0-9]+)?', model)
            if version and tuple(map(int, version.groups())) > (9, 2):
                from crange_capture import _portable_capture_machine
                fields['machine'] = _portable_capture_machine(api, node, config)
        if kind=='qemu' and self.source.get('identity_mode')=='windows-scenario-snapshot-v1':
            from crange_capture import private_bridge_request
            markers=[tag for tag in str(config.get('tags','')).split(';') if tag.startswith('crange-job-')]
            if len(markers)==1:
                private_bridge_request(node,vmid=vmid,marker=markers[0])
            else:
                record=getattr(api,'record',None)
                if (not isinstance(record,dict) or record.get('probe_id')!=vmid or
                        record.get('node')!=node or not cache_owned(config,vmid,record.get('job_id'))):
                    raise DistributionError('Windows snapshot probe requires its exact journal-bound ownership marker.')
                private_bridge_request(node,vmid=vmid,marker=f'crange-cache-{vmid}',cache_job_id=record['job_id'])
            fields={key:value.replace('link_down=1','link_down=0') if re.fullmatch(r'net[0-9]+',key) else value
                    for key,value in fields.items()}
        api.request('PUT', f'/nodes/{node}/{kind}/{vmid}/config', fields)
        start_requested = False
        try:
            upid = api.request('POST', f'/nodes/{node}/{kind}/{vmid}/status/start', {})
            start_requested = True
            api.task(node, upid, timeout=self.timeout)
            return self._check_guest(api, node, kind, vmid)
        finally:
            # A timeout or failed guest-agent check must still attempt to stop
            # the disposable guest. Its caller retains the reservation if this
            # provider task cannot be confirmed terminal.
            if start_requested:
                upid = api.request('POST', f'/nodes/{node}/{kind}/{vmid}/status/stop', {})
                api.task(node, upid, timeout=self.timeout)

    def _check_guest(self, api, node, kind, vmid):
        import time
        def read(command):
            if kind == 'lxc':
                if self.ct_exec is None:
                    raise DistributionError('Fixed CT exec adapter is unavailable.')
                return self.ct_exec(node, vmid, command).strip()
            endpoint = f'/nodes/{node}/qemu/{vmid}/agent/exec'
            started = time.time()
            try:
                response = api.request('POST', endpoint, {'command': command})
            except Exception as exc:
                if hasattr(api, 'record_evidence'):
                    api.record_evidence({'type': 'qga-exec-error', 'path': endpoint,
                        'command': command, 'error_type': type(exc).__name__,
                        'error': str(exc)[:2048], 'observed_at': datetime.now(timezone.utc).isoformat()})
                if getattr(exc, 'code', None) == 403 or 'HTTP 403' in str(exc):
                    raise GuestAgentPermissionError('QEMU Guest Agent explicitly denied the fixed identity check (HTTP 403).') from exc
                raise
            if hasattr(api, 'record_evidence'):
                api.record_evidence({'type': 'qga-exec', 'path': endpoint,
                    'command': command, 'response': response,
                    'observed_at': datetime.now(timezone.utc).isoformat()})
            pid = int(response['pid'])
            deadline = time.monotonic() + self.timeout
            last_checkpoint = 0.0
            while time.monotonic() < deadline:
                status_path = f'/nodes/{node}/qemu/{vmid}/agent/exec-status'
                try:
                    result = api.request('GET', status_path, {'pid': pid})
                except Exception as exc:
                    if hasattr(api, 'record_evidence'):
                        api.record_evidence({'type': 'qga-status-error', 'path': status_path,
                            'pid': pid, 'error_type': type(exc).__name__, 'error': str(exc)[:2048],
                            'elapsed_seconds': round(time.time() - started, 2),
                            'observed_at': datetime.now(timezone.utc).isoformat()})
                    if getattr(exc, 'code', None) == 403 or 'HTTP 403' in str(exc):
                        raise GuestAgentPermissionError('QEMU Guest Agent explicitly denied the fixed identity check (HTTP 403).') from exc
                    raise
                if (hasattr(api, 'record_evidence') and
                        (result.get('exited') or time.monotonic() - last_checkpoint >= 15)):
                    api.record_evidence({'type': 'qga-status', 'path': status_path,
                        'pid': pid, 'response': result,
                        'elapsed_seconds': round(time.time() - started, 2),
                        'observed_at': datetime.now(timezone.utc).isoformat()})
                    last_checkpoint = time.monotonic()
                if result.get('exited'):
                    if int(result.get('exitcode', -1)) != 0:
                        error = DistributionError('Probe identity/readiness command failed.')
                        if (command == ['/usr/bin/systemctl', 'is-system-running'] and
                                str(result.get('out-data', '')).strip() == 'degraded'):
                            error.collector_state = 'degraded'
                        raise error
                    return str(result.get('out-data', '')).strip()
                time.sleep(1)
            if hasattr(api, 'record_evidence'):
                api.record_evidence({'type': 'qga-timeout', 'path': status_path,
                    'pid': pid, 'timeout_seconds': self.timeout,
                    'elapsed_seconds': round(time.time() - started, 2),
                    'observed_at': datetime.now(timezone.utc).isoformat()})
            raise DistributionError('Probe guest-agent check timed out.')
        if self.init_system == 'windows':
            deadline = time.monotonic() + self.timeout
            identity = None
            while time.monotonic() < deadline:
                try:
                    if self.source.get('identity_mode')=='windows-scenario-snapshot-v1':
                        from crange_windows_snapshot import collect
                        live=api.request('GET',f'/nodes/{node}/{kind}/{vmid}/config')
                        identity=collect(read,live.get('vmgenid'),self.source['snapshot_project_id'])
                    else: identity = _windows_guest_identity(read)
                    break
                except GuestAgentPermissionError:
                    raise
                except Exception:
                    # QEMU Agent availability may precede Windows service readiness.
                    pass
                time.sleep(2)
            if identity is None:
                raise DistributionError('Disposable Windows clone did not become ready.')
            if self.source.get('identity_mode')=='windows-scenario-snapshot-v1':
                from crange_windows_snapshot import verify_clone
                result=verify_clone(self.source,identity)
                return {'passed':result['passed'],'identity_unique':result['instance_generation_unique'],
                        'evidence':{'type':'windows-scenario-restore','identity':identity,'isolated_bridge':self.bridge,
                                    'service_identity_preserved':True}}
            if self.sealed_windows:
                unique = all(identity[field] not in {probe['identity'][field] for probe in self.source['probes']}
                             for field in ('machine_guid', 'machine_sid', 'local_administrator_sid'))
                reference = self.source['probes'][0]['identity']
            else:
                unique = (identity['machine_guid'] != self.source['machine_guid'] and
                          identity['machine_sid'] != self.source['machine_sid'] and
                          identity['local_administrator_sid'] != self.source['local_administrator_sid'])
                reference = self.source
            unique = (unique and identity['os_caption'] == reference['os_caption'] and
                      identity['os_build'] == reference['os_build'])
            return {'passed': unique, 'identity_unique': unique,
                    'evidence': {'readiness': 'windows-ready', 'identity': identity,
                                 'isolated_bridge': self.bridge}}
        deadline = time.monotonic() + self.timeout
        readiness = ''
        offline_preparation = None
        while time.monotonic() < deadline:
            try:
                readiness = _guest_readiness(read, self.init_system)
                if readiness == ('running' if self.init_system == 'systemd' else 'default'): break
            except Exception as error:
                # Guest-agent initialization may lag behind host task completion.
                if getattr(error, 'collector_state', None) == 'degraded':
                    offline_preparation = normalize_isolated_network_wait(api, node, kind, vmid, self.bridge,
                        **({'ct_exec':self.ct_exec} if kind=='lxc' else {}))
            time.sleep(2)
        if readiness != ('running' if self.init_system == 'systemd' else 'default'):
            raise DistributionError('Disposable clone did not become ready.')
        machine_id = read(['/bin/cat', '/etc/machine-id'])
        ssh_host_key = read(['/bin/cat', '/etc/ssh/ssh_host_ed25519_key.pub'])
        valid_id = bool(re.fullmatch(r'[a-f0-9]{32}', machine_id))
        key_identity = lambda value: ' '.join(value.split()[:2])
        unique = (valid_id and machine_id != self.source['machine_id'] and
                  key_identity(ssh_host_key) != key_identity(self.source['ssh_host_key']) and
                  ssh_host_key.startswith('ssh-ed25519 '))
        evidence = {'readiness': readiness, 'machine_id': machine_id,
                    'ssh_host_key': ssh_host_key, 'isolated_bridge': self.bridge}
        if offline_preparation:
            evidence['offline_network_readiness'] = offline_preparation
        return {'passed': unique, 'identity_unique': unique, 'evidence': evidence}


def owned_container_marker(config):
    tags=str(config.get('tags','')).split(';')
    jobs=[t for t in tags if re.fullmatch(r'crange-job-[a-zA-Z0-9-]{1,64}',t)]
    captures=[t for t in tags if re.fullmatch(r'crange-capture-[a-f0-9]{12}',t)]
    compact=[t for t in tags if re.fullmatch(r'crange-cache-[0-9]+',t)]
    if not jobs and not captures and len(compact)==1:
        return compact[0]
    if len(jobs)>1 or len(captures)>1 or not (jobs or captures):
        raise DistributionError('Owned container lacks one current worker marker.')
    return jobs[0] if jobs else captures[0]


class UnixContainerBackingVerifier:
    def __init__(self, api=None): self.api=api
    def __call__(self, node, source_id, probe_id):
        import socket
        request = {'node': node, 'vmid': probe_id, 'source_id': source_id, 'command': 'linked-backing'}
        if self.api is not None:
            markers=[]
            for vmid in (probe_id,source_id):
                config=self.api.request('GET',f'/nodes/{node}/lxc/{vmid}/config')
                markers.append(owned_container_marker(config))
            request.update(command='owned-linked-backing',marker=markers[0],source_marker=markers[1])
            if markers[0].startswith('crange-cache-'):
                probe=self.api.request('GET',f'/nodes/{node}/lxc/{probe_id}/config')
                description=str(probe.get('description','')).rstrip('\r\n')
                match=re.fullmatch(r'CRANGE verification (cache-[a-f0-9]{32})',description)
                if not match:raise DistributionError('Compact probe lacks its exact hidden job identity.')
                request['cache_job_id']=match[1]
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(65); client.connect('/run/crange-ct-read/socket')
            client.sendall(json.dumps(request).encode() + b'\n')
            data = b''
            while not data.endswith(b'\n') and len(data) <= 8192:
                chunk = client.recv(8193 - len(data))
                if not chunk: break
                data += chunk
        if len(data) > 8192 or not data.endswith(b'\n'):
            raise DistributionError('Fixed backing relay response is invalid.')
        response = json.loads(data)
        if set(response) != {'value'} or not isinstance(response['value'], str):
            raise DistributionError('Fixed backing relay denied the inspection.')
        return json.loads(response['value'])


class SSHContainerExec:
    """Only the verifier's three fixed readonly commands are executable."""
    def __init__(self, run=None, api=None): self.run, self.api = run or subprocess.run, api

    def __call__(self, node, vmid, command):
        allowed = [['/usr/bin/systemctl', 'is-system-running'], ['/bin/rc-status', '--runlevel'],
                   ['/usr/bin/systemctl', '--failed', '--no-pager', '--no-legend'],
                   ['/usr/bin/systemctl', 'status', 'postgresql@15-main.service', '--no-pager', '--lines=15'],
                   ['/sbin/rc-status', '--runlevel'],
                   ['/bin/cat', '/etc/machine-id'],
                   ['/bin/cat', '/etc/ssh/ssh_host_ed25519_key.pub']]
        if command not in allowed or node not in {f'proxmox{i}' for i in range(1, 7)} or int(vmid) < 100:
            raise DistributionError('Unsupported CT verification command.')
        relay = Path('/run/crange-ct-read/socket')
        if relay.exists():
            from crange_ct_read_relay import COMMANDS
            read = next(key for key, value in COMMANDS.items() if value == command)
            return self._relay(node,vmid,'owned-image-read',read=read)
        result = self.run(['ssh', '-o', 'BatchMode=yes', node,
                          shlex.join(['pct', 'exec', str(int(vmid)), '--'] + command)],
                         check=True, capture_output=True, text=True, timeout=60)
        return result.stdout

    def _relay(self,node,vmid,operation,*,read=None):
        import socket
        request={'node':node,'vmid':int(vmid),'command':read}
        if self.api is not None:
            config=self.api.request('GET',f'/nodes/{node}/lxc/{int(vmid)}/config')
            request.update(command=operation,marker=owned_container_marker(config))
            if read is not None:request['read']=read
            if request['marker'].startswith('crange-cache-'):
                match=re.fullmatch(r'CRANGE verification (cache-[a-f0-9]{32})',str(config.get('description','')).rstrip('\r\n'))
                if not match:raise DistributionError('Compact probe lacks its exact hidden job identity.')
                request['cache_job_id']=match[1]
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(65);client.connect(str(Path('/run/crange-ct-read/socket')))
            client.sendall(json.dumps(request).encode()+b'\n');data=b''
            while not data.endswith(b'\n') and len(data)<=8192:
                chunk=client.recv(8193-len(data))
                if not chunk:break
                data+=chunk
        if len(data)>8192 or not data.endswith(b'\n'):
            raise DistributionError('Fixed CT relay response is invalid.')
        response=json.loads(data)
        if set(response)!={'value'} or not isinstance(response['value'],str):
            raise DistributionError('Fixed CT relay denied the owned image request.')
        return response['value']

    def reset_network_wait(self,node,vmid):
        if self.api is None:
            raise DistributionError('Network-wait reset requires verified image ownership.')
        result=json.loads(self._relay(node,vmid,'owned-image-network-wait-reset'))
        if (not isinstance(result,dict) or type(result.get('reset')) is not bool or
                (result['reset'] and (result.get('unit')!='systemd-networkd-wait-online.service'
                                      or result.get('services_disabled') is not False))):
            raise DistributionError('Invalid offline network-wait reset acknowledgement.')
        return result

    def prepare_capture_identity(self,node,vmid):
        result=json.loads(self._relay(node,vmid,'capture-container-identity-prepare'))
        if not isinstance(result,dict) or result.get('prepared') is not True:
            raise DistributionError('Capture identity startup preparation failed.')
        return result


def generalize_capture_container(node, vmid, marker):
    """Ask the authenticated root relay to clear identity only in a capture clone."""
    import socket
    value = {'node': node, 'vmid': vmid, 'command': 'capture-container-generalize', 'marker': marker}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(65); client.connect('/run/crange-ct-read/socket')
        client.sendall(json.dumps(value).encode()+b'\n')
        data=b''
        while not data.endswith(b'\n') and len(data)<=8192:
            chunk=client.recv(8193-len(data))
            if not chunk: break
            data+=chunk
    if len(data)>8192 or not data.endswith(b'\n'):
        raise DistributionError('Container generalization relay response is invalid.')
    result=json.loads(data)
    if set(result)!={'value'} or not isinstance(result['value'],str):
        raise DistributionError('Container generalization relay refused the capture scope.')
    return json.loads(result['value'])


def catalog_transaction(path, update):
    """Serialize publication and atomically replace a restrictive catalog file."""
    path = Path(path)
    with path.with_suffix(path.suffix + '.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        catalog = json.loads(path.read_text()) if path.exists() else {'schema_version': 1, 'images': {}}
        try:
            previous_stat = path.stat()
        except FileNotFoundError:
            previous_stat = None
        result = update(catalog)
        validate_catalog(result)
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(result, output, indent=2, sort_keys=True)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            if previous_stat is None:
                os.chmod(temporary, 0o600)
            else:
                os.chown(temporary, previous_stat.st_uid, previous_stat.st_gid)
                os.chmod(temporary, stat.S_IMODE(previous_stat.st_mode))
            os.replace(temporary, path)
        finally: Path(temporary).unlink(missing_ok=True)
        return result


def main(argv=None):
    """Administrator CLI; GUI workers call only fixed reviewed argument sets."""
    import argparse
    from crange import ProxmoxAPI
    from crange_scheduling import discover, node_locks, DEFAULT_POLICY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--journal-dir', type=Path, required=True)
    parser.add_argument('--policy', type=Path, required=True,
                        help='Administrator policy including approved_storage/backup_storage/tested_paths')
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('inventory')
    commands.add_parser('capture-sealed-windows-p100',
                        help='Read fixed root-private P100 evidence and revalidate the stopped source/probes')
    commands.add_parser('template-sealed-windows-p100',
                        help='Convert only the fixed, journaled P100 sealed Windows source to a template')
    capture = commands.add_parser('identity-capture')
    capture.add_argument('--node', required=True)
    capture.add_argument('--kind', choices=['vm', 'container'], required=True)
    capture.add_argument('--source-id', type=int, required=True)
    capture.add_argument('--job-id', required=True)
    capture.add_argument('--init-system', choices=['systemd', 'openrc', 'windows'], default='systemd')
    export = commands.add_parser('export')
    export.add_argument('--node', required=True)
    export.add_argument('--kind', choices=['vm', 'container'], required=True)
    export.add_argument('--source-id', type=int, required=True)
    export.add_argument('--storage', required=True)
    export.add_argument('--job-id', required=True)
    export.add_argument('--identity-record', type=Path, required=True)
    publish = commands.add_parser('publish')
    publish.add_argument('--export-record', type=Path, required=True)
    publish.add_argument('--verification', type=Path,
                         help='Normal source verification record; sealed P100 derives this from protected proof.')
    publish.add_argument('--image-id', required=True)
    publish.add_argument('--architecture', default='amd64')
    publish.add_argument('--source-revision', required=True)
    _add_project_approval_arguments(publish)
    publish.add_argument('--datastore-id', required=True)
    stock = commands.add_parser('publish-stock')
    for field in ('image-id', 'node', 'template-file-id', 'source-revision', 'sha256'):
        stock.add_argument('--' + field, required=True)
    stock.add_argument('--architecture', default='amd64')
    _add_project_approval_arguments(stock)
    stock.add_argument('--disk-gb', type=float, required=True)
    stock.add_argument('--verification', type=Path, required=True)
    replica = commands.add_parser('replica')
    for field in ('image-id', 'source-node', 'node', 'datastore-id', 'job-id', 'isolated-bridge'):
        replica.add_argument('--' + field, required=True)
    replica.add_argument('--project', type=int, required=True,
                         help='Project whose quota and image approval fund this verification job.')
    replica.add_argument('--source-id', type=int, required=True)
    replica.add_argument('--source-identity', type=Path, required=True)
    replica.add_argument('--archive', type=Path)
    source_probe = commands.add_parser('source-probe')
    for field in ('source-node', 'datastore-id', 'job-id', 'isolated-bridge'):
        source_probe.add_argument('--' + field, required=True)
    source_probe.add_argument('--project', type=int, required=True)
    source_probe.add_argument('--source-id', type=int, required=True)
    source_probe.add_argument('--identity-record', type=Path, required=True)
    source_probe.add_argument('--clone-mode', choices=['full', 'linked'], default='full')
    source_probe.add_argument('--auto-storage', action='store_true', help='Prefer the selected datastore; allow another approved local full-VM probe destination')
    reconcile_probe = commands.add_parser('source-probe-reconcile')
    reconcile_probe.add_argument('--job-id', required=True)
    resume_probe = commands.add_parser('source-probe-resume')
    for field in ('source-node', 'job-id', 'isolated-bridge'):
        resume_probe.add_argument('--' + field, required=True)
    resume_probe.add_argument('--project', type=int, required=True)
    resume_probe.add_argument('--source-id', type=int, required=True)
    resume_probe.add_argument('--identity-record', type=Path, required=True)
    args = parser.parse_args(argv)
    policy = json.loads(args.policy.read_text())
    api = ProxmoxAPI()
    journal = FileJournal(args.journal_dir)
    if args.action == 'inventory':
        print(json.dumps(discover(api, policy.get('placement', DEFAULT_POLICY)), indent=2))
    elif args.action == 'capture-sealed-windows-p100':
        from crange_capture import capture_sealed_windows_source
        with node_locks(['proxmox4'], library=True), journal.allocation_lock():
            record = capture_sealed_windows_source(api, journal)
        print(json.dumps(record, indent=2))
    elif args.action == 'template-sealed-windows-p100':
        from crange_capture import convert_sealed_windows_p100
        with node_locks(['proxmox4'], library=True), journal.allocation_lock():
            record = convert_sealed_windows_p100(api, journal)
        print(json.dumps(record, indent=2))
    elif args.action == 'identity-capture':
        with node_locks([args.node]), journal.allocation_lock():
            record = capture_source_identity(api, node=args.node, kind=args.kind, vmid=args.source_id,
                job_id=args.job_id, journal=journal, ct_exec=SSHContainerExec(api=api), init_system=args.init_system)
        print(json.dumps(record, indent=2))
    elif args.action == 'export':
        with node_locks([args.node], library=True), journal.allocation_lock():
            record = export_image(api, node=args.node, kind=args.kind,
                vmid=args.source_id, storage=args.storage, job_id=args.job_id,
                journal=journal, hash_volume=SSHVolumeHasher(),
                approved_backup_storage=map(tuple, policy.get('approved_backup_storage', [])),
                identity_record=json.loads(args.identity_record.read_text()))
        print(json.dumps(record, indent=2))
    elif args.action == 'publish':
        export_record = json.loads(args.export_record.read_text())
        capture = export_record.get('identity_capture', {})
        if capture.get('provenance_type') == 'sealed-windows-source-v1':
            if args.verification is not None:
                raise DistributionError('Sealed P100 verification is derived from protected clone proof, not a supplied file.')
            verification = sealed_windows_verification(capture)
        elif args.verification is not None:
            verification = json.loads(args.verification.read_text())
        else:
            raise DistributionError('Normal publication requires a reviewed verification record.')
        catalog_transaction(args.catalog, lambda catalog: publish_exported_image(
            catalog, args.image_id, export_record, architecture=args.architecture,
            source_revision=args.source_revision, approved_projects=_project_approval_scope(args),
            datastore_id=args.datastore_id, verification=verification))
        print('Published immutable image ' + args.image_id)
    elif args.action == 'publish-stock':
        verification = json.loads(args.verification.read_text())
        with node_locks([args.node], library=True):
            catalog_transaction(args.catalog, lambda catalog: publish_stock_archive(
                catalog, args.image_id, node=args.node, template_file_id=args.template_file_id,
                architecture=args.architecture, source_revision=args.source_revision,
                approved_projects=_project_approval_scope(args), disk_gb=args.disk_gb, expected_sha256=args.sha256,
                hash_volume=SSHVolumeHasher(), verification=verification))
        print('Published immutable stock archive ' + args.image_id)
    elif args.action == 'source-probe-reconcile':
        from crange_placement import ReservationLedger
        import crange_scheduling as scheduling
        if not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', args.job_id):
            raise DistributionError('Invalid reconciliation job ID.')
        record = json.loads((args.journal_dir / (args.job_id + '.json')).read_text())
        ledger = ReservationLedger(scheduling.DATA / 'reservations.sqlite3')
        try:
            with node_locks([record['node']]), journal.allocation_lock():
                result = reconcile_source_probe(api, journal, ledger, args.job_id)
            print('Reconciled absent source probe ' + args.job_id)
        finally:
            ledger.close()
    elif args.action == 'source-probe':
        if not policy.get('enabled'):
            raise DistributionError('Image distribution is disabled by administrator policy.')
        if args.source_node not in policy.get('allowed_nodes', []):
            raise DistributionError('Source-probe node is not enabled by administrator policy.')
        if (args.source_node, args.datastore_id) not in set(map(tuple, policy.get('approved_storage', []))):
            raise DistributionError('Source-probe datastore is not approved on the selected node.')
        if args.isolated_bridge not in policy.get('isolated_bridges', []):
            raise DistributionError('Probe bridge is not approved by administrator policy.')
        from crange_placement import PlacementError, ReservationLedger
        import crange_scheduling as scheduling
        capture = json.loads(args.identity_record.read_text())
        if capture.get('provenance_type') == 'sealed-windows-source-v1':
            _require_worker_sealed_record(capture, args.identity_record)
        else:
            from crange_catalog import validate_source_identity
            from crange_windows_snapshot import validate_captured_identity
            validate_captured_identity(capture.get('source_identity'))
        kind = _source_guest_type(capture)
        profile = _source_capture_profile(capture)
        valid_states = ('sealed-source-captured', 'identity-captured') if capture.get('provenance_type') == 'sealed-windows-source-v1' else ('identity-captured',)
        if ((capture.get('state') not in valid_states or
                (capture.get('node'), capture.get('kind'), capture.get('source_id')) !=
                (args.source_node, 'vm' if kind == 'qemu' else 'container', args.source_id)) or
                not capture.get('job_id')):
            raise DistributionError('Identity record is not bound to the selected captured source.')
        verify = ProbeVerifier(args.isolated_bridge, policy.get('isolated_bridges', []),
            capture if profile == 'windows' and capture.get('provenance_type') == 'sealed-windows-source-v1'
            else capture['source_identity'], ct_exec=SSHContainerExec(api=api), init_system=profile)
        ledger = ReservationLedger(scheduling.DATA / 'reservations.sqlite3')
        source_ref = f'source:{args.source_node}:{kind}:{args.source_id}'
        source_sha = capture['source_config_sha256']
        try:
            with node_locks([args.source_node]), journal.allocation_lock():
                _require_probe_pool(api, args.source_node, kind, args.source_id)
                source = api.request('GET', f'/nodes/{args.source_node}/{kind}/{args.source_id}/config')
                _owned(source)
                if (int(source.get('template', 0)) != 1 or
                        api.request('GET', f'/nodes/{args.source_node}/{kind}/{args.source_id}/status/current').get('status') != 'stopped' or
                        _source_config_identity(source, args.source_id) != source_sha):
                    raise DistributionError('Captured source template identity changed or is not stopped.')
                capacity = _source_capacity(source, kind)
                inventory = discover(api, policy)
                try:
                    allocation = reserve_probe_storage(ledger, auto_storage=args.auto_storage, project_id=args.project,
                        job_id=args.job_id, owner='cli', source_ref=source_ref,
                        source_sha256=source_sha, node_name=args.source_node,
                        datastore_id=args.datastore_id, capacity=capacity,
                        inventory=inventory, policy=policy, kind=capture['kind'], clone_mode=args.clone_mode)
                    ledger.mark_running(args.job_id)
                except PlacementError as exc:
                    raise DistributionError('Shared-ledger source-probe reservation failed: ' + str(exc)) from exc
                validator = shared_reservation_validator(ledger, args.job_id,
                    allocation, source_ref, source_sha)
                if args.auto_storage:
                    print(f"Full-copy probe storage: {args.source_node}/{allocation['resolved']['datastore_id']}",flush=True)
                worker = SourceProbeWorker(api, journal, verify, validator, backing_verifier=UnixContainerBackingVerifier(api=api))
                record = worker.run(source_node=args.source_node, source_id=args.source_id,
                    datastore_id=allocation['resolved']['datastore_id'], job_id=args.job_id,
                    capture=capture, reservation=allocation)
                inventory = discover(api, policy)
                if (record.get('state') != 'source-probed' or
                        str(record.get('probe_id')) in inventory.get('guests', {}) or
                        any(task.get('state') != 'confirmed-terminal'
                            for task in record.get('provider_tasks', []))):
                    raise DistributionError('Source probe or provider task remains uncertain; reservation retained.')
                ledger.reconcile_job(args.job_id, operation_terminal=True,
                                     allocations_reconciled=True)
            print('Verified and cleaned isolated source probe ' + args.job_id)
        finally:
            ledger.close()
    elif args.action == 'source-probe-resume':
        if not policy.get('enabled'):
            raise DistributionError('Image distribution is disabled by administrator policy.')
        if args.source_node not in policy.get('allowed_nodes', []):
            raise DistributionError('Source-probe node is not enabled by administrator policy.')
        if args.isolated_bridge not in policy.get('isolated_bridges', []):
            raise DistributionError('Probe bridge is not approved by administrator policy.')
        from crange_placement import PlacementError, ReservationLedger
        import crange_scheduling as scheduling
        capture = json.loads(args.identity_record.read_text())
        if capture.get('provenance_type') == 'sealed-windows-source-v1':
            _require_worker_sealed_record(capture, args.identity_record)
        else:
            from crange_catalog import validate_source_identity
            from crange_windows_snapshot import validate_captured_identity
            validate_captured_identity(capture.get('source_identity'))
        kind = _source_guest_type(capture)
        profile = _source_capture_profile(capture)
        valid_states = ('sealed-source-captured', 'identity-captured') if capture.get('provenance_type') == 'sealed-windows-source-v1' else ('identity-captured',)
        if ((capture.get('state') not in valid_states or
                (capture.get('node'), capture.get('kind'), capture.get('source_id')) !=
                (args.source_node, 'vm' if kind == 'qemu' else 'container', args.source_id)) or
                not capture.get('job_id')):
            raise DistributionError('Identity record is not bound to the selected captured source.')
        verify = ProbeVerifier(args.isolated_bridge, policy.get('isolated_bridges', []),
            capture if profile == 'windows' and capture.get('provenance_type') == 'sealed-windows-source-v1'
            else capture['source_identity'], ct_exec=SSHContainerExec(api=api), init_system=profile)
        ledger = ReservationLedger(scheduling.DATA / 'reservations.sqlite3')
        source_ref = f'source:{args.source_node}:{kind}:{args.source_id}'
        source_sha = capture['source_config_sha256']
        try:
            with node_locks([args.source_node]), journal.allocation_lock():
                try:
                    job = ledger.status(args.job_id)
                    allocations = ledger.validate_job(args.job_id)
                except PlacementError as exc:
                    raise DistributionError('No active source-probe reservation is available to reconcile.') from exc
                if (job['state'] != 'running' or job['project_id'] != args.project or
                        job['owner'] != 'cli' or len(allocations) != 1):
                    raise DistributionError('Existing source-probe reservation scope is not exact.')
                allocation = allocations[0]
                if (allocation.get('operation') != 'source_probe' or
                        allocation.get('image') != source_ref or
                        allocation.get('artifact_sha256') != source_sha or
                        allocation.get('resolved', {}).get('node_name') != args.source_node):
                    raise DistributionError('Existing source-probe reservation does not match capture.')
                record_path = Path(args.journal_dir) / (args.job_id + '.json')
                try:
                    record = json.loads(record_path.read_text())
                except (OSError, ValueError) as exc:
                    raise DistributionError('Source-probe journal is missing or unreadable.') from exc
                datastore_id = record.get('datastore_id')
                if allocation.get('resolved', {}).get('datastore_id') != datastore_id:
                    raise DistributionError('Source-probe journal storage differs from reservation.')
                validator = shared_reservation_validator(ledger, args.job_id,
                    allocation, source_ref, source_sha)
                api_source = api.request('GET', f'/nodes/{args.source_node}/{kind}/{args.source_id}/config')
                _owned(api_source)
                if _source_config_identity(api_source, args.source_id) != source_sha:
                    raise DistributionError('Captured source template identity changed.')
                probe_id = int(record.get('probe_id', 0))
                task_upid = find_source_clone_task(api, args.source_node, args.source_id, probe_id, kind)
                worker = SourceProbeWorker(api, journal, verify, validator, backing_verifier=UnixContainerBackingVerifier(api=api))
                completed = worker.resume(source_node=args.source_node,
                    source_id=args.source_id, datastore_id=datastore_id,
                    job_id=args.job_id, capture=capture, reservation=allocation,
                    task_upid=task_upid)
                inventory = discover(api, policy)
                if (completed.get('state') != 'source-probed' or
                        str(probe_id) in inventory.get('guests', {}) or
                        any(task.get('state') != 'confirmed-terminal'
                            for task in completed.get('provider_tasks', []))):
                    raise DistributionError('Source probe or provider task remains uncertain; reservation retained.')
                ledger.reconcile_job(args.job_id, operation_terminal=True,
                                     allocations_reconciled=True)
            print('Reconciled, verified and cleaned isolated source probe ' + args.job_id)
        finally:
            ledger.close()
    else:
        if not policy.get('enabled'):
            raise DistributionError('Image distribution is disabled by administrator policy.')
        if args.node not in policy.get('allowed_nodes', []):
            raise DistributionError('Replica destination node is not enabled by administrator policy.')
        if args.isolated_bridge not in policy.get('isolated_bridges', []):
            raise DistributionError('Probe bridge is not approved by administrator policy.')
        from crange_placement import PlacementError, ReservationLedger
        from crange_cache_jobs import artifact_pin
        import crange_scheduling as scheduling
        source_identity = json.loads(args.source_identity.read_text())
        sealed_windows = source_identity.get('provenance_type') == 'sealed-windows-source-v1'
        if sealed_windows:
            _require_worker_sealed_record(source_identity, args.source_identity)
        else:
            from crange_catalog import validate_source_identity
            from crange_windows_snapshot import validate_captured_identity
            validate_captured_identity(source_identity)
        verify = ProbeVerifier(args.isolated_bridge, policy.get('isolated_bridges', []),
            source_identity, ct_exec=SSHContainerExec(api=api),
            init_system='windows' if sealed_windows or source_identity.get('os_family') == 'windows' else 'systemd')
        archive = json.loads(args.archive.read_text()) if args.archive else None
        ledger = ReservationLedger(scheduling.DATA / 'reservations.sqlite3')
        try:
            with node_locks([args.source_node, args.node], library=True):
                def prepare(catalog):
                    image = catalog.get('images', {}).get(args.image_id)
                    if (not image or ('*' not in image.get('approved_projects', []) and
                                      args.project not in image.get('approved_projects', []))):
                        raise DistributionError('Image is not approved for the selected project.')
                    image_capture = image.get('identity_capture') or {}
                    image_is_sealed = image_capture.get('provenance_type') == 'sealed-windows-source-v1'
                    expected_identity = (image_capture if image_is_sealed else
                                         image.get('source_identity') or
                                         image.get('verification', {}).get('source_identity'))
                    if archive:
                        archive_capture = archive.get('identity_capture', {})
                        expected_identity = archive_capture if image_is_sealed else archive_capture.get('source_identity')
                        artifact = image.get('artifact', {})
                        if (archive.get('state') != 'exported' or not archive.get('complete_capture') or
                                (archive.get('identity_capture') != source_identity if image_is_sealed else
                                 archive.get('source_identity') != source_identity) or
                                archive.get('artifact_sha256') != image.get('artifact_sha256') or
                                archive.get('source_id') != args.source_id or
                                archive.get('node') != args.source_node or
                                artifact.get('source_id') != args.source_id or
                                artifact.get('node') != args.source_node or
                                artifact.get('volume_id') != archive.get('volume_id')):
                            raise DistributionError('Archive does not match the approved source image and identity.')
                    if expected_identity != source_identity:
                        raise DistributionError('Probe identity must match captured image provenance.')
                    if not archive and args.source_node != args.node:
                        raise DistributionError('Cross-node source cloning requires a verified central archive.')
                    capacity = required_capacity(image)
                    inventory = discover(api, policy)
                    try:
                        allocation = ledger.reserve_preparation(project_id=args.project,
                            job_id=args.job_id, owner='cli', image_id=args.image_id,
                            catalog=catalog, node_name=args.node, datastore_id=args.datastore_id,
                            capacity=capacity, inventory=inventory, policy=policy)
                        ledger.mark_running(args.job_id)
                    except PlacementError as exc:
                        raise DistributionError('Shared-ledger capacity reservation failed.') from exc
                    validator = shared_reservation_validator(ledger, args.job_id,
                        allocation, args.image_id, image['artifact_sha256'])
                    worker = ReplicaWorker(api, journal, verify,
                        approved_storage=map(tuple, policy.get('approved_storage', [])),
                        tested_paths=policy.get('tested_paths', []),
                        allocation_lock=journal.allocation_lock, hash_volume=SSHVolumeHasher(),
                        reservation_validator=validator, backing_verifier=UnixContainerBackingVerifier(api=api))
                    if archive:
                        with artifact_pin(archive['volume_id'], image['artifact_sha256'], args.job_id):
                            return worker.prepare(catalog, args.image_id,
                                source_node=args.source_node, source_id=args.source_id,
                                node=args.node, datastore_id=args.datastore_id,
                                job_id=args.job_id, archive=archive, reservation=allocation)
                    return worker.prepare(catalog, args.image_id,
                        source_node=args.source_node, source_id=args.source_id,
                        node=args.node, datastore_id=args.datastore_id,
                        job_id=args.job_id, reservation=allocation)
                catalog_transaction(args.catalog, prepare)
                inventory = discover(api, policy)
                record = json.loads((Path(args.journal_dir) / (args.job_id + '.json')).read_text())
                guest = inventory.get('guests', {}).get(str(record.get('template_id')))
                if (record.get('state') != 'ready' or not guest or not guest.get('template') or
                        guest.get('node_name') != args.node or
                        str(record.get('probe_id')) in inventory.get('guests', {}) or
                        any(task.get('state') != 'confirmed-terminal'
                            for task in record.get('provider_tasks', []))):
                    raise DistributionError('Replica allocation or provider tasks remain uncertain; reservation retained.')
                ledger.reconcile_job(args.job_id, operation_terminal=True, allocations_reconciled=True)
            print('Published verified replica ' + args.image_id)
        finally:
            ledger.close()




def _add_project_approval_arguments(command):
    scope = command.add_mutually_exclusive_group(required=True)
    scope.add_argument('--project', action='append',
                       help='Approve for one project; repeat for multiple projects.')
    scope.add_argument('--all-projects', action='store_true',
                       help='Administrator-only: approve for all present and future projects.')


def _project_approval_scope(args):
    return ['*'] if args.all_projects else args.project


def normalize_projects(projects):
    if not isinstance(projects, (list, tuple)) or not projects:
        raise DistributionError('At least one image project approval is required.')
    if '*' in projects and list(projects) != ['*']:
        raise DistributionError('Universal image approval cannot be mixed with project approvals.')
    result = []
    for project in projects:
        if project == '*':
            value = '*'
        else:
            if isinstance(project, bool) or not re.fullmatch(r'[0-9]{1,3}', str(project)):
                raise DistributionError('Invalid image project approval.')
            value = int(project)
            if not 1 <= value <= 255 or value in (60, 61):
                raise DistributionError('Invalid image project approval.')
        if value not in result: result.append(value)
    if not result:
        raise DistributionError('At least one image project approval is required.')
    return result


def publish_stock_archive(catalog, image_id, *, node, template_file_id,
                          architecture, source_revision, approved_projects,
                          disk_gb, expected_sha256, hash_volume, verification):
    """Publish only a stock OS archive; never substitute for captured CTs.

    Hash the actual node-accessible archive through the trusted volume adapter.
    Size is the reviewed minimum root allocation, not compressed archive size.
    """
    from crange_catalog import publish_image
    if node not in {f'proxmox{i}' for i in range(1, 7)} or not re.fullmatch(
            r'[a-zA-Z0-9_-]+:vztmpl/[a-zA-Z0-9_.-]+', template_file_id):
        raise DistributionError('Invalid stock archive identity.')
    if (not isinstance(disk_gb, (int, float)) or isinstance(disk_gb, bool) or
            not math.isfinite(disk_gb) or disk_gb < 1 or verification.get('passed') is not True or not verification.get('evidence')):
        raise DistributionError('Stock archive requires reviewed disk size and verification evidence.')
    if not re.fullmatch(r'[a-f0-9]{64}', expected_sha256) or hash_volume(node, template_file_id) != expected_sha256:
        raise DistributionError('Actual stock archive checksum does not match publication.')
    if image_id in catalog.get('images', {}):
        image = catalog['images'][image_id]
        if (image.get('source_type') != 'stock_archive' or
                image.get('artifact_sha256') != expected_sha256 or
                image.get('architecture') != architecture or
                image.get('source_revision') != source_revision or
                image.get('disk_gb') != disk_gb or
                image.get('approved_projects') != normalize_projects(approved_projects)):
            raise DistributionError('Published archive identity is immutable; use a new image version.')
        return publish_replica(catalog, image_id, {
            'node': node, 'template_file_id': template_file_id,
            'datastore_id': template_file_id.split(':')[0], 'state': 'ready',
            'artifact_sha256': expected_sha256,
            'verified_at': datetime.now(timezone.utc).isoformat(), 'verification': verification})
    return publish_image(catalog, image_id, {
        'kind': 'container', 'source_type': 'stock_archive', 'architecture': architecture,
        'source_revision': source_revision, 'approved_projects': normalize_projects(approved_projects),
        'disk_gb': disk_gb, 'artifact_sha256': expected_sha256, 'verified': True,
        'verification': verification, 'replicas': [{
            'node': node, 'template_file_id': template_file_id,
            'datastore_id': template_file_id.split(':')[0], 'state': 'ready',
            'artifact_sha256': expected_sha256,
            'verified_at': datetime.now(timezone.utc).isoformat(), 'verification': verification}]})
def automatic_probe_candidate(image):
    """A proposed test size, never evidence that the guest boots at that size."""
    if image.get('probe_resources') or image.get('kind') != 'vm' or image.get('os_family') == 'windows':
        return None
    if image.get('init_system', image.get('identity_capture', {}).get('init_system')) != 'systemd':
        return None
    memory, cpu = image.get('memory_mb'), image.get('cpu_cores')
    maintained = str(image.get('source_revision','')).startswith((
        'platform-debian-13-vm-', 'platform-ubuntu-24-04-vm-'))
    if maintained and type(memory) is int and memory > 1024 and type(cpu) is int and cpu >= 1:
        # Propose a smaller verification guest for these fixed server bases.
        # It is approved only after a real boot, identity check and cleanup.
        return {'memory_mb':1024,'cpu_cores':1}
    if (type(memory) is not int or type(cpu) is not int or memory <= 2048 or cpu < 1):
        return None
    return {'memory_mb': 2048, 'cpu_cores': min(cpu, 2)}


def validate_probe_candidate(image, candidate):
    if candidate is None:
        return None
    approved = image.get('probe_resources') or {}
    approved_size = {key: approved.get(key) for key in ('memory_mb', 'cpu_cores')}
    if (not isinstance(candidate, dict) or set(candidate) != {'memory_mb', 'cpu_cores'} or
            any(type(value) is not int for value in candidate.values()) or
            (candidate != automatic_probe_candidate(image) and
             not (approved.get('verified') is True and candidate == approved_size))):
        raise DistributionError('Probe candidate differs from the fixed Linux verification profile.')
    return candidate


def required_capacity(image, *, clone_mode='linked', source_config=None, probe_candidate=None):
    """Reserve the full base plus worst-case disposable-probe disk writes.

    Full allocation is used even for a linked verification probe: thin storage
    cannot assume the probe will write only a tiny fraction of its virtual disk.
    This demand must be admitted atomically in the shared placement ledger.
    """
    source_config = source_config or {}
    disk = image.get('disk_gb')
    memory = image.get('memory_mb') or source_config.get('memory')
    cpu = image.get('cpu_cores') or source_config.get('cores')
    if 'probe_resources' in image:
        from crange_catalog import validate_probe_resources
        profile = validate_probe_resources(image['probe_resources'])
        memory, cpu = profile['memory_mb'], profile['cpu_cores']
    candidate = validate_probe_candidate(image, probe_candidate)
    if candidate:
        memory, cpu = candidate['memory_mb'], candidate['cpu_cores']
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
           not math.isfinite(value) or value <= 0 for value in (disk, memory, cpu)):
        raise DistributionError('Cache preparation requires defensible disk/RAM/CPU allocations.')
    return {'disk_gb': disk * 2.1, 'memory_mb': memory, 'cpu_cores': cpu,
            'base_disk_gb': disk, 'probe_disk_gb': disk, 'temporary_disk_gb': disk * .1,
            'clone_mode': clone_mode}


def preparation_peak_capacity(cache, guests):
    """Budget serialized verification/deployment peaks, retaining the base."""
    result = dict(cache)
    for key in ('memory_mb', 'cpu_cores', 'disk_gb'):
        value = guests.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise DistributionError('Invalid trusted staging capacity demand.')
        result[key] = max(cache.get(key, 0), value + (cache.get('base_disk_gb', 0) if key == 'disk_gb' else 0))
    return result


if __name__ == '__main__':
    import sys
    try:
        main()
    except Exception as exc:
        # Transport errors may include private command/environment details.
        message = str(exc) if isinstance(exc, (DistributionError, ValueError)) else type(exc).__name__
        print('crange: distribution failed: ' + message, file=sys.stderr)
        sys.exit(1)
