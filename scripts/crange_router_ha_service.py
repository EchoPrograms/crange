"""Reconcile fenced native HA relocations and retry completed router creations.

Proxmox owns guest recovery. This service only adopts verified exclusive
assignments; it never guesses that an unreachable source has been fenced.
"""
import json
import re
from pathlib import Path
import time

from crange_router_ha import NODES, container_states, enroll_created, project_operation, reconcile_locations


def refresh_eligibility(project):
    import socket
    request={'command':'router-ha-refresh','node':'proxmox4','project_id':project}
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
        client.settimeout(330);client.connect('/run/crange-ct-read/socket')
        client.sendall(json.dumps(request).encode()+b'\n');data=b''
        while not data.endswith(b'\n') and len(data)<=8192:
            part=client.recv(8193-len(data))
            if not part:break
            data+=part
    response=json.loads(data)
    result=json.loads(response['value']) if set(response)=={'value'} and isinstance(response['value'],str) else {}
    if result.get('phase')!='eligibility-verified' or result.get('project_id')!=project:
        raise ValueError('Returning worker eligibility could not be verified.')
    return result


def reconcile_all(api, environments, registry_root, *, reconcile=reconcile_locations,
                  enroll=enroll_created, operation=project_operation, refresh=refresh_eligibility):
    resources=api.request('GET','/cluster/resources',{'type':'vm'})
    ha=api.request('GET','/cluster/ha/status/current')
    online={r['node'] for r in api.request('GET','/nodes') if r.get('status')=='online'}
    if not any(row.get('type')=='quorum' and row.get('quorate')==1 for row in ha):
        return {'state':'waiting-for-quorum','projects':[]}
    projects={p.stem for p in Path(registry_root).glob('project-*.json')}
    projects.update(p.parent.name for p in Path(environments).glob('project-*/.crange-router-ha-pending.json'))
    result=[]
    for name in sorted(projects):
        if not re.fullmatch(r'project-[0-9]{3}',name):
            continue
        project=int(name.split('-')[-1]);path=Path(environments)/name
        if not (path/'terraform.tfstate').exists():
            continue
        with operation(project,blocking=False) as acquired:
            if not acquired:
                result.append({'project':project,'state':'busy'})
                continue
            try:
                pending=path/'.crange-router-ha-pending.json'
                enrolled=[]
                if pending.exists():
                    record=json.loads(pending.read_text())
                    enrolled=enroll(path,record['plan_id'],record['changes'])
                registry_file=Path(registry_root)/(name+'.json')
                registry=json.loads(registry_file.read_text()) if registry_file.exists() else {'routers':{}}
                if any((online & (NODES-{'proxmox1'}))-set(entry['eligible_nodes']) for entry in registry.get('routers',{}).values()):
                    refresh(project)
                    registry=json.loads(registry_file.read_text())
                states=container_states(json.loads((path/'terraform.tfstate').read_text()))
                relocated=[];waiting=[]
                for address,entry in registry.get('routers',{}).items():
                    rows=[row for row in resources if row.get('vmid')==entry['vmid']]
                    services=[row for row in ha if row.get('sid')==f'ct:{entry["vmid"]}']
                    if len(rows)!=1 or len(services)!=1:
                        waiting.append(entry['vmid']);continue
                    if rows[0].get('node')!=states.get(address,{}).get('node_name'):
                        relocated.append(entry['vmid'])
                    if (rows[0].get('node') not in online or services[0].get('state')!='started'
                            or services[0].get('node')!=rows[0].get('node')):
                        waiting.append(entry['vmid'])
                journal=Path.home()/'.local/state/crange/router-ha-reconcile'/name
                unfinished=any(json.loads(p.read_text()).get('phase') not in ('verified','aborted-before-submit')
                               for p in journal.glob('*/receipt.json'))
                adopted=reconcile(path,api=api) if relocated or unfinished else []
                result.append({'project':project,'state':'recovering' if waiting else 'ready',
                               'enrolled':enrolled,'adopted':adopted,'waiting_vmids':waiting})
            except (RuntimeError,ValueError,OSError) as exc:
                result.append({'project':project,'state':'retry','error':str(exc)[:400]})
    return {'state':'checked','projects':result}


def main():
    from crange import ENVS,ProxmoxAPI
    from crange_scheduling import atomic_json
    result=reconcile_all(ProxmoxAPI(),ENVS,Path('/etc/crange/router-ha'))
    result['checked_at']=time.time()
    atomic_json(Path.home()/'.local/state/crange/router-ha-status.json',result)
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':
    main()
