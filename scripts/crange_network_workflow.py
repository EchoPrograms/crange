"""Root-only fixed network acceptance workflow. No HTTP/queue operation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from datetime import datetime, timezone

from crange_network_acceptance import require, probe_from_plan, validate_bundle
from crange_network_adapters import (SavedTerraformProbe, BoundedPVE, FixedRouterUFW,
    FixedManagementChecks, cluster_probe, durable_json, protected, reconcile_owned_probe)
from crange_network_checks import FixedGuestNetworkChecks, drop_counter
from crange_network_runner import NetworkProbeRunner, plan_digest
from crange_placement import ReservationLedger
import crange_scheduling as scheduling


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


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
        self.router=FixedRouterUFW(self.target,project_router=self.routers['project_router'],
                                   team_router=self.routers['team_router'])
        self.management=FixedManagementChecks(self.transport,saved.manifest['origins'])
        self.guest=FixedGuestNetworkChecks(self.transport,self.router,router_node=self.routers['node'],
                                         other_team_id=saved.manifest['other_team_id'])

    def _project_inputs(self):
        files={path.name:path for path in self.project_path.iterdir()
               if path.name.endswith(('.tf','.tf.json','.tfvars','.tfvars.json')) or path.name=='.terraform.lock.hcl'}
        require(bool(files),'Project desired-state inputs missing.')
        return {name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in sorted(files.items())}

    def _routers(self):
        output=self.run(['terraform','output','-json','routers'],cwd=self.project_path,
                        capture_output=True,text=True,timeout=30,check=False)
        require(output.returncode==0,'Cannot read project router desired-state outputs.')
        routers=json.loads(output.stdout)
        require(self.target['vnet'][4]=='t','Custom-router acceptance needs a separate routed-origin adapter.')
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
        return {'project_router':routers['project']['address'],'team_router':router['address'],'node':node}

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
    def input_unchanged(self,target):return self._project_inputs()==self.inputs

    def run_check(self,target,check,timeout):
        result=(self.management if check in ('controller','guacamole') else self.guest).run(target,check,timeout=timeout)
        file=self.root/(check+'.log')
        require(not file.is_symlink(),'Evidence artifact cannot be a symlink.')
        durable_json(file,result)
        return {'passed':result['passed'],'artifact_sha256':hashlib.sha256(file.read_bytes()).hexdigest()}

    def cleanup_artifact(self,target):
        report={'scope':target,'vmid_absent_after':self.vmid_absent(target),
                'state_empty':self.probe_state_empty(target),'rules_absent':self.temporary_rules_absent(target),
                'input_unchanged':self.input_unchanged(target)}
        durable_json(self.root/'cleanup.log',report)
        return hashlib.sha256((self.root/'cleanup.log').read_bytes()).hexdigest()
    def release_reservation(self,target):
        require(self.vmid_absent(target) and self.probe_state_empty(target) and self.temporary_rules_absent(target),
                'Reservation cannot release before cleanup.')
        require(not self.api.request('GET',f'/nodes/{target["node"]}/tasks',{'running':1}),
                'Node has active tasks; retain reservation until independent task reconciliation.')
        self.ledger.reconcile_job(target['job_id'],operation_terminal=True,allocations_reconciled=True)


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
                router=FixedRouterUFW(saved.target,project_router=routers['project_router'],team_router=routers['team_router'])
                result=reconcile_owned_probe(saved,api,router)
                allocations=ledger.validate_job(saved.target['job_id'])
                require(len(allocations)==1 and allocations[0].get('operation')=='network_probe'
                        and allocations[0].get('network_scope')==saved.target, 'Reconcile reservation owner differs.')
                require(not api.request('GET',f'/nodes/{saved.target["node"]}/tasks',{'running':1}),
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
                    source_revision=saved.manifest['source_revision'],tested_at=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
                durable_json(saved.root/'create-plan.json',backend.plan)
                durable_json(saved.root/'receipt.json',receipt)
                validate_bundle(saved.root)
                print(json.dumps({'scope':saved.target,'accepted_cleaned':True,'policy_changed':False},sort_keys=True))
    except Exception:
        parser.exit(1,'Fixed network workflow failed. Inspect the protected owned journal; never retry allocation blindly.\n')
    finally:
        if ledger is not None:ledger.close()


if __name__=='__main__':main()
