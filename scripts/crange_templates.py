"""Admin-owned ISO installer VMs and immutable, project-approved templates."""
from __future__ import annotations
import json
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDS = ROOT / 'templates/builds'
NODES = {f'proxmox{i}' for i in range(1, 7)}
MEDIA_STORAGES = ('crange-library', 'media-mirror1', 'media-mirror4', 'local')
LIBRARY_STATUS = Path('/var/lib/crange-media-library/status.json')
WINDOWS_SERVER_EVAL_ISO = 'crange-library:iso/26100.32230.260111-0550.lt_release_svc_refresh_SERVER_EVAL_x64FRE_en-us.iso'
WINDOWS_UNATTEND_NS = 'urn:schemas-microsoft-com:unattend'
WINDOWS_UNATTEND_WCM_NS = 'http://schemas.microsoft.com/WMIConfig/2002/State'


def serialize_windows_unattend(root):
    """Serialize answer files with Windows Setup's required default namespace."""
    if root.tag != f'{{{WINDOWS_UNATTEND_NS}}}unattend':
        raise ValueError('Unexpected Windows unattended-answer root element.')
    ET.register_namespace('', WINDOWS_UNATTEND_NS)
    ET.register_namespace('wcm', WINDOWS_UNATTEND_WCM_NS)
    raw = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    if b'<ns0:' in raw or b'</ns0:' in raw or b'<ms0:' in raw or b'</ms0:' in raw:
        raise ValueError('Windows unattended serializer emitted a prefixed core namespace.')
    return raw


def library_status():
    try:
        value = json.loads(LIBRARY_STATUS.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def media_inventory(api, node, content='iso', *, offline_storages=()):
    result = []
    for storage in MEDIA_STORAGES:
        if storage in offline_storages:
            continue
        try:
            rows = api.request('GET', f'/nodes/{node}/storage/{storage}/content', {'content': content})
            if not isinstance(rows, list):
                continue
            result.extend({**row, 'storage': storage, 'node': node, 'content': content} for row in rows
                          if isinstance(row, dict) and int(row.get('size', 0)) > 0 and
                          str(row.get('volid', '')).startswith(storage + ':' + ('iso/' if content == 'iso' else 'vztmpl/')))
        except Exception:
            continue
    return result


def resolve_media(api, node, volid, content):
    if content not in ('iso','vztmpl'):
        raise ValueError('Unsupported installation media type.')
    available = media_inventory(api, node, content)
    if any(row['volid'] == volid for row in available):
        return volid
    # A mirror is equivalent only when the synchronizer recorded its content hash.
    name = volid.split('/')[-1]
    for item in library_status().get('files', []):
        if item.get('content') != content or item.get('name') != name or not item.get('sha256'):
            continue
        verified = {copy.get('storage') for copy in item.get('copies', [])
                    if copy.get('sha256') == item['sha256']}
        for storage in MEDIA_STORAGES[:3]:
            candidate = storage + ':' + content + '/' + name
            if storage in verified and any(row['volid'] == candidate and int(row['size']) == item.get('size') for row in available):
                return candidate
    raise ValueError('Selected installation media is unavailable on this node and no verified mirror is available.')


def resolve_iso(api, node, volid):
    return resolve_media(api,node,volid,'iso')


def resolve_archive(api, node, volid):
    return resolve_media(api,node,volid,'vztmpl')


MODELS = {'virtio', 'e1000', 'e1000e', 'rtl8139', 'vmxnet3'}


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{label} must be {low}–{high}.')
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value):
        raise ValueError('Choose a template build.')
    return value


def validate(value):
    fields = {'name', 'node', 'iso', 'driver_iso', 'os', 'cores', 'memory', 'disk', 'disk_bus',
              'bios', 'machine', 'cpu', 'nic_models', 'agent', 'cloud_init', 'tpm'}
    if not isinstance(value, dict) or set(value) not in (fields, fields | {'windows_unattended'}):
        raise ValueError('Unexpected or missing template settings.')
    value = dict(value)
    value.setdefault('windows_unattended', False)
    if not isinstance(value['name'], str) or not re.fullmatch('[a-z][a-z0-9-]{0,39}', value['name']):
        raise ValueError('Template name must be 1–40 lowercase letters, digits or hyphens.')
    choices = {'node': NODES, 'os': {'linux','windows','bsd','other'}, 'bios': {'seabios','ovmf'},
               'machine': {'q35','i440fx'}, 'cpu': {'host','x86-64-v2-AES','kvm64'}, 'disk_bus': {'scsi','sata','virtio'}}
    for key, allowed in choices.items():
        if value[key] not in allowed: raise ValueError(f'Choose an approved {key} option.')
    for key, low, high in [('cores',1,16),('memory',512,65536),('disk',8,1024)]:
        integer(value[key],low,high,key)
    for key in ('iso','driver_iso'):
        if key == 'driver_iso' and not value[key]:continue
        if not isinstance(value[key],str) or not re.fullmatch(r'(?:local|crange-library|media-mirror1|media-mirror4):iso/[a-zA-Z0-9][a-zA-Z0-9_.-]{0,159}\.iso', value[key], re.IGNORECASE):
            raise ValueError('Choose an ISO from the shared library or an approved local mirror.')
    if not isinstance(value['nic_models'],list) or not 1 <= len(value['nic_models']) <= 8 or any(m not in MODELS for m in value['nic_models']):
        raise ValueError('Configure 1–8 supported network adapters.')
    for key in ('agent','cloud_init','tpm'):
        if not isinstance(value[key],bool):raise ValueError(f'Invalid {key} option.')
    if not isinstance(value['windows_unattended'],bool):raise ValueError('Invalid Windows unattended-install option.')
    if value['windows_unattended'] and (value['os']!='windows' or value['iso']!=WINDOWS_SERVER_EVAL_ISO or value['cloud_init']):
        raise ValueError('The fixed answerfile is only approved for the verified Server 2025 Evaluation media.')
    if value['windows_unattended'] and not value['driver_iso']:
        raise ValueError('The unattended Server 2025 installer requires the approved VirtIO driver ISO for QEMU Guest Agent and serial drivers.')
    if value['windows_unattended'] and not value['agent']:
        raise ValueError('The unattended Windows base requires the QEMU Guest Agent setting.')
    if value['tpm'] and value['bios'] != 'ovmf':raise ValueError('TPM requires UEFI firmware.')
    return value


def save(record):
    BUILDS.mkdir(parents=True,exist_ok=True)
    target=BUILDS / (identifier(record['id']) + '.json')
    temp=target.with_suffix('.tmp');temp.write_text(json.dumps(record,indent=2)+'\n');temp.chmod(0o644);temp.replace(target)


def _release_installer_reservation(job_id):
    import crange_scheduling as scheduling
    from crange_placement import ReservationLedger
    ledger = ReservationLedger(scheduling.DATA / 'reservations.sqlite3')
    try:
        ledger.reconcile_job(job_id, operation_terminal=True, allocations_reconciled=True)
    finally:
        ledger.db.close()


def load(build_id):
    record=json.loads((BUILDS / (identifier(build_id)+'.json')).read_text())
    if record.get('id')!=build_id:raise ValueError('Invalid build record.')
    return record


def owned(api,record,*,resources=None):
    node=record.get('definition',{}).get('node');vmid=record.get('vmid')
    resources=resources if resources is not None else api.request('GET','/cluster/resources',{'type':'vm'})
    matches=[row for row in resources if row.get('vmid')==vmid]
    if len(matches)!=1 or matches[0].get('type')!='qemu' or matches[0].get('node')!=node:
        raise ValueError('Installer VM is missing or no longer matches this build record.')
    actual=api.vm(node,vmid)
    tags=str(actual.get('tags','')).split(';')
    owner_tag=record.get('ownership_tag')
    if (actual.get('name') != record.get('guest_name') or 'crange-template-build' not in tags or
            not isinstance(owner_tag,str) or owner_tag not in tags):
        raise ValueError('Installer VM identity changed; refusing operation.')
    return actual


def options(api=None):
    from crange import ProxmoxAPI
    api=api or ProxmoxAPI()
    nodes=[]
    archives={}
    try:
        online={row['node'] for row in api.request('GET','/nodes') if row.get('status')=='online'}
    except Exception:
        online=set()
    offline_storages={storage for storage,host in [('media-mirror1','proxmox1'),('media-mirror4','proxmox4')] if host not in online}
    def collect(node):
        if node not in online:
            return node, [], []
        return node, media_inventory(api,node,offline_storages=offline_storages), media_inventory(api,node,'vztmpl',offline_storages=offline_storages)
    with ThreadPoolExecutor(max_workers=6) as pool:
        collected=list(pool.map(collect, sorted(NODES)))
    for node, media, containers in collected:
        nodes.append({'name':node,'isos':[m['volid'] for m in media],
                      'container_archives':[m['volid'] for m in containers],
                      'unavailable':node not in online if online else not media and not containers})
        for row in media+containers:
            key=(row['content'],row['volid'].split('/')[-1],int(row['size']))
            item=archives.setdefault(key,{'content':key[0],'name':key[1],'size':key[2],'locations':[]})
            item['locations'].append({'node':node,'storage':row['storage'],'volid':row['volid']})
    try:
        resources=api.request('GET','/cluster/resources',{'type':'vm'})
    except Exception:
        resources=None
    builds=[]
    for path in sorted(BUILDS.glob('*.json')):
        record=json.loads(path.read_text())
        if record.get('definition', {}).get('windows_unattended'):
            try:
                import crange_windows_seed
                status = crange_windows_seed.public_status(record['id'])
                if status:
                    record['seed_upload'] = status
                elif record.get('private_answerfile', {}).get('state') in ('uploading', 'upload_uncertain', 'journal_unavailable'):
                    record['seed_upload'] = dict(record['private_answerfile'], reconcile_required=True)
            except Exception:
                record['seed_upload'] = {'state': 'journal_unavailable', 'reconcile_required': True,
                    'reason': 'Private seed journal could not be verified; preserve this build for administrator review.'}
        record.update(console_available=False,console_node=None,error=None)
        if record.get('status') in ('failed','cancelled','published'):
            record['error']='Installer VM is not available for console or publishing.'
        elif resources is None:
            record['error']='Could not verify installer VM with Proxmox.'
        else:
            try:
                actual=owned(api,record,resources=resources)
                if int(actual.get('template',0)):
                    record['error']='Installer VM is not available for console or publishing.'
                else:
                    record['console_available']=True
                    record['console_node']=record['definition']['node']
            except Exception:
                record['error']='Installer VM is missing or no longer matches this build record.'
        if record.get('definition', {}).get('os') == 'windows' and record['console_available']:
            try:
                from crange_windows_base import eligibility
                record['windows_base_workflow'] = eligibility(record, api=api)
                from crange_windows_installer import eligibility as installer_eligibility
                record['windows_installer_boot'] = installer_eligibility(record, api)
            except Exception:
                record['windows_base_workflow'] = {
                    'eligible': False, 'can_prepare': False, 'can_seal': False,
                    'state': 'ineligible',
                    'reason': 'Windows base preparation status is unavailable; retain the owned build for administrator review.'}
        builds.append(record)
    try:
        from crange_scheduling import read_settings
        catalog = read_settings()[1]
        from crange_template_selection import is_release_template
        logical_images = []
        for image_id, image in sorted(catalog['images'].items()):
            if is_release_template(image_id, image):
                continue
            replicas = [replica for replica in image.get('replicas', []) if replica.get('state') == 'ready']
            logical_images.append({'image_id': image_id, 'kind': image['kind'],
                                   'approved_projects': image['approved_projects'],
                                   'ready_nodes': sorted({replica['node'] for replica in replicas}),
                                   'source_type': image.get('source_type', 'published_template')})
    except Exception:
        logical_images = None
    try:
        from crange_windows_p100_publication import status as sealed_p100_status
        sealed_windows_p100 = sealed_p100_status()
    except Exception as exc:
        sealed_windows_p100 = {'available': False,
            'label': 'Windows Server 2025 Desktop Experience',
            'reason': 'Sealed P100 publication status is unavailable: ' + (str(exc) or 'protected state could not be read.')}
    try:
        from crange_windows_cache_recovery import public_recovery_status
        cache_recovery = public_recovery_status()
    except Exception:
        cache_recovery = []
    try:
        from crange_cache_restore import public_status as restored_cache_status
        restored_caches = restored_cache_status()
    except Exception:
        restored_caches = []
    return {'nodes':nodes,'builds':builds,'media':list(archives.values()),'library':library_status(),
            'logical_images':logical_images,
            'maintained_bases':[{'key':key,'name':value['display_name'],'kind':value['kind']} for key,value in __import__('crange_platform_bases').BASES.items()],
            'sealed_windows_p100': sealed_windows_p100,
            'cache_recovery': cache_recovery,
            'restored_caches': restored_caches,
            'library_storage':'crange-library','mirror_storages':['media-mirror1','media-mirror4']}


def project_image_readiness(project_id):
    """Return only this project's approved-template to logical-image readiness."""
    from crange import project_dir, templates_for, container_templates_for
    from crange_scheduling import read_settings
    path = project_dir(str(project_id))
    catalog = read_settings()[1]
    from crange_template_selection import is_release_template
    result = {}
    for key, filename, approved in (
            ('vm_templates', 'approved-scenario-templates.json', templates_for(path)),
            ('container_templates', 'approved-container-templates.json', container_templates_for(path))):
        result[key] = {}
        for alias, reference in sorted(approved.items()):
            image_id = reference.get('image_id')
            image = catalog['images'].get(image_id) if image_id else None
            ready_nodes = []
            if image and image.get('kind') == ('vm' if key == 'vm_templates' else 'container'):
                scope = image.get('approved_projects', [])
                if '*' in scope or int(project_id) in scope:
                    ready_nodes = sorted({replica['node'] for replica in image.get('replicas', [])
                                          if replica.get('state') == 'ready'})
            if not image_id:
                status, reason = 'missing-image-id', 'Approved Proxmox template has no logical image ID.'
            elif not image:
                status, reason = 'missing-catalog-image', 'Logical image ID is absent from the verified image catalog.'
            elif image.get('kind') != ('vm' if key == 'vm_templates' else 'container'):
                status, reason = 'kind-mismatch', 'Catalog image type does not match this template.'
            elif not ('*' in image.get('approved_projects', []) or int(project_id) in image.get('approved_projects', [])):
                status, reason = 'not-approved', 'Catalog image is not approved for this project.'
            elif not ready_nodes:
                status, reason = 'no-ready-replica', 'Catalog image has no verified ready replica.'
            else:
                status, reason = 'ready', 'Verified catalog image is approved for this project.'
            result[key][alias] = {'status': status, 'reason': reason, 'image_id': image_id,
                                  'display_name': reference.get('display_name') or alias,
                                  'template_id': reference.get('template_id'),
                                  'template_node': reference.get('template_node'),
                                  'ready_nodes': ready_nodes,
                                  **({'system_disk_gb': image['system_disk']['disk_gb']}
                                     if key == 'vm_templates' and isinstance(image, dict) and
                                     isinstance(image.get('system_disk'), dict) else {})}
    catalog_images = []
    for image_id, image in sorted(catalog['images'].items()):
        scope = image.get('approved_projects', [])
        ready_nodes = sorted({replica['node'] for replica in image.get('replicas', [])
                              if replica.get('state') == 'ready'})
        if not is_release_template(image_id, image) and ready_nodes and ('*' in scope or int(project_id) in scope):
            catalog_images.append({'image_id': image_id, 'kind': image['kind'],
                                   'ready_nodes': ready_nodes})
    result['catalog_images'] = catalog_images
    return result


def approve_catalog_image(image_id, projects, api=None):
    """Link a verified, ready VM image into selected projects under a new alias.

    This does not publish evidence or mutate the image catalog. It only grants
    project-local template selection for an already globally approved image.
    """
    from crange import ProxmoxAPI, project_dir, templates_for, container_templates_for
    from crange_scheduling import read_settings

    if not isinstance(image_id, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', image_id):
        raise ValueError('Invalid logical image ID.')
    if not isinstance(projects, list) or not projects:
        raise ValueError('Choose one or more projects.')
    normalized = []
    for project in projects:
        if isinstance(project, bool) or not isinstance(project, int) or not 1 <= project <= 255:
            raise ValueError('Choose a valid project.')
        if project in normalized:
            raise ValueError('Duplicate project selection.')
        normalized.append(project)

    from crange_template_selection import is_release_template
    if is_release_template(image_id):
        raise ValueError('Captured release images must be deployed through Releases, not used as OS base templates.')
    catalog = read_settings()[1]
    image = catalog.get('images', {}).get(image_id)
    if not image or image.get('kind') not in ('vm', 'container') or image.get('verified') is not True:
        raise ValueError('Choose a verified VM or container image from the protected catalog.')
    approved = image.get('approved_projects', [])
    if '*' not in approved and any(project not in approved for project in normalized):
        raise ValueError('Image is not approved for every selected project.')
    digest = image.get('artifact_sha256')
    identity = image.get('identity_capture') or {}
    sealed_windows = identity.get('provenance_type') == 'sealed-windows-source-v1'
    source_identity = identity.get('source_identity') or {}
    if sealed_windows:
        from crange_catalog import validate_sealed_windows_capture
        validate_sealed_windows_capture(identity)
        if (image.get('os_family') != 'windows' or
                image.get('windows_profile') != 'mhl-server2025-desktop-experience' or
                image.get('display_name') != 'Windows Server 2025 Desktop Experience'):
            raise ValueError('Sealed Windows image is missing its approved Server 2025 Desktop Experience profile.')
    elif (identity.get('state') != 'identity-captured' or not source_identity.get('machine_id') or
          not source_identity.get('ssh_host_key')):
        raise ValueError('Image provenance or captured guest identity is incomplete.')
    if (not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or
            identity.get('kind') != image['kind'] or
            not re.fullmatch(r'[0-9a-f]{64}', str(identity.get('source_config_sha256', '')))):
        raise ValueError('Image provenance or captured guest identity is incomplete.')

    ready = []
    for replica in image.get('replicas', []):
        evidence = (replica.get('verification') or {}).get('evidence') or {}
        verification = replica.get('verification') or {}
        if sealed_windows:
            from crange_catalog import validate_source_identity
            replica_identity = evidence.get('identity') or {}
            try:
                validate_source_identity(replica_identity)
                unique = all(replica_identity.get(field) not in
                    {probe['identity'][field] for probe in identity['probes']}
                    for field in ('machine_guid', 'machine_sid', 'local_administrator_sid'))
            except (TypeError, ValueError):
                unique = False
            unique = unique and verification.get('identity_unique') is True
        else:
            replica_identity = (evidence.get('machine_id'), evidence.get('ssh_host_key'))
            unique = (replica_identity[0] and replica_identity[1] and
                      replica_identity[0] != source_identity['machine_id'] and
                      replica_identity[1] != source_identity['ssh_host_key'])
        if (replica.get('state') == 'ready' and replica.get('artifact_sha256') == digest and
                replica.get('cache_local') is True and 'linked' in replica.get('clone_modes_verified', []) and
                verification.get('passed') is True and unique and replica.get('template_id')):
            ready.append(replica)
    if not ready:
        raise ValueError('Image has no verified local linked-clone replica.')
    # Prefer the verified P2 replica called out in the publication record, then
    # use the lowest numbered matching node. Never fall back to the source VM.
    ready.sort(key=lambda row: (row['node'] != 'proxmox2', row['node'], row['template_id']))
    replica = ready[0]
    if api is None:
        api = ProxmoxAPI()
    config = api.request('GET', f"/nodes/{replica['node']}/{'qemu' if image['kind']=='vm' else 'lxc'}/{int(replica['template_id'])}/config")
    tags = str(config.get('tags', '')).split(';') if isinstance(config, dict) else []
    if (not isinstance(config, dict) or str(config.get('template', '0')) not in ('1', 'true', 'True') or
            'crange-image' not in tags or f"crange-cache-{int(replica['template_id'])}" not in tags):
        raise ValueError('The catalog replica is not an owned Proxmox image template.')

    alias = image_id.removesuffix('-20261005')
    alias = re.sub(r'[^a-z0-9-]+', '-', alias.lower()).strip('-')
    if not alias or not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', alias):
        raise ValueError('Logical image ID cannot form a safe template alias.')
    profile = {'init_system': image.get('init_system', 'systemd')} if image['kind']=='container' else {}
    for source_key, profile_key in (('bios', 'bios'), ('machine', 'machine'),
                                    ('scsihw', 'scsi_hardware'), ('ostype', 'os_type'),
                                    ('cpu', 'cpu_type')):
        if isinstance(config.get(source_key), str) and config[source_key]:
            profile[profile_key] = config[source_key]
    agent = config.get('agent')
    if isinstance(agent, (str, int)):
        profile['agent_enabled'] = str(agent) == '1' or str(agent).startswith('enabled=1')
    profile['cloud_init'] = any(str(config.get(key, '')).split(',')[0].endswith('cloudinit') for key in ('ide0', 'ide1', 'ide2', 'sata0', 'scsi0'))
    boot = str(config.get('boot', ''))
    if boot.startswith('order='):
        profile['boot_order'] = [part for part in boot[6:].split(';') if part]
    network_models = []
    for key in sorted((key for key in config if re.fullmatch(r'net[0-9]+', key)),
                      key=lambda value: int(value[3:])):
        model = str(config[key]).partition('=')[0]
        if model:
            network_models.append(model)
    if network_models:
        profile['network_models'] = network_models
    reference = {'template_id': int(replica['template_id']), 'template_node': replica['node'],
                 'display_name': image.get('display_name') or image_id,
                 'image_id': image_id, 'image_artifact_sha256': digest,
                 'source_config_sha256': identity['source_config_sha256'],
                 'replica_datastore': replica.get('datastore_id'),
                 'replica_clone_modes': ['linked'], 'profile': profile}
    reader = templates_for if image['kind']=='vm' else container_templates_for
    filename = 'approved-scenario-templates.json' if image['kind']=='vm' else 'approved-container-templates.json'
    paths = [project_dir(str(project)) for project in normalized]
    retained = {}
    for path in paths:
        previous = reader(path).get(alias)
        if previous:
            if (previous.get('image_id') != image_id or
                    previous.get('image_artifact_sha256') != digest or
                    previous.get('source_config_sha256') != identity['source_config_sha256']):
                raise ValueError(f'Template alias {alias!r} already refers to a different image in one selected project.')
            # Filling another node's cache does not change the immutable image
            # or require rebinding an existing project to a different cache.
            retained[path] = previous
    for path in paths:
        file = path / filename
        approved = reader(path)
        approved[alias] = retained.get(path, reference)
        temp = file.with_suffix('.tmp')
        temp.write_text(json.dumps(approved, indent=2) + '\n')
        temp.chmod(0o640)
        temp.replace(file)
    reference = retained.get(paths[0], reference)
    print(f'Linked verified image {image_id} as {alias} for projects {", ".join(map(str, normalized))}.')
    return {'alias': alias, 'image_id': image_id, 'template_id': reference['template_id'],
            'template_node': reference['template_node'], 'projects': normalized}


def create(definition,api=None):
    from crange import ProxmoxAPI
    import os
    value=validate(definition);api=api or ProxmoxAPI();node=value['node']
    value=dict(value)
    unattend_iso=None
    for key in ('iso','driver_iso'):
        if value[key]:value[key]=resolve_iso(api,node,value[key])
    if not any(n.get('vnet')=='craccess' and n.get('zone')=='crange' for n in api.request('GET','/cluster/sdn/vnets')):
        raise ValueError('Approved installer network craccess is unavailable on this node.')
    guests=api.request('GET','/cluster/resources',{'type':'vm'})
    builds=[r for r in guests if 'crange-template-build' in str(r.get('tags','')).split(';') and not r.get('template')]
    if len(builds)>=20:raise ValueError('The platform limit of 20 installer VMs has been reached.')
    if any(r.get('name')=='crange-build-'+value['name'] for r in guests):
        raise ValueError('An installer VM with that name already exists.')
    record={'id':uuid.uuid4().hex,'definition':value,'vmid':int(api.request('GET','/cluster/nextid')),
            'guest_name':'crange-build-'+value['name'],'created':datetime.now(timezone.utc).isoformat(),'status':'creating'}
    record['ownership_tag']='crange-build-'+record['id']
    # Installer VMs are outside Terraform placement, so explicitly admit them
    # against the same fresh inventory and transactional reservation ledger.
    import crange_scheduling as scheduling
    from crange_placement import ReservationLedger
    policy, _ = scheduling.read_settings()
    inventory = scheduling.discover(api, policy)
    job_id = os.environ.get('CRANGE_JOB_ID') or ('cli-template-' + uuid.uuid4().hex)
    actor = os.environ.get('CRANGE_ACTOR') or 'platform-admin-cli'
    ledger = ReservationLedger(scheduling.DATA / 'reservations.sqlite3')
    try:
        ledger.reserve_external_capacity(project_id=0, job_id=job_id, owner=actor,
            operation='template-create', node_name=node, datastore_id='local-lvm',
            demand={'memory_mb': value['memory'], 'cpu_cores': value['cores'],
                    'disk_gb': value['disk'] + (2 if value['bios'] == 'ovmf' else 0) + (1 if value['tpm'] else 0)},
            inventory=inventory, policy=policy, build_id=record['id'], vmid=record['vmid'])
    finally:
        ledger.db.close()
    record['capacity_job_id']=job_id
    save(record)
    if value['windows_unattended']:
        import crange_windows_seed
        try:
            unattend = crange_windows_seed.create(record['id'], record['vmid'], node, value['driver_iso'], api)
            record['private_answerfile'] = {key: unattend[key] for key in ('build_id', 'vmid', 'volid', 'sha256')}
            record['private_answerfile']['iso_sha256'] = unattend['iso_sha256']
            record['private_answerfile']['driver_iso'] = value['driver_iso']
            save(record)
            unattend_iso = unattend['volid']
        except Exception as exc:
            record.update(status='failed', error=str(exc))
            try:
                seed_state = crange_windows_seed.public_status(record['id'])
            except Exception:
                seed_state = {'state': 'journal_unavailable',
                              'reason': 'Private seed upload journal is unavailable; preserve capacity and reconcile this build.'}
            if seed_state and seed_state.get('state') in ('uploading', 'upload_uncertain', 'journal_unavailable'):
                record['private_answerfile'] = {key: seed_state.get(key) for key in
                    ('volid', 'node', 'vmid', 'iso_size', 'created_at', 'state', 'reason')}
                record['error'] = seed_state.get('reason') or 'Private answerfile upload outcome is uncertain; reconcile this build before retrying.'
                save(record)
                raise
            save(record)
            _release_installer_reservation(job_id)
            raise
    fields={'vmid':record['vmid'],'name':record['guest_name'],'pool':'crange-iac','tags':'crange-template-build;'+record['ownership_tag'],
            'cores':value['cores'],'memory':value['memory'],'cpu':value['cpu'],'bios':value['bios'],
            'machine':value['machine'],'ostype':{'linux':'l26','windows':'win11','bsd':'other','other':'other'}[value['os']],
            'scsihw':'virtio-scsi-single',value['disk_bus']+'0':f'local-lvm:{value["disk"]}',
            'ide2':value['iso']+',media=cdrom','boot':'order=ide2;'+value['disk_bus']+'0',
            'agent':int(value['agent']),'onboot':0}
    if value['driver_iso']:fields['ide1']=value['driver_iso']+',media=cdrom'
    if unattend_iso:fields['ide0']=unattend_iso+',media=cdrom'
    if value['bios']=='ovmf':fields['efidisk0']='local-lvm:1,efitype=4m,pre-enrolled-keys=0'
    if value['tpm']:fields['tpmstate0']='local-lvm:1,version=v2.0'
    for i,model in enumerate(value['nic_models']):fields[f'net{i}']=f'{model},bridge=craccess,firewall=1'
    try:
        api.task(node,api.request('POST',f'/nodes/{node}/qemu',fields),timeout=600)
        owned(api,record)
        api.start(node,record['vmid']);record['status']='installing';save(record)
    except Exception as exc:
        record['status']='failed';record['error']=str(exc);save(record);raise
    # Do not release the short-term reservation until fresh provider discovery
    # includes the exact VM commitment. A partial/uncertain create retains it.
    try:
        refreshed = scheduling.discover(api, policy)
        observed = refreshed.get('guests', {}).get(str(record['vmid']))
        tags = str(observed.get('tags', '')).split(';') if observed else []
        if (observed and observed.get('node_name') == node and observed.get('kind') == 'qemu' and
                observed.get('name') == record['guest_name'] and
                record['ownership_tag'] in tags and 'crange-template-build' in tags and
                observed.get('memory_mb') == value['memory'] and
                observed.get('cpu_cores') == value['cores']):
            _release_installer_reservation(job_id)
    except Exception:
        # Keep the ledger reservation for administrator reconciliation.
        pass
    print(f'Installer VM {record["vmid"]} created on {node}. Open its console, install the OS, prepare it for cloning, then shut it down. Build: {record["id"]}')
    return record


def _reconcile_legacy_seed(record, api):
    """Release a pre-journal failed upload only after read-only absence proof."""
    import crange_scheduling as scheduling
    from crange_placement import ReservationLedger
    import crange_windows_seed as seed
    build_id = identifier(record['id'])
    if (record.get('status') != 'failed' or record.get('reference') or
            record.get('ownership_tag') != 'crange-build-' + build_id or
            record.get('private_answerfile', {}).get('state') != 'journal_unavailable'):
        raise ValueError('This build is not a failed pre-journal seed upload.')
    started = datetime.fromisoformat(record['created'])
    if started.tzinfo is None or (datetime.now(timezone.utc) - started).total_seconds() < 180:
        raise ValueError('Upload may still be processing; preserve the reservation and retry later.')
    if seed._journal_path(build_id).exists() or seed._artifact_path(build_id).exists():
        raise ValueError('Private seed evidence exists; use the journal-backed recovery path.')
    ledger = ReservationLedger(scheduling.DATA / 'reservations.sqlite3')
    try:
        row = ledger.db.execute('SELECT project_id,allocations FROM jobs WHERE job_id=?',
                                (record.get('capacity_job_id'),)).fetchone()
        allocations = json.loads(row[1]) if row else []
        if (not row or row[0] != 0 or len(allocations) != 1 or
                allocations[0].get('build_id') != build_id or
                allocations[0].get('vmid') != record['vmid'] or
                allocations[0].get('operation') != 'template-create' or
                allocations[0].get('resolved', {}).get('node_name') != record['definition']['node']):
            raise ValueError('Installer reservation does not match this build; retain it.')
    finally:
        ledger.db.close()
    guests = api.request('GET', '/cluster/resources', {'type': 'vm'})
    if any(int(g['vmid']) == record['vmid'] for g in guests):
        raise ValueError('The reserved VMID exists; retain the build for ownership review.')
    nodes = api.request('GET', '/cluster/status')
    live = {n['name'] for n in nodes if n.get('type') == 'node' and str(n.get('online')) == '1'}
    if not set(seed.NODE_MANAGEMENT_IPS) <= live:
        raise ValueError('An upload endpoint is offline; seed absence cannot be confirmed.')
    volid = 'local:iso/crange-win-' + build_id + '.iso'
    for node in sorted(seed.NODE_MANAGEMENT_IPS):
        tasks = api.request('GET', f'/nodes/{node}/tasks', {'source': 'active'})
        if any(t.get('type') in ('imgcopy', 'upload', 'download') for t in tasks):
            raise ValueError('An upload task is active; preserve this build and retry later.')
        content = api.request('GET', f'/nodes/{node}/storage/local/content', {'content': 'iso'})
        if any(item.get('volid') == volid for item in content):
            raise ValueError('A seed ISO exists without its journal; retain it for administrator review.')
    proof = {'vm_absent': True, 'iso_absent_nodes': sorted(seed.NODE_MANAGEMENT_IPS),
             'uploads_terminal': True, 'checked_at': datetime.now(timezone.utc).isoformat()}
    record['legacy_seed_cleanup_proof'] = proof
    save(record)
    _release_installer_reservation(record['capacity_job_id'])
    record['private_answerfile']['state'] = 'cleaned'
    record['seed_cleanup_at'] = proof['checked_at']
    save(record)
    return {'build_id': build_id, 'status': 'seed-cleaned', 'cleanup_verified': True}


def cancel(build_id, api=None):
    """Delete only the unfinished installer VM owned by one build record."""
    from crange import ProxmoxAPI
    record=load(build_id);api=api or ProxmoxAPI()
    # An interrupted answerfile upload has no VM yet. Reconcile only its exact,
    # journal-bound artifact; never release capacity while provider state is uncertain.
    if record.get('status') in ('failed', 'cancelled') and record.get('definition', {}).get('windows_unattended'):
        import crange_windows_seed
        state = crange_windows_seed.public_status(build_id)
        if state is None and record.get('private_answerfile', {}).get('state') == 'journal_unavailable':
            return _reconcile_legacy_seed(record, api)
        if state and state.get('reconcile_required'):
            cleaned = crange_windows_seed.reconcile_upload(build_id, record, api)
            if record.get('capacity_job_id'):
                _release_installer_reservation(record['capacity_job_id'])
            record['private_answerfile'] = {key: cleaned.get(key) for key in ('build_id', 'vmid', 'node', 'volid', 'state')}
            record['private_answerfile']['state'] = 'cleaned'
            record['seed_cleanup_at'] = datetime.now(timezone.utc).isoformat()
            record['error'] = 'Private answerfile upload was reconciled and its exact ISO was verified absent.'
            save(record)
            crange_windows_seed.finalize_cleanup(build_id)
            return {'build_id': build_id, 'vmid': record['vmid'], 'status': 'seed-cleaned'}
    if record.get('status') not in ('creating','installing') or record.get('reference'):
        raise ValueError('Only an unpublished, unfinished installer build can be cancelled.')
    node=record['definition']['node'];vmid=record['vmid']
    actual=owned(api,record)
    if int(actual.get('template',0)):
        raise ValueError('A Proxmox template cannot be cancelled as an installer build.')
    status=api.status(node,vmid)
    if status not in ('running','stopped'):
        raise ValueError('Installer VM is in an unsafe state for cancellation.')
    if status=='running':
        api.task(node,api.request('POST',f'/nodes/{node}/qemu/{vmid}/status/stop',{}),timeout=600)
    if api.status(node,vmid)!='stopped':
        raise ValueError('Installer VM did not stop; its build record and storage were retained.')
    task=api.request('DELETE',f'/nodes/{node}/qemu/{vmid}',
                     {'purge':1,'destroy-unreferenced-disks':1})
    if isinstance(task,str) and task.startswith('UPID:'):
        api.task(node,task,timeout=900)
    if any(int(row.get('vmid',-1))==vmid for row in api.request('GET','/cluster/resources',{'type':'vm'})):
        raise ValueError('Installer VM still exists after deletion; retain the build for reconciliation.')
    record.update(status='cancelled',cancelled_at=datetime.now(timezone.utc).isoformat(),
                  cancellation='Unfinished installer build cancelled by administrator.')
    save(record)
    return {'build_id':build_id,'vmid':vmid,'status':'cancelled'}


def publish(build_id,projects,prepared,api=None):
    from crange import ProxmoxAPI, project_dir, templates_for
    if prepared is not True:raise ValueError('Confirm the guest is prepared for cloning before publishing.')
    if not isinstance(projects,list) or not projects:raise ValueError('Choose at least one project to approve this template for.')
    paths=[project_dir(str(integer(p,1,255,'Project'))) for p in projects]
    record=load(build_id);value=record['definition'];api=api or ProxmoxAPI()
    if record.get('status')=='failed':raise ValueError('Installer VM is not available for publishing.')
    actual=owned(api,record)
    if value.get('windows_unattended'):
        from crange_windows_base import _journal_path, _load
        receipt = _load(_journal_path(build_id))
        if receipt.get('state') != 'sealed':
            raise ValueError('Run the fixed Windows base preparation and sealing actions before publishing this unattended build.')
        if actual.get('ide0') not in (None, '', 'none,media=cdrom'):
            raise ValueError('The per-build private answerfile ISO must be detached before publishing the Windows base.')
    if api.status(value['node'],record['vmid'])!='stopped':raise ValueError('Shut down the installer VM before publishing.')
    key='custom-'+value['name']
    for path in paths:
        previous=templates_for(path).get(key)
        if previous and previous.get('template_id')!=record['vmid']:raise ValueError('A different approved template already uses this name. Choose a versioned name.')
    if not int(actual.get('template',0)):
        fields={'ide2':'none,media=cdrom','boot':'order='+value['disk_bus']+'0'}
        delete_media=[]
        if value['driver_iso']:delete_media.append('ide1')
        if delete_media:fields['delete']=','.join(delete_media)
        if value['cloud_init']:fields['ide0']='local-lvm:cloudinit'
        api.request('PUT',f'/nodes/{value["node"]}/qemu/{record["vmid"]}/config',fields)
        api.task(value['node'],api.request('POST',f'/nodes/{value["node"]}/qemu/{record["vmid"]}/template',{}),timeout=600)
    if not int(owned(api,record).get('template',0)):raise ValueError('Proxmox did not convert the installer to a template.')
    reference={'template_id':record['vmid'],'template_node':value['node'],'display_name':value['name'],
               'profile':{'bios':value['bios'],'machine':value['machine'],'scsi_hardware':'virtio-scsi-single',
                          'os_type':{'linux':'l26','windows':'win11','bsd':'other','other':'other'}[value['os']],
                          'agent_enabled':value['agent'],'upgrade_packages':False,'cloud_init':value['cloud_init'],
                          'cpu_type':value['cpu'],'boot_order':[value['disk_bus']+'0'],'network_models':value['nic_models']}}
    for path in paths:
        file=path/'approved-scenario-templates.json';approved=templates_for(path);approved[key]=reference
        temp=file.with_suffix('.tmp');temp.write_text(json.dumps(approved,indent=2)+'\n');temp.replace(file)
    record.update(status='published',reference=reference,approved_projects=projects);save(record)
    print('Published '+key+' to projects '+', '.join(map(str,projects))+'. Existing templates and releases were retained.')


def media_tf_overrides(path, cfg, prepared=None, api=None, *, scoped=False):
    """Bind archive paths before plan review; existing guests retain their source."""
    from crange import ProxmoxAPI
    from crange_scheduling import declarations, tf_overrides, applied_resources, freeze
    values=tf_overrides(prepared) if prepared else {'resolved_placements':{}}
    resolved=values['resolved_placements']
    records={p['resource_key']:p for p in (prepared or {}).get('placements',[])}
    api=api or ProxmoxAPI()
    physical = applied_resources(path)
    applied_file = path / 'crange-applied-placements.json'
    applied = json.loads(applied_file.read_text()) if applied_file.exists() else {}
    existing = physical if not prepared or scoped else {}
    for row in declarations(path,cfg):
        key=row['resource_key'];record=records.get(key,{})
        placement=resolved.get(key,{})
        if key in existing:
            if key not in records:
                prior = applied.get(key, {})
                if (prior.get('guest_id') != existing[key].get('vm_id')
                        or prior.get('node_name') != existing[key].get('node_name')):
                    prior = {}
                resolved[key]=freeze(row,existing[key],prior)['resolved']
                if prior.get('image_id') and prior.get('declared_template') == row['box'].get('template'):
                    resolved[key]['image_id'] = prior['image_id']
            continue
        if scoped and key not in records:
            continue
        if (key in physical and row.get('kind') == 'vm' and physical[key].get('boot_order')
                and applied.get(key, {}).get('image_id') == row.get('image')):
            # A resource resize retains this guest's existing boot devices.
            resolved[key] = {**placement, 'boot_order': list(physical[key]['boot_order'])}
        if key in physical and physical[key].get('machine'):
            resolved[key] = {**resolved.get(key, placement), 'machine': physical[key]['machine']}
        if key not in physical and not record.get('existing') and row.get('kind') == 'vm':
            from crange_scheduling import template_profile
            profile = template_profile(path, row)
            machine = str(profile.get('machine', '')).split(',')[0].removeprefix('type=')
            version = re.fullmatch(r'pc-(?:q35|i440fx)-([0-9]+)\.([0-9]+)(?:\+pve[0-9]+)?', machine)
            if version and tuple(map(int, version.groups())) > (9, 2):
                from crange_capture import _portable_capture_machine
                node = placement.get('node_name') or row['box'].get('node_name')
                if not node or node == 'auto':
                    raise ValueError('Resolve the guest node before selecting its portable machine baseline.')
                resolved[key] = {**placement, 'machine': _portable_capture_machine(api, node, profile)}
        if record.get('existing') or placement.get('template_id') is not None:
            continue
        reference=row['reference']
        archive=placement.get('template_file_id') or reference.get('template_file_id')
        if not archive and key.startswith('router:'):
            archive='crange-library:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst'
        if not archive or reference.get('template_id') is not None:
            continue
        node=placement.get('node_name') or row['box'].get('node_name') or cfg.get('router_node','proxmox4')
        if node=='auto':
            raise ValueError('Automatic archive placement must be resolved before media selection.')
        chosen=resolve_archive(api,node,archive)
        if prepared and placement.get('template_file_id') and chosen!=archive:
            # Saved reservations name exact archive sources; a new review must
            # select a verified catalog mirror instead of changing behind it.
            raise ValueError('Reviewed archive source is unavailable; restage using a verified library mirror.')
        resolved[key]={**placement,'template_file_id':chosen}
    return values
