"""Reviewed VM release capture; no commands or evidence supplied by callers."""
from __future__ import annotations
import json
import hashlib
import re
import socket
import stat
import time
import urllib.parse
from pathlib import Path

from crange_distribution import (DistributionError, PROBE_POOL, FileJournal,
    _owned, _require_probe_pool, _source_config_identity, _sealed_windows_source_volumes, _source_capacity,
    capture_source_identity, export_image, SSHVolumeHasher)
from crange_placement import ReservationLedger, workload_node_allowed
from crange_scheduling import CONFIG, DATA, atomic_json, discover, node_locks, read_settings


SEALED_WINDOWS_PROOF_FILES = {
    'inspection': ('/var/lib/crange-template-builds/windows-locale-candidate8912/inspection.json', 0o644),
    'preparation': ('/var/lib/crange-template-builds/windows-locale-candidate8912/preparation.json', 0o600),
    'probe8913': ('/var/lib/crange-template-builds/windows-locale-probe8913.json', 0o600),
    'probe8914': ('/var/lib/crange-template-builds/windows-locale-probe8914.json', 0o600),
    'qga8914': ('/var/lib/crange-template-builds/windows-locale-probe8914-qga-result.json', 0o600),
}


def archive_headroom(total_bytes):
    """Reserve 10% (at least 20 GiB) beyond one full uncompressed backup."""
    import math
    if not math.isfinite(total_bytes) or total_bytes <= 0:
        raise DistributionError('Central archive capacity is unavailable.')
    return max(total_bytes * .10, 20 * 1024 ** 3)


def _load_sealed_windows_proof():
    """Read the fixed root-owned P4 proof through the peer-UID checked relay."""
    request = {'node': 'proxmox4', 'command': 'sealed-windows-source-proof'}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(300)
            client.connect('/run/crange-ct-read/socket')
            client.sendall(json.dumps(request, separators=(',', ':')).encode() + b'\n')
            data = b''
            while not data.endswith(b'\n') and len(data) <= 300000:
                chunk = client.recv(300001 - len(data))
                if not chunk:
                    break
                data += chunk
        if len(data) > 300000 or not data.endswith(b'\n'):
            raise ValueError('Fixed sealed proof relay response is outside its bound.')
        response = json.loads(data)
        if set(response) != {'value'} or not isinstance(response['value'], str):
            raise ValueError('Fixed sealed proof relay denied the request.')
        proof = json.loads(response['value'])
    except (OSError, ValueError, socket.timeout) as exc:
        raise DistributionError('Protected P4 sealed Windows proof bundle is unavailable or invalid.') from exc
    if (set(proof) not in [{'files', 'live'}, {'files', 'live', 'retirement'}] or
            set(proof.get('files', {})) != set(SEALED_WINDOWS_PROOF_FILES)):
        raise DistributionError('Protected P4 sealed Windows proof bundle is incomplete.')
    return {**proof['files'], 'live': proof['live'],
            **({'retirement': proof['retirement']} if 'retirement' in proof else {})}


def capture_sealed_windows_source(api, journal, *, proof_loader=_load_sealed_windows_proof):
    """Journal the fixed P100 never-booted source proof after fresh PVE checks."""
    from crange_catalog import validate_source_identity
    from crange_distribution import _source_config_identity
    proof = proof_loader()
    if 'retirement' in proof:
        # Published proof guests may be retired only after a root-owned relay
        # receipt binds their final live evidence to the exact immutable image.
        # Reuse the original capture verbatim; never turn historical state into
        # a new claim that deleted guests are running.
        prior_path = Path(journal.directory) / 'p100-sealed-windows-8912.json'
        try:
            info = prior_path.lstat()
            if (prior_path.is_symlink() or not stat.S_ISREG(info.st_mode) or
                    stat.S_IMODE(info.st_mode) != 0o600):
                raise DistributionError('Protected original sealed-source capture has unsafe access controls.')
            prior_capture = json.loads(prior_path.read_text())
        except (OSError, ValueError) as exc:
            raise DistributionError('Protected original sealed-source capture is required for proof retirement.') from exc
        from crange_catalog import validate_sealed_windows_capture, validate_sealed_windows_proof_retirement
        validate_sealed_windows_capture(prior_capture)
        policy, catalog = read_settings()
        del policy
        image = catalog.get('images', {}).get('mhl-windows-server-2025-base-p100-v1')
        if not isinstance(image, dict):
            raise DistributionError('Published P100 catalog image is required before proof retirement can be accepted.')
        validate_sealed_windows_proof_retirement(prior_capture, proof['retirement'],
            {**image, 'image_id': 'mhl-windows-server-2025-base-p100-v1'})
        config = api.request('GET', '/nodes/proxmox4/qemu/8912/config')
        status = api.request('GET', '/nodes/proxmox4/qemu/8912/status/current')
        if (_source_config_identity(config, 8912) != prior_capture['source_config_sha256'] or
                status.get('status') != 'stopped' or not _sealed_windows_source_volumes(config)):
            raise DistributionError('P100 frozen source changed after proof retirement.')
        return prior_capture
    inspection = proof['inspection']['document']
    preparation = proof['preparation']['document']
    first = proof['probe8913']['document']
    second = proof['probe8914']['document']
    qga = proof['qga8914']['document']
    live = proof.get('live')
    expected_sha = '5e9fdb9337ec631b74100dc1c1f47ffdfb02f8873b32ee6119b49bc7cef27e9d'
    prepared_config = preparation.get('frozen_candidate_config')
    prepared_sha = preparation.get('frozen_candidate_config_sha256')
    if prepared_sha is None and isinstance(prepared_config, dict):
        # The protected root record predates the explicit digest field in the
        # checked-in schema. Recompute the exact config identity from its
        # embedded snapshot; do not accept an absent config or caller hash.
        prepared_sha = _source_config_identity(prepared_config, 8912)
    expected_hives = {
        'SYSTEM': 'fb2f3526a7af150d153080eb608aef50650d2abd837efc3b76cec194334aef8e',
        'SOFTWARE': '08e9281118a0bad1d28560483648c0607da1656b3ef50bd703b950687a81f7c9',
        'SAM': 'b9f3b16431b4cfba63812c74967cb07a609eac695152fc56029bcab622ab6c62'}
    answer_sha = '11dc5811c48ec41ffc97c9980171f345abd750a90acd89e3cf0da17da808e2dc'
    if (inspection.get('state') != 'sealed-candidate-read-only-inspected' or
            inspection.get('never_booted') is not True or inspection.get('source115_stopped_unchanged') is not True or
            inspection.get('success_tag') is not True or inspection.get('hive_sha256') != expected_hives or
            preparation.get('state') != 'sealed-candidate-locale-prepared' or preparation.get('vmid') != 8912 or
            preparation.get('never_booted') is not True or preparation.get('source115_stopped_unchanged') is not True or
            prepared_sha != expected_sha or
            preparation.get('hive_sha256_unchanged') != expected_hives or
            preparation.get('image_state') != 'IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE' or
            preparation.get('registry') != {'GeneralizationState': 4, 'CleanupState': 2} or
            {row.get('prepared_sha256') for row in preparation.get('files', [])} != {answer_sha}):
        raise DistributionError('Protected P4 evidence does not prove the reviewed never-booted sealed source.')
    if (not isinstance(live, dict) or set(live) != {'source', 'probes'} or
            live['source'].get('status') != 'stopped' or
            live['source'].get('config_sha256') != expected_sha or
            {row.get('vmid') for row in live['probes'] if isinstance(row, dict)} != {8913, 8914}):
        raise DistributionError('Protected relay snapshot does not prove the fresh stopped source/probe state.')
    live_probes = {row['vmid']: row for row in live['probes']}
    probes = []
    for vmid, record in ((8913, first), (8914, second)):
        identity = dict(record.get('identity') or {})
        identity['os_family'] = 'windows'
        validate_source_identity(identity)
        if (record.get('guest_id', vmid) != vmid or
                record.get('source_vmid', record.get('source_id')) != 8912 or
                record.get('state') != 'unattended-windows-ready' or
                record.get('source_config_unchanged') is not True or
                record.get('clone_completed') is not True or record.get('isolation_before_boot_verified') is not True or
                record.get('manual_interventions') != [] or record.get('identity') is None):
            raise DistributionError('Protected clone journal does not prove an unattended independent clone.')
        if vmid == 8914 and (qga.get('exitcode') != 0 or qga.get('exited') not in (True, 1) or
                             record.get('recovery_result_sha256') != proof['qga8914']['sha256'] or
                             json.loads(qga.get('out-data', '{}')) != {k: v for k, v in identity.items() if k != 'os_family'}):
            raise DistributionError('Protected second-probe QGA result does not match its journal identity.')
        probes.append({'vmid': vmid, 'state': record['state'], 'source_config_sha256': expected_sha,
            'clone_upid': record.get('upid'), 'clone_task_ok': False,
            'isolation_before_boot_verified': True, 'source_config_unchanged': True,
            'manual_interventions': [], 'identity': identity})
    if first.get('identity', {}).get('machine_guid') == second.get('identity', {}).get('machine_guid'):
        raise DistributionError('Independent sealed-source clones have duplicate machine GUIDs.')
    config = api.request('GET', '/nodes/proxmox4/qemu/8912/config')
    status = api.request('GET', '/nodes/proxmox4/qemu/8912/status/current')
    if (_source_config_identity(config, 8912) != expected_sha or
            live['source'].get('config_sha256') != _source_config_identity(config, 8912) or
            status.get('status') != 'stopped' or
            int(config.get('template', 0)) not in (0, 1) or
            set(str(config.get('tags', '')).split(';')) !=
            {'crange-capture-windows-server2025-locale-20261008', 'crange-image', 'mhl-windows-sealed-locale'}):
        raise DistributionError('Fresh PVE source config/status does not match the frozen sealed candidate.')
    source_volumes = ['crange-library:8912/vm-8912-disk-0.raw', 'crange-library:8912/vm-8912-disk-1.raw']
    if not _sealed_windows_source_volumes(config):
        raise DistributionError('Fresh PVE source config does not retain the expected owned volumes.')
    for probe in probes:
        vmid = probe['vmid']
        config = api.request('GET', f'/nodes/proxmox4/qemu/{vmid}/config')
        status = api.request('GET', f'/nodes/proxmox4/qemu/{vmid}/status/current')
        task_path = f'/nodes/proxmox4/tasks/{urllib.parse.quote(probe["clone_upid"], safe="")}/status'
        task = api.request('GET', task_path)
        relay_probe = live_probes.get(vmid, {})
        tags = set(str(config.get('tags', '')).split(';'))
        if (status.get('status') != 'running' or task.get('status') != 'stopped' or task.get('exitstatus') != 'OK' or
                relay_probe.get('config_sha256') != _source_config_identity(config, vmid) or
                relay_probe.get('status') != 'running' or relay_probe.get('task_status') != 'stopped' or
                relay_probe.get('task_exitstatus') != 'OK' or relay_probe.get('probe_pool_member') is not True or
                not {'crange-image', 'p100'} <= tags or
                str(config.get('name', '')).lower() != f'crange-win-locale-proof-{vmid}'):
            raise DistributionError('Fresh PVE clone/task state no longer matches the sealed-source acceptance proof.')
        probe['clone_task_ok'] = True
        probe['probe_config_sha256'] = _source_config_identity(config, vmid)
        if vmid == 8913 and first.get('probe_config_sha256') and probe['probe_config_sha256'] != first['probe_config_sha256']:
            raise DistributionError('First sealed-source clone configuration changed since its protected capture journal.')
        probe['_live_config'] = config
    clone_configs = [probe.pop('_live_config') for probe in probes]
    uuid_values = [str(config.get('smbios1', '')).split('uuid=', 1)[-1] for config in clone_configs]
    genids = [str(config.get('vmgenid', '')) for config in clone_configs]
    macs = [{str(value).split('=', 1)[1].split(',', 1)[0] for key, value in config.items()
             if re.fullmatch(r'net[0-9]+', key) and '=' in str(value)} for config in clone_configs]
    if (not all(uuid_values) or len(set(uuid_values)) != 2 or
            not all(genids) or len(set(genids)) != 2 or not macs[0] or not macs[1] or macs[0] & macs[1]):
        raise DistributionError('Fresh PVE clone configurations do not prove independent VM identities.')
    file_hashes = {key: proof[key]['sha256'] for key in SEALED_WINDOWS_PROOF_FILES}
    bundle_sha = hashlib.sha256(json.dumps(file_hashes,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    capture = {'state': 'sealed-source-captured', 'provenance_type': 'sealed-windows-source-v1',
        'job_id': 'p100-sealed-windows-8912', 'project': 100, 'node': 'proxmox4', 'kind': 'vm',
        'source_id': 8912, 'source_config_sha256': expected_sha, 'source_identity': None,
        'source_state': 'stopped-never-booted', 'source_volumes': source_volumes,
        'hive_sha256': expected_hives, 'answer_sha256': answer_sha,
        'generalization_state': 4, 'cleanup_state': 2,
        'image_state': 'IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE',
        'fresh_source_freeze_verified': True, 'protected_root_evidence_verified': True,
        'protected_bundle_sha256': bundle_sha,
        'protected_file_sha256': file_hashes, 'probes': probes}
    from crange_catalog import validate_sealed_windows_capture
    validate_sealed_windows_capture(capture)
    journal(capture)
    return capture


def convert_sealed_windows_p100(api, journal, *, proof_loader=_load_sealed_windows_proof,
                                timeout=300, poll_interval=3):
    """Convert only the journaled, stopped P100 sealed source to a template.

    The fixed operation journal records intent before submission and the exact
    provider UPID immediately after submission. A submission without a saved
    UPID is never retried automatically.
    """
    from crange_catalog import validate_sealed_windows_capture
    from crange_scheduling import atomic_json

    journal_dir = Path(journal.directory)
    proof_path = journal_dir / 'p100-sealed-windows-8912.json'
    operation_path = journal_dir / 'p100-sealed-windows-8912-template-conversion.json'
    for path in (proof_path, operation_path):
        try:
            st = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o600:
                raise DistributionError('Protected sealed-source journal has unsafe ownership or permissions.')
        except FileNotFoundError:
            if path == operation_path:
                continue
            raise DistributionError('Protected sealed-source capture journal is required before template conversion.')
    try:
        capture = json.loads(proof_path.read_text())
    except (OSError, ValueError) as exc:
        raise DistributionError('Protected sealed-source capture journal is unreadable.') from exc
    validate_sealed_windows_capture(capture)
    if capture.get('source_config_sha256') != '5e9fdb9337ec631b74100dc1c1f47ffdfb02f8873b32ee6119b49bc7cef27e9d':
        raise DistributionError('Protected source journal is not the fixed P100 sealed candidate.')

    def revalidate():
        fresh = capture_sealed_windows_source(api, lambda _record: None, proof_loader=proof_loader)
        if fresh != capture:
            raise DistributionError('Fresh source/probe proof differs from the protected capture journal.')
        config = api.request('GET', '/nodes/proxmox4/qemu/8912/config')
        status = api.request('GET', '/nodes/proxmox4/qemu/8912/status/current')
        if (status.get('status') != 'stopped' or
                _source_config_identity(config, 8912) != capture['source_config_sha256'] or
                not _sealed_windows_source_volumes(config) or
                set(str(config.get('tags', '')).split(';')) !=
                {'crange-image', 'crange-capture-windows-server2025-locale-20261008',
                 'mhl-windows-sealed-locale'} or int(config.get('template', 0)) not in (0, 1)):
            raise DistributionError('Fresh P100 source freeze does not match the captured sealed candidate.')
        return config

    config = revalidate()
    capture_sha = hashlib.sha256(proof_path.read_bytes()).hexdigest()
    if operation_path.exists():
        try:
            operation = json.loads(operation_path.read_text())
        except (OSError, ValueError) as exc:
            raise DistributionError('Template conversion journal is unreadable; do not resubmit.') from exc
        if (operation.get('job_id') != 'p100-sealed-windows-8912-template-conversion' or
                operation.get('source_capture_sha256') != capture_sha or
                operation.get('source_config_sha256') != capture['source_config_sha256']):
            raise DistributionError('Existing template conversion journal is outside the fixed source scope.')
        if operation.get('state') == 'template-ready':
            if int(config.get('template', 0)) != 1:
                raise DistributionError('Template-ready journal disagrees with the live source config.')
            return operation
        if operation.get('state') == 'conversion-intent':
            raise DistributionError('Conversion submission is uncertain without a provider task handle; reconcile before retry.')
        if operation.get('state') != 'conversion-submitted' or not operation.get('upid'):
            raise DistributionError('Existing conversion state is not safe to resume.')
    else:
        if int(config.get('template', 0)) != 0:
            raise DistributionError('Source is already a template without a matching conversion journal.')
        operation = {'state': 'conversion-intent',
            'job_id': 'p100-sealed-windows-8912-template-conversion', 'project': 100,
            'node': 'proxmox4', 'kind': 'vm', 'source_id': 8912,
            'source_capture_sha256': capture_sha,
            'source_config_sha256': capture['source_config_sha256'],
            'source_identity': None, 'created_at': time.time()}
        atomic_json(operation_path, operation, 0o600)
        try:
            upid = api.request('POST', '/nodes/proxmox4/qemu/8912/template')
        except Exception as exc:
            operation['state'] = 'submission-unknown'
            atomic_json(operation_path, operation, 0o600)
            raise DistributionError('Template conversion submission is uncertain; inspect PVE before any retry.') from exc
        if not isinstance(upid, str) or not upid.startswith('UPID:proxmox4:'):
            operation['state'] = 'submission-unknown'
            atomic_json(operation_path, operation, 0o600)
            raise DistributionError('Template conversion returned no valid PVE task handle; do not resubmit.')
        operation.update(state='conversion-submitted', upid=upid, submitted_at=time.time())
        atomic_json(operation_path, operation, 0o600)

    deadline = time.monotonic() + timeout
    task_path = f'/nodes/proxmox4/tasks/{urllib.parse.quote(operation["upid"], safe="")}/status'
    while True:
        task = api.request('GET', task_path)
        if task.get('status') == 'stopped':
            if task.get('exitstatus') != 'OK':
                operation.update(state='conversion-failed', task_status=task)
                atomic_json(operation_path, operation, 0o600)
                raise DistributionError('P100 template conversion task failed; do not resubmit automatically.')
            break
        if time.monotonic() >= deadline:
            operation['last_task_status'] = task
            atomic_json(operation_path, operation, 0o600)
            raise DistributionError('P100 template conversion remains in progress; reconcile its existing UPID.')
        time.sleep(poll_interval)
    config = revalidate()
    if int(config.get('template', 0)) != 1:
        raise DistributionError('Successful conversion task did not produce the expected template config.')
    operation.update(state='template-ready', task_status=task,
                     template_config_sha256=_source_config_identity(config, 8912),
                     completed_at=time.time())
    atomic_json(operation_path, operation, 0o600)
    return operation


PLATFORM_BASE_SOURCES = {
    ('proxmox4', 3014): {'name':'crange-base-ubuntu-server-24-04','kind':'vm','required_tags':{'crange-base','range-lan'}},
    ('proxmox4', 3015): {'name':'crange-base-debian-13','kind':'vm','required_tags':{'crange-base','range-lan'}},
    ('proxmox4', 3000): {'name':'crange-base-debian-13','kind':'container','required_tags':{'crange-base','range-lan'}},
    ('proxmox4', 3001): {'name':'crange-base-ubuntu-24-04','kind':'container','required_tags':{'crange-base','range-lan'}},
    ('proxmox4', 3002): {'name':'crange-base-alpine','kind':'container','required_tags':{'crange-base','range-lan'}},
    ('proxmox4', 112): {'name': 'parrot-jump-template', 'required_tags': {'range-lan', 'jumpbox'}},
    ('proxmox4', 115): {
        'name': 'crange-build-mhl-server-2025-eval',
        'required_tags': {'crange-template-build',
                          'crange-build-286a35040ed44d52b36fb30d097d771d',
                          'crange-image', 'mhl-windows-sealed'},
        'template': False, 'os_family': 'windows',
    },
}


def _approved_platform_source(config, node, vmid, kind):
    admitted = PLATFORM_BASE_SOURCES.get((node, vmid))
    name = config.get('hostname' if kind == 'container' else 'name')
    if (not admitted or admitted.get('kind', 'vm') != kind or name != admitted['name']
            or not admitted['required_tags'] <= set(str(config.get('tags', '')).split(';'))
            or int(config.get('template', 0)) != int(admitted.get('template', True))):
        raise DistributionError('Platform base source is outside the fixed administrator source map.')
    return admitted


def _generalize_platform_derivative(api, node, vmid, *, timeout=120):
    """Reset only Parrot clone identity on the fixed owned derivative.

    The command is constant and administrator-owned. It preserves the source
    account, authorized keys, and service configuration while making the
    published template regenerate machine and SSH host identities at first boot.
    """
    if node not in {f'proxmox{i}' for i in range(1,7)} or type(vmid) is not int or vmid < 100:
        raise DistributionError('Invalid fixed platform derivative scope.')
    _require_probe_pool(api, node, 'qemu', vmid)
    base = f'/nodes/{node}/qemu/{vmid}'
    config = api.request('GET', base + '/config')
    _owned(config)
    if int(config.get('template', 0)) or api.request('GET', base + '/status/current').get('status') != 'running':
        raise DistributionError('Platform identity cleanup requires the owned running derivative.')
    command = [
        '/bin/sh', '-c',
        'set -eu; command -v cloud-init >/dev/null 2>&1; '
        'cloud-init clean --logs; '
        'rm -f /etc/ssh/ssh_host_* /var/lib/dhcp/*leases* /var/lib/dbus/machine-id; '
        'truncate -s 0 /etc/machine-id; '
        'ln -sfn /etc/machine-id /var/lib/dbus/machine-id; '
        'test ! -s /etc/machine-id; '
        'test ! -e /etc/ssh/ssh_host_ed25519_key; '
        'test ! -e /etc/ssh/ssh_host_ed25519_key.pub; '
        'test -L /var/lib/dbus/machine-id',
    ]
    response = api.request('POST', base + '/agent/exec', {'command': command})
    try:
        pid = int(response['pid'])
    except (TypeError, KeyError, ValueError) as exc:
        raise DistributionError('QGA did not accept fixed platform identity cleanup.') from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = api.request('GET', base + '/agent/exec-status', {'pid': pid})
        if result.get('exited'):
            if int(result.get('exitcode', -1)) != 0:
                raise DistributionError('Fixed platform identity cleanup failed; derivative retained.')
            if len(str(result.get('out-data', '')).encode()) + len(str(result.get('err-data', '')).encode()) > 4096:
                raise DistributionError('Fixed platform identity cleanup returned oversized output.')
            if api.request('GET', base + '/status/current').get('status') != 'running':
                raise DistributionError('Platform derivative stopped during identity cleanup.')
            return {'state': 'generalized', 'machine_id_reset': True,
                    'ssh_host_keys_reset': True, 'authorized_keys_touched': False}
        time.sleep(1)
    raise DistributionError('Fixed platform identity cleanup timed out; derivative retained.')


def _generalize_windows_platform_derivative(api, node, vmid, *, expected_marker, identity_record, timeout=900):
    """Run the fixed local Windows Sysprep answer file on an owned derivative.

    The source Windows base is already generalized. This second pass prepares
    the healthy identity-captured derivative as the immutable image that the
    later source-probe will clone. Its OOBE answerfile is on the guest disk.
    """
    if (not re.fullmatch(r'proxmox[1-6]', str(node)) or type(vmid) is not int or vmid < 100 or
            not re.fullmatch(r'crange-capture-[a-zA-Z0-9-]{1,64}', str(expected_marker))):
        raise DistributionError('Invalid reviewed Windows capture derivative scope.')
    _require_probe_pool(api, node, 'qemu', vmid)
    base = f'/nodes/{node}/qemu/{vmid}'
    config = api.request('GET', base + '/config')
    _owned(config, expected_marker)
    identity_record = identity_record if isinstance(identity_record, dict) else {}
    from crange_catalog import validate_source_identity
    validate_source_identity(identity_record.get('source_identity'))
    if ((identity_record.get('node'), identity_record.get('source_id'), identity_record.get('kind')) !=
            (node, vmid, 'vm') or identity_record.get('state') != 'identity-captured' or
            identity_record.get('os_family') != 'windows' or identity_record.get('readiness') != 'windows-ready' or
            identity_record.get('source_config_sha256') != _source_config_identity(config, vmid) or
            int(config.get('template', 0)) or
            api.request('GET', base + '/status/current').get('status') != 'running'):
        raise DistributionError('Windows Sysprep requires the exact identity-captured running owned derivative.')
    command = [r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
        '-NoProfile', '-NonInteractive', '-Command',
        "$ErrorActionPreference='Stop';$f='C:\\ProgramData\\CRANGE\\clone-unattend.xml';"
        "if(-not(Test-Path -LiteralPath $f)){throw 'Fixed Windows clone answerfile missing'};"
        "if((Get-CimInstance Win32_ComputerSystem).PartOfDomain)"
        "{throw 'Never generalize a domain member or controller as a Windows base'};"
        "if(Get-Process -Name sysprep -ErrorAction SilentlyContinue)"
        "{throw 'A prior Sysprep process must be reconciled before capture'};"
        "[xml]$x=[IO.File]::ReadAllText($f);$n=New-Object Xml.XmlNamespaceManager($x.NameTable);"
        "$n.AddNamespace('u','urn:schemas-microsoft-com:unattend');"
        "$s=$x.SelectSingleNode('/u:unattend/u:settings[@pass=\"oobeSystem\"]',$n);"
        "if(-not $s){throw 'Windows clone OOBE settings missing'};"
        "$c=$s.SelectSingleNode('u:component[@name=\"Microsoft-Windows-International-Core\"]',$n);"
        "if(-not $c){$c=$x.CreateElement('component',$x.DocumentElement.NamespaceURI);"
        "foreach($a in @{name='Microsoft-Windows-International-Core';processorArchitecture='amd64';"
        "publicKeyToken='31bf3856ad364e35';language='neutral';versionScope='nonSxS'}.GetEnumerator())"
        "{$c.SetAttribute($a.Key,$a.Value)};[void]$s.AppendChild($c)};"
        "foreach($v in @('InputLocale','SystemLocale','UILanguage','UserLocale'))"
        "{$e=$c.SelectSingleNode('u:'+$v,$n);if(-not $e){$e=$x.CreateElement($v,$x.DocumentElement.NamespaceURI);"
        "[void]$c.AppendChild($e)};$e.InnerText='en-US'};"
        "$g=$x.SelectSingleNode('/u:unattend/u:settings[@pass=\"generalize\"]',$n);"
        "if(-not $g){$g=$x.CreateElement('settings',$x.DocumentElement.NamespaceURI);"
        "$g.SetAttribute('pass','generalize');[void]$x.DocumentElement.AppendChild($g)};"
        "$p=$g.SelectSingleNode('u:component[@name=\"Microsoft-Windows-Security-SPP\"]',$n);"
        "if(-not $p){$p=$x.CreateElement('component',$x.DocumentElement.NamespaceURI);"
        "foreach($a in @{name='Microsoft-Windows-Security-SPP';processorArchitecture='amd64';"
        "publicKeyToken='31bf3856ad364e35';language='neutral';versionScope='nonSxS'}.GetEnumerator())"
        "{$p.SetAttribute($a.Key,$a.Value)};[void]$g.AppendChild($p)};"
        "$skip=$p.SelectSingleNode('u:SkipRearm',$n);if(-not $skip){"
        "$skip=$x.CreateElement('SkipRearm',$x.DocumentElement.NamespaceURI);[void]$p.AppendChild($skip)};"
        "$skip.InnerText='1';"
        "$i=Get-Item -LiteralPath $f;$ro=$i.IsReadOnly;$i.IsReadOnly=$false;"
        "try{$x.Save($f)}finally{$i.IsReadOnly=$ro};"
        "$password=$s.SelectSingleNode('u:component[@name=\"Microsoft-Windows-Shell-Setup\"]/'"
        "+'u:UserAccounts/u:AdministratorPassword',$n);"
        "if(-not $password -or $password.PlainText -ne 'true' -or -not $password.Value)"
        "{throw 'Private Administrator clone credential is unavailable'};"
        "Set-LocalUser -Name 'Administrator' -Password (ConvertTo-SecureString $password.Value -AsPlainText -Force);"
        "$action=New-ScheduledTaskAction -Execute 'C:\\Windows\\System32\\Sysprep\\Sysprep.exe' "
        "-Argument '/generalize /oobe /shutdown /quiet /unattend:C:\\ProgramData\\CRANGE\\clone-unattend.xml';"
        "Register-ScheduledTask -TaskPath '\\CRANGE\\' -TaskName 'CaptureGeneralize' -Action $action "
        "-User ($env:COMPUTERNAME+'\\Administrator') -Password $password.Value -RunLevel Highest "
        "-Force|Out-Null;Start-ScheduledTask -TaskPath '\\CRANGE\\' -TaskName 'CaptureGeneralize';"
        "$until=(Get-Date).AddSeconds(30);$process=$null;while((Get-Date) -lt $until -and -not $process)"
        "{$process=Get-CimInstance Win32_Process|Where-Object {$_.Name -eq 'sysprep.exe'}|Select-Object -First 1;"
        "if(-not $process){Start-Sleep -Seconds 1}};"
        "if(-not $process){throw 'Administrator Sysprep task did not start'};"
        "$owner=Invoke-CimMethod -InputObject $process -MethodName GetOwner;"
        "if($owner.User -ne 'Administrator' -or $owner.Domain -ne $env:COMPUTERNAME)"
        "{throw 'Sysprep must run as the local Administrator, never SYSTEM'};"
        "$proof=[ordered]@{pid=$process.ProcessId;owner=$owner.User;domain=$owner.Domain;"
        "created_at_guest_utc=$process.CreationDate.ToUniversalTime().ToString('o')};"
        "Write-Output ('CRANGE_WINDOWS_SYSPREP_STARTED '+($proof|ConvertTo-Json -Compress))"]
    response = api.request('POST', base + '/agent/exec', {'command': command})
    try:
        pid = int(response['pid'])
    except (TypeError, KeyError, ValueError) as exc:
        raise DistributionError('QGA did not accept the fixed Windows Sysprep request.') from exc
    deadline = time.monotonic() + min(timeout, 120)
    launched = False
    principal_verified = False
    launch_evidence = None
    while time.monotonic() < deadline:
        try:
            result = api.request('GET', base + '/agent/exec-status', {'pid': pid})
        except Exception:
            # Sysprep may shut QGA down after PowerShell launched it. We still
            # require a provider-observed stopped state below.
            result = None
        if result and result.get('exited'):
            output = str(result.get('out-data', '')).strip()
            if int(result.get('exitcode', -1)) != 0 or not output.startswith('CRANGE_WINDOWS_SYSPREP_STARTED '):
                raise DistributionError('Fixed Windows Sysprep did not launch successfully.')
            try:
                launch_evidence = json.loads(output.split(' ', 1)[1])
                if (type(launch_evidence.get('pid')) is not int or launch_evidence['pid'] <= 0 or
                        launch_evidence.get('owner') != 'Administrator' or not launch_evidence.get('domain') or
                        not re.fullmatch(r'[0-9]{4}-[0-9T:.-]+Z', launch_evidence.get('created_at_guest_utc', ''))):
                    raise ValueError('Invalid Administrator launch evidence.')
            except (TypeError, ValueError, AttributeError) as exc:
                raise DistributionError('Windows Sysprep launch lacks actual user/process evidence.') from exc
            launched = True
            principal_verified = True
            break
        if api.request('GET', base + '/status/current').get('status') == 'stopped':
            launched = True
            break
        time.sleep(1)
    if not launched:
        raise DistributionError('Fixed Windows Sysprep launch did not complete; derivative retained.')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if api.request('GET', base + '/status/current').get('status') == 'stopped':
            return {'state': 'sysprep-shutdown-observed', 'os_family': 'windows',
                    'answerfile': 'C:\\ProgramData\\CRANGE\\clone-unattend.xml',
                    'shutdown_observed': True, 'sysprep_principal': 'local-Administrator',
                    'sysprep_principal_verified': principal_verified, 'launch_evidence': launch_evidence,
                    'licensing_state_preserved': principal_verified,
                    'offline_fresh_success_verification_required': True}
        time.sleep(2)
    raise DistributionError('Windows Sysprep did not shut down the owned derivative; it was retained.')


def capture_policy(node, profile, policy_override=None, *, kind="vm"):
    policy = policy_override if policy_override is not None else read_settings()[0]
    if kind not in ('vm', 'container'):
        raise DistributionError('Unsupported release capture guest kind.')
    if kind == 'container' and profile.get('init_system', 'systemd') not in ('systemd', 'openrc'):
        raise DistributionError('Container capture requires a supported Linux readiness profile.')
    if kind == 'vm' and not profile.get('agent_enabled'):
        raise DistributionError('Release capture requires a tested approved QGA-enabled VM profile; rebuild staging with that profile first.')
    if not policy.get('enabled') or node not in policy.get('allowed_nodes', []):
        raise DistributionError('Release capture node is not enabled by administrator policy.')
    if not workload_node_allowed(policy, node, 'capture:probe', kind):
        raise DistributionError('Release capture destination is reserved for router containers.')
    bridge = policy.get('capture_isolated_bridge')
    if bridge not in policy.get('isolated_bridges', []) or not re.fullmatch(r'[A-Za-z0-9_-]{1,15}', str(bridge)):
        raise DistributionError('Configure an approved capture_isolated_bridge before capture.')
    storage = policy.get('capture_datastore')
    preferred=profile.get('_capture_datastores',{}).get(node)
    if preferred and (node,preferred) in set(map(tuple,policy.get('approved_storage',[]))):
        storage=preferred
    if storage not in ('local-lvm', 'ssd-lvm', 'hdd-lvm', 'crange-thin', 'local-hdd'):
        raise DistributionError('Configure approved capture_datastore before capture.')
    snapshots = policy.get('nodes', {}).get(node, {}).get('snapshot_storages', [])
    needs_snapshot = kind == 'container' and not profile.get('_full_clone_probe')
    if ((node, storage) not in set(map(tuple, policy.get('approved_storage', []))) or
            (needs_snapshot and storage not in snapshots)):
        alternatives = [value for value in policy.get('allowed_storages', [])
                        if value in ('local-lvm', 'ssd-lvm', 'hdd-lvm', 'crange-thin', 'local-hdd')
                        and (not needs_snapshot or value in snapshots)
                        and (node, value) in set(map(tuple, policy.get('approved_storage', [])))]
        if not alternatives:
            raise DistributionError('Configure approved capture_datastore before capture.')
        storage = alternatives[0]
    backup = policy.get('capture_backup_storage')
    if (node, backup) not in set(map(tuple, policy.get('approved_backup_storage', []))):
        raise DistributionError('Configure approved capture_backup_storage before capture.')
    return policy, bridge, storage, backup


def _capture_destination(api, source_node, source, profile, policy, *, kind='vm'):
    """Choose approved node/storage headroom and record the transient profile."""
    from crange_placement import capacity_window
    inventory = discover(api, policy)
    capacity = _source_capacity(source, 'lxc' if kind=='container' else 'qemu')
    if profile.get('_transport_template'):
        capacity['disk_gb'] *= 2
    ledger = ReservationLedger(DATA / 'reservations.sqlite3')
    try:
        pending = ledger.active_reservations()
    finally:
        ledger.close()
    preferred = profile.get('_capture_preferred_nodes', [])
    if (not isinstance(preferred, list) or any(not isinstance(node, str) for node in preferred) or
            len(preferred) != len(set(preferred)) or
            any(node not in {f'proxmox{i}' for i in range(1, 7)} for node in preferred)):
        raise DistributionError('Invalid administrator capture node preferences.')
    choices, rejected = [], {}
    for candidate in policy.get('allowed_nodes', []):
        try:
            _, _, storage, _ = capture_policy(candidate, profile, policy,kind=kind)
            host = inventory['nodes'][candidate]
            if not host.get('online') or host.get('draining') or host.get('load', 0) > policy.get('max_load', 1):
                rejected[candidate] = 'offline, draining or above the load limit'
                continue
            if candidate != source_node and kind=='vm':
                cpu = str(source.get('cpu', 'kvm64')).split(',')[0].removeprefix('cputype=')
                if cpu not in ('kvm64', 'qemu64') and cpu not in host.get('features', []):
                    if cpu != 'x86-64-v2-AES':
                        rejected[candidate] = 'source CPU model is not verified on this destination'
                        continue
                    flags = set(api.request('GET', f'/nodes/{candidate}/status').get('cpuinfo', {}).get('flags', '').split())
                    if not {'ssse3', 'sse4_1', 'sse4_2', 'popcnt', 'pni', 'cx16', 'lahf_lm', 'aes'} <= flags:
                        rejected[candidate] = 'destination lacks the source CPU instruction set'
                        continue
            storages=[storage]+sorted({sid for node,sid in policy.get('approved_storage',[])
                                      if node==candidate and sid!=storage})
            rejected[candidate]={}
            for index,storage in enumerate(storages):
                trial={**profile,'_capture_datastores':{**profile.get('_capture_datastores',{}),candidate:storage}}
                try:
                    if capture_policy(candidate,trial,policy,kind=kind)[2]!=storage:
                        continue
                    datastore=host['storages'][storage]
                    if not datastore.get('available') or kind not in datastore.get('guest_types',[]):
                        rejected[candidate][storage]='capture datastore unavailable or incompatible'
                        continue
                    window=capacity_window(host,policy,candidate,datastore,'full')
                    available={key:window[key]-sum(r[key] for r in pending
                        if r['node_name']==candidate and (key!='disk_gb' or r['datastore_id']==storage)) for key in capacity}
                    if any(available[key]<value for key,value in capacity.items()):
                        rejected[candidate][storage]={key:{'required':round(value,2),'available':round(available[key],2)}
                            for key,value in capacity.items() if available[key]<value}
                        continue
                    rank=preferred.index(candidate) if candidate in preferred else len(preferred)
                    choices.append((candidate!=source_node,rank,index,host.get('load',0),candidate,storage))
                except (ValueError,KeyError) as exc:
                    rejected[candidate][storage]=str(exc)
        except (ValueError, KeyError) as exc:
            rejected[candidate] = str(exc)
            continue
    if not choices:
        raise DistributionError('No approved node can host a full isolated capture copy: ' + json.dumps(rejected, sort_keys=True))
    node,storage=min(choices)[-2:]
    profile.setdefault('_capture_datastores',{})[node]=storage
    return node


def _transport_template_id(api, receipt, save):
    """Reserve a separate ID for a never-booted temporary transport template."""
    transport=receipt['transport']
    if transport.get('template_id'):
        raise DistributionError('Retained transport template needs exact reconciliation; no second restore.')
    resources=api.request('GET','/cluster/resources',{'type':'vm'})
    if not isinstance(resources,list):raise DistributionError('Transport allocation inventory is incomplete.')
    used={int(row['vmid']) for row in resources}
    for path in (DATA/'capture-journal').glob('*.json'):
        previous=json.loads(path.read_text());copy=previous.get('transport',{})
        if copy.get('template_id') and not copy.get('template_retired'):
            used.add(int(copy['template_id']))
    for vmid in range(21000,22000):
        if vmid not in used and int(api.request('GET','/cluster/nextid',{'vmid':vmid}))==vmid:
            transport['template_id']=vmid;save();return vmid
    raise DistributionError('No unused temporary transport-template ID is available.')


def _clone_transport_template(api, *, node, kind, source_id, target_id, storage, receipt, save, task):
    """Full-clone a restored immutable template; never boot or unseal it."""
    base=f'/nodes/{node}/{kind}/{source_id}'
    marker=receipt['marker']+'-transport'
    config=api.request('GET',base+'/config')
    if int(config.get('template',0))!=1 or config.get('lock') or api.request('GET',base+'/status/current').get('status')!='stopped':
        raise DistributionError('Restored transport is not a stopped immutable template.')
    _require_probe_pool(api,node,kind,source_id)
    tags=f'crange-image;{marker};p{int(receipt["reservation"]["project_id"]):03d}'
    api.request('PUT',base+'/config',{'tags':tags})
    config=api.request('GET',base+'/config');_owned(config,marker)
    fingerprint=_source_config_identity(config,source_id)
    fields={'newid':target_id,'full':1,'storage':storage,'pool':PROBE_POOL,
            'name' if kind=='qemu' else 'hostname':receipt['capture_name']}
    task('POST',base+'/clone',fields)
    target=f'/nodes/{node}/{kind}/{target_id}'
    derivative=api.request('GET',target+'/config')
    _owned(derivative,marker);_require_probe_pool(api,node,kind,target_id)
    if int(derivative.get('template',0)) or derivative.get('lock') or api.request('GET',target+'/status/current').get('status')!='stopped':
        raise DistributionError('Transport derivative must be a stopped regular guest before isolation.')
    volumes=[]
    for key,value in derivative.items():
        if re.fullmatch(r'(?:scsi|sata|virtio|ide|efidisk|tpmstate)[0-9]+|rootfs',key) and ',media=cdrom' not in str(value):
            volumes.append(str(value).split(',')[0])
    if not volumes or any(not re.fullmatch(re.escape(storage)+rf':vm-{target_id}-(?:disk-[0-9]+|cloudinit)',value) for value in volumes):
        raise DistributionError('Full-clone derivative volumes are not exclusively owned by its target ID.')
    # Keep all NICs disconnected until the normal capture isolation step.
    networks={key:(re.sub(r'(?:^|,)link_down=[01]', '', str(value))+',link_down=1')
              for key,value in derivative.items() if re.fullmatch(r'net[0-9]+',key)}
    if not networks:raise DistributionError('Transport derivative has no declared network interfaces.')
    api.request('PUT',target+'/config',{'tags':f'crange-image;{receipt["marker"]};p{int(receipt["reservation"]["project_id"]):03d}',
                                     'onboot':0,**networks})
    current=api.request('GET',base+'/config');_owned(current,marker)
    if _source_config_identity(current,source_id)!=fingerprint:
        raise DistributionError('Temporary template changed during full clone; retaining both allocations.')
    receipt['transport']['derivative_ready']=True;save()
    task('DELETE',base,{'purge':1})
    if any(int(row['vmid'])==source_id for row in api.request('GET','/cluster/resources',{'type':'vm'})):
        raise DistributionError('Temporary transport-template deletion was not confirmed.')
    receipt['transport']['template_retired']=True;save()
    print(f'Created runnable full clone {node}/{target_id}; removed its temporary transport template.',flush=True)


def _capture_transport(api, *, source_node, source_id, target_node, target_id,
                       storage, backup, project, receipt, save, task, platform_source=False):
    """Frozen transport archive, not a published or approved logical image."""
    source = api.vm(source_node, source_id)
    if platform_source:
        _approved_platform_source(source, source_node, source_id, 'vm')
    if (api.status(source_node, source_id) != 'stopped' or (not platform_source and
            (f'p{project:03d}' not in str(source.get('tags', '')).split(';') or int(source.get('template', 0))))):
        raise DistributionError('Remote capture transport requires its owned stopped staging source.')
    content_path = f'/nodes/{source_node}/storage/{backup}/content'
    if receipt.get('transport'):
        # This branch is reached only through the exact receipt recovery gate.
        before = set(receipt['transport']['content_before'])
        print('Resuming the completed frozen transport backup; no new backup submitted', flush=True)
    else:
        before = {row['volid'] for row in api.request('GET', content_path)}
        receipt['transport'] = {'state': 'exporting', 'source_node': source_node,
                                'source_id': source_id, 'content_before': sorted(before)}
        save()
        print(f'Backing up stopped source for transport: {source_node}/{source_id}', flush=True)
        task('POST', f'/nodes/{source_node}/vzdump',
             {'vmid': source_id, 'storage': backup, 'mode': 'stop', 'compress': 'zstd',
              'remove': 0, 'stdexcludes': 0, 'notes-template': 'CRANGE capture transport ' + receipt['job_id']},
             task_node=source_node)
    if api.status(source_node, source_id) != 'stopped':
        raise DistributionError('Staging source restarted during capture transport.')
    archives = [row['volid'] for row in api.request('GET', content_path)
                if row['volid'] not in before and row.get('content') == 'backup'
                and int(row.get('vmid', -1)) == source_id]
    if len(archives) != 1:
        raise DistributionError('Capture transport archive is ambiguous; retain journal for reconciliation.')
    archive = archives[0]
    print('Verifying transport archive checksum', flush=True)
    digest = SSHVolumeHasher()(source_node, archive)
    if not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise DistributionError('Capture transport archive checksum is invalid.')
    if receipt['transport'].get('state') == 'verified' and (
            receipt['transport'].get('volume_id') != archive or receipt['transport'].get('artifact_sha256') != digest):
        raise DistributionError('Previously verified transport archive changed; retain the capture journal.')
    receipt['transport'].update(state='verified', volume_id=archive, artifact_sha256=digest)
    save()
    restored_id=_transport_template_id(api,receipt,save) if platform_source else target_id
    print(f'Restoring stopped capture copy: {target_node}/{restored_id}', flush=True)
    task('POST', f'/nodes/{target_node}/qemu',
         {'vmid': restored_id, 'archive': archive, 'storage': storage, 'pool': PROBE_POOL,
          'unique': 1, 'start': 0, 'name': receipt['capture_name']})
    if api.status(target_node, restored_id) != 'stopped':
        raise DistributionError('Remote capture copy was unexpectedly started before isolation.')
    if platform_source:
        _clone_transport_template(api,node=target_node,kind='qemu',source_id=restored_id,
            target_id=target_id,storage=storage,receipt=receipt,save=save,task=task)


def _wait_capture_probe_load(api, node, policy, *, timeout=300):
    """Let backup I/O settle without bypassing the scheduler's load limit."""
    import math
    if node not in policy.get('allowed_nodes', []) or policy.get('nodes', {}).get(node, {}).get('draining'):
        raise DistributionError('Capture probe node is excluded or draining.')
    deadline = time.monotonic() + timeout
    while True:
        status = api.request('GET', f'/nodes/{node}/status')
        cpus = int(status['cpuinfo']['cpus'])
        load = float(status['loadavg'][0]) / max(1, cpus)
        limit = float(policy.get('max_load', 1))
        if cpus < 1 or not math.isfinite(load) or not math.isfinite(limit) or limit <= 0:
            raise DistributionError('Capture probe node load evidence is invalid.')
        if load <= limit:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DistributionError('Capture node load did not settle within five minutes; verified image retained for retry.')
        print(f'Waiting for {node} backup I/O to settle before clone verification: load {load:.2f}, limit {limit:.2f}',flush=True)
        time.sleep(min(15,remaining))


def _container_transport(api, *, source_node, source_id, node, vmid, storage, backup, project, receipt, save, task, platform_source=False):
    source=api.container(source_node,source_id)
    if platform_source:
        _approved_platform_source(source, source_node, source_id, 'container')
    if (api.container_status(source_node,source_id)!='stopped' or (not platform_source and
            (f'p{project:03d}' not in str(source.get('tags','')).split(';') or int(source.get('template',0))))):
        raise DistributionError('Container transport requires its owned stopped staging source.')
    _source_capacity(source,'lxc')
    content=f'/nodes/{source_node}/storage/{backup}/content'
    if receipt.get('transport'):
        before=set(receipt['transport']['content_before'])
        print('Reusing verified frozen container transport backup; no new backup.',flush=True)
    else:
        before={row['volid'] for row in api.request('GET',content)}
        receipt['transport']={'state':'exporting','source_node':source_node,'source_id':source_id,
                              'content_before':sorted(before)};save()
        print(f'Backing up stopped container for transport: {source_node}/{source_id}',flush=True)
        task(f'/nodes/{source_node}/vzdump',{'vmid':source_id,'storage':backup,'mode':'stop','compress':'zstd',
             'remove':0,'stdexcludes':0,'notes-template':'CRANGE capture transport '+receipt['job_id']},task_node=source_node)
    if api.container_status(source_node,source_id)!='stopped':
        raise DistributionError('Container source restarted during transport.')
    archives=[row['volid'] for row in api.request('GET',content) if row['volid'] not in before and
              row.get('content')=='backup' and int(row.get('vmid',-1))==source_id]
    if len(archives)!=1:
        raise DistributionError('Container transport archive is ambiguous; retain its journal.')
    digest=SSHVolumeHasher()(source_node,archives[0])
    if not re.fullmatch(r'[a-f0-9]{64}',str(digest)):
        raise DistributionError('Container transport archive checksum is invalid.')
    if receipt['transport'].get('state')=='verified' and (receipt['transport'].get('volume_id')!=archives[0] or receipt['transport'].get('artifact_sha256')!=digest):
        raise DistributionError('Frozen container transport archive changed; refusing restore.')
    receipt['transport'].update(state='verified',volume_id=archives[0],artifact_sha256=digest);save()
    restored_id=_transport_template_id(api,receipt,save) if platform_source else vmid
    print(f'Restoring stopped container capture: {node}/{restored_id}',flush=True)
    networks={}
    bridge=read_settings()[0]['capture_isolated_bridge']
    for key,value in source.items():
        if re.fullmatch(r'net[0-9]+',key):
            fields=dict(part.split('=',1) for part in str(value).split(',') if '=' in part)
            if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_.-]{0,14}',fields.get('name','')):
                raise DistributionError('Container source NIC name is invalid.')
            mac='02:'+':'.join(hashlib.sha256(f'{receipt["job_id"]}:{vmid}:{key}'.encode()).hexdigest()[i:i+2] for i in range(0,10,2))
            networks[key]=f'name={fields["name"]},type=veth,hwaddr={mac},bridge={bridge},ip=manual,ip6=manual,link_down=1'
    if not networks:raise DistributionError('Container transport requires declared NICs.')
    task(f'/nodes/{node}/lxc',{'vmid':restored_id,'ostemplate':archives[0],'restore':1,'storage':storage,
         'pool':PROBE_POOL,'hostname':receipt['capture_name'],'unprivileged':1,'unique':0,'start':0,'onboot':0,**networks})
    if api.container_status(node,restored_id)!='stopped':
        raise DistributionError('Restored container started before isolation.')
    if platform_source:
        _clone_transport_template(api,node=node,kind='lxc',source_id=restored_id,target_id=vmid,
            storage=storage,receipt=receipt,save=save,
            task=lambda method,path,fields:task(path,fields,method=method))


def _restored_network_matches(nets, source_nets):
    if not nets or nets.keys() != source_nets.keys():
        return False
    for key, value in nets.items():
        actual, original = value.split(','), source_nets[key].split(',')
        if (not re.fullmatch(r'[a-z0-9]+=(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}', actual[0]) or
                actual[0].split('=')[0] != original[0].split('=')[0] or actual[1:] != original[1:]):
            return False
    return True


def _prepare_capture_network(api, node, vmid, fields, marker, bridge, *, carrier=False):
    """Establish owned, disconnected NICs before requesting host isolation proof."""
    base = f'/nodes/{node}/qemu/{vmid}'
    api.request('PUT', base + '/config', fields)
    config = api.vm(node, vmid)
    _owned(config, marker)
    _require_probe_pool(api, node, 'qemu', vmid)
    nets = {key: str(value) for key, value in config.items() if re.fullmatch(r'net[0-9]+', key)}
    if not nets or any(f'bridge={bridge}' not in value.split(',') or 'link_down=1' not in value.split(',') for value in nets.values()):
        raise DistributionError('Disconnected capture network was not applied exactly.')
    if carrier:
        private_bridge_request(node, vmid=vmid, marker=marker)
        api.request('PUT', base + '/config', {key: value.replace('link_down=1', 'link_down=0') for key, value in nets.items()})


def private_bridge_request(node, *, vmid=None, marker=None, cache_job_id=None):
    request = {'command':'capture-bridge-prepare' if vmid is None else 'capture-bridge-proof','node':node}
    if vmid is not None:request.update(vmid=vmid,marker=marker)
    if cache_job_id is not None:request["cache_job_id"]=cache_job_id
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
        client.settimeout(60);client.connect('/run/crange-ct-read/socket')
        client.sendall(json.dumps(request).encode()+b'\n');raw=b''
        while not raw.endswith(b'\n') and len(raw)<8192:
            part=client.recv(8192-len(raw))
            if not part:break
            raw+=part
    response=json.loads(raw)
    if set(response)!={'value'} or not isinstance(response['value'],str):
        raise DistributionError('Private capture bridge preparation/proof was rejected.')
    result=json.loads(response['value'])
    if result.get('bridge')!='crprobe1450' or result.get('verified') is not True or result.get('host_and_routed_traffic_blocked') is not True:
        raise DistributionError('Private capture network is not verified isolated.')
    return result


def inspect_container_capture(api,*,project,version,machine,cfg,outputs):
    from crange_distribution import SSHContainerExec
    box=cfg.get('scenario_containers',{}).get(machine)
    if not isinstance(box,dict) or str(box.get('team_id'))!='254':
        raise DistributionError('Choose a declared staging container.')
    slug=hashlib.sha256(f'{project}:{version}:{machine}'.encode()).hexdigest()[:12]
    file=DATA/'capture-journal'/f'capture-p{project:03d}-{slug}.json'
    receipt=json.loads(file.read_text());node,vmid=receipt.get('node'),receipt.get('template_id')
    live=outputs.get('scenario_containers',{}).get('value',{}).get(machine,{})
    marker=f'crange-capture-{slug}';policy=read_settings()[0]
    if (node not in policy.get('allowed_nodes',[]) or type(vmid) is not int or vmid==live.get('id') or
            receipt.get('source_id')!=live.get('id') or receipt.get('marker')!=marker or receipt.get('state')!='allocated'):
        raise DistributionError('Container diagnostic copy differs from its applied source.')
    with node_locks([node],library=True):
        config=api.container(node,vmid);_owned(config,marker);_require_probe_pool(api,node,'lxc',vmid)
        nets=[str(value) for key,value in config.items() if re.fullmatch(r'net[0-9]+',key)]
        active=api.request('GET',f'/nodes/{node}/tasks',{'source':'active','limit':1000})
        if (int(config.get('template',0)) or config.get('lock') or api.container_status(node,vmid)!='stopped' or
                not nets or any(f'bridge={policy["capture_isolated_bridge"]}' not in net.split(',') or 'link_down=1' not in net.split(',') for net in nets) or
                len(active)>=1000 or any(str(task.get('id'))==str(vmid) for task in active)):
            raise DistributionError('Container inspection requires its stopped isolated copy and terminal tasks.')
        api.container_start(node,vmid)
        try:
            reader=SSHContainerExec(api=api)
            deadline=time.monotonic()+60
            while True:
                state=reader(node,vmid,['/usr/bin/systemctl','is-system-running']).strip()
                if state not in ('initializing','starting') or time.monotonic()>=deadline:break
                time.sleep(2)
            result={'system_state':state,'failed_services':reader(node,vmid,['/usr/bin/systemctl','--failed','--no-pager','--no-legend'])}
            if 'postgresql@15-main' in result['failed_services']:
                result['postgres_status']=reader(node,vmid,['/usr/bin/systemctl','status','postgresql@15-main.service','--no-pager','--lines=15'])
            receipt['inspection_evidence']=result;atomic_json(file,receipt)
            return result
        finally:
            _owned(api.container(node,vmid),marker);_require_probe_pool(api,node,'lxc',vmid)
            api.container_stop(node,vmid)


def inspect_capture(api, *, project, version, machine, path):
    """Fixed diagnostics for a retained Windows capture, never the live source."""
    from crange import json_config, output_json
    from crange_windows_guest_initialize import _wait_for_agent
    from crange_distribution import _fixed_guest_read
    cfg, outputs = json_config(path), output_json(path)
    if machine in cfg.get('scenario_containers',{}) and machine not in cfg.get('scenario_boxes',{}):
        return inspect_container_capture(api,project=project,version=version,machine=machine,cfg=cfg,outputs=outputs)
    box = cfg.get('scenario_boxes', {}).get(machine)
    if not isinstance(box, dict) or str(box.get('team_id')) != '254':
        raise DistributionError('Choose a declared staging VM for capture inspection.')
    slug = hashlib.sha256(f'{project}:{version}:{machine}'.encode()).hexdigest()[:12]
    receipt_path = DATA / 'capture-journal' / f'capture-p{project:03d}-{slug}.json'
    if not receipt_path.exists():
        raise DistributionError('This staging VM has no retained capture for that version.')
    receipt = json.loads(receipt_path.read_text())
    node, vmid = receipt.get('node'), receipt.get('template_id')
    live = outputs.get('scenario_boxes', {}).get('value', {}).get(machine, {})
    policy = read_settings()[0]
    if (node not in policy.get('allowed_nodes', []) or type(vmid) is not int or
            receipt.get('source_id') != live.get('id') or vmid == live.get('id') or
            receipt.get('state') != 'allocated' or receipt.get('marker') != f'crange-capture-{slug}'):
        raise DistributionError('Capture inspection receipt differs from its applied source or scope.')
    with node_locks([node], library=True):
        copy = api.vm(node, vmid)
        bridge = policy.get('capture_isolated_bridge')
        nets = [value for key, value in copy.items() if re.fullmatch(r'net[0-9]+', key)]
        active = api.request('GET', f'/nodes/{node}/tasks', {'source': 'active', 'limit': 1000})
        if (copy.get('name') != receipt.get('capture_name') or int(copy.get('template', 0)) or not nets or
                set(str(copy.get('tags', '')).split(';')) != {'crange-image', f'crange-capture-{slug}', f'p{project:03d}'} or
                any(f'bridge={bridge}' not in str(net).split(',') or not {'link_down=0','link_down=1'}.intersection(str(net).split(',')) for net in nets) or
                api.status(node, vmid) != 'stopped' or not isinstance(active, list) or len(active) >= 1000 or
                any(str(task.get('id')) == str(vmid) for task in active)):
            raise DistributionError('Inspection requires the owned stopped isolated capture with no active task.')
        _require_probe_pool(api, node, 'qemu', vmid)
        private_bridge_request(node,vmid=vmid,marker=f'crange-capture-{slug}')
        original_nets={key:value for key,value in copy.items() if re.fullmatch(r'net[0-9]+',key)}
        api.request('PUT',f'/nodes/{node}/qemu/{vmid}/config',
                    {key:str(value).replace('link_down=1','link_down=0') for key,value in original_nets.items()})
        upid = api.request('POST', f'/nodes/{node}/qemu/{vmid}/status/start', {})
        receipt.setdefault('diagnostic_tasks', []).append({'upid': upid, 'operation': 'start'})
        atomic_json(receipt_path, receipt)
        try:
            api.task(node, upid, timeout=180)
            _wait_for_agent(api, f'/nodes/{node}/qemu/{vmid}/agent', 180)
            script = r"""$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
$cs=Get-CimInstance Win32_ComputerSystem
$ds=[ordered]@{ok=$false;error_id=$null}
if([int]$cs.DomainRole -ge 4){try{$dse=Get-ADRootDSE -Server localhost;$ds.ok=$true;$ds.service_name=$dse.dsServiceName}catch{$ds.error_id=$_.FullyQualifiedErrorId}}
$boot=[ordered]@{}
$event=@(Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Microsoft-Windows-Kernel-Boot';ID=20} -MaxEvents 1 -ErrorAction SilentlyContinue)
if($event.Count -eq 1){[xml]$xml=$event[0].ToXml();$boot.time=$event[0].TimeCreated.ToUniversalTime().ToString('o');foreach($item in $xml.Event.EventData.Data){if($item.Name -in @('LastShutdownGood','LastBootGood','LastBootId','BootId')){$boot[$item.Name]=[string]$item.'#text'}}}
[ordered]@{computer=$cs.Name;domain=$cs.Domain;role=[int]$cs.DomainRole;directory=$ds;boot=$boot;
services=@(Get-Service NTDS,ADWS,DNS,QEMU-GA,WinRM,TermService -ErrorAction SilentlyContinue|Select-Object Name,@{n='state';e={[string]$_.Status}});
adapters=@(Get-NetAdapter|Select-Object Name,Status);
addresses=@(Get-NetIPAddress -AddressFamily IPv4|Select-Object IPAddress,AddressState)}|ConvertTo-Json -Depth 5 -Compress"""
            result = _fixed_guest_read(api, node, 'qemu', vmid,
                [r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe','-NoProfile','-NonInteractive','-Command',script], None, 90)
            parsed=json.loads(result)
            receipt['inspection_evidence']=parsed
            atomic_json(receipt_path,receipt)
            return parsed
        finally:
            if api.status(node, vmid) == 'running':
                stop = api.request('POST', f'/nodes/{node}/qemu/{vmid}/status/shutdown', {'timeout':300,'forceStop':0})
                receipt['diagnostic_tasks'].append({'upid':stop,'operation':'shutdown'})
                atomic_json(receipt_path, receipt)
                api.task(node, stop, timeout=330)
            api.request('PUT',f'/nodes/{node}/qemu/{vmid}/config',original_nets)


def _portable_capture_machine(api, node, config):
    machine = str(config.get('machine', 'pc'))
    model, separator, options = machine.partition(',')
    model = model.removeprefix('type=')
    family = 'q35' if model == 'q35' or model.startswith('pc-q35-') else 'i440fx' if model in ('pc', 'i440fx') or model.startswith('pc-i440fx-') else None
    if family is None:
        raise DistributionError('Capture cannot normalize an unknown machine family.')
    capabilities = api.request('GET', f'/nodes/{node}/capabilities/qemu/machines')
    baseline = f'pc-{family}-9.2+pve1'
    if not isinstance(capabilities, list) or not any(row.get('id') == baseline and row.get('type') == family for row in capabilities):
        raise DistributionError('Destination lacks the approved portable machine baseline.')
    return baseline + (separator + options if separator else '')


def _verified_capture_probe(receipt, identity, project):
    probe = json.loads((DATA/'source-journal'/(receipt['probe_job']+'.json')).read_text())
    tasks = probe.get('provider_tasks', [])
    kind=receipt.get('kind','vm')
    api_kind='lxc' if kind=='container' else 'qemu'
    if (probe.get('state') != 'source-probed' or probe.get('source_node') != receipt['node'] or
            probe.get('source_id') != receipt['template_id'] or probe.get('kind') != kind or
            probe.get('source_config_sha256') != identity.get('source_config_sha256') or
            probe.get('source_capture_job_id') != identity.get('job_id') or
            probe.get('verification', {}).get('identity_unique') is not True or
            probe.get('verification', {}).get('passed') is not True or not tasks or
            any(task.get('state') != 'confirmed-terminal' for task in tasks) or
            tasks[-1].get('path') != f"/nodes/{receipt['node']}/{api_kind}/{probe.get('probe_id')}" or
            probe.get('probe_id') == receipt['template_id']):
        raise DistributionError('Completed capture probe identity/cleanup evidence differs.')
    ledger = ReservationLedger(DATA/'reservations.sqlite3')
    try:
        saved = ledger.get_job(receipt['probe_job'])
        if saved.get('state') != 'released' or saved.get('project_id') != project or saved.get('allocations') != [probe.get('reservation')]:
            raise DistributionError('Completed capture probe reservation is not reconciled.')
    finally:
        ledger.close()


def _recover_inspected_boot_readiness(api, receipt, config, project, bridge):
    """Permit fresh identity collection after a proven clean diagnostic boot."""
    node, vmid = receipt['node'], receipt['template_id']
    inspection = receipt.get('inspection_evidence', {})
    boot = inspection.get('boot', {})
    diagnostics = receipt.get('diagnostic_tasks', [])
    nets = [str(value) for key, value in config.items() if re.fullmatch(r'net[0-9]+', key)]
    services = {row.get('Name'): row.get('state') for row in inspection.get('services', [])}
    if (api.status(node, vmid) != 'stopped' or int(config.get('template', 0)) or config.get('lock') or
            config.get('name') != receipt.get('capture_name') or
            set(str(config.get('tags', '')).split(';')) != {'crange-image', receipt['marker'], f'p{project:03d}'} or
            not nets or any(f'bridge={bridge}' not in net.split(',') or
                not {'link_down=0','link_down=1'}.intersection(net.split(',')) for net in nets) or
            str(boot.get('LastShutdownGood', '')).lower() != 'true' or
            str(boot.get('LastBootGood', '')).lower() != 'true' or
            inspection.get('domain') != 'mhl.test' or inspection.get('role') not in (3, 4, 5) or
            any(services.get(name) != 'Running' for name in ('QEMU-GA','WinRM','TermService')) or
            (inspection.get('role') in (4,5) and (services.get('NTDS') != 'Running' or
                inspection.get('directory',{}).get('ok') is not True)) or
            not diagnostics or len(diagnostics)%2 or len(diagnostics)>10):
        raise DistributionError('Retained capture lacks an exact clean owned diagnostic boot.')
    _require_probe_pool(api, node, 'qemu', vmid)
    private_bridge_request(node, vmid=vmid, marker=receipt['marker'])
    for index, task in enumerate(diagnostics):
        operation = 'start' if index%2 == 0 else 'shutdown'
        task_type = 'qmstart' if operation == 'start' else 'qmshutdown'
        if task.get('operation') != operation or f':{task_type}:{vmid}:' not in str(task.get('upid','')):
            raise DistributionError('Diagnostic history differs from the exact retained capture.')
        api.task(node, task['upid'], timeout=30)


def _recover_inspected_shutdown(api, receipt, identity, config, project, bridge):
    """Accept a timeout only after a separate owned inspection shut down cleanly."""
    from crange_windows_snapshot import validate_captured_identity
    node,vmid=receipt['node'],receipt['template_id']
    validate_captured_identity(identity.get('source_identity'))
    source=identity['source_identity'];inspection=receipt.get('inspection_evidence',{})
    diagnostics=receipt.get('diagnostic_tasks',[])
    nets=[str(value) for key,value in config.items() if re.fullmatch(r'net[0-9]+',key)]
    if (source.get('identity_mode')!='windows-scenario-snapshot-v1' or source.get('snapshot_project_id')!=project or
            identity.get('state')!='identity-captured' or identity.get('kind')!='vm' or
            identity.get('node')!=node or identity.get('source_id')!=vmid or
            identity.get('source_config_sha256')!=_source_config_identity(config,vmid) or
            config.get('name')!=receipt['capture_name'] or int(config.get('template',0)) or
            set(str(config.get('tags','')).split(';')) != {'crange-image',receipt['marker'],f'p{project:03d}'} or
            api.status(node,vmid)!='stopped' or not nets or
            any(f'bridge={bridge}' not in value.split(',') for value in nets) or
            inspection.get('computer')!=source.get('computer_name') or
            inspection.get('domain')!=source.get('domain_name') or inspection.get('role')!=source.get('domain_role') or
            inspection.get('boot',{}).get('LastShutdownGood')!='true' or
            inspection.get('boot',{}).get('LastBootGood')!='true' or len(diagnostics)<2):
        raise DistributionError('Shutdown recovery requires the unchanged, clean inspected Windows capture.')
    _require_probe_pool(api,node,'qemu',vmid)
    private_bridge_request(node,vmid=vmid,marker=receipt['marker'])
    for entry,operation in zip(diagnostics[-2:],('start','shutdown')):
        if entry.get('operation')!=operation or f':qm{operation}:{vmid}:' not in str(entry.get('upid','')):
            raise DistributionError('Inspection shutdown recovery task ownership differs.')
        api.task(node,entry['upid'],timeout=30)


def _retire_capture_transport(api, receipt, export, *, save):
    """Retire only the transfer backup after its independent replacement passed."""
    transport = receipt.get('transport')
    if not transport or transport.get('retirement') == 'retired':
        return
    node, volume = transport.get('source_node'), transport.get('volume_id')
    source_id = receipt.get('source_id')
    if (receipt.get('state') != 'verified' or transport.get('state') != 'verified' or
            node != receipt.get('source_node') or transport.get('source_id') != source_id or
            source_id == receipt.get('template_id') or export.get('verified') is not True or
            export.get('complete_capture') is not True or export.get('source_id') != receipt.get('template_id') or
            not isinstance(volume,str) or not re.fullmatch(fr'crange-library:backup/vzdump-qemu-{source_id}-[0-9_-]+\.vma\.zst',volume) or
            volume == export.get('volume_id') or not re.fullmatch(r'[a-f0-9]{64}',str(transport.get('artifact_sha256','')))):
        raise DistributionError('Transfer backup retirement is outside its verified capture scope.')
    catalog = read_settings()[1]
    if any(image.get('artifact',{}).get('volume_id') == volume for image in catalog.get('images',{}).values()):
        raise DistributionError('Transfer archive is referenced by a published image; retaining it.')
    content = f'/nodes/{node}/storage/crange-library/content'
    rows = api.request('GET',content)
    present = [row for row in rows if row.get('volid') == volume]
    if not present and transport.get('retirement') == 'retiring':
        transport['retirement'] = 'retired';save();return
    if transport.get('retirement') == 'retiring':
        raise DistributionError('Transfer retirement remains uncertain; retain its journal and reconcile before retry.')
    if len(present) != 1 or present[0].get('content') != 'backup' or int(present[0].get('vmid',-1)) != source_id:
        raise DistributionError('Transfer backup presence or ownership differs; retaining retirement journal.')
    hasher = SSHVolumeHasher()
    if (hasher(export['node'],export['volume_id']) != export['artifact_sha256'] or
            hasher(node,volume) != transport['artifact_sha256']):
        raise DistributionError('Transfer/replacement backup checksum changed; refusing retirement.')
    transport['retirement'] = 'retiring';save()
    upid = api.request('DELETE',content+'/'+urllib.parse.quote(volume,safe=''))
    if isinstance(upid,str) and upid.startswith('UPID:'):
        transport['retirement_upid']=upid;save();api.task(node,upid,timeout=180)
    if any(row.get('volid') == volume for row in api.request('GET',content)):
        raise DistributionError('Transfer backup retirement was not confirmed.')
    transport['retirement'] = 'retired';save()
    print('Retired verified transfer backup; immutable image/archive and clone evidence retained.',flush=True)


def resume_exported_capture(api, *, receipt_path, receipt, project, job, name, source_id, slug,
                            policy_override=None, policy_path=None):
    """Continue acceptance from the exact immutable template/export checkpoint."""
    from crange_distribution import main
    from crange_windows_snapshot import validate_captured_identity
    node, vmid=receipt.get('node'),receipt.get('template_id')
    if (receipt.get('state') not in ('allocated','captured','verified') or receipt.get('job_id')!=job or receipt.get('source_id')!=source_id or
            receipt.get('capture_name')!=name or receipt.get('marker')!=f'crange-capture-{slug}' or type(vmid) is not int):
        raise DistributionError('Exported capture receipt differs from this capture operation.')
    journal=FileJournal(DATA/'source-journal')
    identity_job=receipt.get('identity_job',job+'-identity')
    export_job=receipt.get('export_job',job+'-export')
    probe_job=receipt.get('probe_job',job+'-probe')
    if (identity_job,export_job,probe_job)!=(job+'-identity',job+'-export',job+'-probe'):
        raise DistributionError('Capture checkpoint job identities differ.')
    identity=json.loads((journal.directory/(identity_job+'.json')).read_text())
    export=json.loads((journal.directory/(export_job+'.json')).read_text())
    with node_locks([node],library=True), journal.allocation_lock():
        config=api.vm(node,vmid);_owned(config,receipt['marker']);_require_probe_pool(api,node,'qemu',vmid)
        validate_captured_identity(identity.get('source_identity'))
        if receipt.get('state')=='allocated':
            checks = {
                'template': int(config.get('template', 0)) == 1,
                'stopped': api.status(node, vmid) == 'stopped',
                'backup_lock': config.get('lock') in (None, 'backup'),
                'identity_state': identity.get('state') == 'identity-captured',
                'identity_guest': identity.get('source_id') == vmid,
                'identity_node': identity.get('node') == node,
                'config_hash': identity.get('source_config_sha256') == _source_config_identity(config, vmid),
                'export_state': export.get('state') in ('exporting', 'exported'),
                'export_identity': export.get('identity_capture') == identity,
                'export_node': export.get('node') == node,
                'export_guest': export.get('source_id') == vmid,
                'export_kind': export.get('kind') == 'vm',
                'export_task': bool(export.get('upid')),
            }
            failed = [label for label, valid in checks.items() if not valid]
            if failed:
                raise DistributionError('Pending immutable export differs from the retained template: '
                                        + ', '.join(failed) + '.')
            policy=policy_override if policy_override is not None else read_settings()[0]
            if node not in policy.get('allowed_nodes',[]) or (node,export.get('storage')) not in set(map(tuple,policy.get('approved_backup_storage',[]))):
                raise DistributionError('Pending export destination is no longer approved.')
            ledger=ReservationLedger(DATA/'reservations.sqlite3')
            try:
                saved=ledger.get_job(job);allocation=receipt.get('reservation')
                if (not saved or saved.get('state')!='running' or saved.get('project_id')!=project or
                        saved.get('owner')!='release-capture' or saved.get('allocations')!=[allocation] or not isinstance(allocation,dict) or
                        allocation.get('operation')!='source_probe' or allocation.get('project_id')!=project or
                        allocation.get('image')!=f"source:{receipt.get('source_node')}:qemu:{source_id}" or
                        allocation.get('artifact_sha256')!=receipt.get('source_config_sha256') or
                        allocation.get('resolved',{}).get('node_name')!=node):
                    raise DistributionError('Pending capture reservation differs from its checkpoint.')
                tasks=receipt.get('provider_tasks',[])
                if not tasks or tasks[-1].get('path')!=f'/nodes/{node}/qemu/{vmid}/template':
                    raise DistributionError('Pending capture lacks its exact template task.')
                for task in tasks:
                    if task.get('state')!='confirmed-terminal':
                        raise DistributionError('Capture provider task remains uncertain.')
                    task_node=task['path'].split('/')[2]
                    api.task(task_node,task['upid'],timeout=30)
                export=export_image(api,node=node,kind='vm',vmid=vmid,storage=export['storage'],
                    job_id=export_job,journal=journal,hash_volume=SSHVolumeHasher(),
                    approved_backup_storage=map(tuple,policy['approved_backup_storage']),identity_record=identity)
                ledger.reconcile_job(job,operation_terminal=True,allocations_reconciled=True)
            finally:ledger.close()
            receipt.update(state='captured',identity_job=identity_job,export_job=export_job,probe_job=probe_job)
            atomic_json(receipt_path,receipt)
        if (int(config.get('template',0))!=1 or api.status(node,vmid)!='stopped' or
                identity.get('state')!='identity-captured' or identity.get('source_id')!=vmid or identity.get('node')!=node or
                identity.get('source_config_sha256')!=_source_config_identity(config,vmid) or
                export.get('state')!='exported' or export.get('verified') is not True or
                export.get('complete_capture') is not True or export.get('identity_capture')!=identity or
                not re.fullmatch(r'[a-f0-9]{64}',str(export.get('artifact_sha256',''))) or
                (identity['source_identity'].get('identity_mode')=='windows-scenario-snapshot-v1' and
                 identity['source_identity'].get('snapshot_project_id')!=project)):
            raise DistributionError('Immutable capture/export checkpoint is not consistent; retain for review.')
    policy=policy_override if policy_override is not None else read_settings()[0]
    storage=receipt.get('probe_datastore', receipt['reservation']['resolved']['datastore_id'])
    if receipt.get('state') != 'verified':
        print('Resuming independent clone acceptance from verified immutable export; no new capture or archive',flush=True)
        _wait_capture_probe_load(api,node,policy)
        probe_path = journal.directory/(probe_job+'.json')
        existing = json.loads(probe_path.read_text()) if probe_path.exists() else None
        if existing is not None and existing.get('state') == 'source-probed':
            # The complete proof is checked below; do not allocate another clone.
            _verified_capture_probe(receipt,identity,project)
        else:
            command = 'source-probe-resume' if existing is not None else 'source-probe'
            main(['--catalog',str(CONFIG/'image-catalog.json'),'--journal-dir',str(journal.directory),
              '--policy',str(policy_path or CONFIG/'distribution-policy.json'),command,'--project',str(project),
              '--source-node',node,'--source-id',str(vmid),
              *([] if existing is not None else ['--datastore-id',storage,'--auto-storage']),
              '--isolated-bridge',policy['capture_isolated_bridge'],'--job-id',probe_job,
              '--identity-record',str(journal.directory/(identity_job+'.json'))])
    else:
        print('Reusing accepted immutable capture and completed clone verification; no new copy or probe.',flush=True)
    _verified_capture_probe(receipt,identity,project)
    receipt['state']='verified';atomic_json(receipt_path,receipt)
    _retire_capture_transport(api,receipt,export,save=lambda:atomic_json(receipt_path,receipt))
    return vmid,{'node':node,'export_job':receipt['export_job'],'probe_job':receipt['probe_job']}


def _resume_platform_transport_copy(api, receipt, saved, *, project, job, source_node,
                                    source_id, source_sha, node, storage, name, slug, bridge,
                                    identity_path):
    """Reuse only a confirmed full derivative after clean failed identity boots."""
    allocation = receipt.get('reservation', {})
    transport = receipt.get('transport', {})
    vmid, temporary = receipt.get('template_id'), transport.get('template_id')
    tasks = receipt.get('provider_tasks', [])
    marker = f'crange-capture-{slug}'
    if (receipt.get('job_id') != job or receipt.get('state') != 'allocated' or
            receipt.get('source_id') != source_id or receipt.get('source_node') != source_node or
            receipt.get('source_config_sha256') != source_sha or receipt.get('node') != node or
            receipt.get('capture_name') != name or receipt.get('marker') != marker or
            receipt.get('platform_source') is not True or receipt.get('generalization') or identity_path.exists() or
            type(vmid) is not int or vmid < 100 or type(temporary) is not int or
            not 21000 <= temporary < 22000 or temporary == vmid or
            transport.get('state') != 'verified' or transport.get('derivative_ready') is not True or
            transport.get('template_retired') is not True or
            transport.get('source_id') != source_id or transport.get('source_node') != source_node or
            not re.fullmatch('[a-f0-9]{64}', str(transport.get('artifact_sha256', ''))) or
            len(tasks) < 6 or len(tasks) > 20 or len(tasks) % 2 or
            saved.get('state') != 'running' or saved.get('owner') != 'release-capture' or
            saved.get('project_id') != project or saved.get('allocations') != [allocation] or
            allocation.get('operation') != 'source_probe' or allocation.get('project_id') != project or
            allocation.get('image') != f'source:{source_node}:qemu:{source_id}' or
            allocation.get('artifact_sha256') != source_sha or
            allocation.get('resolved', {}).get('node_name') != node or
            allocation.get('resolved', {}).get('datastore_id') != storage):
        raise DistributionError('Retained platform derivative is outside confirmed transport recovery.')
    expected = [(source_node, f'/nodes/{source_node}/vzdump', 'vzdump', source_id),
                (node, f'/nodes/{node}/qemu', 'qmrestore', temporary),
                (node, f'/nodes/{node}/qemu/{temporary}/clone', 'qmclone', temporary),
                (node, f'/nodes/{node}/qemu/{temporary}', 'qmdestroy', temporary)]
    for index in range(4, len(tasks)):
        operation = 'start' if index % 2 == 0 else 'shutdown'
        expected.append((node, f'/nodes/{node}/qemu/{vmid}/status/{operation}',
                         'qm' + operation, vmid))
    for entry, (host, path, operation, guest) in zip(tasks, expected):
        parts = str(entry.get('upid', '')).split(':')
        if (entry.get('path') != path or len(parts) < 9 or parts[:2] != ['UPID', host] or
                parts[5:7] != [operation, str(guest)]):
            raise DistributionError('Platform transport task history differs; retain allocation.')
        api.task(host, entry['upid'], timeout=30)
    config = api.vm(node, vmid)
    nics = [str(value).split(',') for key, value in config.items() if re.fullmatch(r'net[0-9]+', key)]
    active = api.request('GET', f'/nodes/{node}/tasks', {'source': 'active', 'limit': 1000})
    if (config.get('name') != name or int(config.get('template', 0)) or config.get('lock') or
            int(config.get('onboot', 0)) or api.status(node, vmid) != 'stopped' or
            set(str(config.get('tags', '')).split(';')) != {'crange-image', marker, f'p{project:03d}'} or
            not nics or any(f'bridge={bridge}' not in nic or 'link_down=1' not in nic for nic in nics) or
            not isinstance(active, list) or len(active) >= 1000 or
            any(str(task.get('id')) == str(vmid) for task in active)):
        raise DistributionError('Platform recovery requires its exact stopped disconnected derivative.')
    _require_probe_pool(api, node, 'qemu', vmid)
    disks = [str(value).split(',')[0] for key, value in config.items()
             if re.fullmatch(r'(?:scsi|sata|ide|virtio)[0-9]+|efidisk0|tpmstate0', key)]
    if not disks or any(not re.fullmatch(re.escape(storage) + rf':(?:{vmid}/)?vm-{vmid}-(?:disk-[0-9]+(?:\.(?:raw|qcow2))?|cloudinit(?:\.raw)?)', disk)
                        for disk in disks):
        raise DistributionError('Platform recovery disks do not belong exclusively to this derivative.')
    for entry in tasks:
        entry['state'] = 'confirmed-terminal'
    receipt['recovery'] = 'owned-platform-transport-clean-identity-retry'
    return receipt


def capture_vm(api, *, project, node, source_id, name, description, slug, profile,
               platform_source=False, policy_override=None, policy_path=None, job_id=None,
               allocated_vmid=None):
    """Clone a stopped staging VM, isolate before boot, capture real identity.

    Failure retains the exact journal, guest, and capacity reservation for review;
    no retry deletes or reuses an uncertain allocation.
    """
    job = job_id or f'capture-p{project:03d}-{slug}'
    if not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', job):
        raise DistributionError('Invalid capture job ID.')
    receipt_path = DATA / 'capture-journal' / (job + '.json')
    dynamic_platform = bool(platform_source and allocated_vmid is None)
    if (not platform_source or dynamic_platform) and receipt_path.exists():
        previous=json.loads(receipt_path.read_text())
        if (previous.get('state') in ('captured','verified') or
                previous.get('state')=='allocated' and (DATA/'source-journal'/(job+'-export.json')).exists()):
            return resume_exported_capture(api,receipt_path=receipt_path,receipt=previous,project=project,
                                           job=job,name=name,source_id=source_id,slug=slug,
                                           policy_override=policy_override,policy_path=policy_path)
    source_node = node
    if dynamic_platform:
        if policy_override is None or policy_path is None:
            raise DistributionError('Platform preparation requires a protected worker policy.')
        _approved_platform_source(api.vm(source_node, source_id), source_node, source_id, 'vm')
        node = (json.loads(receipt_path.read_text())['node'] if receipt_path.exists() else
                _capture_destination(api, source_node, api.vm(source_node, source_id), profile, policy_override))
    policy, bridge, storage, backup = capture_policy(node, profile, policy_override)
    if not platform_source:
        if receipt_path.exists():
            node = json.loads(receipt_path.read_text()).get('node')
            if node not in policy.get('allowed_nodes', []) or policy.get('nodes', {}).get(node, {}).get('draining'):
                raise DistributionError('Reserved capture destination is no longer enrolled or is draining.')
        else:
            node = _capture_destination(api, source_node, api.vm(source_node, source_id), profile, policy)
        policy, bridge, storage, backup = capture_policy(node, profile, policy)
        print(f'Capture destination: {node}/{storage} (source {source_node}/{source_id})', flush=True)
    if platform_source and (policy_override is None or policy_path is None or
                            PLATFORM_BASE_SOURCES.get((source_node, source_id)) is None):
        raise DistributionError('Platform source capture requires its fixed source map and protected temporary policy.')
    identity_job, export_job, probe_job = job + '-identity', job + '-export', job + '-probe'
    journal = FileJournal(DATA / 'source-journal')
    ledger = ReservationLedger(DATA / 'reservations.sqlite3')
    try:
        with node_locks(sorted({node, source_node}), library=True), journal.allocation_lock():
            source = api.vm(source_node, source_id)
            if platform_source:
                _approved_platform_source(source, source_node, source_id, 'vm')
            elif int(source.get('template', 0)):
                raise DistributionError('Staging source must not be an immutable template.')
            if api.status(source_node, source_id) != 'stopped':
                raise DistributionError('Source must be stopped before release cloning.')
            if not str(source.get('agent', '')).split(',')[0] in ('1', 'enabled=1'):
                raise DistributionError('Staging VM QGA is disabled; rebuild with a tested approved agent profile.')
            source_sha = _source_config_identity(source, source_id)
            resume = receipt_path.exists()
            resume_template = False
            resume_identity = False
            if resume and platform_source and not dynamic_platform:
                # Recover only the fixed platform clone whose provider task has
                # conclusively finished. This handles a worker interruption
                # after PVE completed clone but before the receipt was updated.
                receipt = json.loads(receipt_path.read_text())
                if (receipt.get('job_id') != job or receipt.get('source_id') != source_id or
                        receipt.get('source_config_sha256') != source_sha or receipt.get('node') != node or
                        receipt.get('state') != 'allocated' or int(receipt.get('template_id', -1)) != allocated_vmid or
                        receipt.get('provider_tasks') is None or len(receipt['provider_tasks']) < 3):
                    raise DistributionError('Existing platform capture receipt is outside the fixed recovery scope.')
                expected_clone = f'/nodes/{node}/qemu/{source_id}/clone'
                start_path = f'/nodes/{node}/qemu/{allocated_vmid}/status/start'
                shutdown_path = f'/nodes/{node}/qemu/{allocated_vmid}/status/shutdown'
                template_path = f'/nodes/{node}/qemu/{allocated_vmid}/template'
                resume_template = receipt['provider_tasks'][-1].get('path') == template_path
                prior_tasks = receipt['provider_tasks'][:-1] if resume_template else receipt['provider_tasks']
                clone_task = receipt['provider_tasks'][0]
                if clone_task.get('path') != expected_clone or clone_task.get('state') != 'confirmed-terminal':
                    raise DistributionError('Platform recovery receipt has no confirmed source clone.')
                expected_task = start_path
                guest_may_be_running = False
                for recorded_task in prior_tasks[1:]:
                    if recorded_task.get('path') != expected_task:
                        raise DistributionError('Platform recovery task history is outside the fixed start/shutdown sequence.')
                    if expected_task == start_path:
                        if recorded_task.get('state') != 'confirmed-terminal':
                            raise DistributionError('Platform start task is not confirmed terminal.')
                        guest_may_be_running = True
                        expected_task = shutdown_path
                    else:
                        if recorded_task.get('state') == 'confirmed-terminal':
                            guest_may_be_running = False
                            expected_task = start_path
                        elif recorded_task.get('state') == 'confirmed-terminal-error':
                            guest_may_be_running = True
                            expected_task = shutdown_path
                        else:
                            raise DistributionError('Platform shutdown task is not confirmed terminal.')
                    if recorded_task.get('state') not in ('confirmed-terminal', 'confirmed-terminal-error'):
                        raise DistributionError('Platform provider task is not confirmed terminal.')
                    provider_task = api.request('GET', f'/nodes/{node}/tasks/{recorded_task["upid"]}/status')
                    if (provider_task.get('status') != 'stopped' or
                            (recorded_task.get('state') == 'confirmed-terminal' and provider_task.get('exitstatus') != 'OK') or
                            (recorded_task.get('state') == 'confirmed-terminal-error' and
                             (expected_task != shutdown_path or 'powerdown failed - got timeout' not in str(provider_task.get('exitstatus', ''))))):
                        raise DistributionError('Platform provider task is not confirmed successful; retain receipt for review.')
                windows_resume = (resume_template and profile.get('init_system') == 'windows' and
                    expected_task == shutdown_path and
                    receipt.get('generalization', {}).get('state') == 'sysprep-generalized' and
                    api.status(node, allocated_vmid) == 'stopped')
                if guest_may_be_running and not windows_resume:
                    raise DistributionError('Platform recovery has no successful final shutdown task.')
                if resume_template:
                    if expected_task != start_path:
                        if not windows_resume:
                            raise DistributionError('Platform template recovery did not follow a complete guest-generalization sequence.')
                    template_task = receipt['provider_tasks'][-1]
                    if template_task.get('state') != 'confirmed-terminal':
                        raise DistributionError('Platform template conversion is not confirmed terminal.')
                    provider_task = api.request('GET', f'/nodes/{node}/tasks/{template_task["upid"]}/status')
                    if provider_task.get('status') != 'stopped' or provider_task.get('exitstatus') != 'OK':
                        raise DistributionError('Platform template task is not confirmed successful.')
                allocation = receipt.get('reservation')
                marker = receipt.get('marker')
                if (not isinstance(allocation, dict) or marker != f'crange-capture-{slug}'):
                    raise DistributionError('Platform capture receipt lacks its fixed reservation or ownership marker.')
                vmid = int(allocated_vmid)
                config = api.vm(node, vmid)
                source_nets = [str(value) for key, value in source.items() if re.fullmatch(r'net[0-9]+', key)]
                clone_nets = [str(value) for key, value in config.items() if re.fullmatch(r'net[0-9]+', key)]
                source_bridges = [next((part.split('=', 1)[1] for part in net.split(',') if part.startswith('bridge=')), '')
                                  for net in source_nets]
                clone_bridges = [next((part.split('=', 1)[1] for part in net.split(',') if part.startswith('bridge=')), '')
                                 for net in clone_nets]
                already_owned = marker in set(str(config.get('tags', '')).split(';'))
                isolated_network = bool(clone_nets) and all(
                    clone_bridge == bridge and 'link_down=1' in net.split(',')
                    for clone_bridge, net in zip(clone_bridges, clone_nets))
                if (config.get('name') != name or
                        int(config.get('template', 0)) != (1 if resume_template else 0) or
                        api.status(node, vmid) != 'stopped' or not source_nets or len(clone_nets) != len(source_nets) or
                        (not already_owned and clone_bridges != source_bridges) or
                        (already_owned and (not isolated_network or
                         set(str(config.get('tags', '')).split(';')) != {'crange-image', marker, f'p{project:03d}'})) or
                        not any(str(config.get(key, '')).startswith(
                            f'{storage}:{vmid}/{"base" if resume_template else "vm"}-{vmid}-disk-')
                                for key in ('scsi0', 'scsi1', 'sata0', 'sata1', 'virtio0', 'virtio1'))):
                    raise DistributionError('Stopped derivative does not match the completed source clone; retain for review.')
                _require_probe_pool(api, node, 'qemu', vmid)
                identity_path = journal.directory / (identity_job + '.json')
                if identity_path.exists() and not resume_template:
                    raise DistributionError('Identity journal already exists; platform recovery must resume from its recorded stage.')
                if resume_template and not identity_path.exists():
                    raise DistributionError('Captured template is missing its original worker identity record.')
                existing_job = ledger.get_job(job)
                if existing_job.get('state') != 'running' or not existing_job.get('allocations'):
                    raise DistributionError('Platform capture reservation is not running; reconcile before recovery.')
            elif resume and dynamic_platform and json.loads(receipt_path.read_text()).get('transport', {}).get('template_retired'):
                receipt = _resume_platform_transport_copy(api, json.loads(receipt_path.read_text()), ledger.get_job(job),
                    project=project, job=job, source_node=source_node, source_id=source_id,
                    source_sha=source_sha, node=node, storage=storage, name=name, slug=slug,
                    bridge=bridge, identity_path=journal.directory / (identity_job + '.json'))
                allocation, marker, vmid = receipt['reservation'], receipt['marker'], receipt['template_id']
                atomic_json(receipt_path, receipt)
                print('Reusing the confirmed stopped platform derivative; no backup, restore or clone repeated.', flush=True)
            elif resume:
                receipt = json.loads(receipt_path.read_text())
                allocation = receipt.get('reservation', {})
                marker = receipt.get('marker')
                vmid = receipt.get('template_id')
                transport = receipt.get('transport', {})
                tasks = receipt.get('provider_tasks', [])
                saved = ledger.get_job(job)
                if (node == source_node or receipt.get('state') != 'allocated' or
                        receipt.get('job_id') != job or receipt.get('source_id') != source_id or
                        receipt.get('source_node') != source_node or receipt.get('node') != node or
                        receipt.get('capture_name') != name or receipt.get('source_config_sha256') != source_sha or
                        marker != f'crange-capture-{slug}' or type(vmid) is not int or vmid < 100 or
                        transport.get('source_node') != source_node or transport.get('source_id') != source_id or
                        transport.get('state') not in ('exporting', 'verified') or
                        not isinstance(transport.get('content_before'), list) or len(tasks) not in (1, 2, 3, 4, 5, 7, 9, 11) or
                        tasks[0].get('path') != f'/nodes/{source_node}/vzdump' or
                        not str(tasks[0].get('upid', '')).startswith(f'UPID:{source_node}:') or
                        f':vzdump:{source_id}:' not in str(tasks[0].get('upid', '')) or
                        saved.get('state') != 'running' or saved.get('project_id') != project or
                        saved.get('owner') != 'release-capture' or saved.get('allocations') != [allocation] or
                        allocation.get('operation') != 'source_probe' or allocation.get('project_id') != project or
                        allocation.get('image') != f'source:{source_node}:qemu:{source_id}' or
                        allocation.get('artifact_sha256') != source_sha or
                        allocation.get('resolved', {}).get('node_name') != node or
                        allocation.get('resolved', {}).get('datastore_id') != storage):
                    raise DistributionError('Capture receipt is outside the exact completed-backup recovery scope.')
                resources = api.request('GET', '/cluster/resources', {'type': 'vm'})
                active = api.request('GET', f'/nodes/{node}/tasks', {'source': 'active', 'limit': 1000})
                provider = api.request('GET', f'/nodes/{source_node}/tasks/{urllib.parse.quote(tasks[0]["upid"],safe="")}/status')
                if (not isinstance(resources, list) or not isinstance(active, list) or len(active) >= 1000 or
                        any(str(row.get('id')) == str(vmid) for row in active) or provider.get('status') != 'stopped'):
                    raise DistributionError('Capture copy absence or backup termination is not confirmed; retain journal.')
                # Recheck its exact terminal status/log through the normal adapter,
                # including the bounded MTU-warning completion gate.
                api.task(source_node, tasks[0]['upid'], timeout=30)
                tasks[0]['state'] = 'confirmed-terminal'
                present = [row for row in resources if int(row.get('vmid', -1)) == vmid]
                if len(tasks) == 1:
                    if present:
                        raise DistributionError('Capture copy absence or backup termination is not confirmed; retain journal.')
                    receipt['recovery'] = 'completed-frozen-backup-no-copy-present'
                elif len(tasks) == 4:
                    restore,start,shutdown=tasks[1:]
                    if (len(present)!=1 or present[0].get('node')!=node or
                            restore.get('path')!=f'/nodes/{node}/qemu' or f':qmrestore:{vmid}:' not in str(restore.get('upid','')) or
                            start.get('path')!=f'/nodes/{node}/qemu/{vmid}/status/start' or f':qmstart:{vmid}:' not in str(start.get('upid','')) or
                            shutdown.get('path')!=f'/nodes/{node}/qemu/{vmid}/status/shutdown' or f':qmshutdown:{vmid}:' not in str(shutdown.get('upid',''))):
                        raise DistributionError('Capture shutdown history is outside its owned restore/start scope.')
                    api.task(node,restore['upid'],timeout=30);api.task(node,start['upid'],timeout=30)
                    failed=api.request('GET',f'/nodes/{node}/tasks/{urllib.parse.quote(shutdown["upid"],safe="")}/status')
                    if failed.get('status')!='stopped':
                        raise DistributionError('Capture shutdown is not confirmed terminal.')
                    if failed.get('exitstatus')=='OK' and not (journal.directory/(identity_job+'.json')).exists():
                        _recover_inspected_boot_readiness(api,receipt,api.vm(node,vmid),project,bridge)
                        shutdown['state']='confirmed-terminal'
                        receipt['recovery']='inspected-clean-boot-retry-identity'
                    else:
                        if str(failed.get('exitstatus','')).rstrip('.')!='VM quit/powerdown failed':
                            raise DistributionError('Capture shutdown error is not the inspected terminal failure.')
                        identity=json.loads((journal.directory/(identity_job+'.json')).read_text())
                        _recover_inspected_shutdown(api,receipt,identity,api.vm(node,vmid),project,bridge)
                        shutdown.update(state='confirmed-terminal-error',exitstatus=failed['exitstatus'])
                        receipt['recovery']='inspected-clean-shutdown-after-provider-timeout'
                        resume_identity=True
                elif len(tasks) == 2:
                    restore = tasks[1]
                    if (len(present) != 1 or present[0].get('node') != node or
                            restore.get('path') != f'/nodes/{node}/qemu' or
                            f':qmrestore:{vmid}:' not in str(restore.get('upid', '')) or
                            transport.get('state') != 'verified'):
                        raise DistributionError('Stopped restored capture is outside its completed transport scope.')
                    api.task(node, restore['upid'], timeout=30)
                    copy = api.vm(node, vmid)
                    nets = {key: str(value) for key,value in copy.items() if re.fullmatch(r'net[0-9]+',key)}
                    source_nets = {key: str(value) for key,value in source.items() if re.fullmatch(r'net[0-9]+',key)}
                    untouched = (copy.get('tags') == source.get('tags') and _restored_network_matches(nets,source_nets))
                    isolated = (set(str(copy.get('tags','')).split(';')) == {'crange-image',marker,f'p{project:03d}'} and
                        bool(nets) and all(f'bridge={bridge}' in value.split(',') and 'link_down=1' in value.split(',') for value in nets.values()))
                    if (copy.get('name') != name or int(copy.get('template',0)) or api.status(node,vmid) != 'stopped' or
                            not (untouched or isolated) or (journal.directory / (identity_job+'.json')).exists()):
                        raise DistributionError('Stopped restored capture identity or disconnected network differs.')
                    _require_probe_pool(api,node,'qemu',vmid)
                    receipt['recovery'] = 'completed-transport-stopped-copy'
                    restore['state'] = 'confirmed-terminal'
                else:
                    restore, start = tasks[1:3]
                    if (len(present) != 1 or present[0].get('node') != node or
                            restore.get('path') != f'/nodes/{node}/qemu' or
                            start.get('path') != f'/nodes/{node}/qemu/{vmid}/status/start' or
                            f':qmrestore:{vmid}:' not in str(restore.get('upid', '')) or
                            f':qmstart:{vmid}:' not in str(start.get('upid', ''))):
                        raise DistributionError('Existing capture is outside the failed-machine-start recovery scope.')
                    api.task(node, restore['upid'], timeout=30)
                    for index, entry in enumerate(tasks[3:]):
                        operation='start' if index%2==0 else 'shutdown'
                        expected=f'/nodes/{node}/qemu/{vmid}/status/{operation}'
                        task_type='qmstart' if operation=='start' else 'qmshutdown'
                        if entry.get('path')!=expected or f':{task_type}:{vmid}:' not in str(entry.get('upid','')):
                            raise DistributionError('Capture retry history is not a terminal read-only boot/shutdown sequence.')
                        api.task(node,entry['upid'],timeout=30)
                    failed = api.request('GET', f'/nodes/{node}/tasks/{urllib.parse.quote(start["upid"],safe="")}/status')
                    copy = api.vm(node, vmid)
                    nets = [value for key, value in copy.items() if re.fullmatch(r'net[0-9]+', key)]
                    if (failed.get('status') != 'stopped' or
                            not re.fullmatch(r"Installed QEMU version '[0-9.]+' is too old to run machine type 'pc-(?:q35|i440fx)-[0-9.]+(?:\+pve[0-9]+)?', please upgrade node '" + re.escape(node) + r"'\.?", str(failed.get('exitstatus', ''))) or
                            copy.get('name') != name or int(copy.get('template', 0)) or
                            api.status(node, vmid) != 'stopped' or not nets or
                            set(str(copy.get('tags', '')).split(';')) != {'crange-image', marker, f'p{project:03d}'} or
                            any(f'bridge={bridge}' not in str(net).split(',') or 'link_down=1' not in str(net).split(',') for net in nets) or
                            (journal.directory / (identity_job + '.json')).exists()):
                        raise DistributionError('Owned stopped capture does not match the failed incompatible-machine start.')
                    _require_probe_pool(api, node, 'qemu', vmid)
                    start.update(state='confirmed-terminal-error', exitstatus=failed['exitstatus'])
                    receipt['recovery'] = 'owned-copy-machine-version-start-recovery'
                atomic_json(receipt_path, receipt)
            else:
                capacity = _source_capacity(source, 'qemu')
                if profile.get('_transport_template') and node!=source_node:
                    capacity['disk_gb'] *= 2
                backup_status = api.request('GET', f'/nodes/{node}/storage/{backup}/status')
                # The single-storage config endpoint requires write-level
                # Datastore.Allocate even for GET. The status endpoint is read-only
                # under Datastore.Audit and includes the configured shared flag.
                if int(backup_status.get('shared', 0)) != 1:
                    raise DistributionError('Capture backup storage must be administrator-configured shared central storage.')
                total, available = float(backup_status.get('total', 0)), float(backup_status.get('avail', 0))
                reserve = archive_headroom(total)
                if (int(backup_status.get('active', 0)) != 1 or 'backup' not in str(backup_status.get('content', '')).split(',')
                        or total <= 0 or available - capacity['disk_gb'] * 1024 ** 3 < reserve):
                    raise DistributionError(f'Central archive {backup} on {node}: active={backup_status.get("active")}, shared={backup_status.get("shared")}, available={available/1024**3:.1f} GiB, required={capacity["disk_gb"]:.1f} GiB plus {reserve/1024**3:.1f} GiB reserve.')
                allocation = ledger.reserve_source_probe(project_id=project, job_id=job, owner='release-capture',
                    source_ref=f'source:{source_node}:qemu:{source_id}', source_sha256=source_sha,
                    source_node=source_node,
                    node_name=node, datastore_id=storage, capacity=capacity,
                    inventory=discover(api, policy), policy=policy)
                ledger.mark_running(job)
                # The general allocator avoids the source-probe worker's reserved band.
                vmid = int(allocated_vmid if allocated_vmid is not None else api.request('GET', '/cluster/nextid'))
                if vmid < 100 or 20000 <= vmid < 20100:
                    raise DistributionError('Capture allocator returned an invalid or probe-reserved VMID.')
                if allocated_vmid is not None and any(int(row.get('vmid', -1)) == vmid
                                                      for row in api.request('GET', '/cluster/resources', {'type': 'vm'})):
                    raise DistributionError('Reviewed platform capture VMID is already allocated.')
                marker = f'crange-capture-{slug}'
                receipt = dict(job_id=job, source_id=source_id, source_config_sha256=source_sha,
                    node=node, source_node=source_node, capture_name=name, template_id=vmid, platform_source=bool(platform_source),
                    reservation=allocation, marker=marker, state='allocated', provider_tasks=[])
                atomic_json(receipt_path, receipt)
            def task(method, path, fields, task_node=None):
                upid = api.request(method, path, fields)
                receipt['provider_tasks'].append(dict(upid=upid, path=path, state='running'))
                atomic_json(receipt_path, receipt)
                api.task(task_node or node, upid, timeout=1800)
                receipt['provider_tasks'][-1]['state'] = 'confirmed-terminal'
                atomic_json(receipt_path, receipt)
            if not resume or receipt.get('recovery') == 'completed-frozen-backup-no-copy-present':
                if node == source_node:
                    task('POST', f'/nodes/{node}/qemu/{source_id}/clone', dict(newid=vmid, name=name,
                        description=description, pool=PROBE_POOL, full=1, storage=storage))
                else:
                    _capture_transport(api, source_node=source_node, source_id=source_id,
                        target_node=node, target_id=vmid, storage=storage, backup=backup, project=project,
                        receipt=receipt, save=lambda: atomic_json(receipt_path, receipt), task=task, platform_source=platform_source)
            if _source_config_identity(api.vm(source_node, source_id), source_id) != source_sha or api.status(source_node, source_id) != 'stopped':
                raise DistributionError('Staging source changed during capture cloning; reservation retained.')
            base = f'/nodes/{node}/qemu/{vmid}'
            config = api.vm(node, vmid)
            if resume_identity:
                task('POST',base+'/template',{})
                config=api.vm(node,vmid)
                generalization=None
            elif resume_template:
                identity = json.loads(identity_path.read_text())
                if (identity.get('state') != 'identity-captured' or identity.get('source_config_sha256') !=
                        _source_config_identity(config, vmid)):
                    raise DistributionError('Captured template does not match its original worker identity record.')
                from crange_windows_snapshot import validate_captured_identity
                validate_captured_identity(identity.get('source_identity'))
                generalization = receipt.get('generalization') or {
                    'state': 'generalized',
                    'os_family': 'windows' if profile.get('init_system') == 'windows' else 'linux',
                    'recovered_from_confirmed_template_task': True}
                receipt['generalization'] = generalization
                atomic_json(receipt_path, receipt)
            else:
                fields = {'tags': f'crange-image;{marker};p{project:03d}', 'onboot': 0}
                if node != source_node:
                    fields['machine'] = _portable_capture_machine(api, node, config)
                    print(f'Portable capture machine: {fields["machine"]}', flush=True)
                # Disable package upgrades only on the owned disconnected clone.
                if platform_source:
                    fields['ciupgrade'] = 0
                for key, value in config.items():
                    if re.fullmatch(r'net[0-9]+', key):
                        fields[key] = f'{str(value).split(",")[0]},bridge={bridge},link_down=1'
                carrier = bool(profile.get('scenario_snapshot') and profile.get('init_system')=='windows')
                _prepare_capture_network(api,node,vmid,fields,marker,bridge,carrier=carrier)
                config = api.vm(node, vmid)
                _owned(config, marker)
                _require_probe_pool(api, node, 'qemu', vmid)
                for key, value in config.items():
                    if re.fullmatch(r'net[0-9]+', key) and (f'bridge={bridge}' not in str(value).split(',') or f'link_down={0 if carrier else 1}' not in str(value).split(',')):
                        raise DistributionError('Capture network isolation was not applied exactly.')
                task('POST', base + '/status/start', {})
                # Agent startup is asynchronous; commands are fixed and failure remains gated.
                deadline = time.monotonic() + (600 if profile.get('init_system') == 'windows' else 180)
                while True:
                    try:
                        identity = capture_source_identity(api, node=node, kind='vm', vmid=vmid,
                            job_id=identity_job, journal=journal, init_system=profile.get('init_system', 'systemd'),
                            snapshot_project=project if profile.get('scenario_snapshot') and not platform_source else None)
                        break
                    except (DistributionError, RuntimeError) as error:
                        if (platform_source and profile.get('init_system') == 'systemd' and
                                getattr(error, 'collector_state', None) == 'degraded'):
                            from crange_distribution import normalize_isolated_network_wait
                            preparation = normalize_isolated_network_wait(api, node, 'qemu', vmid, bridge)
                            if preparation:
                                receipt['offline_network_readiness'] = preparation
                                atomic_json(receipt_path, receipt)
                        if time.monotonic() >= deadline:
                            # Only the exact, owned, terminal-started allocation may be stopped.
                            _require_probe_pool(api, node, 'qemu', vmid)
                            _owned(api.vm(node, vmid), marker)
                            task('POST', base + '/status/shutdown', {'timeout': 300 if profile.get('init_system') == 'windows' else 90, 'forceStop': 0})
                            raise
                        time.sleep(2)
                # Generalization is submitted once after identity readiness. A
                # launch failure must be reconciled, never retried in the boot loop.
                if platform_source and profile.get('init_system') == 'windows':
                    generalization = _generalize_windows_platform_derivative(api, node, vmid,
                        expected_marker=marker, identity_record=identity)
                    if generalization.get('state') != 'sysprep-generalized':
                        receipt['generalization'] = generalization
                        atomic_json(receipt_path, receipt)
                        raise DistributionError('Windows derivative needs fresh offline Sysprep success verification before publication.')
                elif platform_source:
                    generalization = _generalize_platform_derivative(api, node, vmid)
                else:
                    generalization = None
                if not (platform_source and profile.get('init_system') == 'windows'):
                    task('POST', base + '/status/shutdown', {'timeout': 300 if profile.get('init_system') == 'windows' else 90, 'forceStop': 0})
                if api.status(node, vmid) != 'stopped':
                    raise DistributionError('Captured guest did not shut down cleanly.')
                if generalization:
                    receipt['generalization'] = generalization
                    atomic_json(receipt_path, receipt)
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
            if generalization:
                receipt['generalization'] = generalization
            atomic_json(receipt_path, receipt)
        # Reuse the fixed worker's exact reservation, cleanup, and terminal-task checks.
        from crange_distribution import main
        _wait_capture_probe_load(api,node,policy)
        probe_storage = receipt.setdefault('probe_datastore',
            profile.get('_probe_datastores', {}).get(node, storage))
        if probe_storage != storage and (node, probe_storage) not in set(map(tuple, policy.get('approved_storage', []))):
            raise DistributionError('Independent probe storage is not approved; source capture retained.')
        atomic_json(receipt_path, receipt)
        main(['--catalog', str(CONFIG / 'image-catalog.json'), '--journal-dir', str(journal.directory),
              '--policy', str(policy_path or CONFIG / 'distribution-policy.json'), 'source-probe',
              '--project', str(project), '--source-node', node, '--source-id', str(vmid),
              '--datastore-id', probe_storage, '--auto-storage', '--isolated-bridge', bridge, '--job-id', probe_job,
              '--identity-record', str(journal.directory / (identity_job + '.json'))])
        receipt['state'] = 'verified'
        atomic_json(receipt_path, receipt)
        if receipt.get('transport'):
            _verified_capture_probe(receipt,identity,project)
            _retire_capture_transport(api,receipt,json.loads((journal.directory/(export_job+'.json')).read_text()),
                                      save=lambda:atomic_json(receipt_path,receipt))
        return vmid, {'export_job': export_job, 'probe_job': probe_job, 'node': node}
    finally:
        ledger.close()


def _ct_transport_restore_recovery(api,receipt,*,project,job,source_node,source_id,node,storage,ledger):
    tasks=receipt.get('provider_tasks',[])
    if (receipt.get('state')!='allocated' or receipt.get('job_id')!=job or receipt.get('kind')!='container' or
            receipt.get('source_node')!=source_node or receipt.get('source_id')!=source_id or receipt.get('node')!=node or
            receipt.get('transport',{}).get('state')!='verified' or len(tasks)!=2 or
            tasks[0].get('path')!=f'/nodes/{source_node}/vzdump' or tasks[1].get('path')!=f'/nodes/{node}/lxc'):
        return False
    vmid=receipt['template_id']
    if (type(vmid) is not int or f':vzdump:{source_id}:' not in str(tasks[0].get('upid','')) or
            f':vzrestore:{vmid}:' not in str(tasks[1].get('upid',''))):
        raise DistributionError('Container transport recovery task ownership differs.')
    api.task(source_node,tasks[0]['upid'],timeout=30)
    failed=api.request('GET',f'/nodes/{node}/tasks/{urllib.parse.quote(tasks[1]["upid"],safe="")}/status')
    failure=str(failed.get('exitstatus',''))
    schema_failure='format error host-managed: property is not defined in schema' in failure
    debian_failure=bool(re.search(r"unsupported debian version '13\.[0-9]+'",failure))
    if failed.get('status')!='stopped' or not (schema_failure or debian_failure):
        raise DistributionError('Container restore is not a known terminal compatibility failure.')
    if debian_failure:
        packages=api.request('GET',f'/nodes/{node}/apt/versions')
        matches=[row for row in packages if row.get('Package')=='pve-container'] if isinstance(packages,list) else []
        if len(matches)!=1 or matches[0].get('CurrentState')!='Installed':
            raise DistributionError('Container compatibility package is not confirmed fully installed.')
        installed=matches[0].get('OldVersion',matches[0].get('Version',''))
        version=re.fullmatch(r'([0-9]+)\.([0-9]+)\.([0-9]+)(?:[+~-].*)?',str(installed))
        if not version or tuple(map(int,version.groups()))<(6,0,10):
            raise DistributionError('Prepare container template support on this node before retrying restore.')
    guests=api.request('GET','/cluster/resources',{'type':'vm'})
    active=api.request('GET',f'/nodes/{node}/tasks',{'source':'active','limit':1000})
    volumes=api.request('GET',f'/nodes/{node}/storage/{storage}/content')
    if (any(int(row.get('vmid',-1))==vmid for row in guests) or len(active)>=1000 or
            any(str(row.get('id'))==str(vmid) for row in active) or
            any(int(row.get('vmid',-1))==vmid or re.search(fr'(?:vm|base)-{vmid}-',str(row.get('volid',''))) for row in volumes)):
        raise DistributionError('Failed container restore still has a guest, volume or active task; retain its receipt.')
    saved=ledger.get_job(job)
    if (saved.get('state')!='running' or saved.get('project_id')!=project or
            saved.get('allocations')!=[receipt.get('reservation')] or
            receipt.get('source_config_sha256')!=_source_config_identity(api.container(source_node,source_id),source_id)):
        raise DistributionError('Container restore reservation or frozen source differs.')
    tasks[-1].update(state='confirmed-terminal-error',exitstatus=failed['exitstatus'])
    receipt['recovery']='terminal-compatibility-failure-no-guest-or-volumes'
    return True


def _recover_disconnected_ct_copy(api,receipt,*,project,job,name,source_node,source_id,node,bridge,ledger):
    vmid=receipt.get('template_id');marker=receipt.get('marker')
    if (receipt.get('state')!='allocated' or receipt.get('job_id')!=job or receipt.get('kind')!='container' or
            receipt.get('source_id')!=source_id or receipt.get('node')!=node or type(vmid) is not int or vmid==source_id):
        raise DistributionError('Retained CT readiness recovery scope differs.')
    copy=api.container(node,vmid);_owned(copy,marker);_require_probe_pool(api,node,'lxc',vmid)
    nets=[str(v) for k,v in copy.items() if re.fullmatch(r'net[0-9]+',k)]
    active=api.request('GET',f'/nodes/{node}/tasks',{'source':'active','limit':1000})
    if (copy.get('hostname')!=name or int(copy.get('template',0)) or copy.get('lock') or
            api.container_status(node,vmid)!='stopped' or not nets or
            any(f'bridge={bridge}' not in v.split(',') or 'link_down=1' not in v.split(',') for v in nets) or
            len(active)>=1000 or any(str(t.get('id'))==str(vmid) for t in active) or
            receipt.get('source_config_sha256')!=_source_config_identity(api.container(source_node,source_id),source_id)):
        raise DistributionError('Retained CT is not its unchanged stopped isolated copy.')
    tasks=receipt.get('provider_tasks',[])
    if not tasks or tasks[-1].get('path')!=f'/nodes/{node}/lxc/{vmid}/status/shutdown':
        raise DistributionError('Retained CT readiness failure has no confirmed shutdown.')
    for task in tasks:
        if task.get('state')=='confirmed-terminal-error':
            if 'format error host-managed:' not in task.get('exitstatus',''):
                raise DistributionError('Unexpected provider failure in CT readiness history.')
            continue
        task_node=source_node if task.get('path')==f'/nodes/{source_node}/vzdump' else node
        api.task(task_node,task['upid'],timeout=30)
    saved=ledger.get_job(job)
    if saved.get('state')!='running' or saved.get('project_id')!=project or saved.get('allocations')!=[receipt.get('reservation')]:
        raise DistributionError('CT readiness recovery reservation differs.')
    return True


def capture_container(api, *, project, node, source_id, name, description, slug,
                      profile, policy_override=None, ct_exec=None, generalize=None, platform_source=False, policy_path=None):
    """Capture a stopped CT through the same export/probe gates as VM images.

    An uncertain allocation is retained with its receipt. No template is
    approved until an independent disposable linked clone passes verification.
    """
    from crange_distribution import SSHContainerExec, generalize_capture_container, main as distribution_main
    if profile.get('init_system', 'systemd') not in ('systemd', 'openrc'):
        raise DistributionError('Container capture requires a supported Linux readiness profile.')
    source_node=node
    if platform_source:
        _approved_platform_source(api.container(source_node, source_id), source_node, source_id, 'container')
        if policy_override is None:
            raise DistributionError('Platform preparation requires its worker policy.')
        retained_path = DATA / 'capture-journal' / (f'capture-p{project:03d}-{slug}.json')
        node = (json.loads(retained_path.read_text())['node'] if retained_path.exists() else
                _capture_destination(api, source_node, api.container(source_node, source_id), profile, policy_override, kind='container'))
    policy, bridge, storage, backup = capture_policy(
        node, profile, policy_override, kind='container')
    job = f'capture-p{project:03d}-{slug}'
    if not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', job):
        raise DistributionError('Invalid capture job ID.')
    journal = FileJournal(DATA / 'source-journal')
    receipt_path = DATA / 'capture-journal' / (job + '.json')
    if receipt_path.exists():
        node=json.loads(receipt_path.read_text()).get('node')
        if node not in policy.get('allowed_nodes',[]):
            raise DistributionError('Retained container destination is no longer enrolled.')
    elif not platform_source:
        node=_capture_destination(api,source_node,api.container(source_node,source_id),profile,policy,kind='container')
    policy,bridge,storage,backup=capture_policy(node,profile,policy,kind='container')
    print(f'Container capture destination: {node}/{storage} (source {source_node}/{source_id})',flush=True)
    ledger = ReservationLedger(DATA / 'reservations.sqlite3')
    identity_job, export_job, probe_job = job+'-identity', job+'-export', job+'-probe'
    try:
        retry_transport=False
        reuse_copy=False
        if receipt_path.exists():
            receipt=json.loads(receipt_path.read_text())
            retry_transport=_ct_transport_restore_recovery(api,receipt,project=project,job=job,source_node=source_node,source_id=source_id,node=node,storage=storage,ledger=ledger)
            if not retry_transport and not (journal.directory/(identity_job+'.json')).exists():
                reuse_copy=_recover_disconnected_ct_copy(api,receipt,project=project,job=job,name=name,source_node=source_node,source_id=source_id,node=node,bridge=bridge,ledger=ledger)
        with node_locks([source_node,node], library=True), journal.allocation_lock():
            if receipt_path.exists() and not retry_transport and not reuse_copy:
                receipt=json.loads(receipt_path.read_text())
                vmid=receipt.get('template_id');marker=f'crange-capture-{slug}'
                identity=json.loads((journal.directory/(identity_job+'.json')).read_text())
                tasks=receipt.get('provider_tasks',[])
                expected=f'/nodes/{node}/lxc/{vmid}/template'
                if (type(vmid) is not int or receipt.get('job_id')!=job or receipt.get('kind')!='container'
                        or receipt.get('source_id')!=source_id or receipt.get('node')!=node
                        or receipt.get('marker')!=marker
                        or receipt.get('source_config_sha256')!=_source_config_identity(api.container(source_node,source_id),source_id)
                        or not receipt.get('generalization',{}).get('machine_id_empty')
                        or not tasks or tasks[-1].get('path')!=expected or tasks[-1].get('upid') is not None
                        or any(t.get('state')!='confirmed-terminal' for t in tasks[:-1])):
                    raise DistributionError('Retained CT capture is outside the exact synchronous conversion recovery scope.')
                config=api.container(node,vmid)
                _owned(config,marker);_require_probe_pool(api,node,'lxc',vmid)
                if (int(config.get('template',0))!=1 or config.get('lock')
                        or config.get('hostname')!=name or api.container_status(node,vmid)!='stopped'
                        or _source_config_identity(config,vmid)!=identity.get('source_config_sha256')):
                    raise DistributionError('Retained CT template is not unchanged, stopped and ready for export.')
                tasks[-1].update(state='confirmed-terminal',completion='verified-template-config')
                atomic_json(receipt_path,receipt)
                print(f'Resuming retained immutable container {node}/{vmid}; no clone or conversion was repeated.',flush=True)
            else:
                source = api.container(source_node, source_id)
                if platform_source:
                    _approved_platform_source(source, source_node, source_id, 'container')
                if (not platform_source and int(source.get('template', 0))) or api.container_status(source_node, source_id) != 'stopped':
                    raise DistributionError('Container capture requires a stopped staging source, not a template.')
                capacity = _source_capacity(source, 'lxc')
                if profile.get('_transport_template') and node!=source_node:
                    capacity['disk_gb'] *= 2
                source_sha = _source_config_identity(source, source_id)
                if retry_transport or reuse_copy:
                    vmid=receipt['template_id'];marker=receipt['marker']
                    if receipt['source_config_sha256']!=source_sha:
                        raise DistributionError('Frozen source changed before restore recovery.')
                else:
                    status = api.request('GET', f'/nodes/{node}/storage/{backup}/status')
                    total, available = float(status.get('total', 0)), float(status.get('avail', 0))
                    reserve = archive_headroom(total)
                    if (int(status.get('shared', 0)) != 1 or int(status.get('active', 0)) != 1
                            or 'backup' not in str(status.get('content', '')).split(',')
                            or total <= 0 or available-capacity['disk_gb']*1024**3 < reserve):
                        raise DistributionError(f'Central archive {backup} on {node}: active={status.get("active")}, shared={status.get("shared")}, available={available/1024**3:.1f} GiB, required={capacity["disk_gb"]:.1f} GiB plus {reserve/1024**3:.1f} GiB reserve.')
                    allocation = ledger.reserve_source_probe(project_id=project, job_id=job, owner='release-capture',
                        source_ref=f'source:{source_node}:lxc:{source_id}',source_node=source_node, source_sha256=source_sha, kind='container',
                        node_name=node, datastore_id=storage, capacity=capacity,
                        inventory=discover(api, policy), policy=policy)
                    ledger.mark_running(job)
                    vmid = int(api.request('GET', '/cluster/nextid'))
                    if vmid < 100 or 20000 <= vmid < 20100:
                        raise DistributionError('Capture allocator returned an invalid or probe-reserved VMID.')
                    marker = f'crange-capture-{slug}'
                    receipt = dict(job_id=job, kind='container', source_id=source_id,
                        source_config_sha256=source_sha, source_node=source_node,node=node, capture_name=name,template_id=vmid,
                        reservation=allocation, marker=marker, state='allocated', provider_tasks=[])
                    atomic_json(receipt_path, receipt)
                def task(path, fields, task_node=None, method='POST'):
                    upid = api.request(method, path, fields)
                    receipt['provider_tasks'].append(dict(upid=upid, path=path, state='running'))
                    atomic_json(receipt_path, receipt)
                    if upid is None and path.endswith('/template'):
                        deadline=time.monotonic()+600
                        while True:
                            current=api.container(node,vmid)
                            if int(current.get('template',0))==1 and not current.get('lock') and api.container_status(node,vmid)=='stopped':break
                            if time.monotonic()>=deadline:raise DistributionError('Synchronous CT template conversion has not settled.')
                            time.sleep(2)
                        receipt['provider_tasks'][-1]['completion']='verified-template-config'
                    else:api.task(task_node or node, upid, timeout=1800)
                    receipt['provider_tasks'][-1]['state'] = 'confirmed-terminal'
                    atomic_json(receipt_path, receipt)
                if reuse_copy:
                    print('Reusing stopped isolated CT after verified readiness failure; no clone or restore repeated.',flush=True)
                elif node==source_node:
                    task(f'/nodes/{node}/lxc/{source_id}/clone',dict(newid=vmid,hostname=name,description=description,pool=PROBE_POOL,full=1,storage=storage))
                else:
                    _container_transport(api,source_node=source_node,source_id=source_id,node=node,vmid=vmid,storage=storage,backup=backup,project=project,receipt=receipt,save=lambda:atomic_json(receipt_path,receipt),task=task,platform_source=platform_source)
                if (_source_config_identity(api.container(source_node, source_id), source_id) != source_sha
                        or api.container_status(source_node, source_id) != 'stopped'):
                    raise DistributionError('Container staging source changed during cloning; retain allocation.')
                base = f'/nodes/{node}/lxc/{vmid}'
                config = api.container(node, vmid)
                fields = {'tags': f'crange-image;{marker};p{project:03d}', 'onboot': 0}
                if platform_source and source_id == 3000 and profile.get('init_system') == 'systemd':
                    # Debian 13 systemd needs nesting in an unprivileged LXC.
                    # Configure only this disposable derivative, never its source.
                    features = dict(token.split('=', 1) for token in str(config.get('features') or '').split(',') if '=' in token)
                    features['nesting'] = '1'
                    fields['features'] = ','.join(f'{key}={value}' for key, value in sorted(features.items()))
                for key in config:
                    if re.fullmatch(r'net[0-9]+', key):
                        from crange_distribution import disconnected_container_nic
                        fields[key] = disconnected_container_nic(source.get(key,config[key]),key[3:],bridge)
                if not any(re.fullmatch(r'net[0-9]+', key) for key in fields):
                    raise DistributionError('Captured container has no defensible network isolation.')
                api.request('PUT', base+'/config', fields)
                config = api.container(node, vmid)
                _owned(config, marker); _require_probe_pool(api, node, 'lxc', vmid)
                for key, value in config.items():
                    if re.fullmatch(r'net[0-9]+', key) and (f'bridge={bridge}' not in str(value).split(',')
                            or 'link_down=1' not in str(value).split(',')):
                        raise DistributionError('Container capture network isolation was not applied exactly.')
                task(base+'/status/start', {})
                try:
                    if platform_source:
                        adapter = ct_exec or SSHContainerExec(api=api)
                        receipt['identity_startup_preparation'] = adapter.prepare_capture_identity(node,vmid)
                        atomic_json(receipt_path,receipt)
                    deadline = time.monotonic()+180
                    while True:
                        try:
                            identity = capture_source_identity(api, node=node, kind='container', vmid=vmid,
                                job_id=identity_job, journal=journal, ct_exec=ct_exec or SSHContainerExec(api=api),
                                init_system=profile.get('init_system', 'systemd'))
                            break
                        except DistributionError as error:
                            if profile.get('init_system','systemd')=='systemd' and getattr(error,'collector_state',None)=='degraded':
                                from crange_distribution import normalize_isolated_network_wait
                                preparation=normalize_isolated_network_wait(api,node,'lxc',vmid,bridge,
                                    ct_exec=ct_exec or SSHContainerExec(api=api))
                                if preparation:
                                    receipt['offline_network_readiness']=preparation
                                    atomic_json(receipt_path,receipt)
                            if time.monotonic()>=deadline: raise
                            time.sleep(2)
                    print(f'Captured container identity on {node}/{vmid}; preparing fresh clone identities.',flush=True)
                    prepared = (generalize or generalize_capture_container)(node, vmid, marker)
                    if (prepared.get('generalized') is not True or prepared.get('machine_id_empty') is not True
                            or prepared.get('previous_machine_id') != identity['source_identity']['machine_id']):
                        raise DistributionError('Container clone identity preparation was not verified.')
                    receipt['generalization']=prepared
                    atomic_json(receipt_path,receipt)
                finally:
                    _owned(api.container(node, vmid), marker); _require_probe_pool(api, node, 'lxc', vmid)
                    task(base+'/status/shutdown', {'timeout': 90, 'forceStop': 0})
                if api.container_status(node, vmid) != 'stopped':
                    raise DistributionError('Captured container did not shut down cleanly.')
                task(base+'/template', {})
                config = api.container(node, vmid)
                if int(config.get('template', 0)) != 1 or _source_config_identity(config, vmid) != identity['source_config_sha256']:
                    raise DistributionError('Container template changed after identity capture.')
            print(f'Exporting stopped container template {node}/{vmid} to the central library.',flush=True)
            export_image(api, node=node, kind='container', vmid=vmid, storage=backup,
                job_id=export_job, journal=journal, hash_volume=SSHVolumeHasher(),
                approved_backup_storage=map(tuple, policy.get('approved_backup_storage', [])),
                identity_record=identity)
            print('Container archive verified; checking an independent disposable clone.',flush=True)
        if receipt.get('state')=='verified':
            _verified_capture_probe({**receipt,'probe_job':receipt.get('probe_job',probe_job)},identity,project)
            print('Reusing accepted immutable container and completed clone verification; no new copy or probe.',flush=True)
            return vmid,{'node':node,'export_job':export_job,'probe_job':receipt.get('probe_job',probe_job)}
        inventory = discover(api, policy)
        if str(vmid) not in inventory.get('guests', {}):
            raise DistributionError('Captured CT is missing from live inventory; retain its reservation.')
        ledger.reconcile_job(job, operation_terminal=True, allocations_reconciled=True)
        common=['--catalog',str(CONFIG/'image-catalog.json'),'--journal-dir',str(journal.directory),
                '--policy',str(policy_path or CONFIG/'distribution-policy.json')]
        probe_job=receipt.get('probe_job',probe_job)
        probe_path=journal.directory/(probe_job+'.json')
        resumed=False
        if probe_path.exists():
            previous=json.loads(probe_path.read_text())
            if previous.get('state')=='allocated' and not previous.get('provider_tasks') and not previous.get('upid'):
                distribution_main(common+['source-probe-reconcile','--job-id',probe_job])
                base_job=job+'-probe';attempt=1
                while (journal.directory/(base_job+f'-r{attempt}.json')).exists():attempt+=1
                probe_job=base_job+f'-r{attempt}'
                receipt['probe_job']=probe_job;atomic_json(receipt_path,receipt)
            elif previous.get('provider_tasks'):
                distribution_main(common+['source-probe-resume','--project',str(project),
                    '--source-node',node,'--source-id',str(vmid),'--isolated-bridge',bridge,
                    '--job-id',probe_job,'--identity-record',str(journal.directory/(identity_job+'.json'))])
                resumed=True
            else:
                raise DistributionError('Retained source probe needs exact task reconciliation before another attempt.')
        if not resumed:
            _wait_capture_probe_load(api,node,policy)
            distribution_main(common+['source-probe', '--project', str(project), '--source-node', node, '--source-id', str(vmid), '--datastore-id', storage,
            '--isolated-bridge', bridge, '--job-id', probe_job, '--clone-mode','full' if profile.get('_full_clone_probe') else 'linked',
            '--identity-record', str(journal.directory/(identity_job+'.json'))])
        receipt.update(state='verified',identity_job=identity_job,export_job=export_job,probe_job=probe_job)
        atomic_json(receipt_path, receipt)
        return vmid, {'node':node,'export_job': export_job, 'probe_job': probe_job}
    finally:
        ledger.close()
