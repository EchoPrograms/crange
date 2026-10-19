"""Admin-only cleanup of exact, dependency-free project release image identities."""
import copy
import json
import os
from pathlib import Path
import shutil
import time
from urllib.parse import quote


def purge_images(api, root, project, archive):
    from crange_scheduling import read_settings, node_locks, DATA, CONFIG, atomic_json
    from crange_distribution import FileJournal, catalog_transaction
    from crange_cache_jobs import collect_cache_evidence
    from crange_image_cache import _dependencies
    from crange_cache_ownership import cache_owned
    from crange_placement import ReservationLedger
    from crange_project_lifecycle import private_images, _tags
    releases = [json.loads(p.read_text()) for p in (root / 'releases' / f'project-{project:03d}').glob('*.json')]
    policy, catalog = read_settings()
    images = private_images(catalog, project, releases)
    with node_locks([f'proxmox{i}' for i in range(1, 7)], library=True):
        # Re-read after allocation locks; all proof precedes physical destruction.
        policy, catalog = read_settings()
        images = private_images(catalog, project, releases)
        receipt_file = archive / 'purge-images.json'
        receipt = json.loads(receipt_file.read_text()) if receipt_file.exists() else {'steps': {}, 'images': images}
        if receipt.get('images') and not images and all(key not in catalog.get('images', {}) for key in receipt['images']) and receipt['steps'] and all(step.get('state') == 'done' for step in receipt['steps'].values()):
            receipt['state'] = 'removed'; atomic_json(receipt_file, receipt)
        if receipt.get('state') == 'removed':
            return
        if receipt['images'] != images:
            raise ValueError('Image catalog changed since permanent deletion began; administrator reconciliation required.')
        targets, volumes = [], {}
        current = api.request('GET', '/cluster/resources', {'type': 'vm'}) or []
        known_ids = {int(r['template_id']) for image in catalog['images'].values() for r in image.get('replicas', []) if r.get('template_id')}
        for row in current:
            if f'p{project:03d}' in _tags(row.get('tags')) and 'crange-image' in _tags(row.get('tags')) and int(row['vmid']) not in known_ids:
                raise ValueError(f'Unpublished project image {row["vmid"]} requires publication or owned-image reconciliation before permanent deletion.')
        ledger = ReservationLedger(DATA / 'reservations.sqlite3')
        try:
            for image_id, image in images.items():
                for other_id, other in catalog['images'].items():
                    if other_id in images:
                        continue
                    if image.get('artifact', {}).get('volume_id') == other.get('artifact', {}).get('volume_id') or any(
                            r.get('template_id') == q.get('template_id') and r.get('node') == q.get('node')
                            for r in image.get('replicas', []) for q in other.get('replicas', []) if r.get('template_id')):
                        raise ValueError(f'Image {image_id} shares storage with retained image {other_id}.')
                for replica in image.get('replicas', []):
                    vmid, node = replica.get('template_id'), replica.get('node')
                    if not vmid:
                        raise ValueError('Release image has an unsupported archive-only replica; administrator reconciliation required.')
                    key = f'{node}/{vmid}'
                    matches = [r for r in current if int(r.get('vmid', -1)) == vmid]
                    if not matches:
                        if receipt['steps'].get(key, {}).get('state') == 'done':
                            continue
                        raise ValueError(f'Image replica {key} is missing without a completed purge receipt.')
                    kind = 'qemu' if image['kind'] == 'vm' else 'lxc'
                    if len(matches) != 1 or matches[0].get('node') != node or matches[0].get('type') != kind:
                        raise ValueError('Image replica identity changed.')
                    config = api.request('GET', f'/nodes/{node}/{kind}/{vmid}/config')
                    capture = image.get('identity_capture', {})
                    canonical = (f'p{project:03d}' in _tags(config.get('tags')) and
                                 config.get('name', config.get('hostname')) == capture.get('capture_name') and
                                 vmid == capture.get('template_id') and node == capture.get('node'))
                    local = replica.get('cache_local') and cache_owned(config, vmid, replica.get('job_id'))
                    if not (canonical or local) or 'crange-image' not in _tags(config.get('tags')) or int(config.get('template', 0)) != 1 or config.get('lock'):
                        raise ValueError(f'Image {key} does not match its protected capture/cache ownership.')
                    evidence = collect_cache_evidence(api, catalog, image_id, node, vmid,
                        ledger=ledger, journal=FileJournal(DATA / 'cache-journal'), policy=policy)
                    # This operation removes the whole owned image, including
                    # its own snapshots. Snapshots of any other guest still block.
                    evidence = {**evidence, 'snapshots': [row for row in evidence.get('snapshots', [])
                        if (row.get('node'), row.get('vmid')) != (node, vmid)]}
                    _dependencies(image_id, node, vmid, evidence, time.time(), max_age=600)
                    targets.append((key, node, kind, vmid, config))
                artifact = image.get('artifact', {})
                volume = artifact.get('volume_id', '')
                if not volume.startswith('crange-library:backup/') or not artifact.get('complete_capture'):
                    raise ValueError('Only exact complete release backup artifacts can be purged.')
                volumes[volume] = artifact['node']
            for pin_file in (DATA / 'artifact-pins').glob('*.json'):
                pin = json.loads(pin_file.read_text())
                if pin.get('state') == 'active' and pin.get('volume_id') in volumes:
                    raise ValueError('A retained operation still pins a project release backup; reconcile it before permanent deletion.')
            # A receipt before the first DELETE makes interrupted purges resumable.
            atomic_json(receipt_file, receipt)
            def remove(key, node, endpoint):
                step = receipt['steps'].setdefault(key, {})
                if step.get('state') == 'done':
                    return
                if step.get('upid'):
                    api.task(node, step['upid'], timeout=7200)
                elif step.get('state') == 'submitting':
                    raise ValueError(f'Prior deletion of {key} has an uncertain provider result; reconcile its task before retrying.')
                else:
                    step['state'] = 'submitting'; atomic_json(receipt_file, receipt)
                    step['upid'] = api.request('DELETE', endpoint)
                    atomic_json(receipt_file, receipt)
                    if step['upid']:
                        api.task(node, step['upid'], timeout=7200)
                step['state'] = 'done'; atomic_json(receipt_file, receipt)
            for key, node, kind, vmid, config in targets:
                if api.request('GET', f'/nodes/{node}/{kind}/{vmid}/config') != config:
                    raise ValueError('Image configuration changed after dependency inspection.')
                remove(key, node, f'/nodes/{node}/{kind}/{vmid}')
                if any(int(r.get('vmid', -1)) == vmid for r in api.request('GET', '/cluster/resources', {'type': 'vm'})):
                    raise ValueError('Image deletion did not remove its exact replica.')
            for volume, node in volumes.items():
                remove(volume, node, f'/nodes/{node}/storage/crange-library/content/{quote(volume, safe="")}')
                content = api.request('GET', f'/nodes/{node}/storage/crange-library/content', {'content': 'backup'})
                if any(r.get('volid') == volume for r in content):
                    raise ValueError('Release backup remains after provider deletion.')
            def update(latest):
                if any(latest.get('images', {}).get(k) != v for k, v in images.items()):
                    raise ValueError('Catalog changed during purge; retain the receipt for reconciliation.')
                latest = copy.deepcopy(latest)
                for key in images:
                    del latest['images'][key]
                return latest
            catalog_transaction(CONFIG / 'image-catalog.json', update)
            receipt['state'] = 'removed'; atomic_json(receipt_file, receipt)
        finally:
            ledger.close()


def forget_project(root, project, path, actor):
    from crange_auth import AuthStore
    from crange_jobs import JobStore
    from crange_scheduling import DATA, atomic_json
    auth_path = os.environ.get('CRANGE_AUTH_DB')
    if not auth_path:
        raise ValueError('Permanent deletion requires the worker authorization database.')
    jobs_path = os.environ.get('CRANGE_JOB_DB')
    if not jobs_path:
        raise ValueError('Permanent deletion requires the worker job database.')
    releases = root / 'releases' / f'project-{project:03d}'
    if releases.is_symlink() or path.is_symlink():
        raise ValueError('Project storage must not be a symbolic link.')
    auth = AuthStore(auth_path)
    # Preserve a durable audit receipt outside the folder being removed.
    audit = DATA / 'project-retirements' / f'p{project:03d}-{int(time.time())}.json'
    atomic_json(audit, {'project': project, 'actor': actor, 'purged_at': time.time(),
                       'lifecycle': json.loads((path / '.crange-lifecycle.json').read_text())})
    with auth.connect() as db:
        db.execute('DELETE FROM guacamole_users WHERE project=?', (project,))
    from crange_guac import GuacamoleClient
    from crange_guac_users import apply_users
    from crange_guest_credentials import secret, directory
    applied_users = root / 'guacamole/access' / f'project-{project:03d}.users-applied.json'
    if applied_users.exists():
        apply_users(project, auth, GuacamoleClient(), root, secret())
    with auth.connect() as db:
        for table in ('guacamole_users', 'guacamole_project_settings', 'guacamole_enrollments', 'user_teams', 'user_connections', 'project_managers'):
            db.execute(f'DELETE FROM {table} WHERE project=?', (str(project),))
        for row in db.execute('SELECT name,projects FROM users').fetchall():
            values = [p for p in json.loads(row['projects']) if str(p) != str(project)]
            db.execute('UPDATE users SET projects=? WHERE name=?', (json.dumps(values), row['name']))
    credentials = directory() / f'project-{project:03d}'
    if credentials.is_symlink():
        raise ValueError('Project credentials must not be a symbolic link.')
    if credentials.exists():
        shutil.rmtree(credentials)
    with JobStore(jobs_path).connect() as db:
        db.execute('UPDATE jobs SET archived=1 WHERE project=?', (project,))
    for file in (root / 'guacamole/access').glob(f'project-{project:03d}*.json'):
        file.unlink()
    inventory = root / 'ansible/inventory/generated' / f'project-{project:03d}.json'
    inventory.unlink(missing_ok=True)
    releases = root / 'releases' / f'project-{project:03d}'
    if releases.is_symlink() or path.is_symlink():
        raise ValueError('Project storage must not be a symbolic link.')
    if releases.exists():
        shutil.rmtree(releases)
    shutil.rmtree(path)
    auth.audit(actor, 'project-purged', str(project), {'receipt': str(audit)})
