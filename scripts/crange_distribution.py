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
from datetime import datetime, timezone
from pathlib import Path

from crange_catalog import publish_replica, validate_catalog


class DistributionError(ValueError):
    pass


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


class ReplicaWorker:
    """Journal reservations before submission; retain partial jobs on timeout.

    journal(record) must commit durable ownership; verification(api,node,kind,id)
    must run reviewed disposable-clone readiness and identity checks and return
    {passed: true, identity_unique: true, ...}. It cannot be supplied by GUI input.
    """
    def __init__(self, api, journal, verify, *, approved_storage, tested_paths=(), allocation_lock=None, hash_volume=None, reservation_validator=None):
        self.api, self.journal, self.verify = api, journal, verify
        self.allocation_lock = allocation_lock
        self.hash_volume = hash_volume
        self.reservation_validator = reservation_validator
        self._active_record = None
        self.approved_storage = set(approved_storage)
        self.tested_paths = set(tested_paths)

    def _config(self, node, kind, vmid):
        return self.api.request('GET', f'/nodes/{node}/{kind}/{vmid}/config')

    def _task(self, node, method, path, fields):
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
            _owned(config, f'crange-job-{reusable[0].get("job_id", "")}')
            if int(config.get('template', 0)) != 1 or config.get('lock'):
                raise DistributionError('Published cache generation changed; fixed repair required.')
            return catalog
        if clone_mode not in ('linked', 'full'):
            raise DistributionError('Invalid clone verification mode.')
        if clone_mode == 'linked' and (datastore_id != 'local-lvm' or
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
                                     source_config=None if archive else source)
        if (self.reservation_validator is None or
                not self.reservation_validator(reservation, node, datastore_id, capacity)):
            raise DistributionError('A valid shared-ledger cache preparation reservation is required.')
        marker = f'crange-job-{job_id}'
        owned_tags = ';'.join(sorted(set(str(source.get('tags', '')).split(';')) | {'crange-image', marker}))
        record = dict(job_id=job_id, image_id=image_id, node=node,
                      datastore_id=datastore_id, state='allocated', kind=kind,
                      source_node=source_node, source_id=source_id,
                      artifact_sha256=image['artifact_sha256'], reservation=reservation)
        vmid = int(self.api.request('GET', '/cluster/nextid'))
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
                fields.update(ostemplate=archive['volume_id'], restore=1,
                              hostname=f'crange-replica-{vmid}')
            self._task(node, 'POST', f'/nodes/{node}/{kind}', fields)
        else:
            self._task(node, 'POST', f'/nodes/{node}/{kind}/{source_id}/clone',
                       {'newid': vmid, 'full': 1, 'storage': datastore_id,
                        'pool': 'crange-iac',
                        'name' if kind == 'qemu' else 'hostname': f'crange-replica-{vmid}',
                        'description': f'CRANGE distribution {job_id} image {image_id}'})
        self.api.request('PUT', f'/nodes/{node}/{kind}/{vmid}/config',
                         {'tags': owned_tags})
        record['state'] = 'converting-template'
        self.journal(dict(record))
        if kind == 'qemu':
            self._task(node, 'POST', f'/nodes/{node}/{kind}/{vmid}/template', {})
        else:
            import time
            upid = self.api.request('POST', f'/nodes/{node}/lxc/{vmid}/template', {})
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

        config = self._config(node, kind, vmid)
        _owned(config, marker)
        if int(config.get('template', 0)) != 1:
            raise DistributionError('Replica conversion did not produce a template.')
        probe_id = int(self.api.request('GET', '/cluster/nextid'))
        record.update(state='verifying', probe_id=probe_id, probe_pool=PROBE_POOL)
        self.journal(dict(record))
        self._task(node, 'POST', f'/nodes/{node}/{kind}/{vmid}/clone',
                   {'newid': probe_id, 'full': int(clone_mode == 'full'),
                    **({'storage': datastore_id} if clone_mode == 'full' else {}),
                    'pool': PROBE_POOL, 'description': f'CRANGE verification {job_id}',
                    'name' if kind == 'qemu' else 'hostname': f'crange-probe-{probe_id}'})
        probe_config = {'tags': owned_tags}
        if image.get('probe_resources'):
            profile = image['probe_resources']
            probe_config.update(memory=profile['memory_mb'], cores=profile['cpu_cores'])
            if kind == 'qemu':
                probe_config['sockets'] = 1
            record['probe_resources'] = dict(profile)
            self.journal(dict(record))
        self.api.request('PUT', f'/nodes/{node}/{kind}/{probe_id}/config', probe_config)
        result = self.verify(_JournaledProbeAPI(self.api, record, self.journal), node, kind, probe_id)
        if not isinstance(result, dict) or result.get('passed') is not True or result.get('identity_unique') is not True:
            record['state'] = 'failed-verification'
            self.journal(dict(record))
            raise DistributionError('Disposable clone readiness/identity verification failed.')
        # Probe deletion is guarded and must complete before publication.
        _owned(self._config(node, kind, probe_id), marker)
        self._task(node, 'DELETE', f'/nodes/{node}/{kind}/{probe_id}', {'purge': 1})
        replica = dict(node=node, template_id=vmid, datastore_id=datastore_id,
                       state='ready', artifact_sha256=image['artifact_sha256'],
                       verified_at=datetime.now(timezone.utc).isoformat(),
                       verification=result, job_id=job_id, clone_modes=[clone_mode], clone_modes_verified=[clone_mode], cache_local=True)
        updated = publish_replica(catalog, image_id, replica)
        record['state'] = 'ready'
        self.journal(dict(record))
        return updated


class FileJournal:
    """Controller-owned durable journal and cooperative allocation lock."""
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)

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
                try:
                    other = json.loads(entry.read_text())
                except (OSError, ValueError) as exc:
                    raise DistributionError('Uncertain cache journal; reconcile before allocation.') from exc
                if ((other.get('image_id'), other.get('node'), other.get('datastore_id')) == key and
                        other.get('state') not in ('reconciled-absent', 'cleaned-owned', 'evicted')):
                    raise DistributionError('Cache generation has an existing inflight/uncertain job; reconcile or attach.')
        if record['state'] in ('allocated', 'exporting') and destination.exists():
            raise DistributionError('Existing job must be reconciled, never blindly restarted.')
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


def _source_config_identity(config):
    # PVE changes these metadata fields when stopping/converting to a template.
    stable = {key: value for key, value in config.items() if key not in {'template', 'lock', 'digest'}}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _fixed_guest_read(api, node, guest_type, vmid, command, ct_exec, timeout):
    if guest_type == 'lxc':
        if ct_exec is None: raise DistributionError('Fixed CT exec adapter is unavailable.')
        return ct_exec(node, vmid, command).strip()
    import time
    response = api.request('POST', f'/nodes/{node}/qemu/{vmid}/agent/exec', {'command': command})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = api.request('GET', f'/nodes/{node}/qemu/{vmid}/agent/exec-status', {'pid': int(response['pid'])})
        if result.get('exited'):
            if int(result.get('exitcode', -1)) != 0: raise DistributionError('Source identity command failed.')
            return str(result.get('out-data', '')).strip()
        time.sleep(1)
    raise DistributionError('Source identity guest-agent check timed out.')


def _guest_readiness(read, init_system):
    if init_system == 'systemd':
        return read(['/usr/bin/systemctl', 'is-system-running'])
    # OpenRC installations differ on whether rc-status is linked into /bin or
    # /sbin. Try only these fixed executable paths; never invoke a shell.
    failure = None
    for command in (['/bin/rc-status', '--runlevel'], ['/sbin/rc-status', '--runlevel']):
        try:
            return read(command)
        except Exception as exc:
            failure = exc
    raise DistributionError('OpenRC readiness command failed.') from failure


def capture_source_identity(api, *, node, kind, vmid, job_id, journal, ct_exec=None, timeout=60, init_system='systemd'):
    """Read fixed identity commands before reviewed stop/template conversion.

    This never starts, stops or reconfigures a guest and cannot boot a template.
    The trusted worker journals the result; GUI parameters cannot supply commands.
    """
    from crange_catalog import validate_source_identity
    if (node not in {f'proxmox{i}' for i in range(1, 7)} or kind not in ('vm', 'container')
            or type(vmid) is not int or vmid < 100 or not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', job_id)
            or init_system not in ('systemd', 'openrc') or not 1 <= timeout <= 180):
        raise DistributionError('Invalid source identity capture scope.')
    guest_type = 'qemu' if kind == 'vm' else 'lxc'
    base = f'/nodes/{node}/{guest_type}/{vmid}'
    _require_probe_pool(api, node, guest_type, vmid)
    config = api.request('GET', base + '/config')
    _owned(config)
    if int(config.get('template', 0)) or api.request('GET', base + '/status/current').get('status') != 'running':
        raise DistributionError('Identity capture requires an owned running guest before template conversion.')
    read = lambda command: _fixed_guest_read(api, node, guest_type, vmid, command, ct_exec, timeout)
    readiness = _guest_readiness(read, init_system)
    if readiness != ('running' if init_system == 'systemd' else 'default'):
        raise DistributionError('Source guest is not ready for identity capture.')
    identity = {'machine_id': read(['/bin/cat', '/etc/machine-id']),
                'ssh_host_key': read(['/bin/cat', '/etc/ssh/ssh_host_ed25519_key.pub'])}
    validate_source_identity(identity)
    if config != api.request('GET', base + '/config') or api.request('GET', base + '/status/current').get('status') != 'running':
        raise DistributionError('Source changed during identity capture; repeat capture.')
    record = {'state': 'identity-captured', 'job_id': job_id, 'node': node, 'kind': kind,
              'source_id': vmid, 'source_config_sha256': _source_config_identity(config),
              'source_identity': identity, 'init_system': init_system, 'readiness': readiness,
              'captured_at': datetime.now(timezone.utc).isoformat()}
    journal(record)
    return record


def export_image(api, *, node, kind, vmid, storage, job_id, journal,
                 hash_volume, approved_backup_storage, identity_record=None):
    """Export an owned stopped guest/template via vzdump, including config.

    hash_volume(node, volume_id) is a trusted fixed transport adapter, never a
    user-provided command. Returns the actual SHA256 of the exported file.
    """
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
    from crange_catalog import validate_source_identity
    if (not isinstance(identity_record, dict) or identity_record.get('state') != 'identity-captured'
            or (identity_record.get('node'), identity_record.get('kind'), identity_record.get('source_id')) != (node, kind, vmid)
            or identity_record.get('source_config_sha256') != _source_config_identity(config)
            or identity_record.get('init_system') not in ('systemd', 'openrc')
            or identity_record.get('readiness') != ('running' if identity_record['init_system'] == 'systemd' else 'default')):
        raise DistributionError('Matching worker-captured source identity is required before export.')
    validate_source_identity(identity_record.get('source_identity'))
    content_path = f'/nodes/{node}/storage/{storage}/content'
    before = {item['volid'] for item in api.request('GET', content_path)}
    record = dict(job_id=job_id, node=node, kind=kind, source_id=vmid,
                  storage=storage, state='exporting', disk_gb=disk_gb,
                  source_identity=dict(identity_record['source_identity']), identity_capture=dict(identity_record),
                  init_system=identity_record['init_system'],
                  memory_mb=int(config.get('memory', 0)), cpu_cores=int(config.get('cores', 0)))
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
    from crange_catalog import validate_source_identity
    validate_source_identity(export.get('source_identity'))
    capture = export.get('identity_capture', {})
    if (capture.get('state') != 'identity-captured' or not capture.get('job_id')
            or capture.get('source_identity') != export['source_identity']
            or capture.get('init_system') not in ('systemd', 'openrc')
            or export.get('init_system') != capture['init_system']
            or capture.get('readiness') != ('running' if capture['init_system'] == 'systemd' else 'default')
            or (capture.get('node'), capture.get('kind'), capture.get('source_id')) != (export['node'], export['kind'], export['source_id'])
            or not re.fullmatch(r'[a-f0-9]{64}', capture.get('source_config_sha256', ''))):
        raise DistributionError('Export is missing bound source identity capture provenance.')
    projects = normalize_projects(approved_projects)
    disk_gb = export.get('disk_gb')
    if not isinstance(disk_gb, (int, float)) or isinstance(disk_gb, bool) or not math.isfinite(disk_gb) or disk_gb <= 0:
        raise DistributionError('Published image requires a defensible configured disk size.')
    return publish_image(catalog, image_id, {
        'kind': export['kind'], 'architecture': architecture, 'disk_gb': disk_gb,
        'memory_mb': export.get('memory_mb'), 'cpu_cores': export.get('cpu_cores'),
        'source_identity': dict(export['source_identity']), 'identity_capture': dict(capture),
        'init_system': capture['init_system'],
        'artifact_sha256': export['artifact_sha256'],
        'artifact': {'volume_id': export['volume_id'], 'node': export['node'],
                     'format': 'vzdump', 'complete_capture': True, 'source_id': export['source_id']},
        'source_revision': source_revision, 'verified': True,
        'approved_projects': projects, 'verification': verification,
        'replicas': [{'node': export['node'], 'template_id': int(export['source_id']),
                      'datastore_id': datastore_id, 'state': 'ready',
                      'artifact_sha256': export['artifact_sha256'],
                      'verified_at': datetime.now(timezone.utc).isoformat(),
                      'verification': verification}]})


class ProbeVerifier:
    """Fixed Linux probe checks on a disconnected administrator-approved bridge.

    Source identities must be collected during reviewed capture, not invented.
    VM guest-agent must be enabled; CT exec uses the trusted SSH host adapter.
    This gate checks boot readiness and identity, not scenario service networking.
    """
    def __init__(self, isolated_bridge, approved_bridges, source_identity,
                 *, ct_exec=None, timeout=180, init_system="systemd"):
        if isolated_bridge not in approved_bridges or not re.fullmatch(r'[a-zA-Z0-9_-]{1,15}', isolated_bridge):
            raise DistributionError('Probe bridge is not approved.')
        if not source_identity.get('machine_id') or not source_identity.get('ssh_host_key'):
            raise DistributionError('Captured source identity evidence is required.')
        if init_system not in ('systemd', 'openrc'):
            raise DistributionError('Unsupported reviewed guest readiness profile.')
        self.init_system = init_system
        self.bridge, self.source = isolated_bridge, source_identity
        self.ct_exec, self.timeout = ct_exec, timeout

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
                fields[key] = f'name=eth{key[3:]},bridge={self.bridge},ip=manual,ip6=manual,link_down=1,type=veth'
        fields['onboot'] = 0
        api.request('PUT', f'/nodes/{node}/{kind}/{vmid}/config', fields)
        upid = api.request('POST', f'/nodes/{node}/{kind}/{vmid}/status/start', {})
        api.task(node, upid, timeout=self.timeout)
        def read(command):
            if kind == 'lxc':
                if self.ct_exec is None:
                    raise DistributionError('Fixed CT exec adapter is unavailable.')
                return self.ct_exec(node, vmid, command).strip()
            response = api.request('POST', f'/nodes/{node}/qemu/{vmid}/agent/exec',
                                   {'command': command})
            pid = int(response['pid'])
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                result = api.request('GET', f'/nodes/{node}/qemu/{vmid}/agent/exec-status', {'pid': pid})
                if result.get('exited'):
                    if int(result.get('exitcode', -1)) != 0:
                        raise DistributionError('Probe identity/readiness command failed.')
                    return str(result.get('out-data', '')).strip()
                time.sleep(1)
            raise DistributionError('Probe guest-agent check timed out.')
        deadline = time.monotonic() + self.timeout
        readiness = ''
        while time.monotonic() < deadline:
            try:
                readiness = _guest_readiness(read, self.init_system)
                if readiness == ('running' if self.init_system == 'systemd' else 'default'): break
            except Exception:
                # Guest-agent initialization may lag behind host task completion.
                pass
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
        upid = api.request('POST', f'/nodes/{node}/{kind}/{vmid}/status/stop', {})
        api.task(node, upid, timeout=self.timeout)
        return {'passed': unique, 'identity_unique': unique,
                'evidence': {'readiness': readiness, 'machine_id': machine_id,
                             'ssh_host_key': ssh_host_key, 'isolated_bridge': self.bridge}}


class SSHContainerExec:
    """Only the verifier's three fixed readonly commands are executable."""
    def __init__(self, run=None): self.run = run or subprocess.run

    def __call__(self, node, vmid, command):
        allowed = [['/usr/bin/systemctl', 'is-system-running'], ['/bin/rc-status', '--runlevel'],
                   ['/sbin/rc-status', '--runlevel'],
                   ['/bin/cat', '/etc/machine-id'],
                   ['/bin/cat', '/etc/ssh/ssh_host_ed25519_key.pub']]
        if command not in allowed or node not in {f'proxmox{i}' for i in range(1, 7)} or int(vmid) < 100:
            raise DistributionError('Unsupported CT verification command.')
        result = self.run(['ssh', '-o', 'BatchMode=yes', node,
                          shlex.join(['pct', 'exec', str(int(vmid)), '--'] + command)],
                         check=True, capture_output=True, text=True, timeout=60)
        return result.stdout


def catalog_transaction(path, update):
    """Serialize publication and atomically replace a restrictive catalog file."""
    path = Path(path)
    with path.with_suffix(path.suffix + '.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        catalog = json.loads(path.read_text()) if path.exists() else {'schema_version': 1, 'images': {}}
        result = update(catalog)
        validate_catalog(result)
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(result, output, indent=2, sort_keys=True)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
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
    capture = commands.add_parser('identity-capture')
    capture.add_argument('--node', required=True)
    capture.add_argument('--kind', choices=['vm', 'container'], required=True)
    capture.add_argument('--source-id', type=int, required=True)
    capture.add_argument('--job-id', required=True)
    capture.add_argument('--init-system', choices=['systemd', 'openrc'], default='systemd')
    export = commands.add_parser('export')
    export.add_argument('--node', required=True)
    export.add_argument('--kind', choices=['vm', 'container'], required=True)
    export.add_argument('--source-id', type=int, required=True)
    export.add_argument('--storage', required=True)
    export.add_argument('--job-id', required=True)
    export.add_argument('--identity-record', type=Path, required=True)
    publish = commands.add_parser('publish')
    publish.add_argument('--export-record', type=Path, required=True)
    publish.add_argument('--verification', type=Path, required=True)
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
    replica.add_argument('--source-id', type=int, required=True)
    replica.add_argument('--source-identity', type=Path, required=True)
    replica.add_argument('--archive', type=Path)
    args = parser.parse_args(argv)
    policy = json.loads(args.policy.read_text())
    api = ProxmoxAPI()
    journal = FileJournal(args.journal_dir)
    if args.action == 'inventory':
        print(json.dumps(discover(api, policy.get('placement', DEFAULT_POLICY)), indent=2))
    elif args.action == 'identity-capture':
        with node_locks([args.node]), journal.allocation_lock():
            record = capture_source_identity(api, node=args.node, kind=args.kind, vmid=args.source_id,
                job_id=args.job_id, journal=journal, ct_exec=SSHContainerExec(), init_system=args.init_system)
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
        verification = json.loads(args.verification.read_text())
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
    else:
        verify = ProbeVerifier(args.isolated_bridge, policy.get('isolated_bridges', []),
            json.loads(args.source_identity.read_text()), ct_exec=SSHContainerExec())
        worker = ReplicaWorker(api, journal, verify,
            approved_storage=map(tuple, policy.get('approved_storage', [])),
            tested_paths=policy.get('tested_paths', []), allocation_lock=journal.allocation_lock, hash_volume=SSHVolumeHasher())
        archive = json.loads(args.archive.read_text()) if args.archive else None
        with node_locks([args.source_node, args.node], library=True):
            catalog_transaction(args.catalog, lambda catalog: worker.prepare(catalog,
                args.image_id, source_node=args.source_node, source_id=args.source_id,
                node=args.node, datastore_id=args.datastore_id, job_id=args.job_id,
                archive=archive))
        print('Published verified replica ' + args.image_id)




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
def required_capacity(image, *, clone_mode='linked', source_config=None):
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
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
           not math.isfinite(value) or value <= 0 for value in (disk, memory, cpu)):
        raise DistributionError('Cache preparation requires defensible disk/RAM/CPU allocations.')
    return {'disk_gb': disk * 2.1, 'memory_mb': memory, 'cpu_cores': cpu,
            'base_disk_gb': disk, 'probe_disk_gb': disk, 'temporary_disk_gb': disk * .1,
            'clone_mode': clone_mode}


if __name__ == '__main__':
    import sys
    try:
        main()
    except Exception as exc:
        # Transport errors may include private command/environment details.
        message = str(exc) if isinstance(exc, (DistributionError, ValueError)) else type(exc).__name__
        print('crange: distribution failed: ' + message, file=sys.stderr)
        sys.exit(1)
