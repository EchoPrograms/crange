"""Root-only fixed network acceptance workflow. No HTTP/queue operation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import re
import subprocess
import stat
import traceback
from datetime import datetime, timezone

from crange_network_acceptance import require, probe_from_plan, validate_bundle
from crange_network_adapters import (SavedTerraformProbe, BoundedPVE, router_firewall,
    FixedManagementChecks, cluster_probe, durable_json, protected, reconcile_owned_probe,
    controller_source_hashes, trusted_relay_source_hash)
from crange_network_checks import FixedGuestNetworkChecks, drop_counter
from crange_network_runner import NetworkProbeRunner, plan_digest
from crange_placement import ReservationLedger
import crange_scheduling as scheduling


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def no_active_node_tasks(api, node):
    tasks = api.request('GET', f'/nodes/{node}/tasks', {'source': 'active'})
    require(isinstance(tasks, list), 'Active Proxmox task response is invalid.')
    return not tasks


def cancellation_requested():
    """Read the durable queue flag; do not infer cancellation from signals."""
    job_id=os.environ.get('CRANGE_JOB_ID')
    database=os.environ.get('CRANGE_JOB_DB')
    if not job_id or not database:
        return False
    import sqlite3
    with sqlite3.connect(database,timeout=10) as db:
        row=db.execute('SELECT operation,cancel_requested FROM jobs WHERE id=?',(job_id,)).fetchone()
    return bool(row and row[0]=='network-verify' and row[1])


def _persist_workflow_error(job_id, error, *, probe_root=Path('/var/lib/crange-network-probes')):
    """Keep precise workflow diagnostics in the root-only owned bundle."""
    if isinstance(job_id,str) and job_id.startswith('network-'):
        job_id=job_id.removeprefix('network-')
    if not re.fullmatch(r'[a-f0-9]{32}', job_id or ''):
        return
    directory=Path(probe_root)/('network-'+job_id)
    try:
        info=directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
            return
        fd=os.open(directory/'workflow-error.txt',os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                return
            os.fchmod(fd,0o600)
            with os.fdopen(fd,'w',closefd=False) as stream:
                stream.write('exception='+type(error).__name__+'\n')
                stream.write(traceback.format_exc())
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        return


class NetworkAcceptanceBackend:
    def __init__(self, saved, api, ledger, *, run=subprocess.run):
        self.saved=saved
        self.api=api
        self.ledger=ledger
        self.run=run
        self.target=saved.target
        self.root=saved.root
        self.transport=BoundedPVE(api)
        self.policy,self.catalog=scheduling.read_settings()
        self.project=saved.manifest['scope']['project_id']
        from crange import project_dir
        self.project_path=project_dir(str(self.project))
        from crange_network_acceptance import validate_source_binding
        self.source_binding=validate_source_binding(saved.manifest.get('source_binding'))
        require(self.source_binding['controller_sha256']==controller_source_hashes() and
                self.source_binding['relay_sha256']==trusted_relay_source_hash(),
                'Reviewed workflow source binding differs from deployment.')
        self.inputs=self._project_inputs()
        require(saved.manifest.get('project_input_sha256')==self.inputs,'Project input revision differs from approved manifest.')
        self.plan=saved.inspect('create')
        value=probe_from_plan(self.plan,'create',self.target)
        clones=value.get('clone',[])
        require(len(clones)==1 and clones[0].get('full') is True
                and clones[0].get('node_name') in (None,'',self.target['node']),'Probe must be a local full clone.')
        source=saved.manifest['source']
        require(set(source)=={'image_id','template_id','datastore_id','config_sha256'},'Invalid approved source binding.')
        require(type(source['template_id']) is int and source['template_id'] != self.target['vmid']
                and clones[0].get('vm_id')==source['template_id'],'Approved source/template identity mismatch.')
        image=self.catalog['images'].get(source['image_id'],{})
        require(image.get('kind')=='vm' and image.get('verified') is True
                and (image.get('approved_projects')==['*'] or self.project in image.get('approved_projects',[])),
                'Image is not approved for this project.')
        replicas=[row for row in image.get('replicas',[]) if row.get('node')==self.target['node']
                  and row.get('template_id')==source['template_id'] and row.get('datastore_id')==source['datastore_id']
                  and row.get('state')=='ready' and row.get('artifact_sha256')==image.get('artifact_sha256')]
        require(len(replicas)==1,'Approved source replica is not uniquely ready on this node/storage.')
        self.source=source
        source_config=self.api.request('GET',f'/nodes/{self.target["node"]}/qemu/{source["template_id"]}/config')
        require(source_config.get('template') in (1,'1',True) and not source_config.get('lock'),
                'Approved source must be an unlocked template.')
        require(digest(source_config)==source['config_sha256'],'Approved source config changed.')
        memory=value.get('memory',[])
        cpu=value.get('cpu',[])
        require(len(memory)==1 and len(cpu)==1 and type(memory[0].get('dedicated')) is int
                and 512 <= memory[0]['dedicated'] <= 4096 and type(cpu[0].get('cores')) is int
                and 1 <= cpu[0]['cores'] <= 2 and cpu[0].get('sockets',1)==1,'Probe resource bounds exceeded.')
        disks=value.get('disk',[])
        require(all(row.get('datastore_id')==source['datastore_id'] and type(row.get('size')) in (int,float)
                    and 0 < row['size'] <= 32 for row in disks),'Probe disks must use approved bounded storage.')
        source_disk=sum(scheduling.disk_allocations(source_config).values())
        require(0 < source_disk <= 32,'Approved source exceeds network probe disk budget.')
        self.capacity={'memory_mb':memory[0]['dedicated'],'cpu_cores':cpu[0]['cores'],
                       'disk_gb':max(source_disk,sum(row['size'] for row in disks))*1.1}
        self.routers=self._routers()
        self.router=router_firewall(self.target,self.routers)
        self.management=FixedManagementChecks(self.transport,saved.manifest['origins'],
                                               guac_probe=self._guac_origin_probe)
        self.guest=FixedGuestNetworkChecks(self.transport,self.router,router_node=self.routers['node'],
                                         project_router_node=self.routers.get('project_node'),
                                         other_team_id=saved.manifest['other_team_id'])

    def _project_inputs(self):
        from crange_network_adapters import project_input_hashes
        return project_input_hashes(self.project_path)

    def _guac_origin_probe(self,target,origin,*,timeout):
        require(1 <= timeout <= 60 and origin == self.saved.manifest['origins']['guacamole'],
                'Fixed Guac origin request differs from the protected manifest.')
        command=['/usr/sbin/runuser','-u','crange','--',sys.executable,
                 str(Path(__file__).with_name('crange_network_guac_probe.py')),
                 '--project',str(target['project_id']),'--node',target['node'],
                 '--network',target['vnet'],'--address',target['address'],
                 '--vmid',str(target['vmid']),'--job-id',target['job_id'],
                 '--guac-node',origin['node'], '--relay-source-sha256',self.source_binding['relay_sha256']]
        result=self.run(command,capture_output=True,text=True,timeout=timeout,check=False)
        require(result.returncode==0,'Fixed Guac-origin relay request failed.')
        response=json.loads(result.stdout)
        require(isinstance(response,dict) and response.get('passed') is True and
                response.get('origin')==origin,'Fixed Guac-origin relay response differs.')
        return response

    def _routers(self):
        output=self.run(['terraform','output','-json','routers'],cwd=self.project_path,
                        capture_output=True,text=True,timeout=30,check=False)
        require(output.returncode==0,'Cannot read project router desired-state outputs.')
        routers=json.loads(output.stdout)
        if self.target['vnet'][4] == 'c':
            return self._custom_routers(routers)
        team=int(self.target['vnet'][-3:])
        router=routers['staging'] if team==254 else routers['teams'][str(team)]
        require(router['lan']==str(__import__('ipaddress').ip_interface(self.target['address']).network),
                'Target VNet and desired router LAN differ.')
        node=router['placement']['node_name']
        rows=self.api.request('GET','/cluster/resources',{'type':'vm'})
        matches=[row for row in rows if int(row.get('vmid',0))==router['id']]
        require(len(matches)==1 and matches[0].get('node')==node and matches[0].get('type')=='lxc',
                'Desired team router identity differs from cluster.')
        config=self.api.request('GET',f'/nodes/{node}/lxc/{router["id"]}/config')
        tags=set(config.get('tags','').split(';'))
        require({f'p{self.project:03d}',self.target['vnet']} <= tags,'Team router ownership differs.')
        net=dict(item.split('=',1) for item in config.get('net1','').split(',') if '=' in item)
        gateway=self.target['address'].split('/')[0].rsplit('.',1)[0]+'.1/24'
        require(net.get('ip')==gateway and net.get('bridge')==self.target['vnet'], 'Router downstream interface differs.')
        result = {'project_router':routers['project']['address'],'team_router':router['address'],'node':node}
        if node == self.target['node']:
            project = routers['project']
            parent = [row for row in rows if int(row.get('vmid', 0)) == project['id']]
            require(len(parent) == 1 and parent[0].get('type') == 'lxc' and parent[0].get('status') == 'running',
                    'Project router identity is not live.')
            parent_node = parent[0]['node']
            require(parent_node in {f'proxmox{i}' for i in range(1,7)}, 'Project router node is not enrolled.')
            parent_config = self.api.request('GET', f'/nodes/{parent_node}/lxc/{project["id"]}/config')
            require(f'p{self.project:03d}' in str(parent_config.get('tags', '')).split(';'),
                    'Project router ownership differs.')
            transit = f'p{self.project:03d}tr'
            parent_lan = dict(item.split('=',1) for item in parent_config.get('net1','').split(',') if '=' in item)
            uplink = dict(item.split('=',1) for item in config.get('net0','').split(',') if '=' in item)
            require(parent_lan.get('bridge') == transit and parent_lan.get('ip') == f'10.{self.project}.0.1/24'
                    and uplink.get('bridge') == transit and uplink.get('gw') == f'10.{self.project}.0.1',
                    'Verified routers do not share the declared project transit.')
            result['project_node'] = parent_node
        return result

    def _custom_routers(self, routers):
        from crange import json_config
        from crange_network_worker import declared_router_path
        cfg = json_config(self.project_path)
        chain = declared_router_path(self.project, cfg, routers, self.target['vnet'])
        project, parent, custom = chain
        owner = int(custom['owner_team_id'])
        rows = self.api.request('GET', '/cluster/resources', {'type': 'vm'})
        configs = []
        for index, router in enumerate(chain):
            node = router['placement']['node_name']
            matches = [row for row in rows if int(row.get('vmid', 0)) == router['id']]
            require(node in {f'proxmox{i}' for i in range(1, 7)} and len(matches) == 1
                    and matches[0].get('node') == node and matches[0].get('type') == 'lxc'
                    and matches[0].get('status') == 'running',
                    'Applied custom router chain is not live on its recorded nodes.')
            config = self.api.request('GET', f'/nodes/{node}/lxc/{router["id"]}/config')
            required_tags = {f'p{self.project:03d}'}
            if index:
                required_tags.add(f'p{self.project:03d}t{owner:03d}' if index == 1 else self.target['vnet'])
            require(required_tags <= set(str(config.get('tags', '')).split(';'))
                    and not config.get('template') and not config.get('lock'),
                    'Custom router chain ownership or readiness differs.')
            configs.append({name: dict(part.split('=', 1) for part in str(config.get(name, '')).split(',')
                                      if '=' in part) for name in ('net0', 'net1')})
        transit = f'p{self.project:03d}tr'
        parent_lan = f'p{self.project:03d}t{owner:03d}'
        require(configs[0]['net0'].get('bridge') == 'crtrans'
                and configs[0]['net0'].get('ip') == project['address'] + '/24'
                and configs[0]['net1'].get('bridge') == transit
                and configs[0]['net1'].get('ip') == f'10.{self.project}.0.1/24'
                and configs[1]['net0'].get('bridge') == transit
                and configs[1]['net0'].get('ip') == parent['address'] + '/24'
                and configs[1]['net0'].get('gw') == f'10.{self.project}.0.1'
                and configs[1]['net1'].get('bridge') == parent_lan
                and configs[1]['net1'].get('ip') == f'10.{self.project}.{owner}.1/24'
                and configs[2]['net0'].get('bridge') == parent_lan
                and configs[2]['net0'].get('ip') == custom['address'] + '/24'
                and configs[2]['net0'].get('gw') == f'10.{self.project}.{owner}.1'
                and configs[2]['net1'].get('bridge') == self.target['vnet']
                and configs[2]['net1'].get('ip') == f'10.{self.project}.{custom["network_id"]}.1/24',
                'Custom router chain interfaces differ from declared topology.')
        node = custom['placement']['node_name']
        require(node != self.target['node'],
                'Select another candidate node to verify the custom LAN across VXLAN.')
        return {'project_router': project['address'], 'parent_team_router': parent['address'],
                'team_router': custom['address'], 'node': node}

    def _live_addresses(self):
        addresses=[]
        rows=self.api.request('GET','/cluster/resources',{'type':'vm'})
        for row in rows:
            config=self.api.request('GET',f'/nodes/{row["node"]}/{row["type"]}/{int(row["vmid"])}/config')
            for key,value in config.items():
                if re.fullmatch(r'(?:net|ipconfig)\d+',key):
                    for field in str(value).split(','):
                        if field.startswith('ip=') and field[3:] not in ('dhcp','manual'):
                            addresses.append(field[3:].split('/')[0])
        # Project-defined but not yet deployed addresses must also remain reserved.
        from crange import json_config
        cfg=json_config(self.project_path)
        for field in ('jump_boxes','scenario_boxes','scenario_containers'):
            for box in cfg.get(field,{}).values():
                team=int(box.get('network_id',box['team_id']))
                host=10+int(box['jump_number']) if field=='jump_boxes' else int(box['ip_last_octet'])
                addresses.append(f'10.{self.project}.{team}.{host}')
        return sorted(set(addresses))

    def vmid_absent(self,target):
        require(target==self.target,'Probe scope differs.')
        rows=self.api.request('GET','/cluster/resources',{'type':'vm'})
        return not any(int(row.get('vmid',0))==target['vmid'] for row in rows)

    def reserve(self,target):
        require(target==self.target,'Probe scope differs.')
        require(self.saved.state_addresses()==[],'Standalone state must be empty before probe creation.')
        inventory=scheduling.discover(self.api,self.policy)
        inventory['declared_addresses']=self._live_addresses()
        allocation=self.ledger.reserve_network_probe(target=target,owner='controller-network',
            source_ref=f'source:{target["node"]}:qemu:{self.source["template_id"]}',
            source_sha256=self.source['config_sha256'],datastore_id=self.source['datastore_id'],
            capacity=self.capacity,inventory=inventory,policy=self.policy)
        self.ledger.mark_running(target['job_id'])
        durable_json(self.root/'reservation.json',allocation)

    def create_plan(self,target):return self.saved.inspect('create')
    def destroy_plan(self,target):
        require(cluster_probe(self.api,target) is not None,'Owned probe must exist before planning destroy.')
        return self.saved.generate_delete_plan()
    def reviewed_plan(self,target,operation,sha):
        return target==self.target and plan_digest(self.saved.inspect(operation))==sha
    def apply_saved_plan(self,target,operation,sha,timeout):
        allocations=self.ledger.validate_job(target['job_id'])
        require(len(allocations)==1 and allocations[0].get('operation')=='network_probe'
                and allocations[0].get('network_scope')==target
                and allocations[0].get('artifact_sha256')==self.source['config_sha256'],
                'Shared reservation ownership differs.')
        if operation=='create':
            policy,_=scheduling.read_settings()
            require(policy.get('revision')==allocations[0]['policy_revision']
                    and target['node'] in policy.get('allowed_nodes',[]), 'Admission policy changed before apply.')
            config=self.api.request('GET',f'/nodes/{target["node"]}/qemu/{self.source["template_id"]}/config')
            require(digest(config)==self.source['config_sha256'],'Source changed before apply.')
            require(self.input_unchanged(target),'Project inputs changed before apply.')
            require(self.vmid_absent(target),'Probe VMID changed before apply.')
        else:require(cluster_probe(self.api,target) is not None,'Destroy target no longer has exact ownership.')
        self.saved.apply_saved_plan(target,operation,sha,timeout)
        if operation=='create':require(cluster_probe(self.api,target) is not None,'Created probe differs from reviewed scope.')
    def install_management_rule(self,target,rule):self.router.install_management_rule(target,rule)
    def remove_management_rule(self,target,rule):self.router.remove_management_rule(target,rule)
    def temporary_rules_absent(self,target):return self.router.temporary_rules_absent(target)
    def probe_state_empty(self,target):return self.saved.state_addresses()==[]
    def input_unchanged(self,target):
        return (self._project_inputs()==self.inputs and
                controller_source_hashes()==self.source_binding['controller_sha256'] and
                trusted_relay_source_hash()==self.source_binding['relay_sha256'])

    def run_check(self,target,check,timeout):
        require(check in __import__('crange_network_acceptance').CHECKS,'Unsupported evidence check.')
        try:
            if check == 'vxlan_mtu':
                require(self._routers() == self.routers, 'Router topology changed before MTU evidence.')
            result=(self.management if check in ('controller','guacamole') else self.guest).run(target,check,timeout=timeout)
            if check == 'vxlan_mtu':
                require(self._routers() == self.routers, 'Router topology changed during MTU evidence.')
        except Exception as error:
            failed=getattr(error,'network_check_log',None)
            if isinstance(failed,dict):
                durable_json(self.root/(check+'.failed.log'),failed)
            raise
        file=self.root/(check+'.log')
        require(not file.is_symlink(),'Evidence artifact cannot be a symlink.')
        durable_json(file,result)
        summary={'passed':result['passed'],'artifact_sha256':hashlib.sha256(file.read_bytes()).hexdigest()}
        if check=='guacamole':
            require(result.get('relay_sha256')==self.source_binding['relay_sha256'],'Actual relay code differs from reviewed deployment.')
            summary.update({key:result[key] for key in ('source_address','target_address','target_port','relay_sha256')})
        return summary

    def cleanup_artifact(self,target):
        report={'scope':target,'vmid_absent_after':self.vmid_absent(target),
                'state_empty':self.probe_state_empty(target),'rules_absent':self.temporary_rules_absent(target),
                'input_unchanged':self.input_unchanged(target)}
        durable_json(self.root/'cleanup.log',report)
        return hashlib.sha256((self.root/'cleanup.log').read_bytes()).hexdigest()
    def release_reservation(self,target):
        require(self.vmid_absent(target) and self.probe_state_empty(target) and self.temporary_rules_absent(target),
                'Reservation cannot release before cleanup.')
        require(no_active_node_tasks(self.api, target['node']),
                'Node has active tasks; retain reservation until independent task reconciliation.')
        self.ledger.reconcile_job(target['job_id'],operation_terminal=True,allocations_reconciled=True)


def reconcile_unadmitted(saved, api, router, ledger):
    """Read-only absence proof for a journal whose admission never committed."""
    journal = json.loads(protected(saved.root / 'journal.json').read_text())
    if journal.get('state') not in ('reserving', 'reservation_uncertain'):
        return False
    require(journal.get('scope') == saved.target, 'Journal and manifest ownership differ.')
    rule = {'owner': saved.target['job_id'], 'sources': ['10.60.0.20', '10.60.0.10'],
            'destination': saved.target['address'].split('/')[0] + '/32',
            'protocol': 'tcp', 'port': 22}
    require(journal.get('rule') == rule, 'Journal management rule identity mismatch.')
    ledger.db.execute('BEGIN IMMEDIATE')
    try:
        row = ledger.db.execute('SELECT state FROM jobs WHERE job_id=?',
                                (saved.target['job_id'],)).fetchone()
        if row is not None:
            return False
        require(cluster_probe(api, saved.target) is None, 'Unadmitted probe guest is present.')
        require(saved.state_addresses() == [], 'Unadmitted probe state is present.')
        require(router.temporary_rules_absent(saved.target) is True, 'Unadmitted probe rules remain.')
        require(no_active_node_tasks(api, saved.target['node']), 'Node tasks active; retain journal.')
        from crange import project_dir
        from crange_network_adapters import project_input_hashes
        require(project_input_hashes(project_dir(str(saved.target['project_id']))) ==
                saved.manifest['project_input_sha256'], 'Project inputs changed during unadmitted probe.')
        proof = {'vmid_absent': True, 'state_empty': True, 'rules_absent': True,
                 'reservation_absent': True, 'input_unchanged': True}
        durable_json(saved.root / 'unadmitted-cleanup.json', proof)
        journal.update(state='failed_cleaned', unadmitted_cleanup=proof)
        durable_json(saved.root / 'journal.json', journal)
        return True
    finally:
        ledger.db.rollback()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('preflight','run','reconcile'))
    parser.add_argument('job_id')
    args=parser.parse_args()
    ledger=None
    try:
        require(os.geteuid()==0,'Root-only controller workflow.')
        from crange import ProxmoxAPI
        scheduling.CONFIG=Path('/home/crange/.config/crange')
        scheduling.DATA=Path('/var/lib/crange-distribution')
        saved=SavedTerraformProbe(args.job_id)
        api=ProxmoxAPI()
        with scheduling.node_locks([saved.target['node']],library=True):
            if args.operation != 'preflight':
                ledger=ReservationLedger(scheduling.DATA/'reservations.sqlite3')
            if args.operation=='reconcile':
                routers=saved.manifest['routers']
                router=router_firewall(saved.target,routers)
                if reconcile_unadmitted(saved,api,router,ledger):
                    print(json.dumps({'cleanup_verified':True,'reservation_absent':True}))
                    return
                result=reconcile_owned_probe(saved,api,router)
                allocations=ledger.validate_job(saved.target['job_id'])
                require(len(allocations)==1 and allocations[0].get('operation')=='network_probe'
                        and allocations[0].get('network_scope')==saved.target, 'Reconcile reservation owner differs.')
                require(no_active_node_tasks(api, saved.target['node']),
                        'Node tasks still active; retain reconciled reservation.')
                ledger.reconcile_job(saved.target['job_id'],operation_terminal=True,allocations_reconciled=True)
                journal=json.loads((saved.root/'journal.json').read_text())
                journal['state']='failed_cleaned'
                durable_json(saved.root/'journal.json',journal)
                result['reservation_released']=True
                print(json.dumps(result,sort_keys=True))
                return
            backend=NetworkAcceptanceBackend(saved,api,ledger)
            if args.operation=='preflight':
                require(backend.vmid_absent(saved.target),'Requested probe VMID is in use.')
                require(backend.saved.state_addresses()==[],'Standalone probe state is not empty.')
                require(saved.target['address'].split('/')[0] not in backend._live_addresses(),'Requested probe IP is allocated.')
                backend.router._status()
                counter=drop_counter(backend.router.observe('firewall_counters'))
                print(json.dumps({'scope':saved.target,'capacity':backend.capacity,'routers':backend.routers,
                                  'router_drop_counter':counter,
                                  'mutated':False,'policy_changed':False},sort_keys=True))
            elif args.operation=='run':
                require(re.fullmatch(r'[a-f0-9]{40}',saved.manifest.get('source_revision','')),'Bind exact reviewed source revision.')
                saved.manifest['routers']=backend.routers
                durable_json(saved.root/'review.json',saved.manifest)
                receipt=NetworkProbeRunner(backend,saved.root/'journal.json').run(saved.target,
                    source_binding=backend.source_binding,
                    source_revision=saved.manifest['source_revision'],tested_at=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    cancellation_requested=cancellation_requested,
                    progress=lambda message: print(message, flush=True),
                    diagnostic=lambda error: _persist_workflow_error(args.job_id, error))
                durable_json(saved.root/'create-plan.json',backend.plan)
                durable_json(saved.root/'receipt.json',receipt)
                validate_bundle(saved.root)
                print(json.dumps({'scope':saved.target,'accepted_cleaned':True,'policy_changed':False},sort_keys=True))
    except Exception as error:
        _persist_workflow_error(args.job_id,error)
        parser.exit(1,'Fixed network workflow failed. Inspect the protected owned journal; never retry allocation blindly.\n')
    finally:
        if ledger is not None:ledger.close()


if __name__=='__main__':main()
