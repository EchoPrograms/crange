"""Immutable, controller-owned logical image catalog validation."""
import copy
import base64
import struct
import hashlib
import json
import math
import re


class CatalogError(ValueError):
    pass


def validate_source_identity(identity):
    if not isinstance(identity, dict) or not re.fullmatch(r'[a-f0-9]{32}', identity.get('machine_id', '')) or identity['machine_id'] == '0' * 32:
        raise CatalogError('Captured source machine identity is invalid')
    parts = str(identity.get('ssh_host_key', '')).split()
    try:
        raw = base64.b64decode(parts[1], validate=True)
        name_length = struct.unpack('>I', raw[:4])[0]
        offset = 4 + name_length
        valid = (parts[0] == 'ssh-ed25519' and raw[4:offset] == b'ssh-ed25519'
                 and struct.unpack('>I', raw[offset:offset+4])[0] == 32 and len(raw) == offset + 4 + 32)
    except (ValueError, IndexError, struct.error):
        valid = False
    if not valid:
        raise CatalogError('Captured source Ed25519 public host key is invalid')
    return identity


def validate_probe_resources(profile):
    if (not isinstance(profile, dict) or
            set(profile) != {'memory_mb', 'cpu_cores', 'verified', 'evidence'} or
            profile.get('verified') is not True or
            not isinstance(profile.get('evidence'), str) or not profile['evidence'].strip()):
        raise CatalogError('Reduced probe resources require reviewed live boot evidence')
    for key, low, high in (('memory_mb', 128, 65536), ('cpu_cores', 1, 64)):
        value = profile.get(key)
        if type(value) is not int or not low <= value <= high:
            raise CatalogError('Invalid verified probe resource allocation')
    return profile


def validate_approved_projects(scope):
    if not isinstance(scope, list) or not scope:
        raise CatalogError('approval scope must be a non-empty project list')
    if '*' in scope:
        if scope != ['*']:
            raise CatalogError('universal approval cannot be mixed with project approvals')
        return scope
    seen = set()
    for project in scope:
        if (type(project) is not int or not 1 <= project <= 255 or project in (60, 61)
                or project in seen):
            raise CatalogError('approval scope contains an invalid or duplicate project')
        seen.add(project)
    return scope


def validate_catalog(catalog):
    if catalog.get('schema_version') != 1 or not isinstance(catalog.get('images'), dict):
        raise CatalogError('catalog requires schema_version 1 and images mapping')
    vmids = set()
    archives = set()
    for image_id, image in catalog['images'].items():
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', image_id):
            raise CatalogError('invalid image ID')
        if image.get('kind') not in ('vm', 'container') or not image.get('architecture'):
            raise CatalogError(f'{image_id}: kind and architecture required')
        if not re.fullmatch(r'[a-f0-9]{64}', image.get('artifact_sha256', '')):
            raise CatalogError(f'{image_id}: published artifact SHA256 required')
        if not image.get('source_revision') or not image.get('verified'):
            raise CatalogError(f'{image_id}: verified publication and source revision required')
        try:
            validate_approved_projects(image.get('approved_projects'))
        except CatalogError as exc:
            raise CatalogError(f'{image_id}: invalid approval scope: {exc}') from exc
        if 'init_system' in image and image['init_system'] not in ('systemd', 'openrc'):
            raise CatalogError(f'{image_id}: init_system must be systemd or openrc')
        if 'source_identity' in image:
            validate_source_identity(image['source_identity'])
        if 'identity_capture' in image:
            capture = image['identity_capture']
            if not isinstance(capture, dict):
                raise CatalogError(f'{image_id}: identity capture provenance must be an object')
            if ('init_system' in capture or 'init_system' in image) and (
                    capture.get('init_system') not in ('systemd', 'openrc')
                    or capture.get('init_system') != image.get('init_system')):
                raise CatalogError(f'{image_id}: identity capture init_system is invalid or inconsistent')
        if 'probe_resources' in image:
            validate_probe_resources(image['probe_resources'])
        for replica in image.get('replicas', []):
            vmid = replica.get('template_id')
            archive = replica.get('template_file_id')
            if archive:
                if image['kind'] != 'container' or image.get('source_type') != 'stock_archive' or vmid is not None:
                    raise CatalogError('only stock container archives may use template_file_id instead of VMID')
                if not isinstance(archive, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+:vztmpl/[A-Za-z0-9_.-]+', archive):
                    raise CatalogError('archive must be an approved Proxmox vztmpl volume ID')
                identity = (replica.get('node'), archive)
                if identity in archives:
                    raise CatalogError('duplicate archive replica on node')
                archives.add(identity)
            else:
                if not isinstance(vmid, int) or isinstance(vmid, bool) or vmid < 100 or vmid in vmids:
                    raise CatalogError('VM/captured-container replicas require unique cluster-wide VMIDs')
                if image.get('source_type') == 'stock_archive':
                    raise CatalogError('stock archives require template_file_id')
                vmids.add(vmid)
            if not replica.get('node') or not replica.get('datastore_id'):
                raise CatalogError('replica node and datastore required')
            if replica.get('cache_local'):
                size = image.get('disk_gb')
                if (not isinstance(size, (int, float)) or isinstance(size, bool) or
                        not math.isfinite(size) or size <= 0):
                    raise CatalogError('local cache requires defensible virtual disk allocation')
                if replica.get('datastore_id') != 'local-lvm' or not replica.get('job_id'):
                    raise CatalogError('local cache requires owned local-lvm generation')
                if replica.get('state') == 'ready' and (
                        not replica.get('clone_modes_verified') or
                        not set(replica['clone_modes_verified']).issubset({'linked', 'full'})):
                    raise CatalogError('ready local cache requires tested clone modes')
            if replica.get('state') == 'ready':
                if replica.get('artifact_sha256') != image['artifact_sha256'] or not replica.get('verified_at'):
                    raise CatalogError('ready replica must verify exact artifact identity')
                if replica.get('representation_sha256', image['artifact_sha256']) != image['artifact_sha256'] and not replica.get('conversion_verification'):
                    raise CatalogError('converted representation requires verification provenance')
    return catalog


def catalog_identity(catalog):
    validate_catalog(catalog)
    return hashlib.sha256(json.dumps(catalog, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def publish_image(catalog, image_id, definition):
    """Return a new catalog; published IDs cannot be replaced in place."""
    result = copy.deepcopy(catalog)
    if image_id in result.get('images', {}):
        raise CatalogError('published image IDs are immutable; publish a new version')
    result.setdefault('schema_version', 1)
    result.setdefault('images', {})[image_id] = copy.deepcopy(definition)
    return validate_catalog(result)


def publish_replica(catalog, image_id, replica):
    result = copy.deepcopy(catalog)
    if image_id not in result['images']:
        raise CatalogError('unknown image version')
    result['images'][image_id].setdefault('replicas', []).append(copy.deepcopy(replica))
    return validate_catalog(result)


def retire_image(catalog, image_id, linked_references=()):
    if any(r.get('cache_local') for r in catalog['images'][image_id].get('replicas', [])):
        raise CatalogError('evict local cache generations with dependency proof before retirement')
    if linked_references or catalog['images'][image_id].get('retained_release_references'):
        raise CatalogError('image still has clone or retained release dependencies')
    result = copy.deepcopy(catalog)
    del result['images'][image_id]
    return validate_catalog(result)
