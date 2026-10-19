"""Privileged fixed network verification job lane.

Only project/node/declared LAN choices cross the authenticated API.
This worker derives every probe identity and fixed Terraform input itself.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
PROBE_ROOT = Path('/var/lib/crange-network-probes')
NODE_PATTERN = re.compile(r'proxmox[1-6]\Z')
JOB_PATTERN = re.compile(r'[a-f0-9]{32}\Z')


def require(value, message):
    if not value:
        raise ValueError(message)


def declared_vnets(project, cfg):
    require(type(project) is int and cfg.get('project_id') == project,
            'Project configuration identity differs.')
    teams = cfg.get('team_ids')
    require(isinstance(teams, list), 'Project team declarations are invalid.')
    result = {f'p{project:03d}t{int(team):03d}' for team in teams}
    result.add(f'p{project:03d}t254')
    used = {0, 254, *map(int, teams)}
    for network in cfg.get('custom_networks', {}).values():
        owner, number = network.get('owner_team_id'), network.get('network_id')
        require(str(owner) in {*map(str, teams), '254'},
                'Custom LAN owner is not a declared team.')
        require(type(number) is int and 1 <= number <= 253 and number not in used,
                'Custom LAN number is invalid or collides with another subnet.')
        used.add(number)
        result.add(f'p{project:03d}c{number:03d}')
    return result


def declared_router_path(project, cfg, routers, vnet):
    """Derive a router chain; inputs are declarations and applied outputs only."""
    require(vnet in declared_vnets(project, cfg), 'LAN is not declared in this project.')
    number = int(vnet[-3:])
    if vnet[4] == 't':
        owner = number
        key = None
    else:
        matches = [(key, net) for key, net in cfg.get('custom_networks', {}).items()
                   if net['network_id'] == number]
        require(len(matches) == 1, 'Custom LAN declaration is ambiguous.')
        key, network = matches[0]
        owner = int(network['owner_team_id'])
    parent = routers['staging'] if owner == 254 else routers['teams'][str(owner)]
    require(parent.get('lan') == f'10.{project}.{owner}.0/24',
            'Applied parent router LAN differs from its declaration.')
    chain = [routers['project'], parent]
    if key is not None:
        router = routers.get('custom_networks', {}).get(key)
        require(isinstance(router, dict), 'Custom LAN router has not been deployed.')
        require(router.get('vnet') == vnet and router.get('network_id') == number
                and int(router.get('owner_team_id', -1)) == owner
                and router.get('lan') == f'10.{project}.{number}.0/24'
                and router.get('upstream_address') ==
                    f'10.{project}.{owner}.{network["uplink_last_octet"]}'
                and router.get('address') == router.get('upstream_address'),
                'Applied custom router differs from its declaration.')
        chain.append(router)
    return chain


def scope_from_declared_state(project, node, vnet, job_id, cfg, *, used_addresses,
                              used_vmids, reserved_scopes, next_vmid):
    """Pick free LAN address and VMID; callers supply live and desired usage."""
    require(NODE_PATTERN.fullmatch(node or ''), 'Choose an enrolled candidate node.')
    require(vnet in declared_vnets(project, cfg), 'Choose a declared team, staging or custom LAN.')
    network_id = int(vnet[-3:])
    occupied = set(used_addresses)
    for field in ('jump_boxes', 'scenario_boxes', 'scenario_containers'):
        for box in cfg.get(field, {}).values():
            if int(box.get('network_id', box.get('team_id', -1))) != network_id:
                continue
            octet = (10 + int(box['jump_number']) if field == 'jump_boxes'
                     else int(box['ip_last_octet']))
            occupied.add(f'10.{project}.{network_id}.{octet}')
    reserved = list(reserved_scopes)
    occupied_vmids = set(used_vmids)
    occupied_vmids.update(int(item['vmid']) for item in reserved if type(item.get('vmid')) is int)
    occupied.update(str(item.get('address', '')).split('/')[0] for item in reserved)
    address = next((f'10.{project}.{network_id}.{host}' for host in range(2, 101)
                    if f'10.{project}.{network_id}.{host}' not in occupied), None)
    require(address is not None, 'No free static probe address remains on this LAN.')
    require(type(next_vmid) is int and 100 <= next_vmid <= 999999999,
            'Cluster VMID allocator returned an invalid ID.')
    vmid = next((candidate for candidate in range(next_vmid, min(next_vmid + 10000, 1000000000))
                 if candidate not in occupied_vmids), None)
    require(vmid is not None, 'No free probe VMID was found in the allocator window.')
    require(JOB_PATTERN.fullmatch(job_id or ''), 'Invalid queue job identity.')
    from crange_network_acceptance import scope
    return scope(project, node, vnet, address + '/24', vmid, 'network-' + job_id)


def job_row(database, job_id):
    with sqlite3.connect(database, timeout=10) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM jobs WHERE id=? AND operation=?',
                         (job_id, 'network-verify')).fetchone()
    require(row is not None, 'Network verification job was not found.')
    return dict(row)


def cancellation_requested(database, job_id):
    with sqlite3.connect(database, timeout=10) as db:
        row = db.execute('SELECT cancel_requested FROM jobs WHERE id=? AND operation=?',
                         (job_id, 'network-verify')).fetchone()
    return bool(row and row[0])


def project_inputs(path):
    from crange_network_adapters import project_input_hashes
    return project_input_hashes(path)


def _json_hcl(value):
    return json.dumps(value, ensure_ascii=True)


def _git_revision_args(repository):
    return ['git', '-c', 'safe.directory=' + str(Path(repository).resolve()),
            'rev-parse', 'HEAD']


def _run(args, *, cwd, env=None, timeout=600):
    label = str(args[1]) if len(args) > 1 and re.fullmatch(r'[a-z-]{1,24}', str(args[1])) else 'command'
    print('Preparing fixed network probe: ' + label + '.', flush=True)
    try:
        result = subprocess.run(args, cwd=cwd, env=env, capture_output=True,
                                text=True, timeout=timeout, check=False)
    except OSError as error:
        _write_command_stderr(cwd, label, f'worker_error_class={type(error).__name__}\n')
        raise ValueError('Fixed network worker command could not start.') from None
    if result.returncode:
        # Keep exact provider diagnostics root-only. The queue/API returns only
        # the fixed error below and cannot serve this private bundle file.
        _write_command_stderr(cwd, label,
                              f'returncode={result.returncode}\n{result.stderr or ""}')
        raise ValueError('Fixed network worker command failed.')
    return result


def _write_command_stderr(cwd, label, contents):
    directory = Path(cwd)
    info = directory.lstat()
    require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode),
            'Private worker diagnostics need a real bundle directory.')
    path = directory / f'worker-{label}.stderr'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid(),
                'Private worker diagnostic ownership is invalid.')
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', closefd=False) as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _project_static_addresses(project, cfg):
    result = []
    for field in ('jump_boxes', 'scenario_boxes', 'scenario_containers'):
        for box in cfg.get(field, {}).values():
            network_id = int(box.get('network_id', box.get('team_id', -1)))
            host = 10 + int(box['jump_number']) if field == 'jump_boxes' else int(box['ip_last_octet'])
            result.append(f'10.{project}.{network_id}.{host}')
    return result


def _source_for_project(project, node, api, catalog):
    candidates = []
    for image_id, image in catalog.get('images', {}).items():
        linux_image = (image.get('os_family') == 'linux' or
                       image.get('init_system') in ('systemd', 'openrc') or
                       image.get('source_identity', {}).get('os_family') == 'linux')
        if (image.get('kind') != 'vm' or image.get('verified') is not True or
                not linux_image or
                (image.get('approved_projects') != ['*'] and project not in image.get('approved_projects', []))):
            continue
        for replica in image.get('replicas', []):
            if (replica.get('node') == node and replica.get('state') == 'ready' and
                    replica.get('artifact_sha256') == image.get('artifact_sha256') and
                    type(replica.get('template_id')) is int and replica.get('datastore_id')):
                candidates.append((image_id, image, replica))
    for image_id, image, replica in sorted(candidates, key=lambda item: item[0]):
        config = api.request('GET', f'/nodes/{node}/qemu/{replica["template_id"]}/config')
        cloudinit = any('cloudinit' in str(config.get(key, '')).lower() for key in ('ide0', 'ide1', 'ide2', 'ide3'))
        if (config.get('template') in (1, '1', True) and not config.get('lock') and
                cloudinit and config.get('agent') and 'enabled=1' in str(config.get('agent'))):
            from crange_network_workflow import digest
            return image_id, image, replica, config, digest(config)
    raise ValueError('No approved ready Linux VM image with cloud-init and QEMU agent exists on this node.')


def _resource_config(target, source, source_config, *, memory_mb=2048, cores=2):
    source_net = str(source_config.get('net0', 'virtio=00:00:00:00:00:00,bridge=vmbr0')).split(',')
    model = source_net[0].split('=', 1)[0] if source_net else 'virtio'
    model = model if model in ('virtio', 'e1000', 'e1000e', 'vmxnet3') else 'virtio'
    def attr(key, default):
        value = source_config.get(key)
        return default if value in (None, '') else value
    name = f'crange-network-{target["job_id"].removeprefix("network-")[:12]}'
    lines = [
        'terraform {', '  required_providers {', '    proxmox = {',
        '      source = "bpg/proxmox"', '      version = "= 0.104.0"',
        '    }', '  }', '}',
        'provider "proxmox" {}',
        'resource "proxmox_virtual_environment_vm" "network_acceptance_probe" {',
        f'  name = {_json_hcl(name)}',
        f'  description = {_json_hcl("CRANGE fixed network acceptance probe " + target["job_id"]) }',
        f'  node_name = {_json_hcl(target["node"])}',
        '  pool_id = "crange-probes"',
        '  tags = ["crange-network-probe", ' + _json_hcl(target['job_id']) + ']',
        '  started = true', '  on_boot = false', '  stop_on_destroy = true',
        f'  vm_id = {target["vmid"]}',
        f'  bios = {_json_hcl(attr("bios", "seabios"))}',
        f'  scsi_hardware = {_json_hcl(attr("scsihw", "virtio-scsi-single"))}',
        '  clone {', f'    vm_id = {source["template_id"]}',
        f'    node_name = {_json_hcl(target["node"])}', '    full = true',
        f'    datastore_id = {_json_hcl(source["datastore_id"])}', '  }',
        '  cpu {', f'    cores = {cores}', '    sockets = 1',
        f'    type = {_json_hcl(attr("cpu", "x86-64-v2-AES"))}', '  }',
        '  memory {', f'    dedicated = {memory_mb}', '  }',
        '  operating_system {', f'    type = {_json_hcl(attr("ostype", "l26"))}', '  }',
        '  agent {', '    enabled = true', '    timeout = "5m"', '  }',
        '  cdrom {', '    interface = "ide2"', '    file_id = "none"', '  }',
        '  network_device {', f'    bridge = {_json_hcl(target["vnet"])}',
        f'    model = {_json_hcl(model)}', '    firewall = true', '    mtu = 1450', '  }',
        '  initialization {', f'    datastore_id = {_json_hcl(source["datastore_id"])}',
        '    interface = "ide0"', '    dns {', '      servers = ["10.60.2.1"]', '    }',
        '    ip_config {', '      ipv4 {', f'        address = {_json_hcl(target["address"])}',
        f'        gateway = {_json_hcl(target["address"].rsplit(".", 1)[0] + ".1")}',
        '      }', '    }', '  }', '}']
    return '\n'.join(lines) + '\n'


def probe_resources(image, source_config):
    """Use approved source sizing, rather than overprovisioning small probes."""
    profile = image.get('probe_resources') or {}
    require(isinstance(profile, dict), 'Approved probe resources must be an object.')
    source_memory = source_config.get('memory', 512)
    source_cores = source_config.get('cores', 1)
    if isinstance(source_memory, str) and re.fullmatch(r'[1-9][0-9]*', source_memory):
        source_memory = int(source_memory)
    if isinstance(source_cores, str) and re.fullmatch(r'[1-9][0-9]*', source_cores):
        source_cores = int(source_cores)
    require(type(source_memory) is int and source_memory > 0
            and type(source_cores) is int and source_cores > 0,
            'Approved source resources are invalid.')
    memory = profile.get('memory_mb', max(512, source_memory))
    cores = profile.get('cpu_cores', source_cores)
    require(type(memory) is int and 512 <= memory <= 4096
            and type(cores) is int and 1 <= cores <= 2,
            'Approved source probe size exceeds fixed verification limits.')
    return memory, cores


def build_bundle(job, *, api=None, config_root=None, project_root=None, probe_root=PROBE_ROOT):
    """Derive exact target, approved source, fixed config and inspected create plan."""
    require(os.geteuid() == 0, 'Network verification requires the privileged worker.')
    import crange
    import crange_scheduling as scheduling
    from crange_network_acceptance import probe_from_plan
    from crange_network_adapters import durable_json
    from crange_network_runner import plan_digest
    from crange_placement import ReservationLedger
    from crange_network_workflow import digest

    queue_id = job['id']
    options = json.loads(job['options']) if isinstance(job['options'], str) else job['options']
    project = int(job['project'])
    selected_node, vnet = options['node'], options['network']
    print('Checking project declaration, enrolled node policy, image catalog and live reservations.', flush=True)
    path = Path(project_root) if project_root else crange.project_dir(str(project))
    cfg = crange.json_config(path)
    require(vnet in declared_vnets(project, cfg), 'LAN is no longer declared in this project.')
    require(NODE_PATTERN.fullmatch(selected_node or ''), 'Invalid candidate node.')
    scheduling.CONFIG = Path(config_root or '/home/crange/.config/crange')
    scheduling.DATA = Path('/var/lib/crange-distribution')
    api = api or crange.ProxmoxAPI()
    policy, catalog = scheduling.read_settings()
    require(selected_node in policy.get('allowed_nodes', []), 'Candidate node is not approved for placement.')
    rows = api.request('GET', '/cluster/resources', {'type': 'vm'})
    used_ids = {int(row['vmid']) for row in rows if str(row.get('vmid', '')).isdigit()}
    ledger = ReservationLedger(scheduling.DATA / 'reservations.sqlite3')
    try:
        ledger.active_reservations()
        reserved_scopes = []
        for row in ledger.db.execute("SELECT allocations FROM jobs WHERE state!='released'"):
            for allocation in json.loads(row[0]):
                reserved_scopes.append(allocation.get('network_scope', {}))
                if type(allocation.get('vmid')) is int:
                    used_ids.add(allocation['vmid'])
        occupied = set(_project_static_addresses(project, cfg))
        for guest in rows:
            guest_type = guest.get('type')
            vmid = int(guest.get('vmid', 0))
            if not guest_type or not vmid:
                continue
            config = api.request('GET', f'/nodes/{guest["node"]}/{guest_type}/{vmid}/config')
            for key, value in config.items():
                if re.fullmatch(r'(?:net|ipconfig)\d+', key):
                    for part in str(value).split(','):
                        if part.startswith('ip=') and part[3:] not in ('dhcp', 'manual'):
                            occupied.add(part[3:].split('/')[0])
        target = scope_from_declared_state(project, selected_node, vnet, queue_id, cfg,
            used_addresses=occupied, used_vmids=used_ids, reserved_scopes=reserved_scopes,
            next_vmid=900000)
        image_id, image, replica, source_config, source_hash = _source_for_project(
            project, selected_node, api, catalog)
        memory_mb, cores = probe_resources(image, source_config)
        owner = (int(vnet[-3:]) if vnet[4] == 't' else next(
            int(net['owner_team_id']) for net in cfg['custom_networks'].values()
            if net['network_id'] == int(vnet[-3:])))
        other_teams = [int(team) for team in cfg.get('team_ids', []) if int(team) != owner]
        require(other_teams, 'Team isolation requires another declared project team.')
        origins = _management_origins(api)
        print('Derived one free address, one VMID, an approved Linux image and bounded capacity.', flush=True)
        protected_root = Path(probe_root)
        protected_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = protected_root.lstat()
        require(info.st_uid == 0 and stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode),
                'Network probe root must be a root-owned real directory.')
        os.chown(protected_root, 0, 0)
        os.chmod(protected_root, 0o700)
        bundle = protected_root / target['job_id']
        bundle.mkdir(mode=0o700)
        os.chown(bundle, 0, 0)
        lock_source = Path(crange.ROOT) / 'terraform/environments/project-template/.terraform.lock.hcl'
        if not lock_source.is_file():
            lock_source = path / '.terraform.lock.hcl'
        require(lock_source.is_file(), 'Pinned Proxmox provider lockfile is unavailable.')
        shutil.copyfile(lock_source, bundle / '.terraform.lock.hcl')
        (bundle / '.terraform.lock.hcl').chmod(0o600)
        main_tf = bundle / 'main.tf'
        main_tf.write_text(_resource_config(target,
            {'template_id': replica['template_id'], 'datastore_id': replica['datastore_id']},
            source_config, memory_mb=memory_mb, cores=cores))
        main_tf.chmod(0o600)
        print('Wrote the root-protected single-probe Terraform configuration and provider lock.', flush=True)
        input_hashes = {name: hashlib.sha256((bundle / name).read_bytes()).hexdigest()
                        for name in ('main.tf', '.terraform.lock.hcl')}
        project_input_hashes = project_inputs(path)
        git = subprocess.run(_git_revision_args(crange.ROOT), cwd=crange.ROOT,
                             capture_output=True, text=True, timeout=10, check=False)
        if git.returncode != 0:
            _write_command_stderr(bundle, 'git-rev-parse',
                                  f'returncode={git.returncode}\n{git.stderr or ""}')
        require(git.returncode == 0 and re.fullmatch(r'[a-f0-9]{40}', git.stdout.strip()),
                'Cannot bind the source revision.')
        from crange_network_adapters import controller_source_hashes, trusted_relay_source_hash
        dirty=subprocess.run(_git_revision_args(crange.ROOT)[:-2]+['status','--porcelain','--untracked-files=all'],
                             cwd=crange.ROOT,capture_output=True,text=True,timeout=10,check=False)
        require(dirty.returncode==0,'Cannot bind dirty source status.')
        source_binding={'schema_version':1,'controller_sha256':controller_source_hashes(),
                        'relay_sha256':trusted_relay_source_hash(),'git_dirty':bool(dirty.stdout.strip())}
        manifest = {
            'source_binding':source_binding,
            'scope': target, 'input_sha256': input_hashes, 'plans': {},
            'source': {'image_id': image_id, 'template_id': replica['template_id'],
                       'datastore_id': replica['datastore_id'], 'config_sha256': source_hash},
            'project_input_sha256': project_input_hashes,
            'source_revision': git.stdout.strip(), 'other_team_id': other_teams[0],
            'origins': origins,
        }
        durable_json(bundle / 'review.json', manifest)
        print('Bound the protected manifest to project inputs and source revision.', flush=True)
        from crange_network_adapters import SavedTerraformProbe
        saved = SavedTerraformProbe(target['job_id'])
        env = saved._env()
        _run(['terraform', 'init', '-input=false', '-no-color', '-lockfile=readonly'],
             cwd=bundle, env=env, timeout=600)
        create_plan = bundle / 'create.tfplan'
        _run(['terraform', 'plan', '-input=false', '-no-color', '-out=' + str(create_plan)],
             cwd=bundle, env=env, timeout=600)
        create_plan.chmod(0o600)
        shown = _run(['terraform', 'show', '-json', str(create_plan)], cwd=bundle,
                     env=env, timeout=60)
        plan = json.loads(shown.stdout)
        probe_from_plan(plan, 'create', target)
        resource = plan['resource_changes'][0]['change']['after']
        require(resource.get('clone', [{}])[0].get('vm_id') == replica['template_id'] and
                resource.get('memory', [{}])[0].get('dedicated') == memory_mb and
                resource.get('cpu', [{}])[0].get('cores') == cores,
                'Generated plan differs from the approved source and fixed size.')
        manifest['plans']['create'] = {
            'binary_sha256': hashlib.sha256(create_plan.read_bytes()).hexdigest(),
            'json_sha256': plan_digest(plan), 'review': 'fixed-network-probe-create',
        }
        durable_json(bundle / 'review.json', manifest)
        durable_json(bundle / 'create-plan.json', plan)
        return target
    finally:
        ledger.close()


def _management_origins(api):
    rows = api.request('GET', '/cluster/resources', {'type': 'vm'})
    origins = {}
    for key, vmid, address, kind in (
            ('controller', 114, '10.60.0.20', 'lxc'),
            ('guacamole', 110, '10.60.0.10', 'qemu')):
        found = [row for row in rows if int(row.get('vmid', 0)) == vmid and row.get('type') == kind]
        require(len(found) == 1, f'The fixed {key} origin identity is unavailable.')
        origins[key] = {'node': found[0]['node'], 'vmid': vmid,
                        'address': address, 'kind': kind}
    return origins


def run_job(jobdb, job_id):
    """Build or reopen only this queue-owned bundle, then run fixed workflow."""
    require(os.geteuid() == 0, 'Network verification requires the privileged worker.')
    job = job_row(jobdb, job_id)
    options = json.loads(job['options'])
    if cancellation_requested(jobdb, job_id):
        return {'status': 'cancelled-before-start'}
    print('Deriving approved image, declared LAN, free address, VMID and bounded probe plan.', flush=True)
    bundle = PROBE_ROOT / ('network-' + job_id)
    if bundle.exists():
        from crange_network_adapters import SavedTerraformProbe
        saved = SavedTerraformProbe('network-' + job_id)
        require(saved.target['project_id'] == int(job['project']) and
                saved.target['node'] == options['node'] and saved.target['vnet'] == options['network'],
                'Existing owned probe bundle differs from queued scope.')
    else:
        build_bundle(job)
    if cancellation_requested(jobdb, job_id):
        return {'status': 'cancelled-before-probe'}
    print('Starting fixed acceptance workflow; cancellation remains cooperative through cleanup.', flush=True)
    env = {**os.environ, 'CRANGE_JOB_DB': str(jobdb), 'CRANGE_JOB_ID': job_id,
           'PYTHONUNBUFFERED': '1'}
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/crange_network_workflow.py'),
                             'run', 'network-' + job_id], cwd=ROOT, env=env, check=False)
    if result.returncode:
        raise ValueError('Fixed network verification failed; inspect owned journal.')
    return {'status': 'completed'}


def _journal(job_id):
    try:
        return json.loads((PROBE_ROOT / ('network-' + job_id) / 'journal.json').read_text())
    except (OSError, ValueError, TypeError):
        return None


def reconcile_startup(store):
    """Recover interrupted network jobs before claiming new work."""
    with store.connect() as db:
        rows = db.execute("SELECT id,cancel_requested FROM jobs WHERE operation='network-verify' AND status IN ('running','reconcile-required')").fetchall()
    for row in rows:
        job_id, requested = row['id'], bool(row['cancel_requested'])
        journal = _journal(job_id)
        if journal is None:
            bundle = PROBE_ROOT / ('network-' + job_id)
            uncertain = bundle.exists() and any((bundle / name).exists()
                                                for name in ('terraform.tfstate', 'terraform.tfstate.backup'))
            if bundle.exists() and (bundle / 'review.json').is_file():
                try:
                    scope = json.loads((bundle / 'review.json').read_text())['scope']
                    from crange import ProxmoxAPI
                    live = ProxmoxAPI().request('GET', '/cluster/resources', {'type': 'vm'})
                    uncertain = uncertain or any(int(item.get('vmid', 0)) == scope['vmid'] for item in live)
                except Exception:
                    uncertain = True
            with store.connect() as db:
                state = ('reconcile-required' if uncertain else
                         'interrupted' if requested else 'queued')
                finished = time.time() if requested and not uncertain else None
                db.execute('UPDATE jobs SET status=?,finished=? WHERE id=?', (state, finished, job_id))
            continue
        phase = journal.get('state')
        if phase in ('accepted_cleaned', 'failed_cleaned'):
            with store.connect() as db:
                state = 'interrupted' if requested else ('succeeded' if phase == 'accepted_cleaned' else 'failed')
                db.execute('UPDATE jobs SET status=?,finished=?,output=substr(output || ?, -50000) WHERE id=?',
                           (state, time.time(), '\nRecovered a terminal owned journal after worker restart.', job_id))
            continue
        proc = subprocess.run([sys.executable, str(ROOT / 'scripts/crange_network_workflow.py'),
                               'reconcile', 'network-' + job_id], cwd=ROOT,
                              check=False)
        after = _journal(job_id)
        if proc.returncode == 0 and after and after.get('state') == 'failed_cleaned':
            state = 'interrupted' if requested else 'failed'
            with store.connect() as db:
                db.execute('UPDATE jobs SET status=?,finished=?,output=substr(output || ?, -50000) WHERE id=?',
                           (state, time.time(), '\nStartup recovery confirmed owned cleanup.', job_id))
        else:
            with store.connect() as db:
                db.execute("UPDATE jobs SET status='reconcile-required',finished=NULL,output=substr(output || ?, -50000) WHERE id=?",
                           ('\nStartup recovery could not confirm cleanup; reservation remains held.', job_id))
    with store.connect() as db:
        db.execute("UPDATE jobs SET status='queued',finished=NULL WHERE operation='network-reconcile' AND status='running'")


def reconcile_job(jobdb, source_job_id):
    source = job_row(jobdb, source_job_id)
    require(source['status'] == 'reconcile-required', 'Source network job does not need reconciliation.')
    journal = _journal(source_job_id)
    require(journal is not None, 'Owned journal is missing; manual review is required.')
    if journal.get('state') not in ('failed_cleaned', 'accepted_cleaned'):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/crange_network_workflow.py'),
                                 'reconcile', 'network-' + source_job_id], cwd=ROOT,
                                check=False)
        journal = _journal(source_job_id)
        require(result.returncode == 0 and journal and journal.get('state') == 'failed_cleaned',
                'Owned reconciliation did not confirm guest, router-rule, state and reservation cleanup.')
    with sqlite3.connect(jobdb, timeout=10) as db:
        row = db.execute('SELECT cancel_requested FROM jobs WHERE id=?', (source_job_id,)).fetchone()
        state = 'interrupted' if row and row[0] else 'failed'
        db.execute('UPDATE jobs SET status=?,finished=?,output=substr(output || ?, -50000) WHERE id=?',
                   (state, time.time(), '\nAdministrator reconciliation confirmed owned cleanup.', source_job_id))


def serve(jobdb, authdb):
    """Root lane that only claims network-verify jobs and runs recovery first."""
    require(os.geteuid() == 0, 'The network worker must run as root.')
    from crange_jobs import JobStore
    PROBE_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = PROBE_ROOT / '.worker.lock'
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    require(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600,
            'Network worker lock has unsafe ownership or mode.')
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    store = JobStore(jobdb, auth_path=authdb)
    # A single process owns this lane. Recovery state remains active and blocks
    # queue retries until the journal confirms cleanup.
    reconcile_startup(store)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        try:
            if not store.run_next(network_only=True):
                stop.wait(0.5)
        except Exception:
            stop.wait(1)
    os.close(fd)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('run', 'reconcile', 'worker'))
    parser.add_argument('job_id', nargs='?')
    args = parser.parse_args()
    database = os.environ.get('CRANGE_JOB_DB', '/var/lib/crange-jobs/jobs.sqlite3')
    if args.operation == 'worker':
        serve(database, os.environ.get('CRANGE_AUTH_DB', '/var/lib/crange-web/.local/share/crange/auth.sqlite3'))
    elif args.operation == 'reconcile':
        require(args.job_id and JOB_PATTERN.fullmatch(args.job_id), 'Invalid source job identity.')
        reconcile_job(database, args.job_id)
    else:
        require(args.job_id and JOB_PATTERN.fullmatch(args.job_id), 'Invalid queued job identity.')
        run_job(database, args.job_id)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        if len(sys.argv) == 3 and sys.argv[1] in ('run', 'reconcile'):
            from crange_network_workflow import _persist_workflow_error
            _persist_workflow_error(sys.argv[2], error)
        raise SystemExit('Fixed network verification failed; inspect the protected owned journal.')
