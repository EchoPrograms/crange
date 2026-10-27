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


_SEALED_WINDOWS_SOURCE = {
    'project': 100, 'node': 'proxmox4', 'source_id': 8912,
    'source_config_sha256': '5e9fdb9337ec631b74100dc1c1f47ffdfb02f8873b32ee6119b49bc7cef27e9d',
    'hive_sha256': {
        'SYSTEM': 'fb2f3526a7af150d153080eb608aef50650d2abd837efc3b76cec194334aef8e',
        'SOFTWARE': '08e9281118a0bad1d28560483648c0607da1656b3ef50bd703b950687a81f7c9',
        'SAM': 'b9f3b16431b4cfba63812c74967cb07a609eac695152fc56029bcab622ab6c62'},
    'answer_sha256': '11dc5811c48ec41ffc97c9980171f345abd750a90acd89e3cf0da17da808e2dc',
}
SEALED_WINDOWS_IMAGE_ID = 'mhl-windows-server-2025-base-p100-v1'


def validate_sealed_windows_proof_retirement(capture, receipt, image):
    """Validate a root-relay retirement receipt against immutable P100 evidence.

    The receipt is trusted only when obtained from the fixed authenticated P4
    relay. It is deliberately separate from the original capture: historical
    proof is never rewritten to claim that a stopped/deleted guest is live.
    """
    validate_sealed_windows_capture(capture)
    if (not isinstance(image, dict) or image.get('image_id') != SEALED_WINDOWS_IMAGE_ID or
            image.get('verified') is not True or image.get('approved_projects') != [100] or
            not re.fullmatch(r'[a-f0-9]{64}', str(image.get('artifact_sha256', '')))):
        raise CatalogError('Retirement requires the exact approved P100 published artifact.')
    capture_sha = hashlib.sha256(json.dumps(capture, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if (not isinstance(receipt, dict) or receipt.get('schema_version') != 1 or
            receipt.get('state') != 'proof-guests-retired' or
            receipt.get('image_id') != SEALED_WINDOWS_IMAGE_ID or
            receipt.get('artifact_sha256') != image['artifact_sha256'] or
            receipt.get('capture_sha256') != capture_sha or
            receipt.get('source_id') != 8912 or
            receipt.get('source_config_sha256') != _SEALED_WINDOWS_SOURCE['source_config_sha256'] or
            receipt.get('retired_vmids') != [8913, 8914] or
            receipt.get('source_preserved') is not True or
            receipt.get('delete_tasks_ok') is not True or
            receipt.get('guests_absent') is not True):
        raise CatalogError('Root retirement receipt is not bound to this published P100 proof.')
    rows = receipt.get('proofs')
    originals = {row['vmid']: row for row in capture['probes']}
    if not isinstance(rows, list) or len(rows) != 2:
        raise CatalogError('Retirement receipt lacks both original proof guests.')
    by_id = {}
    for row in rows:
        vmid = row.get('vmid') if isinstance(row, dict) else None
        original = originals.get(vmid)
        if (vmid not in (8913, 8914) or vmid in by_id or original is None or
                row.get('clone_upid') != original['clone_upid'] or
                row.get('config_sha256') != original['probe_config_sha256'] or
                not re.fullmatch(r'[a-f0-9]{64}', str(row.get('final_config_sha256', ''))) or
                row.get('final_status') != 'stopped' or row.get('qga_identity_match') is not True or
                row.get('identity_sha256') != hashlib.sha256(json.dumps(
                    original['identity'], sort_keys=True, separators=(',', ':')).encode()).hexdigest() or
                not re.fullmatch(r'UPID:proxmox4:[A-Fa-f0-9]+:[A-Fa-f0-9]+:[A-Fa-f0-9]+:qmstop:8(?:913|914):root@pam:',
                                 str(row.get('stop_upid', ''))) or
                not re.fullmatch(r'UPID:proxmox4:[A-Fa-f0-9]+:[A-Fa-f0-9]+:[A-Fa-f0-9]+:qmdestroy:8(?:913|914):root@pam:',
                                 str(row.get('delete_upid', '')))):
            raise CatalogError('Retirement record does not preserve the exact successful proof identity and lifecycle tasks.')
        if not re.fullmatch(r'[a-f0-9]{64}', str(row.get('qga_sha256', ''))):
            raise CatalogError('Retirement receipt lacks the final protected QGA identity digest.')
        by_id[vmid] = row
    if set(by_id) != {8913, 8914}:
        raise CatalogError('Retirement receipt is missing a fixed P100 proof guest.')
    return receipt


def validate_sealed_windows_capture(capture):
    """Validate the exceptional never-booted P100 source proof.

    The source intentionally has no machine identity. Two independently cloned
    guest identities provide the uniqueness proof. Callers must obtain this
    record from the fixed administrator worker workflow, never GUI fields.
    """
    if (not isinstance(capture, dict) or capture.get('state') != 'sealed-source-captured' or
            capture.get('provenance_type') != 'sealed-windows-source-v1' or
            capture.get('project') != 100 or capture.get('node') != 'proxmox4' or
            capture.get('kind') != 'vm' or capture.get('source_id') != 8912 or
            capture.get('source_config_sha256') != _SEALED_WINDOWS_SOURCE['source_config_sha256'] or
            capture.get('source_identity') is not None or capture.get('source_state') != 'stopped-never-booted' or
            capture.get('source_volumes') != ['crange-library:8912/vm-8912-disk-0.raw',
                                                'crange-library:8912/vm-8912-disk-1.raw'] or
            capture.get('hive_sha256') != _SEALED_WINDOWS_SOURCE['hive_sha256'] or
            capture.get('answer_sha256') != _SEALED_WINDOWS_SOURCE['answer_sha256'] or
            capture.get('generalization_state') != 4 or capture.get('cleanup_state') != 2 or
            capture.get('image_state') != 'IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE' or
            capture.get('fresh_source_freeze_verified') is not True or
            capture.get('protected_root_evidence_verified') is not True):
        raise CatalogError('Sealed Windows source provenance is not the fixed reviewed P100 source proof')
    probes = capture.get('probes')
    if not isinstance(probes, list) or len(probes) != 2:
        raise CatalogError('Sealed Windows source requires two independent ready clone proofs')
    by_id = {}
    for probe in probes:
        if (not isinstance(probe, dict) or type(probe.get('vmid')) is not int or
                probe.get('vmid') not in (8913, 8914) or probe.get('state') != 'unattended-windows-ready' or
                probe.get('source_config_sha256') != _SEALED_WINDOWS_SOURCE['source_config_sha256'] or
                not re.fullmatch(r'UPID:proxmox4:[A-Fa-f0-9]+:[A-Fa-f0-9]+:[A-Fa-f0-9]+:qmclone:8912:root@pam:',
                                 str(probe.get('clone_upid', ''))) or probe.get('clone_task_ok') is not True or
                probe.get('isolation_before_boot_verified') is not True or
                probe.get('source_config_unchanged') is not True or probe.get('manual_interventions') != [] or
                not re.fullmatch(r'[a-f0-9]{64}', str(probe.get('probe_config_sha256', '')))):
            raise CatalogError('Sealed Windows clone provenance is incomplete or invalid')
        identity = probe.get('identity')
        try:
            validate_source_identity(identity)
        except CatalogError as exc:
            raise CatalogError('Sealed Windows clone identity/readiness proof is invalid') from exc
        if (identity.get('os_caption') != 'Microsoft Windows Server 2025 Standard Evaluation' or
                identity.get('os_version') != '10.0.26100' or identity.get('os_build') != '26100' or
                identity.get('installation_type') != 'Server' or identity.get('part_of_domain') is not False):
            raise CatalogError('Sealed Windows clone is not the approved Server 2025 Desktop Experience profile')
        by_id[probe['vmid']] = probe
    if set(by_id) != {8913, 8914}:
        raise CatalogError('Sealed Windows source proof is missing an independent clone')
    if (by_id[8913]['probe_config_sha256'] == by_id[8914]['probe_config_sha256'] or
            by_id[8913]['clone_upid'] == by_id[8914]['clone_upid']):
        raise CatalogError('Sealed Windows clone PVE configurations or task origins are not independent')
    first, second = by_id[8913]['identity'], by_id[8914]['identity']
    if any(first.get(field) == second.get(field)
           for field in ('machine_guid', 'machine_sid', 'local_administrator_sid')):
        raise CatalogError('Sealed Windows clone identities are not distinct')
    if not re.fullmatch(r'[a-f0-9]{64}', str(capture.get('protected_bundle_sha256', ''))):
        raise CatalogError('Sealed Windows protected evidence bundle checksum is required')
    file_hashes = capture.get('protected_file_sha256')
    if (not isinstance(file_hashes, dict) or set(file_hashes) !=
            {'inspection', 'preparation', 'probe8913', 'probe8914', 'qga8914'} or
            any(not re.fullmatch(r'[a-f0-9]{64}', str(value)) for value in file_hashes.values())):
        raise CatalogError('Sealed Windows protected evidence file checksums are incomplete')
    return capture


def validate_source_identity(identity):
    if isinstance(identity, dict) and identity.get('os_family') == 'windows':
        guid = str(identity.get('machine_guid', ''))
        sid = str(identity.get('machine_sid', ''))
        admin_sid = str(identity.get('local_administrator_sid', ''))
        name = str(identity.get('computer_name', ''))
        sid_pattern = r'S-1-5-21-([1-9][0-9]{0,9})-([1-9][0-9]{0,9})-([1-9][0-9]{0,9})'
        valid_sid = re.fullmatch(sid_pattern, sid)
        valid_name = re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,13}[A-Za-z0-9])?', name)
        try:
            parsed_guid = __import__('uuid').UUID(guid)
            valid_guid = str(parsed_guid) == guid.lower() and parsed_guid.int != 0
        except (ValueError, AttributeError):
            valid_guid = False
        if (not valid_guid or not valid_sid or admin_sid != sid + '-500' or not valid_name
                or not str(identity.get('os_caption', '')).startswith('Microsoft Windows ')
                or not re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}', str(identity.get('os_version', '')))
                or not re.fullmatch(r'[0-9]{4,6}', str(identity.get('os_build', '')))
                or identity.get('installation_type') not in ('Server', 'Client')
                or (identity.get('installation_type') == 'Server' and
                    not str(identity.get('os_caption', '')).startswith('Microsoft Windows Server '))
                or (identity.get('installation_type') == 'Client' and
                    str(identity.get('os_caption', '')).startswith('Microsoft Windows Server '))
                or any(identity.get(field) is not True for field in
                       ('qemu_guest_agent_automatic', 'term_service_automatic', 'winrm_service_automatic'))
                or identity.get('qemu_guest_agent') is not True
                or identity.get('term_service_running') is not True
                or identity.get('winrm_service_running') is not True
                or identity.get('image_state') != 'IMAGE_STATE_COMPLETE'
                or any(type(identity.get(field)) is not int or identity[field] != 0
                       for field in ('oobe_in_progress', 'system_setup_in_progress', 'setup_type'))
                or identity.get('part_of_domain') is not False
                or identity.get('rdp_allowed_sources') != ['10.60.0.20']):
            raise CatalogError('Captured Windows source identity or readiness evidence is invalid')
        return identity
    if isinstance(identity, dict) and identity.get('os_family') not in (None, 'linux'):
        raise CatalogError('Captured source operating-system family is invalid')
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


def verified_local_cache_storage(replica):
    if replica.get('datastore_id')=='local-lvm':return True
    proof=replica.get('storage_verification',{})
    return (isinstance(proof,dict) and proof.get('node')==replica.get('node') and
            proof.get('datastore_id')==replica.get('datastore_id') and
            proof.get('local') is True and proof.get('snapshots_verified') is True)


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
        if 'windows_profile' in image and image['windows_profile'] not in (
                'mhl-server2025-desktop-experience', 'windows-client'):
            raise CatalogError(f'{image_id}: unsupported approved Windows profile')
        if 'system_disk' in image:
            profile = image['system_disk']
            capture = image.get('identity_capture', {})
            if (image.get('kind') != 'vm' or not isinstance(profile, dict) or
                    set(profile) != {'interface', 'disk_gb', 'source_config_sha256'} or
                    not re.fullmatch(r'(?:scsi|sata|virtio|ide)[0-9]+', str(profile.get('interface', ''))) or
                    not isinstance(profile.get('disk_gb'), (int, float)) or isinstance(profile.get('disk_gb'), bool) or
                    not math.isfinite(profile['disk_gb']) or profile['disk_gb'] <= 0 or
                    profile['disk_gb'] > image.get('disk_gb', 0) or
                    profile.get('source_config_sha256') != capture.get('source_config_sha256')):
                raise CatalogError(f'{image_id}: system disk must be tied to a valid captured VM config identity')
        if 'source_identity' in image:
            from crange_windows_snapshot import validate_captured_identity
            validate_captured_identity(image['source_identity'])
            if image['source_identity'].get('identity_mode')=='windows-scenario-snapshot-v1' and (image.get('kind')!='vm' or image.get('approved_projects')!=[image['source_identity']['snapshot_project_id']]):
                raise CatalogError('Scenario snapshots require exactly their own project approval.')
        if 'identity_capture' in image:
            capture = image['identity_capture']
            if not isinstance(capture, dict):
                raise CatalogError(f'{image_id}: identity capture provenance must be an object')
            if capture.get('provenance_type') == 'sealed-windows-source-v1':
                validate_sealed_windows_capture(capture)
                if ('source_identity' in image or image.get('os_family') != 'windows' or
                        image.get('kind') != 'vm' or 'init_system' in capture or 'init_system' in image or
                        image_id != SEALED_WINDOWS_IMAGE_ID or
                        image.get('windows_profile') != 'mhl-server2025-desktop-experience' or
                        image.get('display_name') != 'Windows Server 2025 Desktop Experience'):
                    raise CatalogError(f'{image_id}: sealed Windows source profile is inconsistent')
            elif capture.get('source_identity', {}).get('os_family') == 'windows':
                if image.get('os_family') != 'windows' or capture.get('os_family') != 'windows' or 'init_system' in capture or 'init_system' in image:
                    raise CatalogError(f'{image_id}: Windows identity capture profile is inconsistent')
            elif ('init_system' in capture or 'init_system' in image) and (
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
                if not verified_local_cache_storage(replica) or not replica.get('job_id'):
                    raise CatalogError('local cache requires an owned verified local thin-storage generation')
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


def publish_release_image(catalog, release, alias, export, probe, *, source_datastore):
    """Bind completed worker evidence to one immutable release guest image.

    The caller reads administrator-owned journals. Staging health checks alone
    cannot substitute for a fresh, isolated source-clone identity test.
    """
    from crange_distribution import publish_exported_image
    reference = release.get('scenario', {}).get('templates', {}).get(alias)
    if not reference:
        raise CatalogError('Unknown release template alias')
    if export.get('verified') is not True or not export.get('upid'):
        raise CatalogError('Completed verified export task evidence required')
    image_id = reference.get('image_id', alias)
    kind = reference.get('kind', 'vm')
    source = (reference['template_node'], kind, int(reference['template_id']))
    if (export.get('node'), export.get('kind'), export.get('source_id')) != source:
        raise CatalogError('Export does not match the immutable release template')
    capture = export.get('identity_capture', {})
    tasks = probe.get('provider_tasks', [])
    reservation = probe.get('reservation', {})
    if (probe.get('state') != 'source-probed'
            or (probe.get('source_node'), probe.get('kind'), probe.get('source_id')) != source
            or probe.get('source_config_sha256') != capture.get('source_config_sha256')
            or probe.get('source_capture_job_id') != capture.get('job_id')
            or reservation.get('operation') != 'source_probe'
            or reservation.get('project_id') != int(release['project_id'])
            or reservation.get('artifact_sha256') != capture.get('source_config_sha256')
            or not probe.get('probe_id') or probe['probe_id'] == source[2]
            or not tasks or any(t.get('state') != 'confirmed-terminal' for t in tasks)
            or tasks[-1].get('path') != f'/nodes/{source[0]}/{"qemu" if kind == "vm" else "lxc"}/{probe["probe_id"]}'
            or probe.get('verification', {}).get('identity_unique') is not True):
        raise CatalogError('Completed matching source-clone verification and cleanup required')
    if not export.get('volume_id', '').startswith('crange-library:backup/'):
        raise CatalogError('Release distribution requires a complete central library artifact')
    updated = publish_exported_image(catalog, image_id, export, architecture='amd64',
        source_revision=release['source_revision'], approved_projects=[int(release['project_id'])],
        datastore_id=source_datastore, verification=probe['verification'])
    updated['images'][image_id]['release'] = {
        'release_id': release['release_id'], 'manifest_sha256': release['integrity_sha256'],
        'template_alias': alias, 'export_job_id': export['job_id'], 'probe_job_id': probe['job_id']}
    updated['images'][image_id]['retained_release_references'] = [release['release_id']]
    return validate_catalog(updated)


def retire_image(catalog, image_id, linked_references=()):
    if any(r.get('cache_local') for r in catalog['images'][image_id].get('replicas', [])):
        raise CatalogError('evict local cache generations with dependency proof before retirement')
    if linked_references or catalog['images'][image_id].get('retained_release_references'):
        raise CatalogError('image still has clone or retained release dependencies')
    result = copy.deepcopy(catalog)
    del result['images'][image_id]
    return validate_catalog(result)
