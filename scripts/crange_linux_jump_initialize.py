"""Fixed worker bootstrap for approved, applied Linux jump boxes.

Cloud-init must finish and its MAC-bound static connection must be active before
Guacamole publication. No caller-supplied guest commands or addresses are accepted.
"""
import ipaddress
import json
import re
import time


def targets(path, keys, *, cfg=None, outputs=None, applied=None, catalog=None, api=None):
    from crange import json_config, output_json, ProxmoxAPI
    from crange_scheduling import read_settings
    project = int(path.name.split('-')[-1])
    cfg = json_config(path) if cfg is None else cfg
    outputs = output_json(path) if outputs is None else outputs
    applied = json.loads((path/'crange-applied-placements.json').read_text()) if applied is None else applied
    catalog = read_settings()[1] if catalog is None else catalog
    api = ProxmoxAPI() if api is None else api
    resources = api.request('GET', '/cluster/resources?type=vm')
    result = []
    for key in sorted(set(keys)):
        box = cfg.get('jump_boxes', {}).get(key)
        live = outputs.get('jump_boxes', {}).get('value', {}).get(key)
        receipt = applied.get('jumpbox:'+key, {})
        if not box or not live:
            raise ValueError('Linux jump box is not declared and applied.')
        team = int(box['team_id']); network_key = box.get('network_key')
        if team != 254 and str(team) not in {str(v) for v in cfg.get('team_ids', [])}:
            raise ValueError('Linux jump box team is not declared.')
        network = cfg.get('custom_networks', {}).get(network_key) if network_key else None
        if network_key and (not network or int(network['owner_team_id']) != team):
            raise ValueError('Linux jump box custom network is not owned by its team.')
        number = int(network['network_id']) if network else team
        vnet = f'p{project:03d}c{number:03d}' if network else f'p{project:03d}t{number:03d}'
        host = 10 + int(box['jump_number'])
        if not 1 <= number <= 254 or not 11 <= host <= 100:
            raise ValueError('Linux jump box network is invalid.')
        address = str(ipaddress.IPv4Address(f'10.{project}.{number}.{host}'))
        image = catalog.get('images', {}).get(box.get('image'), {})
        placement = live.get('placement', {})
        if (image.get('verified') is not True or image.get('kind') != 'vm'
                or image.get('init_system') != 'systemd'
                or not any(v in image.get('approved_projects', []) for v in ('*', project))
                or placement.get('image_id') != box.get('image')
                or not any(r.get('state') == 'ready' and r.get('node') == placement.get('template_node')
                           and r.get('template_id') == placement.get('template_id')
                           and r.get('artifact_sha256') == image.get('artifact_sha256')
                           for r in image.get('replicas', []))):
            raise ValueError('Linux jump box does not use an approved verified image replica.')
        node = placement.get('node_name'); vid = live.get('id')
        if (type(vid) is not int or not re.fullmatch(r'proxmox[1-6]', str(node))
                or receipt.get('guest_id') != vid or receipt.get('node_name') != node
                or any(receipt.get(k) != placement.get(k) for k in ('image_id','template_id','template_node'))
                or live.get('address') != address or live.get('network_vnet') != vnet):
            raise ValueError('Linux jump box applied generation or address differs from desired state.')
        name = f'{vnet}-jump-{int(box["jump_number"]):02d}'
        rows = [r for r in resources if str(r.get('vmid')) == str(vid)]
        if (len(rows) != 1 or any(rows[0].get(k) != v for k,v in
                {'type':'qemu','pool':'crange-iac','node':node,'name':name,'status':'running'}.items())):
            raise ValueError('Linux jump box native ownership differs from applied state.')
        native = api.request('GET', f'/nodes/{node}/qemu/{vid}/config')
        nics = [k for k in native if re.fullmatch(r'net[0-9]+', k)]
        nic = dict(p.split('=',1) for p in str(native.get('net0','')).split(',') if '=' in p)
        mac = nic.get('virtio','')
        gateway = f'10.{project}.{number}.1'
        ipconfig = dict(p.split('=',1) for p in str(native.get('ipconfig0','')).split(',') if '=' in p)
        if (native.get('name') != name or native.get('ostype') != 'l26' or native.get('template',0)
                or not {'jumpbox','terraform',f'p{project:03d}',vnet} <= set(native.get('tags','').split(';'))
                or nics != ['net0'] or nic.get('bridge') != vnet
                or not re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}', mac)
                or ipconfig != {'ip':address+'/24','gw':gateway}):
            raise ValueError('Linux jump box native identity or cloud-init network differs.')
        disk = box.get('disk_gb')
        if disk is not None:
            from crange_proxmox_disk import managed_volume, size_bytes
            if type(disk) is not int or not 8 <= disk <= 1024:
                raise ValueError('Linux jump-box disk declaration is invalid.')
            try:
                volume = managed_volume(native.get('scsi0',''))
                capacity = size_bytes(native.get('scsi0',''))
            except ValueError as exc:
                raise ValueError('Linux jump-box native system disk cannot be verified.') from exc
            if not re.fullmatch(r'[A-Za-z0-9_.-]+:vm-' + str(vid) + r'-disk-[0-9]+(?:\.raw|\.qcow2)?', volume) or capacity != disk*2**30:
                raise ValueError('Linux jump-box native system disk differs from its declared capacity.')
        result.append({'key':key,'node':node,'guest_id':vid,'mac':mac,
                       'address':address,'gateway':gateway,**({'disk_gb':disk} if disk is not None else {})})
    return result


def initialize_selected(path, cfg, resource_keys, team):
    """Finish selected running Linux boxes before SSH identity/configuration."""
    keys=[]
    for resource in resource_keys:
        if not resource.startswith('jumpbox:'):
            continue
        key=resource.split(':',1)[1]
        box=cfg.get('jump_boxes',{}).get(key)
        if box is None:  # A reviewed deletion has no guest to initialize.
            continue
        if int(box['team_id']) != team:
            raise ValueError('Linux initialization scope belongs to another team.')
        if box.get('started',True):
            keys.append(key)
    return initialize(path,keys) if keys else []


def initialize_inventory(path, hosts):
    """Resolve every inventory address to one declaration before any writes."""
    from crange import json_config, output_json
    cfg=json_config(path);outputs=output_json(path).get('jump_boxes',{}).get('value',{})
    keys=[]
    for values in hosts.values():
        address=values.get('ansible_host')
        matching=[key for key,guest in outputs.items() if address and guest.get('address')==address]
        if len(matching)!=1 or matching[0] not in cfg.get('jump_boxes',{}):
            raise ValueError('Linux inventory host has no unique declared applied jump box.')
        key=matching[0]
        if cfg['jump_boxes'][key].get('started',True):
            keys.append(key)
    return initialize(path,keys) if keys else []


def guest_script():
    return r'''import json, subprocess, sys, time
from pathlib import Path
p=json.load(sys.stdin)
def run(args, **kw):
    return subprocess.run(args,check=True,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=180,**kw).stdout
def ensure_disk_space():
    import os
    rootfs=os.statvfs('/')
    free=rootfs.f_bavail*rootfs.f_frsize
    if free < 16*2**30:
        # The hypervisor can enlarge an already booted clone after its first
        # cloud-init run. Repeat only the two fixed disk expansion modules.
        run(['cloud-init','single','--name','growpart','--frequency','always'])
        run(['cloud-init','single','--name','resizefs','--frequency','always'])
        rootfs=os.statvfs('/')
        free=rootfs.f_bavail*rootfs.f_frsize
    if free < 16*2**30:
        raise RuntimeError('Linux jump-box disk must provide at least 16 GiB usable free space after cloud-init resize')
    return round(free/2**30,2)
mac=p['mac'].lower()
devices=[x.name for x in Path('/sys/class/net').iterdir() if (x/'address').read_text().strip().lower()==mac]
if len(devices)!=1: raise RuntimeError('Declared adapter identity mismatch')
dev=devices[0]
# Some approved desktop images detect NoCloud but do not pull its target into
# the boot transaction. Persist that dependency, then finish a missed first boot.
run(['systemctl','add-wants','multi-user.target','cloud-init.target'])
run(['systemctl','start','cloud-init.target'])
status=subprocess.run(['cloud-init','status','--format','json'],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
if status.returncode not in (0,2): raise RuntimeError('Cloud-init status failed')
state=json.loads(status.stdout)
if state.get('status')!='done' or state.get('errors'): raise RuntimeError('Cloud-init did not finish successfully')
disk_free_gib=ensure_disk_space() if p.get('disk_gb') is not None else None

run(['nmcli','connection','reload'])
profiles=[]
for path in Path('/etc/NetworkManager/system-connections').glob('*.nmconnection'):
    import configparser
    c=configparser.ConfigParser(interpolation=None);c.read(path)
    if (c.get('user','org.freedesktop.NetworkManager.origin',fallback='')=='cloud-init'
        and c.get('ethernet','mac-address',fallback='').lower()==mac
        and c.get('ipv4','method',fallback='')=='manual'
        and c.get('ipv4','address1',fallback='').split(',')[0]==p['address']+'/24'
        and c.get('ipv4','gateway',fallback='')==p['gateway']):
        profiles.append(c.get('connection','uuid'))
if len(profiles)!=1: raise RuntimeError('No unique cloud-init static profile for declared adapter')
addresses=json.loads(run(['ip','-j','-4','addr','show','dev',dev]))
current=[a['local'] for x in addresses for a in x.get('addr_info',[]) if a.get('scope')=='global']
active=run(['nmcli','-g','GENERAL.CON-UUID','device','show',dev]).strip()
changed=active!=profiles[0] or current!=[p['address']]
if changed: run(['nmcli','connection','up','uuid',profiles[0],'ifname',dev])
addresses=json.loads(run(['ip','-j','-4','addr','show','dev',dev]))
current=[a['local'] for x in addresses for a in x.get('addr_info',[]) if a.get('scope')=='global']
routes=json.loads(run(['ip','-j','-4','route','show','default','dev',dev]))
if current!=[p['address']] or not any(r.get('gateway')==p['gateway'] for r in routes):
    raise RuntimeError('Active Linux network differs from declared static network')
print(json.dumps({'ready':True,'address':p['address'],'cloud_init':'done','static_profile_active':True,'network_changed':changed,**({'disk_free_gib':disk_free_gib} if disk_free_gib is not None else {})}))
'''


def initialize(path, keys, *, api=None, timeout=300):
    from crange import ProxmoxAPI
    api = api or ProxmoxAPI()
    selected = targets(path, keys, api=api)  # Prevalidate the entire scope before any guest writes.
    results = []
    for target in selected:
        root = f'/nodes/{target["node"]}/qemu/{target["guest_id"]}/agent'
        deadline = time.monotonic()+min(timeout,180)
        while True:
            try:
                info = api.request('GET', root+'/get-osinfo')
                break
            except Exception as exc:
                if getattr(exc.__cause__, 'code', None) == 403 or time.monotonic() >= deadline:
                    raise ValueError('Linux guest agent is not available for approved initialization.') from None
                time.sleep(2)
        info = info.get('result',info)
        if str(info.get('kernel-release','')) == '' or info.get('id') not in ('parrot', 'kali', 'debian', 'ubuntu'):
            raise ValueError('Approved Linux jump box reported a different OS.')
        pid = api.request('POST',root+'/exec',{'command':['/usr/bin/python3','-c',guest_script()],
                    'input-data':json.dumps(target)})['pid']
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            result = api.request('GET',root+'/exec-status',{'pid':pid})
            if result.get('exited'):
                if result.get('exitcode') != 0:
                    raise ValueError('Linux jump-box initialization failed for '+target['key']+': '+result.get('err-data','')[-1500:])
                receipt=json.loads(result.get('out-data',''))
                if receipt.get('ready') is not True or receipt.get('address') != target['address']:
                    raise ValueError('Linux jump-box initialization returned an invalid receipt.')
                results.append({'key':target['key'],'guest_id':target['guest_id'],**receipt})
                print('Linux jump box '+target['key']+' has completed cloud-init and its static network.',flush=True)
                break
            time.sleep(2)
        else:
            raise ValueError('Linux jump-box initialization timed out; inspect its existing agent task before retrying.')
    return results
