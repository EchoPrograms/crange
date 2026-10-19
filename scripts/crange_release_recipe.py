"""Publish a fixed service-recipe update while retaining immutable parent images."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def build(parent, cfg, outputs, profiles, catalog, *, version, revision, dirty, created_at, actor):
    if (cfg.get('project_id') != 100 or parent.get('project_id') != 100
            or parent.get('schema_version') != 4):
        raise ValueError('Service updates require a captured Project 100 release.')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', version) or version == parent.get('version'):
        raise ValueError('Choose a distinct new release version.')
    original = copy.deepcopy(parent)
    digest = original.pop('integrity_sha256', None)
    if checksum(original) != digest:
        raise ValueError('Parent release integrity changed.')
    scenario = parent['scenario']
    current = {}
    ignored = {'key', 'source_template', 'source_image_id', 'source_vm_id', 'template', 'image', 'full_clone'}
    for label, field in (('boxes', 'scenario_boxes'), ('containers', 'scenario_containers')):
        staging = {key: box for key, box in cfg.get(field, {}).items() if str(box['team_id']) == '254'}
        sources = {box['key']: box for box in scenario.get(label, [])}
        if staging.keys() != sources.keys():
            raise ValueError('Staging machine definitions changed; capture a new image release.')
        for key, source in sources.items():
            box = staging[key]
            if ({k: v for k, v in box.items() if k not in ignored} !=
                    {k: v for k, v in source.items() if k not in ignored}
                    or box.get('template') != source.get('source_template')
                    or (box.get('image') and box['image'] != source.get('source_image_id'))):
                raise ValueError(f'Staging definition {key} changed; capture a new image release.')
            live = outputs.get(field, {}).get('value', {}).get(key, {})
            if (type(live.get('id')) is not int or live['id'] != source.get('source_vm_id')
                    or live.get('address') != f'10.100.{source.get("network_id", 254)}.{source["ip_last_octet"]}'
                    or live.get('placement', {}).get('image_id') != source.get('source_image_id')):
                raise ValueError('Staging guest generation differs from the captured parent.')
            current[key] = live
    fields = ('owner_team_id', 'network_id', 'uplink_last_octet', 'node_name', 'cores', 'memory_mb')
    networks = {key: {k: value[k] for k in fields if k in value}
                for key, value in cfg.get('custom_networks', {}).items() if str(value['owner_team_id']) == '254'}
    if networks != scenario.get('networks', {}):
        raise ValueError('Staging networks changed; capture a new image release.')
    old_profiles = scenario.get('service_profiles', {})
    if not profiles or profiles.keys() != old_profiles.keys():
        raise ValueError('Configure all captured staging service profiles before publishing an update.')
    for kind, profile in profiles.items():
        if set(profile.get('guests', {})) != set(old_profiles[kind].get('guests', {})):
            raise ValueError('Service profile guest scope differs from the parent release.')
        for key, binding in profile['guests'].items():
            live = current.get(key, {})
            if (binding.get('guest_id') != live.get('id') or not live
                    or binding.get('image_id') != live.get('placement', {}).get('image_id')):
                raise ValueError('Configure services against the current staging guests first.')
    if all(profiles[k].get('source_sha256') == old_profiles[k].get('source_sha256') for k in profiles):
        raise ValueError('No staged service recipe changed; retain the existing release.')
    images = {}
    for alias, reference in scenario['templates'].items():
        image = catalog.get('images', {}).get(reference.get('image_id'))
        if (not image or image.get('verified') is not True
                or not re.fullmatch(r'[a-f0-9]{64}', str(image.get('artifact_sha256', '')))
                or not ({100, '*'} & set(image.get('approved_projects', [])))
                or not isinstance(image.get('retained_release_references'), list)
                or parent['release_id'] not in image.get('retained_release_references', [])):
            raise ValueError('A parent image is unpublished, unapproved or no longer retained.')
        images[alias] = image['artifact_sha256']
    result = copy.deepcopy(parent)
    result.pop('integrity_sha256')
    result.update(version=version, release_id=f'p100-{version}', source_revision=revision,
                  source_tree_dirty=dirty, created_at=created_at, created_by=actor)
    result['scenario']['service_profiles'] = copy.deepcopy(profiles)
    result['derivation'] = {'kind': 'service-recipe-update', 'parent_release_id': parent['release_id'],
        'parent_integrity_sha256': digest, 'reused_images': images, 'staging_disks_captured': False}
    result['integrity_sha256'] = checksum(result)
    return result


def publish(path, release, catalog):
    """Called inside the catalog lock; interrupted publication is safely retryable."""
    unsigned = copy.deepcopy(release)
    if unsigned.pop('integrity_sha256', None) != checksum(unsigned):
        raise ValueError('Service update integrity changed before publication.')
    updated = copy.deepcopy(catalog)
    for alias, sha in release['derivation']['reused_images'].items():
        image = updated['images'][release['scenario']['templates'][alias]['image_id']]
        if (image.get('artifact_sha256') != sha or image.get('verified') is not True
                or not ({100, '*'} & set(image.get('approved_projects', [])))
                or not isinstance(image.get('retained_release_references'), list)
                or release['derivation']['parent_release_id'] not in image['retained_release_references']):
            raise ValueError('Parent image changed during publication.')
        refs = image.setdefault('retained_release_references', [])
        if release['release_id'] not in refs:
            refs.append(release['release_id'])
    path = Path(path)
    if path.exists():
        existing = json.loads(path.read_text())
        unsigned = copy.deepcopy(existing)
        digest = unsigned.pop('integrity_sha256', None)
        if checksum(unsigned) != digest:
            raise ValueError('Existing release integrity failed; no overwrite performed.')
        # A retry after a catalog write failure may only finish the exact same
        # publication. Creation time and its derived checksum are incidental.
        def comparable(value):
            return {k: v for k, v in value.items() if k not in ('created_at', 'integrity_sha256')}
        if comparable(existing) != comparable(release):
            raise ValueError('Release version already exists with different contents.')
        return updated
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.recipe-release-', dir=path.parent)
    try:
        os.fchmod(fd, 0o440)
        with os.fdopen(fd, 'w') as stream:
            stream.write(json.dumps(release, indent=2, sort_keys=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)
    return updated
