"""Admin-owned ISO installer VMs and immutable, project-approved templates."""
from __future__ import annotations
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDS = ROOT / 'templates/builds'
NODES = {f'proxmox{i}' for i in range(1, 7)}
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
        if not isinstance(value[key],str) or not re.fullmatch(r'local:iso/[a-zA-Z0-9][a-zA-Z0-9_.-]{0,159}\.iso', value[key], re.IGNORECASE):
            raise ValueError('Choose an ISO on the selected node’s local storage.')
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
    for node in sorted(NODES):
        try:
            media=api.request('GET',f'/nodes/{node}/storage/local/content',{'content':'iso'})
            nodes.append({'name':node,'isos':[m['volid'] for m in media if m.get('volid','').lower().endswith('.iso') and int(m.get('size',0))>0]})
        except Exception:
            nodes.append({'name':node,'isos':[],'unavailable':True})
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
    return {'nodes':nodes,'builds':builds}


def create(definition,api=None):
    from crange import ProxmoxAPI
    value=validate(definition);api=api or ProxmoxAPI();node=value['node']
    inventory=api.request('GET',f'/nodes/{node}/storage/local/content',{'content':'iso'})
    media={m['volid'] for m in inventory if int(m.get('size',0))>0}
    if any(iso and iso not in media for iso in (value['iso'],value['driver_iso'])):
        raise ValueError('Selected installation media is not on this node.')
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
