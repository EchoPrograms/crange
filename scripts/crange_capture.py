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


PLATFORM_BASE_SOURCES = {
    ('proxmox4', 112): {'name': 'parrot-jump-template', 'required_tags': {'range-lan', 'jumpbox'}},
    ('proxmox4', 115): {
        'name': 'crange-build-mhl-server-2025-eval',
        'required_tags': {'crange-template-build',
                          'crange-build-286a35040ed44d52b36fb30d097d771d',
                          'crange-image', 'mhl-windows-sealed'},
        'template': False, 'os_family': 'windows',
    },
}


def _generalize_platform_derivative(api, node, vmid, *, timeout=120):
    """Reset only Parrot clone identity on the fixed owned derivative.

    The command is constant and administrator-owned. It preserves the source
    account, authorized keys, and service configuration while making the
    published template regenerate machine and SSH host identities at first boot.
    """
    if node != 'proxmox4' or type(vmid) is not int or vmid < 100:
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


def _generalize_windows_platform_derivative(api, node, vmid, *, timeout=900):
    """Run the fixed local Windows Sysprep answer file on an owned derivative.

    The source Windows base is already generalized. This second pass prepares
    the healthy identity-captured derivative as the immutable image that the
    later source-probe will clone. Its OOBE answerfile is on the guest disk.
    """
    if node != 'proxmox4' or type(vmid) is not int or vmid < 100:
        raise DistributionError('Invalid fixed Windows derivative scope.')
    _require_probe_pool(api, node, 'qemu', vmid)
    base = f'/nodes/{node}/qemu/{vmid}'
    config = api.request('GET', base + '/config')
    _owned(config)
    tags = set(str(config.get('tags', '')).split(';'))
    if ('crange-capture-windows-server2025-20261008' not in tags or
            int(config.get('template', 0)) or
            api.request('GET', base + '/status/current').get('status') != 'running'):
        raise DistributionError('Windows Sysprep requires the exact running owned capture derivative.')
    command = [r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
        '-NoProfile', '-NonInteractive', '-Command',
        "$ErrorActionPreference='Stop';$f='C:\\ProgramData\\CRANGE\\clone-unattend.xml';"
        "if(-not(Test-Path -LiteralPath $f)){throw 'Fixed Windows clone answerfile missing'};"
        "Start-Process -FilePath 'C:\\Windows\\System32\\Sysprep\\Sysprep.exe' "
        "-ArgumentList '/generalize /oobe /shutdown /unattend:C:\\ProgramData\\CRANGE\\clone-unattend.xml' "
        "-PassThru|Out-Null;Write-Output 'CRANGE_WINDOWS_SYSPREP_STARTED'"]
    response = api.request('POST', base + '/agent/exec', {'command': command})
    try:
        pid = int(response['pid'])
    except (TypeError, KeyError, ValueError) as exc:
        raise DistributionError('QGA did not accept the fixed Windows Sysprep request.') from exc
    deadline = time.monotonic() + min(timeout, 120)
    launched = False
    while time.monotonic() < deadline:
        try:
            result = api.request('GET', base + '/agent/exec-status', {'pid': pid})
        except Exception:
            # Sysprep may shut QGA down after PowerShell launched it. We still
            # require a provider-observed stopped state below.
            result = None
        if result and result.get('exited'):
            if int(result.get('exitcode', -1)) != 0 or result.get('out-data', '').strip() != 'CRANGE_WINDOWS_SYSPREP_STARTED':
                raise DistributionError('Fixed Windows Sysprep did not launch successfully.')
            launched = True
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
            return {'state': 'sysprep-generalized', 'os_family': 'windows',
                    'answerfile': 'C:\\ProgramData\\CRANGE\\clone-unattend.xml',
                    'shutdown_observed': True}
        time.sleep(2)
    raise DistributionError('Windows Sysprep did not shut down the owned derivative; it was retained.')


def capture_policy(node, profile, policy_override=None):
    policy = policy_override if policy_override is not None else read_settings()[0]
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


def capture_vm(api, *, project, node, source_id, name, description, slug, profile,
               platform_source=False, policy_override=None, policy_path=None, job_id=None,
               allocated_vmid=None):
    """Clone a stopped staging VM, isolate before boot, capture real identity.

    Failure retains the exact journal, guest, and capacity reservation for review;
    no retry deletes or reuses an uncertain allocation.
    """
    policy, bridge, storage, backup = capture_policy(node, profile, policy_override)
    job = job_id or f'capture-p{project:03d}-{slug}'
    if not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', job):
        raise DistributionError('Invalid capture job ID.')
    if platform_source and (policy_override is None or policy_path is None or
                            PLATFORM_BASE_SOURCES.get((node, source_id)) is None):
        raise DistributionError('Platform source capture requires its fixed source map and protected temporary policy.')
    identity_job, export_job, probe_job = job + '-identity', job + '-export', job + '-probe'
    journal = FileJournal(DATA / 'source-journal')
    receipt_path = DATA / 'capture-journal' / (job + '.json')
    ledger = ReservationLedger(DATA / 'reservations.sqlite3')
    try:
        with node_locks([node], library=True), journal.allocation_lock():
            source = api.vm(node, source_id)
            if platform_source:
                admitted = PLATFORM_BASE_SOURCES.get((node, source_id))
                tags = set(str(source.get('tags', '')).split(';'))
                expected_template = int(admitted.get('template', True)) if admitted else 1
                if (not admitted or source.get('name') != admitted['name'] or
                        not admitted['required_tags'] <= tags or
                        int(source.get('template', 0)) != expected_template):
                    raise DistributionError('Platform base source is outside the fixed administrator source map.')
            elif int(source.get('template', 0)):
                raise DistributionError('Staging source must not be an immutable template.')
            if api.status(node, source_id) != 'stopped':
                raise DistributionError('Source must be stopped before release cloning.')
            if not str(source.get('agent', '')).split(',')[0] in ('1', 'enabled=1'):
                raise DistributionError('Staging VM QGA is disabled; rebuild with a tested approved agent profile.')
            source_sha = _source_config_identity(source, source_id)
            resume = receipt_path.exists()
            resume_template = False
            if resume:
                # Recover only the fixed platform clone whose provider task has
                # conclusively finished. This handles a worker interruption
                # after PVE completed clone but before the receipt was updated.
                if not platform_source:
                    raise DistributionError('Capture already allocated; reconcile protected capture journal before retry.')
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
            else:
                capacity = _source_capacity(source, 'qemu')
                backup_status = api.request('GET', f'/nodes/{node}/storage/{backup}/status')
                # The single-storage config endpoint requires write-level
                # Datastore.Allocate even for GET. The status endpoint is read-only
                # under Datastore.Audit and includes the configured shared flag.
                if int(backup_status.get('shared', 0)) != 1:
                    raise DistributionError('Capture backup storage must be administrator-configured shared central storage.')
                total, available = float(backup_status.get('total', 0)), float(backup_status.get('avail', 0))
                reserve = max(total * .20, 20 * 1024 ** 3)
                if (int(backup_status.get('active', 0)) != 1 or 'backup' not in str(backup_status.get('content', '')).split(',')
                        or total <= 0 or available - capacity['disk_gb'] * 1024 ** 3 < reserve):
                    raise DistributionError('Central archive storage lacks capture space and reserved headroom.')
                allocation = ledger.reserve_source_probe(project_id=project, job_id=job, owner='release-capture',
                    source_ref=f'source:{node}:qemu:{source_id}', source_sha256=source_sha,
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
                    node=node, template_id=vmid, platform_source=bool(platform_source),
                    reservation=allocation, marker=marker, state='allocated', provider_tasks=[])
                atomic_json(receipt_path, receipt)
            def task(method, path, fields):
                upid = api.request(method, path, fields)
                receipt['provider_tasks'].append(dict(upid=upid, path=path, state='running'))
                atomic_json(receipt_path, receipt)
                api.task(node, upid, timeout=1800)
                receipt['provider_tasks'][-1]['state'] = 'confirmed-terminal'
                atomic_json(receipt_path, receipt)
            if not resume:
                task('POST', f'/nodes/{node}/qemu/{source_id}/clone', dict(newid=vmid, name=name,
                    description=description, pool=PROBE_POOL, full=1, storage=storage))
            if _source_config_identity(api.vm(node, source_id), source_id) != source_sha or api.status(node, source_id) != 'stopped':
                raise DistributionError('Staging source changed during capture cloning; reservation retained.')
            base = f'/nodes/{node}/qemu/{vmid}'
            config = api.vm(node, vmid)
            if resume_template:
                identity = json.loads(identity_path.read_text())
                if (identity.get('state') != 'identity-captured' or identity.get('source_config_sha256') !=
                        _source_config_identity(config, vmid)):
                    raise DistributionError('Captured template does not match its original worker identity record.')
                from crange_catalog import validate_source_identity
                validate_source_identity(identity.get('source_identity'))
                generalization = receipt.get('generalization') or {
                    'state': 'generalized',
                    'os_family': 'windows' if profile.get('init_system') == 'windows' else 'linux',
                    'recovered_from_confirmed_template_task': True}
                receipt['generalization'] = generalization
                atomic_json(receipt_path, receipt)
            else:
                fields = {'tags': f'crange-image;{marker};p{project:03d}', 'onboot': 0}
                # Disable package upgrades only on the owned disconnected clone.
                if platform_source:
                    fields['ciupgrade'] = 0
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
                            job_id=identity_job, journal=journal, init_system=profile.get('init_system', 'systemd'))
                        if platform_source and profile.get('init_system') == 'windows':
                            generalization = _generalize_windows_platform_derivative(api, node, vmid)
                        elif platform_source:
                            generalization = _generalize_platform_derivative(api, node, vmid)
                        else:
                            generalization = None
                        break
                    except (DistributionError, RuntimeError):
                        if time.monotonic() >= deadline:
                            # Only the exact, owned, terminal-started allocation may be stopped.
                            _require_probe_pool(api, node, 'qemu', vmid)
                            _owned(api.vm(node, vmid), marker)
                            task('POST', base + '/status/shutdown', {'timeout': 90, 'forceStop': 0})
                            raise
                        time.sleep(2)
                if not (platform_source and profile.get('init_system') == 'windows'):
                    task('POST', base + '/status/shutdown', {'timeout': 90, 'forceStop': 0})
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
        main(['--catalog', str(CONFIG / 'image-catalog.json'), '--journal-dir', str(journal.directory),
              '--policy', str(policy_path or CONFIG / 'distribution-policy.json'), 'source-probe',
              '--project', str(project), '--source-node', node, '--source-id', str(vmid),
              '--datastore-id', storage, '--isolated-bridge', bridge, '--job-id', probe_job,
              '--identity-record', str(journal.directory / (identity_job + '.json'))])
        receipt['state'] = 'verified'
        atomic_json(receipt_path, receipt)
        return vmid, {'export_job': export_job, 'probe_job': probe_job}
    finally:
        ledger.close()
