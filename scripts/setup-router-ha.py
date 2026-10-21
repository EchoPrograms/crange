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
import ssl
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError

STORE = 'crange-router-ha'
ROOT = Path('/root/crange-resilience/router-ha-enrollment')
NODES = {f'proxmox{i}' for i in range(2, 7)}
SOURCE_NODES = NODES | {'proxmox1'}


def run(args, *, timeout=40):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout


def api(path, method='get', **params):
    if method in ('create', 'set', 'delete'):
        return native_mutation(path, method, params)
    args = ['pvesh', method, path, '--output-format', 'json']
    for key, value in params.items(): args.extend(['--' + key, str(value)])
    result = run(args)
    # Successful void API mutations (including HA enrollment) produce no
    # JSON through pvesh. Reads must still return an actual JSON response.
    if not result.strip() and method in ('create', 'set', 'delete'):
        return None
    return json.loads(result)


def native_mutation(path, method, params):
    """Submit asynchronous REST tasks without attaching their stdout to pvesh.

    A proxied CLI task can keep the CLI attached for its entire lifetime. Killing
    that observer closes the worker's stdout and can abort a successful disk
    copy with SIGPIPE before it releases its lock. REST returns the native UPID.
    This root-only operator already has the same authority through pvesh; its
    short-lived ticket stays in memory and is never sent to the controller.
    """
    credentials = json.loads(run(['perl', '-MPVE::Cluster', '-MPVE::AccessControl',
        '-MJSON::PP', '-e', 'PVE::Cluster::cfs_update(1); print encode_json({'
        'ticket=>PVE::AccessControl::assemble_ticket(q(root@pam)),'
        'csrf=>PVE::AccessControl::assemble_csrf_prevention_token(q(root@pam))});']))
    request = Request('https://proxmox4:8006/api2/json' + path,
        data=urlencode(params).encode(),
        headers={'Cookie': 'PVEAuthCookie=' + credentials['ticket'],
                 'CSRFPreventionToken': credentials['csrf'],
                 'Content-Type': 'application/x-www-form-urlencoded'},
        method={'create': 'POST', 'set': 'PUT', 'delete': 'DELETE'}[method])
    context = ssl.create_default_context(cafile='/etc/pve/pve-root-ca.pem')
    # Older native PVE root CAs omit the keyUsage extension required by
    # Python 3.13's new strict default. Keep CA signatures, validity and hostname
    # verification; accept the cluster's existing CA profile without replacing it.
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    with urlopen(request, context=context, timeout=30) as response:
        result = json.loads(response.read())
    if not isinstance(result, dict) or 'data' not in result:
        raise ValueError('Native HA API returned an uncertain mutation response; do not replay it.')
    return result['data']


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def save(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2) + '\n'); tmp.chmod(0o600); tmp.replace(path)


def guard(project, key):
    if os.geteuid() != 0 or socket.gethostname() != 'proxmox4':
        raise ValueError('Run as root only on the surviving core, Proxmox4.')
    if (type(project) is not int or not 1 <= project <= 255 or project in (60, 61) or
            not re.fullmatch(r'router:(?:project|staging|team:(?:[1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-3])|custom:[a-z][a-z0-9_-]{0,63})', key)):
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
    module = direct.get(key) or ('module.custom_router[' + json.dumps(key.removeprefix('router:custom:')) + ']'
                                if key.startswith('router:custom:') else
                                'module.team_router[' + json.dumps(key.removeprefix('router:team:')) + ']')
    rows = [r for r in snapshot.get('resources', []) if r.get('module') == module and
            r.get('type') == 'proxmox_virtual_environment_container' and r.get('name') == 'router']
    if len(rows) != 1 or len(rows[0].get('instances', [])) != 1:
        raise ValueError('Router has no unique Terraform container identity.')
    instance = rows[0]['instances'][0]; value = instance['attributes']
    clone = value.get('clone') or []
    independent = not clone or (len(clone)==1 and clone[0].get('full') is True and type(clone[0].get('vm_id')) is int)
    if (instance.get('deposed') or instance.get('status') == 'tainted' or not independent or
            type(value.get('vm_id')) is not int or str(value['vm_id']) != str(value.get('id')) or
            len(value.get('disk', [])) != 1 or not snapshot.get('lineage')):
        raise ValueError('Only intact independent router state is accepted.')
    return module + '.proxmox_virtual_environment_container.router', value


def config_identity(config):
    return {k:v for k,v in config.items() if k in ('hostname','rootfs','unprivileged','tags') or re.fullmatch(r'net\d+', k)}


def verify_rule(rule,vmid,nodes):
    if (rule.get('type')!='node-affinity' or rule.get('strict')!=1 or rule.get('disable',0) or
            set(str(rule.get('resources','')).split(','))!={f'ct:{vmid}'} or
            set(str(rule.get('nodes','')).split(','))!=set(nodes)):
        raise ValueError('Existing router HA affinity differs from its reviewed enrollment.')


def inspect_router(project, key):
    state = snapshot(project); address, value = router(state, key); vmid = value['vm_id']; node = value['node_name']
    online = {r['node'] for r in api('/nodes') if r.get('status') == 'online'}
    if node not in online or node not in SOURCE_NODES:
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
        if not match or not (match[1] == 'crtrans' or re.fullmatch(fr'p{project:03d}(?:tr|[tc]\d{{3}})', match[1])):
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
    verify_clients(sorted(set(nodes) | {value['node_name']}), bridges)
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
    try:
        upid = api(endpoint, 'create', **params)
    except (TimeoutError, URLError, subprocess.TimeoutExpired):
        # An observation failure is never authority to submit the mutation again.
        upid = None
    if not isinstance(upid,str) or not upid.startswith('UPID:'+plan['node']+':'):
        # A proxied CLI response need not expose the source node's task ID.
        # Find the exact source task, including a still-running or warning
        # result. Its status is checked below; never resubmit the mutation.
        kind={'stop':'vzstop','start':'vzstart','move-rootfs':'move_volume'}[name]
        rows=api('/nodes/'+plan['node']+'/tasks',vmid=plan['vmid'],limit=20)
        matches=[r for r in rows if r.get('type')==kind and str(r.get('id'))==str(plan['vmid'])
                 and r.get('starttime',0)>=int(submitted)-1]
        if len(matches)!=1:raise ValueError('Proxied router task result is uncertain; preserve its journal.')
        upid=matches[0]['upid']
    if not isinstance(upid,str) or not upid.startswith('UPID:'+plan['node']+':'):
        raise ValueError('Native task submission is uncertain; preserve its journal.')
    plan.update(phase='waiting-'+name,upid=upid);save(file,plan)
    observe_task(plan, file, name, upid)


def observe_task(plan, file, name, upid):
    for _ in range(120):
        result=api(f'/nodes/{plan["node"]}/tasks/{upid}/status')
        if result.get('status')=='stopped':
            if result.get('exitstatus')!='OK':
                if name=='start' and result.get('exitstatus')=='WARNINGS: 1':
                    log=api(f'/nodes/{plan["node"]}/tasks/{upid}/log',limit=100)
                    messages=[r.get('t','').strip() for r in log if r.get('t','').strip()]
                    if (len(log)<100 and messages==['WARN: Systemd 252 detected. You may need to enable nesting.','TASK WARNINGS: 1']
                            and api(f'/nodes/{plan["node"]}/lxc/{plan["vmid"]}/status/current').get('status')=='running'):
                        break
                raise ValueError('Native router task failed; preserve its journal.')
            break
        time.sleep(1)
    else:
        raise ValueError('Native router task is still pending; do not replay it.')
    plan.setdefault('completed_tasks', {})[name] = {'upid': upid, 'status': result}
    plan.update(phase='completed-'+name);save(file,plan)


@contextmanager
def pause_controller():
    """Close job submission before serializing this one-time operator action."""
    services=['crange.service','crange-guac-broker.service','crange-router-ha.timer']
    stopped=[]
    try:
        for service in services:
            controller('import json,subprocess;subprocess.run('+repr(['systemctl','stop',service])+",check=True);print('{}')",root=True)
            stopped.append(service)
        active=controller("import json,sqlite3;d=sqlite3.connect('/var/lib/crange-jobs/jobs.sqlite3');print(json.dumps(d.execute(\"SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running','reconcile-required')\").fetchone()[0]))",root=True)
        if active:raise ValueError('Controller jobs are active; defer router conversion.')
        active=controller("import json,subprocess;p=subprocess.run(['systemctl','is-active','--quiet','crange-router-ha.service']);print(json.dumps(p.returncode==0))",root=True)
        if active:raise ValueError('Router HA reconciliation is active; defer manual conversion.')
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
    verify_clients(sorted(set(nodes) | {value['node_name']}),bridges)
    node,vmid=plan['node'],plan['vmid'];base=f'/nodes/{node}/lxc/{vmid}'
    if 'move-rootfs' in plan['actions']:
        task(plan,file,'stop',base+'/status/stop')
        stopped=api(base+'/config')
        task(plan,file,'move-rootfs',base+'/move_volume',volume='rootfs',storage=STORE,delete=1,digest=stopped['digest'])
        task(plan,file,'start',base+'/status/start')
    return refresh_and_enroll(project,key,state,plan,file,address)


def refresh_and_enroll(project,key,state,plan,file,address):
    vmid=plan['vmid'];base=f'/nodes/{plan["node"]}/lxc/{vmid}'
    current=api(base+'/config')
    if (not current['rootfs'].startswith(f'{STORE}:{vmid}/') or current.get('lock') or
            api(base+'/status/current').get('status')!='running'):
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
    return finish_enrollment(project,key,state,plan,file,current,address)


def source_command(node, command, *, timeout=60):
    if node not in SOURCE_NODES:
        raise ValueError('Unverified router source node.')
    if node != 'proxmox4':
        command=['ssh','-oBatchMode=yes','-oConnectTimeout=5','-oStrictHostKeyChecking=yes',
            '-oHostKeyAlias='+node,'-oUserKnownHostsFile=/etc/pve/nodes/'+node+'/ssh_known_hosts',
            'root@10.0.0.'+str(100+int(node[-1])),shlex.join(command)]
    return run(command, timeout=timeout)


def verify_committed_files(plan, current):
    """Read-only checks for the specific old CLI SIGPIPE failure after copy."""
    old=plan['config']['rootfs'].split(',')[0];new=current['rootfs'].split(',')[0]
    if (not re.fullmatch(r'[a-z][a-z0-9-]*:vm-'+str(plan['vmid'])+r'-disk-\d+',old) or
            not re.fullmatch(STORE+':'+str(plan['vmid'])+r'/vm-'+str(plan['vmid'])+r'-disk-\d+\.raw',new)):
        raise ValueError('Committed disk recovery requires exact managed volume identities.')
    code="""import hashlib,json,subprocess
from pathlib import Path
old=OLD;new=NEW;vmid=VMID
rows=json.loads(subprocess.check_output(['pvesh','get','/nodes/'+NODE+'/storage/'+old.split(':')[0]+'/content','--vmid',str(vmid),'--output-format','json'],text=True))
if any(r.get('volid')==old for r in rows):raise SystemExit('Original volume still exists; retain both copies.')
p=Path(subprocess.check_output(['pvesm','path',new],text=True).strip())
expected=Path('/mnt/pve/crange-router-ha/images')/str(vmid)/new.split('/')[-1]
if p!=expected or p.is_symlink() or not p.is_file() or p.stat().st_size!=SIZE:raise SystemExit('Shared volume identity differs.')
loops=subprocess.run(['losetup','-j',str(p)],capture_output=True,text=True,check=True)
if loops.stdout.strip():raise SystemExit('Shared filesystem still has a loop attachment.')
check=subprocess.run(['e2fsck','-f','-n',str(p)],capture_output=True,timeout=60)
if check.returncode:raise SystemExit('Read-only shared filesystem check failed.')
print(json.dumps({'source_volume_absent':True,'filesystem_readonly_check':True,'target_volume':new,
 'bytes':p.stat().st_size,'fsck_output_sha256':hashlib.sha256(check.stdout+check.stderr).hexdigest()}))
""".replace('OLD',repr(old)).replace('NEW',repr(new)).replace('VMID',str(plan['vmid'])).replace('NODE',repr(plan['node'])).replace('SIZE',str(int(plan['disk_gib']*2**30)))
    proof=json.loads(source_command(plan['node'],['python3','-c',code],timeout=85))
    if (proof.get('source_volume_absent') is not True or proof.get('filesystem_readonly_check') is not True
            or proof.get('target_volume')!=new or proof.get('bytes')!=int(plan['disk_gib']*2**30)):
        raise ValueError('Committed filesystem proof differs; preserve its lock.')
    return proof


def resume_conversion(project,key,state,address,value,plan,file):
    """Resume a proven committed disk move; never repeat stop, copy or restore."""
    node,vmid=plan['node'],plan['vmid'];base=f'/nodes/{node}/lxc/{vmid}'
    if (plan.get('state_lineage')!=state.get('lineage') or plan.get('address')!=address or
            value['vm_id']!=vmid or value['node_name']!=node or
            sha(plan['config'])!=plan.get('config_sha256')):
        raise ValueError('Interrupted router conversion identity differs.')
    rows=[r for r in api('/cluster/resources',type='vm') if r.get('vmid')==vmid]
    online={r['node'] for r in api('/nodes') if r.get('status')=='online'}
    current=api(base+'/config');status=api(base+'/status/current').get('status')
    original={k:v for k,v in plan['config'].items() if k!='rootfs'}
    actual={k:v for k,v in config_identity(current).items() if k!='rootfs'}
    if (len(rows)!=1 or rows[0].get('node')!=node or node not in online or
            rows[0].get('pool')!='crange-iac' or rows[0].get('type')!='lxc' or
            actual!=original or current.get('template') or current.get('lock') not in (None,'disk') or
            any(re.fullmatch(r'(?:mp|unused)\d+',k) for k in current) or
            not re.fullmatch(STORE+':'+str(vmid)+r'/vm-'+str(vmid)+r'-disk-\d+\.raw,size='+format(plan['disk_gib'],'g')+'G',current.get('rootfs','')) or
            any(r.get('sid')==f'ct:{vmid}' for r in api('/cluster/ha/resources'))):
        raise ValueError('Interrupted conversion no longer has an exclusive managed shared disk.')
    verify_clients(sorted(set(plan['eligible_nodes'])|{node}),plan['bridges'])
    phase=plan['phase']
    if phase in ('submitting-move-rootfs','waiting-move-rootfs','completed-move-rootfs','move-confirmed'):
        if status!='stopped':raise ValueError('Interrupted move requires the exact stopped router.')
        tasks=api('/nodes/'+node+'/tasks',vmid=vmid,limit=100)
        recent=[r for r in tasks if r.get('starttime',0)>=int(plan['submitted_at'])-1]
        if (len(tasks)>=100 or len(recent)!=1 or recent[0].get('type')!='move_volume' or
                str(recent[0].get('id'))!=str(vmid) or recent[0].get('user')!='root@pam' or
                not str(recent[0].get('upid','')).startswith('UPID:'+node+':')):
            raise ValueError('Interrupted disk task history is ambiguous; preserve its lock.')
        upid=recent[0]['upid'];result=api('/nodes/'+node+'/tasks/'+upid+'/status')
        if result.get('status')!='stopped':raise ValueError('Original disk move is still running; do not replay it.')
        if result.get('exitstatus')=='broken pipe ':
            logs=api('/nodes/'+node+'/tasks/'+upid+'/log',limit=1000)
            messages=[r.get('t','') for r in logs]
            if (len(logs)>=1000 or not messages or messages[-1].strip()!='TASK ERROR: broken pipe' or
                    not any(re.fullmatch(r'total size is [\d,]+  speedup is [\d.]+',s.strip()) for s in messages) or
                    any('error' in s.lower() or 'failed' in s.lower() for s in messages[:-1])):
                raise ValueError('Failed move has no complete native copy evidence.')
            proof=verify_committed_files(plan,current)
            plan['committed_move_recovery']={'task':result,'upid':upid,'proof':proof,
                'log_sha256':sha(logs),'original_failure_preserved':True}
            save(file,plan)
            if current.get('lock')=='disk':source_command(node,['pct','unlock',str(vmid)])
            unlocked=api(base+'/config')
            if unlocked.get('lock') or config_identity(unlocked)!=config_identity(current):
                raise ValueError('Recovered router changed while releasing its stale disk lock.')
        elif result.get('exitstatus')!='OK' or current.get('lock'):
            raise ValueError('Native move failed; retain the exact task and disk lock.')
        plan.update(phase='move-confirmed');save(file,plan)
        task(plan,file,'start',base+'/status/start')
    elif phase in ('submitting-start','waiting-start'):
        tasks=api('/nodes/'+node+'/tasks',vmid=vmid,limit=100)
        matches=[r for r in tasks if r.get('type')=='vzstart' and str(r.get('id'))==str(vmid)
                 and r.get('starttime',0)>=int(plan['submitted_at'])-1 and r.get('user')=='root@pam']
        if len(tasks)>=100 or len(matches)!=1:
            raise ValueError('Interrupted router start is uncertain; never submit it again.')
        observe_task(plan,file,'start',matches[0]['upid'])
    elif phase not in ('completed-start','refreshing-state'):
        raise ValueError('Interrupted conversion needs inspection; no mutation was repeated.')
    return refresh_and_enroll(project,key,state,plan,file,address)


def finish_enrollment(project,key,state,plan,file,current,address):
    vmid=plan['vmid'];nodes=plan['eligible_nodes']
    plan.update(phase='enrolling-native-ha');save(file,plan)
    managed=[row for row in api('/cluster/ha/resources') if row.get('sid')==f'ct:{vmid}']
    if not managed:
        api('/cluster/ha/resources','create',sid=f'ct:{vmid}',state='ignored',failback=0,max_restart=1,max_relocate=4)
    elif len(managed)!=1 or managed[0].get('state') not in ('ignored','started') or managed[0].get('failback')!=0:
        raise ValueError('Existing HA service differs from its enrollment journal.')
    rule=f'crange-p{project:03d}-ct{vmid}'
    existing=[row for row in api('/cluster/ha/rules') if row.get('rule')==rule]
    if not existing:
        run(['ha-manager','rules','add','node-affinity',rule,
             '--resources',f'ct:{vmid}','--nodes',','.join(nodes),'--strict','1'])
    else:
        verify_rule(existing[0],vmid,nodes)
    api(f'/cluster/ha/resources/ct:{vmid}','set',state='started')
    registration={'vmid':vmid,'identity_sha256':sha(config_identity(current)),
                  'eligible_nodes':nodes,'initial_node':plan['node']}
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


def automatic(project, key, origin_plan_id):
    """Worker entry point: a completed, exact create plan is mandatory."""
    guard(project,key)
    if not re.fullmatch(r'[a-f0-9]{32}',str(origin_plan_id)):
        raise ValueError('Automatic HA requires an exact completed plan.')
    read="""import json
from pathlib import Path
p=Path('/home/crange/crange/terraform/environments/project-PROJECT')
summary=json.loads((p/'.crange-plans'/'PLAN.json').read_text())
policy=Path('/etc/crange/router-ha/policy.json')
for item in (policy,*policy.parents):
 s=item.stat()
 if item.is_symlink() or s.st_uid!=0 or s.st_mode&0o022:raise SystemExit('Untrusted HA policy.')
if json.loads(policy.read_text()).get('automatic_enrollment') is not True:raise SystemExit('Automatic HA is disabled.')
from sys import path
path.insert(0,'/home/crange/crange/scripts')
from crange_project_lifecycle import require_active
require_active(p)
print(json.dumps(summary))
""".replace('PROJECT',f'{project:03d}').replace('PLAN',origin_plan_id)
    summary=controller(read)
    state=snapshot(project);address,value=router(state,key)
    try:
        applied=datetime.fromisoformat(summary.get('applied_at','')).timestamp()
    except (ValueError,TypeError):
        raise ValueError('Automatic HA requires a successfully applied creation plan.') from None
    if (summary.get('plan_id')!=origin_plan_id or not 0<applied<=time.time()+5 or
            not any(row.get('address')==address and row.get('actions')==['create'] for row in summary.get('changes',[]))):
        raise ValueError('Router is not a completed creation from the requested plan.')
    # Idempotent completion never replays a disk move, stop, or start.
    registration=controller("import json;from pathlib import Path;p=Path('/etc/crange/router-ha/project-%03d.json');print(p.read_text() if p.exists() else '{}')"%project,root=True)
    entry=registration.get('routers',{}).get(address)
    if entry:
        current=api(f'/nodes/{value["node_name"]}/lxc/{value["vm_id"]}/config')
        managed=[r for r in api('/cluster/ha/resources') if r.get('sid')==f'ct:{value["vm_id"]}']
        if (registration.get('state_lineage')!=state['lineage'] or entry.get('vmid')!=value['vm_id'] or
                sha(config_identity(current))!=entry.get('identity_sha256') or len(managed)!=1 or
                managed[0].get('state')!='started' or managed[0].get('failback')!=0):
            raise ValueError('Existing HA registration differs; retain its recovery journal.')
        return {'phase':'enrolled','project_id':project,'router_key':key,'vmid':value['vm_id']}
    # Recover only our own interrupted HA enrollment, never an unrelated HA
    # resource that happens to have the same VMID or project tags.
    pending=[(p,json.loads(p.read_text())) for p in ROOT.glob('*.json')]
    pending=[(p,r) for p,r in pending if r.get('origin_plan_id')==origin_plan_id and
             r.get('project_id')==project and r.get('router_key')==key and
             r.get('vmid')==value['vm_id'] and r.get('phase')!='enrolled']
    if len(pending)>1:raise ValueError('Ambiguous interrupted HA enrollment journals.')
    if pending:
        file,plan=pending[0]
        if plan.get('phase')!='enrolling-native-ha':
            return resume_conversion(project,key,state,address,value,plan,file)
        rows=[r for r in api('/cluster/resources',type='vm') if r.get('vmid')==value['vm_id']]
        online={r['node'] for r in api('/nodes') if r.get('status')=='online'}
        if (len(rows)!=1 or rows[0].get('pool')!='crange-iac' or rows[0].get('type')!='lxc' or
                rows[0].get('node') not in online or rows[0]['node'] not in plan['eligible_nodes'] or
                plan.get('state_lineage')!=state['lineage']):
            raise ValueError('Interrupted HA enrollment has not reached an exclusive online identity.')
        current=api(f'/nodes/{rows[0]["node"]}/lxc/{value["vm_id"]}/config')
        if sha(config_identity(current))!=plan.get('shared_config_sha256'):
            raise ValueError('Interrupted HA enrollment configuration differs.')
        return finish_enrollment(project,key,state,plan,file,current,address)
    reviewed=review(project,key)
    journal=ROOT/(reviewed['plan_id']+'.json');record=json.loads(journal.read_text())
    record['origin_plan_id']=origin_plan_id;save(journal,record)
    # The parent worker holds the project operation lock. Pausing that same
    # worker would interrupt the creation that authorized this enrollment.
    return apply_reviewed(project,key,reviewed['plan_id'])


def refresh_eligibility(project):
    """Admit returning workers only after their shared store and bridges work."""
    guard(project,'router:project')
    read="""import json
from pathlib import Path
p=Path('/etc/crange/router-ha/project-PROJECT.json')
for item in (p,*p.parents):
 s=item.stat()
 if item.is_symlink() or s.st_uid!=0 or s.st_mode&0o022:raise SystemExit('Untrusted router registry.')
print(p.read_text())
""".replace('PROJECT',f'{project:03d}')
    registry=controller(read,root=True);state=snapshot(project)
    if registry.get('project_id')!=project or registry.get('state_lineage')!=state.get('lineage'):
        raise ValueError('Eligibility registry lineage differs.')
    before=json.loads(json.dumps(registry));added={}
    online={r['node'] for r in api('/nodes') if r.get('status')=='online'}
    resources=api('/cluster/resources',type='vm')
    for address,entry in registry['routers'].items():
        missing=(online & NODES)-set(entry['eligible_nodes'])
        if not missing:continue
        matches=[r for r in resources if r.get('vmid')==entry['vmid']]
        if len(matches)!=1 or matches[0].get('type')!='lxc' or matches[0].get('pool')!='crange-iac':
            raise ValueError('Registered router identity is ambiguous.')
        if matches[0]['node'] not in online:continue
        current=api(f'/nodes/{matches[0]["node"]}/lxc/{entry["vmid"]}/config')
        if sha(config_identity(current))!=entry['identity_sha256']:
            raise ValueError('Router identity changed before worker admission.')
        bridges=[]
        for field in ('net0','net1'):
            match=re.search(r'(?:^|,)bridge=([^,]+)',current.get(field,''))
            if not match:raise ValueError('Router bridge identity is missing.')
            bridges.append(match[1])
        ready=[]
        for node in sorted(missing):
            try:verify_clients([node],bridges)
            except (RuntimeError,ValueError,OSError,subprocess.SubprocessError):continue
            ready.append(node)
        if not ready:continue
        managed=[r for r in api('/cluster/ha/resources') if r.get('sid')==f'ct:{entry["vmid"]}']
        if len(managed)!=1 or managed[0].get('state')!='started' or managed[0].get('failback')!=0:
            raise ValueError('Native HA policy changed before worker admission.')
        rule=f'crange-p{project:03d}-ct{entry["vmid"]}'
        rules=[r for r in api('/cluster/ha/rules') if r.get('rule')==rule]
        if len(rules)!=1:raise ValueError('Router affinity rule is missing.')
        target=sorted(set(entry['eligible_nodes'])|set(ready))
        # A prior successful rule update may precede a crash before registry
        # publication. Accept only that exact verified extension on retry.
        actual=set(str(rules[0].get('nodes','')).split(','))
        if actual==set(target):verify_rule(rules[0],entry['vmid'],target)
        else:
            verify_rule(rules[0],entry['vmid'],entry['eligible_nodes'])
            api('/cluster/ha/rules/'+rule,'set',type='node-affinity',nodes=','.join(target),digest=rules[0]['digest'])
        entry['eligible_nodes']=target;added[str(entry['vmid'])]=ready
    if added:
        publish="""import json,os
from pathlib import Path
p=Path('/etc/crange/router-ha/project-PROJECT.json');old=BEFORE;new=AFTER
if json.loads(p.read_text())!=old:raise SystemExit('Registry changed before eligibility publication.')
t=p.with_suffix('.tmp');t.write_text(json.dumps(new,indent=2));s=p.stat();os.chown(t,s.st_uid,s.st_gid);t.chmod(0o640);t.replace(p);print('{}')
""".replace('PROJECT',f'{project:03d}').replace('BEFORE',repr(before)).replace('AFTER',repr(registry))
        controller(publish,root=True)
    return {'phase':'eligibility-verified','project_id':project,'added':added}


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
    parser.add_argument('action',choices=('review','apply','retire','automatic','refresh'));parser.add_argument('project',type=int)
    parser.add_argument('router',nargs='?');parser.add_argument('--plan-id');parser.add_argument('--vmid',type=int)
    args=parser.parse_args()
    ROOT.mkdir(parents=True,mode=0o700,exist_ok=True)
    with (ROOT/'operator.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        result=(refresh_eligibility(args.project) if args.action=='refresh' else
                automatic(args.project,args.router,args.plan_id) if args.action=='automatic' else
                review(args.project,args.router) if args.action=='review' else
                retire(args.project,args.vmid) if args.action=='retire' else apply(args.project,args.router,args.plan_id))
    print(json.dumps(result,indent=2))
