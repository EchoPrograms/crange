"""Fixed project teardown/restore/purge workflows; immutable images are retained on delete.

Web requests may read lifecycle metadata, but only queued workers change it or
operate infrastructure. An incomplete teardown remains fenced until retried.
"""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import time
import uuid

OPERATIONS = {'project-delete', 'project-restore', 'project-purge'}
MARKER = '.crange-lifecycle.json'
ACTIVE = {'active', 'restored'}


def project_id(value):
    if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= 255 or int(value) in (60, 61):
        raise ValueError('Choose a project from 1–255, excluding 60 and 61.')
    return int(value)


def path_for(project):
    from crange import ENVS
    return ENVS / f'project-{project_id(project):03d}'


def state(path):
    file = Path(path) / MARKER
    if not file.exists():
        return {'state': 'active'}
    try:
        value = json.loads(file.read_text())
        if not isinstance(value, dict) or value.get('state') not in ACTIVE | {'deleting', 'delete_failed', 'deleted', 'purging', 'purge_failed'}:
            raise ValueError('Invalid lifecycle state.')
        return value
    except (OSError, ValueError) as exc:
        raise ValueError('Project lifecycle record is unreadable; administrator review is required.') from exc


def is_active(path):
    return state(path)['state'] in ACTIVE


def require_active(path):
    if not is_active(path):
        raise ValueError('This project is deleted or undergoing deletion. An administrator can restore or retry it from Projects.')


def validate_action(path, operation):
    if not Path(path).is_dir() or Path(path).is_symlink():
        raise ValueError('Project configuration is unavailable.')
    status = state(path)['state']
    allowed = (ACTIVE | {'deleting', 'delete_failed'}) if operation == 'project-delete' else {'deleted', 'purging', 'purge_failed'} if operation == 'project-purge' else {'deleted'}
    if status not in allowed:
        raise ValueError(f'{operation} is unavailable while the project is {status}.')


def write_state(path, value):
    from crange_scheduling import atomic_json
    atomic_json(Path(path) / MARKER, value, 0o640)


@contextmanager
def project_lock(path):
    # Outside the project folder: purge cannot remove a held lock's inode.
    lock = Path(path).parent / ('.' + Path(path).name + '.lifecycle.lock')
    with lock.open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _tags(value):
    return set(value if isinstance(value, list) else str(value or '').split(';'))


def _namespace(name, project):
    return bool(re.fullmatch(rf'p{project:03d}(?:tr|[tc][0-9]{{3}})', str(name)))


def validate_destroy_plan(detail, project, api):
    """Every destructive target must belong to this project's exact TF state."""
    prefix = f'p{project:03d}'
    resources = api.request('GET', '/cluster/resources', {'type': 'vm'}) or []
    online = {item['node'] for item in api.request('GET', '/nodes') if item.get('status') == 'online'}
    guests = {}
    allowed = {'proxmox_virtual_environment_vm', 'proxmox_virtual_environment_container', 'proxmox_sdn_vnet', 'proxmox_sdn_applier', 'terraform_data'}
    for item in detail.get('resource_changes', []):
        if item.get('mode') == 'data':
            continue
        actions = item.get('change', {}).get('actions', [])
        if actions == ['no-op']:
            continue
        if actions != ['delete'] or item.get('type') not in allowed:
            raise ValueError('Teardown plan contains an unsupported or non-deletion action.')
        before = item['change'].get('before') or {}
        if item['type'] == 'terraform_data':
            if item.get('address') != 'terraform_data.sdn_revision':
                raise ValueError('Unknown Terraform bookkeeping resource.')
            continue
        if item['type'] == 'proxmox_sdn_applier':
            if item.get('address') not in ('proxmox_sdn_applier.apply', 'proxmox_sdn_applier.finalizer'):
                raise ValueError('Unrecognized shared SDN action in project state.')
            continue
        if item['type'] == 'proxmox_sdn_vnet':
            if not _namespace(before.get('id', before.get('vnet')), project) or before.get('zone') != 'crange':
                raise ValueError('Teardown plan includes a network outside this project.')
            continue
        kind = 'qemu' if item['type'].endswith('_vm') else 'lxc'
        vmid, node = before.get('vm_id'), before.get('node_name')
        if type(vmid) is not int or vmid < 100 or node not in online or prefix not in _tags(before.get('tags')) or before.get('template'):
            raise ValueError(f'Guest {vmid} has uncertain ownership, is a template, or its node {node} is offline.')
        matches = [row for row in resources if int(row.get('vmid', -1)) == vmid]
        if len(matches) != 1 or matches[0].get('type') != kind or matches[0].get('node') != node or matches[0].get('pool') != 'crange-iac':
            raise ValueError(f'Guest {vmid} has moved or is outside the CRANGE infrastructure pool.')
        config = api.request('GET', f'/nodes/{node}/{kind}/{vmid}/config')
        initialization = before.get('initialization') or [{}]
        expected = before.get('name') if kind == 'qemu' else initialization[0].get('hostname')
        if (config.get('name', config.get('hostname')) != expected or int(config.get('template', 0)) or config.get('lock') or
                prefix not in _tags(config.get('tags')) or 'crange-image' in _tags(config.get('tags'))):
            raise ValueError(f'Guest {vmid} no longer matches the reviewed project identity.')
        guests[vmid] = {'id': vmid, 'node': node, 'kind': kind, 'name': expected}
    # Missing Terraform ownership must be reconciled, never treated as permission
    # to delete an arbitrary guest by name/tag alone. Captured images stay intact.
    for resource in resources:
        tags = _tags(resource.get('tags'))
        if prefix in tags and 'crange-image' not in tags and not resource.get('template') and int(resource['vmid']) not in guests:
            raise ValueError(f'Project guest {resource["vmid"]} is outside the teardown plan; reconcile its Terraform ownership first.')
    return list(guests.values())


def _revoke_connections(root, project, archive):
    from crange_guac import GuacamoleClient
    access = root / 'guacamole/access'
    applied = access / f'project-{project:03d}.applied.json'
    desired = access / f'project-{project:03d}.json'
    for file in (applied, desired):
        if file.exists():
            target = archive / file.name
            if not target.exists():
                shutil.copy2(file, target)
    if applied.exists():
        client = GuacamoleClient()
        client.apply({'project_id': project, 'connections': [], 'users': []}, applied)
    if desired.exists():
        from crange_scheduling import atomic_json
        atomic_json(desired, {'project_id': project, 'connections': [], 'users': [], 'status': 'deleted'}, 0o640)


def _retire_observations(path, archive):
    for name in ('terraform.tfstate', 'terraform.tfstate.backup', 'crange-applied-placements.json', '.crange-bootstrap.json', '.crange-plans'):
        file = path / name
        if file.exists():
            target = archive / ('previous-' + name.lstrip('.'))
            if target.exists():
                target = target.with_name(target.name + '-' + uuid.uuid4().hex[:8])
            shutil.move(str(file), target)
    from crange_scheduling import DATA
    from crange_placement import ReservationLedger
    ledger_file = DATA / 'reservations.sqlite3'
    if ledger_file.exists():
        ledger = ReservationLedger(ledger_file)
        try:
            for record in ledger.status():
                if (record['project_id'] == int(path.name[-3:]) and record['state'] in ('reviewed','running')
                        and not any(item.get('operation') for item in record['allocations'])):
                    ledger.reconcile_job(record['job_id'], operation_terminal=True, allocations_reconciled=True)
        finally:
            ledger.close()
    plans = Path.home() / '.local/share/crange/plans' / path.name
    if plans.exists():
        shutil.move(str(plans), archive / ('retired-plans-' + uuid.uuid4().hex[:8]))


def _remove_route(path, project, dry_run=False):
    from crange import ROOT, json_config, run
    cfg = json_config(path)
    credential_file = Path.home() / '.config/crange/opnsense-api.txt'
    argv = [os.sys.executable, str(ROOT / 'scripts/opnsense-project-routes.py'), 'retire', str(project),
            cfg['transit_address'], cfg.get('opnsense_transit_interface', 'opt2')]
    if dry_run:
        argv.append('--dry-run')
    shell = 'set -a; . ' + shlex.quote(str(credential_file)) + '; set +a; export OPNSENSE_INSECURE=1; exec ' + shlex.join(argv)
    run(['bash','-c',shell], timeout=180)


class WaitingForNodes(ValueError):
    def __init__(self, nodes):
        self.nodes = sorted(set(nodes))
        super().__init__('Waiting for ' + ', '.join(self.nodes) + ' to reconnect. Cleanup will resume automatically.')


def required_guest_nodes(path, project, api):
    """Read recorded placements before Terraform tries to refresh offline guests."""
    result = set()
    file = path / 'terraform.tfstate'
    if file.exists():
        document = json.loads(file.read_text())
        for resource in document.get('resources', []):
            if resource.get('mode') == 'data' or resource.get('type') not in ('proxmox_virtual_environment_vm','proxmox_virtual_environment_container'):
                continue
            for instance in resource.get('instances', []):
                node = instance.get('attributes', {}).get('node_name')
                if node in {f'proxmox{i}' for i in range(1,7)}:
                    result.add(node)
    # Includes orphaned tagged guests: reconnect before proving their identity.
    for row in api.request('GET', '/cluster/resources', {'type':'vm'}) or []:
        if f'p{project:03d}' in _tags(row.get('tags')) and not row.get('template') and 'crange-image' not in _tags(row.get('tags')):
            if row.get('node') in {f'proxmox{i}' for i in range(1,7)}:
                result.add(row['node'])
    return result


def require_nodes(api, nodes):
    if not nodes:
        return
    previous_timeout, previous_attempts = getattr(api,'request_timeout',90),getattr(api,'read_attempts',3)
    try:
        api.request_timeout=3;api.read_attempts=1
        try:
            rows=api.request('GET','/nodes')
            online={r['node'] for r in rows if r.get('status')=='online'}
        except Exception:
            online=set()
    finally:
        api.request_timeout=previous_timeout;api.read_attempts=previous_attempts
    missing=set(nodes)-online
    if missing:
        raise WaitingForNodes(missing)


def delete_project(project, *, actor='cli', api=None, terraform=None, revoke=None, route_remove=None):
    from crange import ROOT, ProxmoxAPI, tf_run, run, json_config
    project = project_id(project); path = path_for(project)
    with project_lock(path):
        validate_action(path, 'project-delete')
        previous = state(path)
        record = {**previous, 'schema_version': 1, 'state': 'deleting', 'project_id': project,
                  'requested_by': actor, 'job_id': os.environ.get('CRANGE_JOB_ID'), 'requested_at': time.time()}
        archive = path / '.crange-lifecycle'
        archive.mkdir(mode=0o700, exist_ok=True)
        # This snapshot is the original desired state, not a substitute VM backup.
        config = path / 'crange.auto.tfvars.json'
        if config.exists() and not (archive / 'retained-config.json').exists():
            shutil.copy2(config, archive / 'retained-config.json')
        write_state(path, record)
        api = api or ProxmoxAPI(); terraform = terraform or tf_run
        try:
            require_nodes(api, required_guest_nodes(path, project, api))
            if (path / 'terraform.tfstate').exists():
                terraform(path, ['init', '-input=false'])
                plan = archive / 'teardown.tfplan'
                terraform(path, ['plan', '-destroy', '-input=false', '-out=' + str(plan)])
                detail = json.loads(terraform(path, ['show', '-json', str(plan)], capture=True).stdout)
                targets = validate_destroy_plan(detail, project, api)
                if not route_remove:
                    _remove_route(path, project, dry_run=True)
                print(f'Reviewed teardown of {len(targets)} project guests and project-only networks.', flush=True)
                for guest in targets:
                    print(f'  Delete {guest["kind"]} {guest["node"]}/{guest["id"]}: {guest["name"]}', flush=True)
                # Revoke access before removing guest disks; retry uses the same
                # retained ownership files if the connector is unavailable.
                (revoke or _revoke_connections)(ROOT, project, archive)
                terraform(path, ['apply', '-parallelism=1', '-input=false', str(plan)])
                after = json.loads(terraform(path, ['show', '-json'], capture=True).stdout)
                def populated(module):
                    return bool(module.get('resources')) or any(populated(m) for m in module.get('child_modules', []))
                if populated(after.get('values', {}).get('root_module', {})):
                    raise ValueError('Terraform still contains project resources; teardown is incomplete.')
            else:
                validate_destroy_plan({'resource_changes': []}, project, api)
                if not route_remove:
                    _remove_route(path, project, dry_run=True)
                (revoke or _revoke_connections)(ROOT, project, archive)
            remaining = api.request('GET', '/cluster/resources', {'type': 'vm'}) or []
            if any(f'p{project:03d}' in _tags(r.get('tags')) and 'crange-image' not in _tags(r.get('tags')) and not r.get('template') for r in remaining):
                raise ValueError('Project guests remain in Proxmox; retaining the deletion fence.')
            if route_remove:
                route_remove(project)
            else:
                _remove_route(path, project)
            _retire_observations(path, archive)
            record.pop('waiting_nodes',None)
            record.update(state='deleted', deleted_at=time.time(), deleted_by=actor, error=None)
            write_state(path, record)
            print('Project deleted. Configuration, releases and immutable images are retained. An administrator can restore it.', flush=True)
        except WaitingForNodes as exc:
            record.update(state='deleting', waiting_nodes=exc.nodes, error=None)
            write_state(path, record)
            raise
        except Exception as exc:
            record.update(state='delete_failed', error=str(exc));write_state(path, record)
            raise


def restore_project(project, *, actor='cli'):
    from crange import json_config, save_config
    project = project_id(project); path = path_for(project)
    with project_lock(path):
        validate_action(path, 'project-restore')
        cfg = json_config(path)
        # Applied endpoint IDs cannot survive destruction/recreation. Keep all
        # desired rollout definitions, approved images, and release manifests.
        for rollout in cfg.get('release_rollouts', {}).values():
            for key in ('applied', 'applied_endpoints'):
                rollout.pop(key, None)
        save_config(path, cfg)
        record = {**state(path), 'state': 'restored', 'restored_at': time.time(), 'restored_by': actor,
                  'needs_redeploy': True, 'error': None}
        write_state(path, record)
        print('Project restored to the dashboard. Retained definitions and releases are ready to redeploy; deleted live disks are not restored.', flush=True)


def pending(store, project):
    with store.connect() as db:
        return db.execute("SELECT operation,status,id FROM jobs WHERE project=? AND operation IN ('project-delete','project-restore','project-purge') AND status IN ('queued','running','waiting-for-node','reconcile-required') ORDER BY created DESC LIMIT 1", (project,)).fetchone()


def require_editable(store, project):
    require_active(path_for(project))
    if pending(store, project):
        raise ValueError('Project deletion or restoration is queued. Wait for its job before making changes.')


def private_images(catalog, project, releases):
    """Project approval alone never confers ownership of a platform base image."""
    ids = {r['release_id'] for r in releases if r.get('project_id') == project}
    result = {}
    for key, image in catalog.get('images', {}).items():
        release = image.get('release', {})
        if release.get('release_id') not in ids and image.get('source_identity', {}).get('snapshot_project_id') != project:
            continue
        if (set(map(str, image.get('approved_projects', []))) != {str(project)} or
                not set(image.get('retained_release_references', [])).issubset(ids)):
            raise ValueError(f'Image {key} is shared with another project or release; remove its external dependencies before permanent deletion.')
        result[key] = image
    return result


def purge_project(project, *, actor='cli'):
    from crange import ROOT, ProxmoxAPI
    from crange_project_purge import purge_images, forget_project
    project = project_id(project); path = path_for(project)
    with project_lock(path):
        validate_action(path, 'project-purge')
        if not os.environ.get('CRANGE_AUTH_DB') or not os.environ.get('CRANGE_JOB_DB'):
            raise ValueError('Permanent deletion must run through the configured administrator worker.')
        record = {**state(path), 'state': 'purging', 'purge_requested_by': actor, 'purge_requested_at': time.time(), 'job_id': os.environ.get('CRANGE_JOB_ID')}
        write_state(path, record)
        try:
            # Teardown must already be complete. No implicit destructive retry.
            api = ProxmoxAPI()
            from crange_scheduling import read_settings
            releases = [json.loads(p.read_text()) for p in (ROOT/'releases'/path.name).glob('*.json')]
            if private_images(read_settings()[1], project, releases):
                require_nodes(api, {f'proxmox{i}' for i in range(1,7)})
            validate_destroy_plan({'resource_changes': []}, project, api)
            purge_images(api, ROOT, project, path / '.crange-lifecycle')
            forget_project(ROOT, project, path, actor)
            print('Project permanently deleted. Its private release images, definitions and access records were removed; platform templates and audit history were retained.', flush=True)
        except WaitingForNodes as exc:
            write_state(path, {**record, 'state':'purging', 'waiting_nodes':exc.nodes, 'error':None})
            raise
        except Exception as exc:
            if path.exists():
                write_state(path, {**record, 'state': 'purge_failed', 'error': str(exc)})
            raise


def cli(args):
    project = project_id(args.project)
    if args.confirm != f'p{project:03d}':
        raise ValueError(f'Type p{project:03d} to confirm this action.')
    actor = os.environ.get('CRANGE_ACTOR', 'cli')
    try:
        {'project-delete': delete_project, 'project-restore': restore_project, 'project-purge': purge_project}[args.lifecycle_operation](project, actor=actor)
    except WaitingForNodes as exc:
        print('crange: ' + str(exc), flush=True)
        raise SystemExit(75)
