#!/usr/bin/env python3
"""Operator-only migration of one existing router to the core HA store.

Review and apply run on P4. An offline source is never recovered by this tool.
Native HA owns fencing and recovery after enrollment; returning hosts do not
trigger failback. Interrupted writes retain their journal for operator review.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import time
import uuid
from contextlib import contextmanager

STORE = 'crange-router-ha'
ROOT = Path('/root/crange-resilience/router-ha-enrollment')
NODES = {f'proxmox{i}' for i in range(2, 7)}


def run(args, *, timeout=40):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout


def api(path, method='get', **params):
    args = ['pvesh', method, path, '--output-format', 'json']
    for key, value in params.items(): args.extend(['--' + key, str(value)])
    result = run(args)
    # Successful void API mutations (including HA enrollment) produce no
    # JSON through pvesh. Reads must still return an actual JSON response.
    if not result.strip() and method in ('create', 'set', 'delete'):
        return None
    return json.loads(result)


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def save(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2) + '\n'); tmp.chmod(0o600); tmp.replace(path)


def guard(project, key):
    if os.geteuid() != 0 or socket.gethostname() != 'proxmox4':
        raise ValueError('Run as root only on the surviving core, Proxmox4.')
    if (type(project) is not int or not 1 <= project <= 255 or project in (60, 61) or
            not re.fullmatch(r'router:(?:project|staging|team:(?:[1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-3]))', key)):
        raise ValueError('Select one exact project/staging/team router.')
    rows = api('/cluster/status')
    if not any(r.get('type') == 'cluster' and r.get('quorate') == 1 for r in rows):
        raise ValueError('Native cluster quorum is required.')


def controller(code, *, root=False, timeout=100):
    args = ['pct', 'exec', '114', '--']
    if not root: args += ['runuser', '-u', 'crange', '--', 'env', 'HOME=/home/crange']
    return json.loads(run(args + ['python3', '-c', code], timeout=timeout))


def snapshot(project):
    return controller("import json;from pathlib import Path;p=Path('/home/crange/crange/terraform/environments/project-%03d');print(p.joinpath('terraform.tfstate').read_text())" % project)


def router(snapshot, key):
    direct = {'router:project':'module.project_router', 'router:staging':'module.staging_router'}
    module = direct.get(key) or 'module.team_router[' + json.dumps(key.removeprefix('router:team:')) + ']'
    rows = [r for r in snapshot.get('resources', []) if r.get('module') == module and
            r.get('type') == 'proxmox_virtual_environment_container' and r.get('name') == 'router']
    if len(rows) != 1 or len(rows[0].get('instances', [])) != 1:
        raise ValueError('Router has no unique Terraform container identity.')
    instance = rows[0]['instances'][0]; value = instance['attributes']
    if (instance.get('deposed') or instance.get('status') == 'tainted' or value.get('clone') or
            type(value.get('vm_id')) is not int or str(value['vm_id']) != str(value.get('id')) or
            len(value.get('disk', [])) != 1 or not snapshot.get('lineage')):
        raise ValueError('Only intact stock-archive router state is accepted.')
    return module + '.proxmox_virtual_environment_container.router', value


def config_identity(config):
    return {k:v for k,v in config.items() if k in ('hostname','rootfs','unprivileged','tags') or re.fullmatch(r'net\d+', k)}


def inspect_router(project, key):
    state = snapshot(project); address, value = router(state, key); vmid = value['vm_id']; node = value['node_name']
    online = {r['node'] for r in api('/nodes') if r.get('status') == 'online'}
    if node not in online or node not in NODES:
        raise ValueError('Router source is offline or unverified; never start a second copy.')
    matches = [r for r in api('/cluster/resources', type='vm') if r.get('vmid') == vmid]
    if (len(matches) != 1 or matches[0].get('type') != 'lxc' or matches[0].get('node') != node or
            matches[0].get('pool') != 'crange-iac' or matches[0].get('status') != 'running'):
        raise ValueError('Router must be uniquely owned and running on its recorded source.')
    config = api(f'/nodes/{node}/lxc/{vmid}/config')
    tags = set(config.get('tags', '').split(';'))
    expected = value.get('initialization', [{}])[0].get('hostname')
    if (not {f'p{project:03d}','network-infrastructure','range-lan','terraform'} <= tags or
            config.get('hostname') != expected or not config.get('unprivileged') or config.get('lock') or
            config.get('template') or any(re.fullmatch(r'(?:mp|unused)\d+', k) for k in config)):
        raise ValueError('Router ownership or filesystem configuration differs.')
    if any(r.get('sid') == f'ct:{vmid}' for r in api('/cluster/ha/resources')):
        raise ValueError('Router is already HA managed; reconcile its enrollment instead.')
    bridges = []
    for field in ('net0','net1'):
        match = re.search(r'(?:^|,)bridge=([^,]+)', config.get(field, ''))
        if not match or not (match[1] == 'crtrans' or re.fullmatch(fr'p{project:03d}(?:tr|t\d{{3}})', match[1])):
            raise ValueError('Router network is outside its project.')
        bridges.append(match[1])
    return state, address, value, config, sorted(online & NODES), sorted(set(bridges))


def verify_clients(nodes, bridges):
    code = """import json,subprocess
mount=json.loads(subprocess.check_output(['findmnt','--json','--target','/mnt/pve/crange-router-ha','-o','TARGET,SOURCE,FSTYPE'],text=True))['filesystems'][0]
if mount['target']!='/mnt/pve/crange-router-ha' or mount['fstype'] not in ('nfs','nfs4') or mount['source']!='10.0.0.104:/srv/crange-router-ha':raise SystemExit('Shared router mount differs.')
for bridge in BRIDGES:
 value=json.loads(subprocess.check_output(['ip','-json','link','show','dev',bridge],text=True))[0]
 if 'UP' not in value['flags'] or value['mtu']<1450:raise SystemExit('Router bridge is not ready.')
print(json.dumps({'shared_mount':True,'bridges':BRIDGES}))
""".replace('BRIDGES', repr(bridges))
    for node in nodes:
        command = ['python3','-c',code]
        if node != 'proxmox4':
            command = ['ssh','-oBatchMode=yes','-oConnectTimeout=5','-oStrictHostKeyChecking=yes',
                       '-oHostKeyAlias='+node,'-oUserKnownHostsFile=/etc/pve/nodes/'+node+'/ssh_known_hosts',
                       'root@10.0.0.'+str(100+int(node[-1])),shlex.join(command)]
        json.loads(run(command, timeout=25))


def review(project, key):
    guard(project, key)
    state, address, value, config, nodes, bridges = inspect_router(project, key)
    verify_clients(nodes, bridges)
    disk_gib = value['disk'][0]['size']
    stats = os.statvfs('/srv/crange-router-ha')
    free = stats.f_bavail * stats.f_frsize / 2**30
    total = stats.f_blocks * stats.f_frsize / 2**30
    volumes = api('/nodes/proxmox4/storage/' + STORE + '/content')
    committed = sum(v.get('size', 0) for v in volumes if v.get('content') in ('images','rootdir')) / 2**30
    growth = 0 if config['rootfs'].startswith(STORE + ':') else disk_gib
    reserve = max(20, total * .2)
    if not isinstance(disk_gib, (int,float)) or disk_gib <= 0 or committed + growth > total - reserve or growth > free - reserve:
        raise ValueError('Core shared-store commitments or free-space reserve would be exceeded.')
    plan = {'schema_version':1,'project_id':project,'router_key':key,'plan_id':uuid.uuid4().hex,
            'created':time.time(),'state_sha256':sha(state),'state_lineage':state['lineage'],
            'address':address,'vmid':value['vm_id'],'node':value['node_name'],
            'config':config_identity(config),'config_sha256':sha(config_identity(config)),
            'eligible_nodes':nodes,'bridges':bridges,'disk_gib':disk_gib,
            'storage_commitments_gib':committed,'storage_free_gib':round(free,2),
            'actions':(['move-rootfs'] if growth else []) + ['refresh-router-state','enroll-native-ha'],
            'failback':False,'phase':'reviewed'}
    ROOT.mkdir(parents=True, mode=0o700, exist_ok=True); save(ROOT / (plan['plan_id']+'.json'), plan)
    return {k:v for k,v in plan.items() if k not in ('config',)}


def task(plan, file, name, endpoint, **params):
    submitted=time.time()
    plan.update(phase='submitting-'+name,submitted_at=submitted);save(file, plan)
    upid = api(endpoint, 'create', **params)
    if not isinstance(upid,str) or not upid.startswith('UPID:'+plan['node']+':'):
        # A proxied CLI response need not expose the source node's task ID.
        # Confirm that exact terminal task; never resubmit the mutation.
        kind={'stop':'vzstop','start':'vzstart','move-rootfs':'move_volume'}[name]
        rows=api('/nodes/'+plan['node']+'/tasks',vmid=plan['vmid'],limit=20)
        matches=[r for r in rows if r.get('type')==kind and str(r.get('id'))==str(plan['vmid'])
                 and r.get('starttime',0)>=int(submitted)-1 and r.get('status')=='OK']
        if len(matches)!=1:raise ValueError('Proxied router task result is uncertain; preserve its journal.')
        upid=matches[0]['upid']
    if not isinstance(upid,str) or not upid.startswith('UPID:'+plan['node']+':'):
        raise ValueError('Native task submission is uncertain; preserve its journal.')
    plan.update(phase='waiting-'+name,upid=upid);save(file,plan)
    for _ in range(120):
        result=api(f'/nodes/{plan["node"]}/tasks/{upid}/status')
        if result.get('status')=='stopped':
            if result.get('exitstatus')!='OK':raise ValueError('Native router task failed; preserve its journal.')
            return
        time.sleep(1)
    raise ValueError('Native router task is still pending; do not replay it.')


@contextmanager
def pause_controller():
    """Close job submission before serializing this one-time operator action."""
    services=['crange.service','crange-guac-broker.service']
    stopped=[]
    try:
        for service in services:
            controller('import json,subprocess;subprocess.run('+repr(['systemctl','stop',service])+",check=True);print('{}')",root=True)
            stopped.append(service)
        active=controller("import json,sqlite3;d=sqlite3.connect('/var/lib/crange-jobs/jobs.sqlite3');print(json.dumps(d.execute(\"SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running','reconcile-required')\").fetchone()[0]))",root=True)
        if active:raise ValueError('Controller jobs are active; defer router conversion.')
        for service in ('crange-worker.service','crange-network-worker.service'):
            controller('import json,subprocess;subprocess.run('+repr(['systemctl','stop',service])+",check=True);print('{}')",root=True)
            stopped.append(service)
        yield
    finally:
        for service in reversed(stopped):
            controller('import json,subprocess;subprocess.run('+repr(['systemctl','start',service])+",check=True);print('{}')",root=True)


def apply_reviewed(project, key, plan_id):
    guard(project,key)
    if not re.fullmatch(r'[a-f0-9]{32}',plan_id):raise ValueError('Invalid reviewed plan ID.')
    file=ROOT/(plan_id+'.json');plan=json.loads(file.read_text())
    if plan.get('project_id')!=project or plan.get('router_key')!=key or plan.get('phase')!='reviewed' or not 0<=time.time()-plan['created']<900:
        raise ValueError('Review expired, differs, or has already been submitted.')
    state,address,value,config,nodes,bridges=inspect_router(project,key)
    if sha(state)!=plan['state_sha256'] or sha(config_identity(config))!=plan['config_sha256'] or nodes!=plan['eligible_nodes']:
        raise ValueError('Router or cluster changed since review; create a fresh review.')
    verify_clients(nodes,bridges)
    node,vmid=plan['node'],plan['vmid'];base=f'/nodes/{node}/lxc/{vmid}'
    if 'move-rootfs' in plan['actions']:
        task(plan,file,'stop',base+'/status/stop')
        stopped=api(base+'/config')
        task(plan,file,'move-rootfs',base+'/move_volume',volume='rootfs',storage=STORE,delete=1,digest=stopped['digest'])
        task(plan,file,'start',base+'/status/start')
    current=api(base+'/config')
    if not current['rootfs'].startswith(f'{STORE}:{vmid}/'):
        raise ValueError('Shared router disk identity differs; preserve the journal.')
    plan.update(phase='refreshing-state',shared_config_sha256=sha(config_identity(current)));save(file,plan)
    # Run only a provider refresh through the scoped controller account. The
    # helper preserves unrelated guest state and rejects any resource mutation.
    code="""import json,sys
from pathlib import Path
sys.path.insert(0,'/home/crange/crange/scripts')
from crange import tf_run
from crange_router_ha import refresh_storage
print(json.dumps(refresh_storage(Path(PROJECT_PATH),ADDRESS,VMID,STORAGE)))
""".replace('PROJECT_PATH',repr('/home/crange/crange/terraform/environments/project-%03d'%project)).replace('ADDRESS',repr(address)).replace('VMID',repr(vmid)).replace('STORAGE',repr(STORE))
    controller(code, timeout=150)
    plan.update(phase='enrolling-native-ha');save(file,plan)
    api('/cluster/ha/resources','create',sid=f'ct:{vmid}',state='ignored',failback=0,max_restart=1,max_relocate=4)
    run(['ha-manager','rules','add','node-affinity',f'crange-p{project:03d}-ct{vmid}',
         '--resources',f'ct:{vmid}','--nodes',','.join(nodes),'--strict','1'])
    api(f'/cluster/ha/resources/ct:{vmid}','set',state='started')
    registration={'vmid':vmid,'identity_sha256':sha(config_identity(current)),'eligible_nodes':nodes}
    publish="""import grp,json,os
from pathlib import Path
p=Path('/etc/crange/router-ha');p.mkdir(parents=True,mode=0o750,exist_ok=True)
for item in (p.parent,p):
 if item.is_symlink() or item.stat().st_uid!=0 or item.stat().st_mode&0o022:raise SystemExit('Registry parent is untrusted.')
 os.chown(item,0,grp.getgrnam('crange').gr_gid);item.chmod(0o750)
f=p/'project-PROJECT.json';r=json.loads(f.read_text()) if f.exists() else {'schema_version':1,'project_id':PROJECT_INT,'state_lineage':LINEAGE,'routers':{}}
if r['state_lineage']!=LINEAGE:raise SystemExit('Registry lineage differs.')
r['routers'][ADDRESS]=REGISTRATION
t=f.with_suffix('.tmp');t.write_text(json.dumps(r,indent=2));os.chown(t,0,grp.getgrnam('crange').gr_gid);t.chmod(0o640);t.replace(f);print('{}')
""".replace('PROJECT_INT',str(project)).replace('PROJECT',f'{project:03d}').replace('LINEAGE',repr(state['lineage'])).replace('ADDRESS',repr(address)).replace('REGISTRATION',repr(registration))
    controller(publish,root=True)
    plan.update(phase='enrolled',registration=registration);save(file,plan)
    return {k:plan[k] for k in ('phase','project_id','router_key','vmid','node','eligible_nodes','failback')}


def apply(project,key,plan_id):
    guard(project,key)
    with pause_controller():
        return apply_reviewed(project,key,plan_id)


def retire(project, vmid):
    """Release HA ownership only for an exact root-registered project router."""
    guard(project,'router:project')
    if type(vmid) is not int or vmid < 100:raise ValueError('Invalid router identity.')
    read="""import json
from pathlib import Path
p=Path('/etc/crange/router-ha/project-PROJECT.json')
for item in (p,*p.parents):
 if item.is_symlink() or item.stat().st_uid!=0 or item.stat().st_mode&0o022:raise SystemExit('Untrusted router registry.')
print(p.read_text())
""".replace('PROJECT',f'{project:03d}')
    registry=controller(read,root=True)
    entries=[(key,value) for key,value in registry.get('routers',{}).items() if value.get('vmid')==vmid]
    if registry.get('project_id')!=project or len(entries)!=1:raise ValueError('Router is not uniquely registered to this project.')
    key,entry=entries[0]
    guests=[r for r in api('/cluster/resources',type='vm') if r.get('vmid')==vmid]
    online={r['node'] for r in api('/nodes') if r.get('status')=='online'}
    if (len(guests)!=1 or guests[0].get('type')!='lxc' or guests[0].get('pool')!='crange-iac' or
            guests[0].get('node') not in online):raise ValueError('Router has not reached an online exclusive assignment.')
    config=api(f'/nodes/{guests[0]["node"]}/lxc/{vmid}/config')
    status=[r for r in api('/cluster/ha/status/current') if r.get('sid')==f'ct:{vmid}']
    if (sha(config_identity(config))!=entry['identity_sha256'] or len(status)!=1 or
            status[0].get('node')!=guests[0]['node'] or status[0].get('state') not in ('started','stopped') or
            status[0].get('request_state')!=status[0].get('state')):
        raise ValueError('Router ownership changed or HA transition is still active.')
    journal=ROOT/(f'retire-p{project:03d}-ct{vmid}-'+uuid.uuid4().hex+'.json')
    receipt={'phase':'submitting-retirement','project_id':project,'vmid':vmid,'registration':entry}
    save(journal,receipt)
    # Removing HA does not stop/start the guest. The already-reviewed project
    # teardown owns its subsequent shutdown and deletion.
    run(['ha-manager','remove',f'ct:{vmid}'])
    if any(r.get('sid')==f'ct:{vmid}' for r in api('/cluster/ha/resources')):
        raise ValueError('Router HA retirement remains uncertain; retain its journal.')
    publish="""import json,os
from pathlib import Path
p=Path('/etc/crange/router-ha/project-PROJECT.json');before=REGISTRY
if json.loads(p.read_text())!=before:raise SystemExit('Registry changed during retirement.')
del before['routers'][ADDRESS]
t=p.with_suffix('.tmp');t.write_text(json.dumps(before,indent=2));s=p.stat();os.chown(t,s.st_uid,s.st_gid);t.chmod(0o640);t.replace(p);print('{}')
""".replace('PROJECT',f'{project:03d}').replace('REGISTRY',repr(registry)).replace('ADDRESS',repr(key))
    controller(publish,root=True)
    receipt['phase']='verified';save(journal,receipt)
    return {'state':'retired','project_id':project,'vmid':vmid}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('review','apply','retire'));parser.add_argument('project',type=int)
    parser.add_argument('router',nargs='?');parser.add_argument('--plan-id');parser.add_argument('--vmid',type=int)
    args=parser.parse_args()
    ROOT.mkdir(parents=True,mode=0o700,exist_ok=True)
    with (ROOT/'operator.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        result=(review(args.project,args.router) if args.action=='review' else
                retire(args.project,args.vmid) if args.action=='retire' else apply(args.project,args.router,args.plan_id))
    print(json.dumps(result,indent=2))
