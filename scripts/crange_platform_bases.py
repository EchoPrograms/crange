"""Publish the fixed, administrator-maintained Linux bases through clone acceptance.

The originals stay stopped and immutable. Source selection, profiles and names
are code-owned; callers cannot supply arbitrary guest IDs or capture policies.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from crange_capture import capture_container, capture_vm, _approved_platform_source
from crange_distribution import DistributionError, catalog_transaction, publish_exported_image
from crange_scheduling import CONFIG, DATA, atomic_json, read_settings
from pathlib import Path

BASES = {
    'debian-13-vm': {'source_id': 3015, 'kind': 'vm', 'display_name': 'Debian 13 VM',
                     'profile': {'agent_enabled': True, 'init_system': 'systemd'}},
    'ubuntu-24-04-vm': {'source_id': 3014, 'kind': 'vm', 'display_name': 'Ubuntu Server 24.04 VM',
                        'profile': {'agent_enabled': True, 'init_system': 'systemd'}},
    'debian-13-container': {'source_id': 3000, 'kind': 'container', 'display_name': 'Debian 13 Container',
                            'profile': {'init_system': 'systemd'}},
    'ubuntu-24-04-container': {'source_id': 3001, 'kind': 'container', 'display_name': 'Ubuntu Server 24.04 Container',
                               'publication_version': '4',
                               'profile': {'init_system': 'systemd'}},
    'alpine-container': {'source_id': 3002, 'kind': 'container', 'display_name': 'Alpine Container',
                         'profile': {'init_system': 'openrc'}},
}
SOURCE_NODE = 'proxmox4'


def source_policy(base_policy):
    """Read fixed P4 sources without enrolling P4 for new allocations."""
    policy = copy.deepcopy(base_policy)
    if SOURCE_NODE not in policy.get('allowed_nodes', []):
        policy.setdefault('allowed_nodes', []).append(SOURCE_NODE)
        policy.setdefault('nodes', {}).setdefault(SOURCE_NODE, {})['draining'] = True
    return policy


def distribute(image_id, *, api=None, on_ready=None):
    """Fill verified local caches on every enrolled node, retaining failures."""
    from crange_cache_jobs import prepare_cache
    policy, catalog = read_settings()
    image = catalog.get('images', {}).get(image_id)
    from crange_template_selection import is_release_template
    if not image or image.get('verified') is not True or is_release_template(image_id, image):
        raise DistributionError('Choose a verified OS base, not a scenario release image.')
    project = 1 if '*' in image['approved_projects'] else image['approved_projects'][0]
    from crange_placement import workload_node_allowed
    report = {'image_id': image_id, 'ready': [], 'errors': {}, 'excluded': {}}
    for node in sorted(policy.get('allowed_nodes', [])):
        if not workload_node_allowed(policy, node, 'cache:'+image_id, image['kind']):
            report['excluded'][node] = 'Node is reserved for router containers.'
            continue
        candidates = [storage for candidate, storage in policy.get('approved_storage', [])
                      if candidate == node and (image['kind'] == 'vm' or
                      storage in policy.get('nodes', {}).get(node, {}).get('snapshot_storages', []))]
        existing = [r['datastore_id'] for r in image.get('replicas', [])
                    if r.get('node') == node and r.get('state') == 'ready' and r.get('cache_local') is True
                    and r.get('datastore_id') in candidates and r.get('artifact_sha256') == image.get('artifact_sha256')
                    and 'linked' in r.get('clone_modes_verified', [])]
        preferred = {'proxmox5': 'crange-thin'}.get(node, 'local-lvm')
        storage = next(iter(existing or sorted(candidates, key=lambda item: item != preferred)), None)
        try:
            if not storage:
                raise DistributionError('No approved local clone storage on this node.')
            print(f'Preparing {image_id} on {node}/{storage}', flush=True)
            prepare_cache(project_id=project, image_id=image_id, node_name=node,
                          datastore_id=storage, owner='platform-base-distribution', auto_storage=True)
            report['ready'].append(node)
            if on_ready is not None:
                on_ready()
                on_ready = None
        except Exception as exc:
            report['errors'][node] = str(exc)
            print(f'{node}: {exc}', flush=True)
    for node in sorted({f'proxmox{i}' for i in range(1, 7)} - set(policy.get('allowed_nodes', []))):
        report['excluded'][node] = 'Node needs verified enrollment before automatic placement.'
    atomic_json(DATA / 'base-distribution' / (image_id + '.json'), report, 0o600)
    return report


def link_global_base(image_id, definition, *, api=None):
    """Add a new base alias; preserve old aliases used by existing guests."""
    from crange_templates import ROOT, approve_catalog_image
    from crange import templates_for, container_templates_for
    environments = ROOT / 'terraform/environments'
    paths = [path for path in environments.glob('project-[0-9][0-9][0-9]')
             if (path / 'crange.auto.tfvars.json').exists()]
    # Existing project layouts also use terraform.tfvars.json.
    paths += [path for path in environments.glob('project-[0-9][0-9][0-9]')
              if path not in paths and (path / 'terraform.tfvars').exists()]
    from crange_project_lifecycle import is_active
    paths = [path for path in paths if is_active(path)]
    if not paths:
        raise DistributionError('No real project is available to validate base approval.')
    projects = [int(path.name[-3:]) for path in paths]
    result = approve_catalog_image(image_id, projects, api)
    reader = templates_for if definition['kind'] == 'vm' else container_templates_for
    filename = 'approved-scenario-templates.json' if definition['kind'] == 'vm' else 'approved-container-templates.json'
    reference = reader(paths[0])[result['alias']]
    for path in paths + [environments / 'project-template']:
        refs = reader(path)
        refs.setdefault(result['alias'], reference)
        for alias, old in refs.items():
            if (alias in {'approved-base','approved-ct-base','debian-13','ubuntu-server-24-04','alpine-3-24'}
                    and alias != result['alias'] and not old.get('image_id') and
                    old.get('template_id') == definition['source_id'] and
                    old.get('template_node') == SOURCE_NODE):
                old['superseded_by'] = result['alias']
        atomic_json(path / filename, refs, 0o640)
    return result


def base_key(value):
    if value not in BASES:
        raise ValueError('Choose an administrator-maintained Linux base.')
    return value


def image_id_for(base, version):
    base_key(base)
    if not isinstance(version, str) or not re.fullmatch(r'[1-9][0-9]{0,3}', version):
        raise ValueError('Base version must be an integer from 1 to 9999.')
    return f'{base}-base-v{version}'


def prepare(base, version=None, *, api=None):
    from crange import ProxmoxAPI
    base_key(base)
    version = version if version is not None else BASES[base].get('publication_version', '3')
    image_id = image_id_for(base, version)
    definition = BASES[base]
    source_id = definition['source_id']
    api = api or ProxmoxAPI()
    source = (api.vm if definition['kind'] == 'vm' else api.container)(SOURCE_NODE, source_id)
    _approved_platform_source(source, SOURCE_NODE, source_id, definition['kind'])
    policy, catalog = read_settings()
    if image_id in catalog.get('images', {}):
        existing = catalog['images'][image_id]
        if (existing.get('verified') is not True or existing.get('kind') != definition['kind']
                or not str(existing.get('source_revision', '')).startswith(f'platform-{base}-')):
            raise DistributionError('Immutable base version already exists with different provenance.')
        print(f'Base {image_id} is already published; retaining its immutable version.', flush=True)
        report = distribute(image_id, api=api,
                            on_ready=lambda: link_global_base(image_id, definition, api=api))
        link_global_base(image_id, definition, api=api)
        if report['errors']:
            raise DistributionError('Base distribution needs attention: ' + json.dumps(report['errors'], sort_keys=True))
        return {'image_id': image_id, 'reused': True}
    slug = hashlib.sha256(image_id.encode()).hexdigest()[:12]
    job = 'capture-p001-' + slug
    policy_path = DATA / 'source-policies' / (job + '.json')
    if policy_path.exists():
        # Resume the same allocation and policy rather than reselecting a host.
        policy = json.loads(policy_path.read_text())
        admitted = source_policy(policy)
        if admitted != policy:
            if (DATA / 'capture-journal' / (job + '.json')).exists():
                raise DistributionError('Retained allocation requires its original source policy; reconcile before changing admission.')
            from crange_placement import PlacementError, ReservationLedger
            ledger = ReservationLedger(DATA / 'reservations.sqlite3')
            try:
                try:
                    ledger.get_job(job)
                except PlacementError:
                    pass
                else:
                    raise DistributionError('Retained capacity reservation requires exact reconciliation before source admission.')
            finally:
                ledger.close()
            backup = policy_path.with_suffix('.pre-source-admission.json')
            if not backup.exists():
                atomic_json(backup, policy, 0o600)
            policy = admitted
            atomic_json(policy_path, policy, 0o600)
    else:
        policy = source_policy(policy)
        atomic_json(policy_path, policy, 0o600)
    profile = {**definition['profile'], '_transport_template': True,
               # Build on eligible SSD-backed nodes before the archive HDD.
               # This is a soft preference; all headroom checks still apply.
               '_capture_preferred_nodes': ['proxmox5', 'proxmox1', 'proxmox2', 'proxmox3', 'proxmox6'],
               '_probe_datastores': {'proxmox6': 'local-lvm', 'proxmox5': 'crange-thin'},
               '_capture_datastores': {'proxmox6':'local-lvm','proxmox5':'crange-thin'}}
    # A preference change must never rebind an existing allocation or cause a
    # second restore. Its recorded destination remains authoritative.
    receipt_path = DATA / 'capture-journal' / (job + '.json')
    if receipt_path.exists():
        retained = json.loads(receipt_path.read_text())
        destination = retained.get('reservation', {}).get('resolved', {})
        retained_node = destination.get('node_name')
        retained_storage = destination.get('datastore_id')
        if ((retained_node, retained_storage) not in
                set(map(tuple, policy.get('approved_storage', []))) or
                retained.get('node') != retained_node):
            raise DistributionError('Retained base destination no longer matches its protected policy.')
        profile['_capture_datastores'][retained_node] = retained_storage
    if definition['kind'] == 'container':
        profile['_full_clone_probe'] = True
    kwargs = dict(project=1, node=SOURCE_NODE, source_id=source_id,
                  name='crange-' + image_id, description='Administrator base publication ' + image_id,
                  slug=slug, profile=profile, platform_source=True, policy_override=policy)
    print(f'Preparing {definition["display_name"]}: isolated derivative, archive and fresh clone acceptance.', flush=True)
    if definition['kind'] == 'vm':
        derivative, jobs = capture_vm(api, **kwargs, policy_path=policy_path, job_id=job)
    else:
        derivative, jobs = capture_container(api, **kwargs, policy_path=policy_path)
    journal = DATA / 'source-journal'
    export = json.loads((journal / (jobs['export_job'] + '.json')).read_text())
    probe = json.loads((journal / (jobs['probe_job'] + '.json')).read_text())
    if (probe.get('state') != 'source-probed' or probe.get('verification', {}).get('passed') is not True
            or any(t.get('state') != 'confirmed-terminal' for t in probe.get('provider_tasks', []))):
        raise DistributionError('Fresh clone acceptance is incomplete; base remains unpublished.')
    receipt_path = DATA / 'capture-journal' / (job + '.json')
    receipt = json.loads(receipt_path.read_text())
    fingerprint = export.get('identity_capture', {}).get('source_config_sha256', '')
    if not re.fullmatch(r'[a-f0-9]{64}', fingerprint):
        raise DistributionError('Base has no bound source fingerprint.')
    def publish(current):
        updated = publish_exported_image(current, image_id, export, architecture='amd64',
            source_revision=f'platform-{base}-{fingerprint}', approved_projects=['*'],
            datastore_id=receipt['reservation']['resolved']['datastore_id'], verification=probe['verification'])
        updated['images'][image_id]['display_name'] = definition['display_name']
        return updated
    catalog_transaction(CONFIG / 'image-catalog.json', publish)
    receipt.update(state='published', image_id=image_id)
    atomic_json(receipt_path, receipt, 0o600)
    print(f'Published {image_id}; ready for verified node distribution.', flush=True)
    report = distribute(image_id, api=api,
                        on_ready=lambda: link_global_base(image_id, definition, api=api))
    link_global_base(image_id, definition, api=api)
    if report['errors']:
        raise DistributionError('Base published; distribution needs attention: ' + json.dumps(report['errors'], sort_keys=True))
    return {'image_id': image_id, 'template_id': derivative, 'node': jobs['node']}
