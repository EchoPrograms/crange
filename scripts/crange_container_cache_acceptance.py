"""Admin-only isolated CT cache acceptance; publish capabilities only after proof."""
import copy
import hashlib
import json
import uuid
from urllib.parse import quote


def run(project, image_id, node, storage):
    from crange import ProxmoxAPI
    from crange_scheduling import CONFIG, DATA, read_settings, discover, atomic_json, provider_container_placement_verified
    from crange_cache_jobs import prepare_cache
    from crange_network_publish import policy_write_lock
    from crange_cache_ownership import cache_owned
    policy,catalog=read_settings();image=catalog['images'].get(image_id,{})
    if (type(project) is not int or not 1<=project<=255 or image.get('kind')!='container' or
            ('*' not in image.get('approved_projects',[]) and project not in image.get('approved_projects',[])) or
            not image.get('artifact',{}).get('complete_capture') or
            node not in policy.get('allowed_nodes',[]) or storage not in policy.get('allowed_storages',[]) or
            not provider_container_placement_verified(policy,node)):
        raise ValueError('Choose an approved complete container image and a verified container-capable node.')
    api=ProxmoxAPI();inventory=discover(api,policy);store=inventory['nodes'][node].get('storages',{}).get(storage,{})
    if not store.get('available') or store.get('shared') or not store.get('snapshots_verified'):
        raise ValueError('Container acceptance requires verified available local snapshot storage.')
    scoped=copy.deepcopy(policy);scoped['allowed_nodes']=[node];scoped['approved_storage']=[[node,storage]]
    paths={'local-linked-container','container-archive-restore'}
    if 'cache' not in scoped:
        scoped['cache']={key:copy.deepcopy(policy[key]) for key in ('isolated_bridge','isolated_bridges','tested_paths') if key in policy}
    scoped['cache'].setdefault('tested_paths',[])
    scoped['cache']['tested_paths']=sorted(set(scoped['cache']['tested_paths'])|paths)
    print(f'Running isolated container restore/linked-clone acceptance on {node}/{storage}; global capabilities remain unchanged until verification passes.',flush=True)
    result=prepare_cache(project_id=project,image_id=image_id,node_name=node,datastore_id=storage,
                         _controlled_acceptance_policy=scoped)
    job_id=result['job_id'];journal=DATA/'cache-journal'/(job_id+'.json');record=json.loads(journal.read_text())
    if (record.get('state')!='ready' or record.get('image_id')!=image_id or record.get('node')!=node or
            record.get('datastore_id')!=storage or record.get('artifact_sha256')!=image['artifact_sha256'] or
            record.get('linked_backing',{}).get('verified') is not True):
        raise ValueError('Container cache acceptance lacks completed physical snapshot backing evidence.')
    for task in record.get('provider_tasks',[]):
        task_node=task.get('node') or task.get('path','').split('/')[2]
        status=api.request('GET',f'/nodes/{task_node}/tasks/{quote(task["upid"],safe="")}/status')
        if status.get('status')!='stopped' or status.get('exitstatus')!='OK':
            raise ValueError('Container acceptance provider tasks are not confirmed successful.')
    vmid=record['template_id'];config=api.request('GET',f'/nodes/{node}/lxc/{vmid}/config')
    live=api.request('GET','/cluster/resources',{'type':'vm'})
    if (not cache_owned(config,vmid,job_id) or int(config.get('template',0))!=1 or config.get('lock') or
            not str(config.get('rootfs','')).startswith(storage+':') or
            any(str(row.get('vmid'))==str(record.get('probe_id')) for row in live)):
        raise ValueError('Accepted container cache identity or disposable-probe cleanup changed.')
    receipt={'image_id':image_id,'artifact_sha256':image['artifact_sha256'],'cache_job_id':job_id,
             'template_id':vmid,'node':node,'storage':storage,'paths':sorted(paths),
             'journal_sha256':hashlib.sha256(journal.read_bytes()).hexdigest(),'verified':True}
    path=CONFIG/'distribution-policy.json'
    with policy_write_lock(path):
        original=hashlib.sha256(path.read_bytes()).hexdigest();current,_=read_settings()
        if current['revision']!=policy['revision']:raise ValueError('Policy changed during acceptance; retain the verified cache and retry publication.')
        if 'cache' not in current:
            current['cache']={key:copy.deepcopy(current[key]) for key in ('isolated_bridge','isolated_bridges','tested_paths') if key in current}
        for target in (current,current['cache']):
            target['tested_paths']=sorted(set(target.get('tested_paths',[]))|paths)
        current.setdefault('nodes',{}).setdefault(node,{}).setdefault('container_cache_receipts',{})[storage]=receipt
        pair=[node,storage]
        if pair not in current.setdefault('approved_storage',[]):current['approved_storage'].append(pair)
        current['revision']='container-cache-'+uuid.uuid4().hex
        if hashlib.sha256(path.read_bytes()).hexdigest()!=original:raise ValueError('Policy changed before acceptance publication.')
        atomic_json(path,current,0o600)
    print(json.dumps(receipt,indent=2))
    return receipt
