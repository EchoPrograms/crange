"""Identify allocated guests in Terraform output maps after targeted plans.

Unapplied declarations may have computed addresses but a null guest ID. They
must not become Ansible targets, firewall endpoints, or Guacamole connections.
"""
import copy
import json
import re


def has_guest_id(value):
    return (isinstance(value, dict) and type(value.get('id')) is int
            and 100 <= value['id'] <= 999999999)


def complete_metadata(outputs, desired, applied, *, state=None):
    """Fill missing computed metadata only for the exact saved guest generation.

    Targeted Terraform plans can omit placement input overrides for unchanged
    guests. Explicit conflicts remain visible to the callers' identity checks.
    """
    if not all(isinstance(value, dict) for value in (outputs, desired, applied)):
        raise ValueError('Applied guest metadata must be mappings.')
    result = copy.deepcopy(outputs)
    fields = ('image_id', 'node_name', 'template_id', 'template_node', 'datastore_id',
              'clone_mode', 'template_file_id')
    for field, kind in (('scenario_boxes', 'vm'), ('scenario_containers', 'container')):
        for key, guest in (result.get(field, {}).get('value') or {}).items():
            box = desired.get(field, {}).get(key, {})
            record = applied.get(f'{kind}:{key}', {})
            if (not isinstance(record, dict) or not has_guest_id(guest) or type(record.get('guest_id')) is not int
                    or record['guest_id'] != guest['id'] or not box.get('template')
                    or record.get('declared_template') != box['template']
                    or (box.get('image') and box['image'] != record.get('image_id'))):
                continue
            placement = guest.get('placement') or {}
            if not isinstance(placement, dict):
                continue
            for name in fields:
                if placement.get(name) in (None, '') and record.get(name) not in (None, ''):
                    placement[name] = record[name]
            guest['placement'] = placement
    if state is not None:
        complete_custom_router_metadata(result,desired,applied,state)
    return result


def complete_custom_router_metadata(outputs,desired,applied,state):
    """Targeted foundations may allocate a router without evaluating its map output.

    Desired declarations alone never create an inventory host. Require the same
    allocated LXC generation in state and the worker's applied receipt, with its
    complete project/network identity, before filling missing output metadata.
    """
    project=desired.get('project_id')
    if (type(project) is not int or not 1<=project<=255 or not isinstance(state,dict) or
            state.get('version')!=4 or not state.get('lineage') or
            outputs.get('project_id',{}).get('value')!=project):
        return
    routers=outputs.get('routers',{}).get('value')
    if not isinstance(routers,dict):return
    custom=routers.setdefault('custom_networks',{})
    if not isinstance(custom,dict):raise ValueError('Applied custom router output must be a mapping.')
    for key,net in desired.get('custom_networks',{}).items():
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',key):continue
        record=applied.get('router:custom:'+key,{})
        module='module.custom_router['+json.dumps(key)+']'
        resources=[r for r in state.get('resources',[]) if r.get('mode')=='managed' and
            r.get('module')==module and r.get('type')=='proxmox_virtual_environment_container' and r.get('name')=='router']
        if len(resources)!=1 or len(resources[0].get('instances',[]))!=1:continue
        instance=resources[0]['instances'][0];attrs=instance.get('attributes') or {}
        vmid=attrs.get('vm_id');owner=str(net.get('owner_team_id'));number=net.get('network_id');uplink=net.get('uplink_last_octet')
        if (instance.get('deposed') or instance.get('status')=='tainted' or type(vmid) is not int or
                not 100<=vmid<=999999999 or str(attrs.get('id'))!=str(vmid) or record.get('guest_id')!=vmid or
                record.get('node_name')!=attrs.get('node_name') or not re.fullmatch(r'proxmox[1-6]',str(attrs.get('node_name'))) or
                not record.get('image_id') or attrs.get('pool_id')!='crange-iac' or attrs.get('unprivileged') is not True or
                attrs.get('template') or owner not in {str(t) for t in desired.get('team_ids',[])}|{'254'} or
                type(number) is not int or not 1<=number<=253 or type(uplink) is not int or not 2<=uplink<=100):
            continue
        vnet=f'p{project:03d}c{number:03d}';parent=f'p{project:03d}t{int(owner):03d}'
        address=f'10.{project}.{owner}.{uplink}';gateway=f'10.{project}.{owner}.1'
        interfaces=attrs.get('network_interface') or [];initialization=attrs.get('initialization') or []
        disks=attrs.get('disk') or [];clone=attrs.get('clone') or []
        if (len(disks)!=1 or disks[0].get('datastore_id')!=record.get('datastore_id') or
                (clone and (len(clone)!=1 or clone[0].get('full') is not True)) or
                not {f'p{project:03d}',vnet,'terraform','network-infrastructure','range-lan'}<=set(attrs.get('tags') or []) or
                len(interfaces)!=2 or interfaces[0].get('bridge')!=parent or interfaces[1].get('bridge')!=vnet or
                len(initialization)!=1 or initialization[0].get('hostname')!=f'p{project:03d}-custom-{key}-router'):
            continue
        ips=initialization[0].get('ip_config') or []
        first=(ips[0].get('ipv4') or [{}])[0] if len(ips)==2 else {}
        second=(ips[1].get('ipv4') or [{}])[0] if len(ips)==2 else {}
        if (first.get('address')!=address+'/24' or first.get('gateway')!=gateway or
                second.get('address')!=f'10.{project}.{number}.1/24'):
            continue
        expected={'id':vmid,'address':address,'upstream_address':address,
                  'lan':f'10.{project}.{number}.0/24','owner_team_id':owner,'network_id':number,'vnet':vnet}
        row=custom.setdefault(key,{})
        if not isinstance(row,dict):raise ValueError('Applied custom router entry must be a mapping.')
        for field,value in expected.items():
            present=row.get(field)
            if present not in (None,'') and str(present)!=str(value):
                raise ValueError('Custom router output differs from its saved applied identity.')
            if present in (None,''):row[field]=value
        placement=row.setdefault('placement',{})
        if isinstance(placement,dict):
            for field in ('node_name','datastore_id','image_id','clone_mode','template_id','template_node','template_file_id'):
                if placement.get(field) in (None,'') and record.get(field) not in (None,''):
                    placement[field]=record[field]
