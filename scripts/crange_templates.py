"""Admin-owned ISO installer VMs and immutable, project-approved templates."""
from __future__ import annotations
import json
import re
import uuid
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDS = ROOT / 'templates/builds'
NODES = {f'proxmox{i}' for i in range(1, 7)}
MEDIA_STORAGES = ('crange-library', 'media-mirror1', 'media-mirror4', 'local')
LIBRARY_STATUS = Path('/var/lib/crange-media-library/status.json')


def library_status():
    try:
        value = json.loads(LIBRARY_STATUS.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def media_inventory(api, node, content='iso'):
    result = []
    for storage in MEDIA_STORAGES:
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
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('Unexpected or missing template settings.')
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
    if value['tpm'] and value['bios'] != 'ovmf':raise ValueError('TPM requires UEFI firmware.')
    return value


def save(record):
    BUILDS.mkdir(parents=True,exist_ok=True)
    target=BUILDS / (identifier(record['id']) + '.json')
    temp=target.with_suffix('.tmp');temp.write_text(json.dumps(record,indent=2)+'\n');temp.chmod(0o644);temp.replace(target)


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
    def collect(node):
        if online and node not in online:
            return node, [], []
        return node, media_inventory(api,node), media_inventory(api,node,'vztmpl')
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
        record.update(console_available=False,console_node=None,error=None)
        if record.get('status') in ('failed','published'):
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
        builds.append(record)
    try:
        from crange_scheduling import read_settings
        catalog = read_settings()[1]
        logical_images = []
        for image_id, image in sorted(catalog['images'].items()):
            replicas = [replica for replica in image.get('replicas', []) if replica.get('state') == 'ready']
            logical_images.append({'image_id': image_id, 'kind': image['kind'],
                                   'approved_projects': image['approved_projects'],
                                   'ready_nodes': sorted({replica['node'] for replica in replicas}),
                                   'source_type': image.get('source_type', 'published_template')})
    except Exception:
        logical_images = None
    return {'nodes':nodes,'builds':builds,'media':list(archives.values()),'library':library_status(),
            'logical_images':logical_images,
            'library_storage':'crange-library','mirror_storages':['media-mirror1','media-mirror4']}


def project_image_readiness(project_id):
    """Return only this project's approved-template to logical-image readiness."""
    from crange import project_dir, templates_for, container_templates_for
    from crange_scheduling import read_settings
    path = project_dir(str(project_id))
    catalog = read_settings()[1]
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
                                  'ready_nodes': ready_nodes}
    catalog_images = []
    for image_id, image in sorted(catalog['images'].items()):
        scope = image.get('approved_projects', [])
        ready_nodes = sorted({replica['node'] for replica in image.get('replicas', [])
                              if replica.get('state') == 'ready'})
        if ready_nodes and ('*' in scope or int(project_id) in scope):
            catalog_images.append({'image_id': image_id, 'kind': image['kind'],
                                   'ready_nodes': ready_nodes})
    result['catalog_images'] = catalog_images
    return result


def approve_catalog_image(image_id, projects, api=None):
    """Link a verified, ready VM image into selected projects under a new alias.

    This does not publish evidence or mutate the image catalog. It only grants
    project-local template selection for an already globally approved image.
    """
    from crange import ProxmoxAPI, project_dir, templates_for
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

    catalog = read_settings()[1]
    image = catalog.get('images', {}).get(image_id)
    if not image or image.get('kind') != 'vm' or image.get('verified') is not True:
        raise ValueError('Choose a verified VM image from the protected catalog.')
    if '*' not in image.get('approved_projects', []):
        raise ValueError('This action only links images already approved for all projects.')
    digest = image.get('artifact_sha256')
    identity = image.get('identity_capture') or {}
    source_identity = identity.get('source_identity') or {}
    if (not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or
            identity.get('state') != 'identity-captured' or identity.get('kind') != 'vm' or
            not re.fullmatch(r'[0-9a-f]{64}', str(identity.get('source_config_sha256', ''))) or
            not source_identity.get('machine_id') or
            not source_identity.get('ssh_host_key')):
        raise ValueError('Image provenance or captured guest identity is incomplete.')

    ready = []
    for replica in image.get('replicas', []):
        evidence = (replica.get('verification') or {}).get('evidence') or {}
        replica_identity = (evidence.get('machine_id'), evidence.get('ssh_host_key'))
        if (replica.get('state') == 'ready' and replica.get('artifact_sha256') == digest and
                replica.get('cache_local') is True and 'linked' in replica.get('clone_modes_verified', []) and
                (replica.get('verification') or {}).get('passed') is True and
                replica_identity[0] and replica_identity[1] and
                replica_identity[0] != source_identity['machine_id'] and
                replica_identity[1] != source_identity['ssh_host_key'] and replica.get('template_id')):
            ready.append(replica)
    if not ready:
        raise ValueError('Image has no verified local linked-clone replica.')
    # Prefer the verified P2 replica called out in the publication record, then
    # use the lowest numbered matching node. Never fall back to the source VM.
    ready.sort(key=lambda row: (row['node'] != 'proxmox2', row['node'], row['template_id']))
    replica = ready[0]
    if api is None:
        api = ProxmoxAPI()
    config = api.request('GET', f"/nodes/{replica['node']}/qemu/{int(replica['template_id'])}/config")
    tags = str(config.get('tags', '')).split(';') if isinstance(config, dict) else []
    if (not isinstance(config, dict) or str(config.get('template', '0')) not in ('1', 'true', 'True') or
            'crange-image' not in tags or f"crange-cache-{int(replica['template_id'])}" not in tags):
        raise ValueError('The catalog replica is not an owned Proxmox image template.')

    alias = image_id.removesuffix('-20261005')
    alias = re.sub(r'[^a-z0-9-]+', '-', alias.lower()).strip('-')
    if not alias or not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', alias):
        raise ValueError('Logical image ID cannot form a safe template alias.')
    profile = {}
    for source_key, profile_key in (('bios', 'bios'), ('machine', 'machine'),
                                    ('scsihw', 'scsi_hardware'), ('ostype', 'os_type'),
                                    ('cpu', 'cpu_type')):
        if isinstance(config.get(source_key), str) and config[source_key]:
            profile[profile_key] = config[source_key]
    agent = config.get('agent')
    if isinstance(agent, (str, int)):
        profile['agent_enabled'] = str(agent) == '1' or str(agent).startswith('enabled=1')
    profile['cloud_init'] = any(str(config.get(key, '')).endswith('cloudinit') for key in ('ide0', 'sata0', 'scsi0'))
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
                 'display_name': image.get('display_name') or 'Alpine 3.24 portable',
                 'image_id': image_id, 'image_artifact_sha256': digest,
                 'source_config_sha256': identity['source_config_sha256'],
                 'replica_datastore': replica.get('datastore_id'),
                 'replica_clone_modes': ['linked'], 'profile': profile}
    paths = [project_dir(str(project)) for project in normalized]
    for path in paths:
        previous = templates_for(path).get(alias)
        if previous and (previous.get('image_id') != image_id or
                         previous.get('template_id') != reference['template_id'] or
                         previous.get('template_node') != reference['template_node']):
            raise ValueError(f'Template alias {alias!r} already refers to a different image in one selected project.')
    for path in paths:
        file = path / 'approved-scenario-templates.json'
        approved = templates_for(path)
        approved[alias] = reference
        temp = file.with_suffix('.tmp')
        temp.write_text(json.dumps(approved, indent=2) + '\n')
        temp.chmod(0o640)
        temp.replace(file)
    print(f'Linked verified image {image_id} as {alias} for projects {", ".join(map(str, normalized))}.')
    return {'alias': alias, 'image_id': image_id, 'template_id': reference['template_id'],
            'template_node': reference['template_node'], 'projects': normalized}


def create(definition,api=None):
    from crange import ProxmoxAPI
    value=validate(definition);api=api or ProxmoxAPI();node=value['node']
    value=dict(value)
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
    save(record)
    fields={'vmid':record['vmid'],'name':record['guest_name'],'pool':'crange-iac','tags':'crange-template-build;'+record['ownership_tag'],
            'cores':value['cores'],'memory':value['memory'],'cpu':value['cpu'],'bios':value['bios'],
            'machine':value['machine'],'ostype':{'linux':'l26','windows':'win11','bsd':'other','other':'other'}[value['os']],
            'scsihw':'virtio-scsi-single',value['disk_bus']+'0':f'local-lvm:{value["disk"]}',
            'ide2':value['iso']+',media=cdrom','boot':'order=ide2;'+value['disk_bus']+'0',
            'agent':int(value['agent']),'onboot':0}
    if value['driver_iso']:fields['ide1']=value['driver_iso']+',media=cdrom'
    if value['bios']=='ovmf':fields['efidisk0']='local-lvm:1,efitype=4m,pre-enrolled-keys=0'
    if value['tpm']:fields['tpmstate0']='local-lvm:1,version=v2.0'
    for i,model in enumerate(value['nic_models']):fields[f'net{i}']=f'{model},bridge=craccess,firewall=1'
    try:
        api.task(node,api.request('POST',f'/nodes/{node}/qemu',fields),timeout=600)
        owned(api,record)
        api.start(node,record['vmid']);record['status']='installing';save(record)
    except Exception as exc:
        record['status']='failed';record['error']=str(exc);save(record);raise
    print(f'Installer VM {record["vmid"]} created on {node}. Open its console, install the OS, prepare it for cloning, then shut it down. Build: {record["id"]}')
    return record


def publish(build_id,projects,prepared,api=None):
    from crange import ProxmoxAPI, project_dir, templates_for
    if prepared is not True:raise ValueError('Confirm the guest is prepared for cloning before publishing.')
    if not isinstance(projects,list) or not projects:raise ValueError('Choose at least one project to approve this template for.')
    paths=[project_dir(str(integer(p,1,255,'Project'))) for p in projects]
    record=load(build_id);value=record['definition'];api=api or ProxmoxAPI()
    if record.get('status')=='failed':raise ValueError('Installer VM is not available for publishing.')
    actual=owned(api,record)
    if api.status(value['node'],record['vmid'])!='stopped':raise ValueError('Shut down the installer VM before publishing.')
    key='custom-'+value['name']
    for path in paths:
        previous=templates_for(path).get(key)
        if previous and previous.get('template_id')!=record['vmid']:raise ValueError('A different approved template already uses this name. Choose a versioned name.')
    if not int(actual.get('template',0)):
        fields={'ide2':'none,media=cdrom','boot':'order='+value['disk_bus']+'0'}
        if value['driver_iso']:fields['delete']='ide1'
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


def media_tf_overrides(path, cfg, prepared=None, api=None):
    """Bind archive paths before plan review; existing guests retain their source."""
    from crange import ProxmoxAPI
    from crange_scheduling import declarations, tf_overrides, applied_resources, freeze
    values=tf_overrides(prepared) if prepared else {'resolved_placements':{}}
    resolved=values['resolved_placements']
    records={p['resource_key']:p for p in (prepared or {}).get('placements',[])}
    api=api or ProxmoxAPI()
    existing=applied_resources(path) if not prepared else {}
    for row in declarations(path,cfg):
        key=row['resource_key'];record=records.get(key,{})
        placement=resolved.get(key,{})
        if key in existing:
            resolved[key]=freeze(row,existing[key])['resolved']
            continue
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
