"""Validated jump-box roles and remote services, independent of guest identity."""
from collections import defaultdict

FIELDS = {'jumpbox': 'jump_boxes', 'vm': 'scenario_boxes', 'container': 'scenario_containers'}
PORTS = {'ssh': 22, 'rdp': 3389, 'vnc': 5900}


def services(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 3:
        raise ValueError('Select one to three remote services.')
    if any(not isinstance(item, str) or item not in PORTS for item in value):
        raise ValueError('Remote services must be SSH, RDP or VNC.')
    if len(set(value)) != len(value):
        raise ValueError('Select each remote service once.')
    return value


def settings(kind, display_name, is_jumpbox, protocols):
    if kind not in FIELDS:
        raise ValueError('Choose a declared VM, container or jump box.')
    if not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 80 or any(ord(c) < 32 for c in display_name):
        raise ValueError('Box display name must have 1–80 characters on one line.')
    if not isinstance(is_jumpbox, bool):
        raise ValueError('Invalid jump-box role.')
    if kind == 'jumpbox' and not is_jumpbox:
        raise ValueError('An existing dedicated jump box retains its jump-box role.')
    return {'display_name': display_name.strip(), 'is_jumpbox': is_jumpbox,
            'guac_services': services(protocols)}


def effective_services(box, kind):
    return services(box.get('guac_services', ['rdp' if kind == 'jumpbox' else box.get('guac_protocol', 'ssh')]))


def connection_records(project, kind, key, box, address, default_for_team=False):
    """Keep the original primary connection name while adding extra services."""
    label = {'jumpbox': '', 'vm': 'VM · ', 'container': 'CT · '}[kind]
    title = box.get('display_name') or key
    # Releases and per-user setups can reuse display names in different teams.
    # Keep legacy names unchanged, but scope custom display names to the team.
    team_label = f"Team {0 if int(box['team_id']) == 254 else int(box['team_id'])} · " if box.get('display_name') else ''
    base = f'P{project:03d} · {team_label}{label}{title}'
    return [{'key': f'{kind}:{key}', 'name': base if index == 0 else f'{base} · {protocol.upper()}',
             'resource': key, 'kind': kind, 'team_id': str(box['team_id']),
             'network_id': int(box.get('network_id', box['team_id'])),
             'project_id': int(project), 'credential_ref': box.get('guac_credentials'),
             'address': address, 'protocol': protocol, 'port': PORTS[protocol],
             'is_jumpbox': kind == 'jumpbox' or box.get('is_jumpbox', False),
             'default_for_team': default_for_team}
            for index, protocol in enumerate(effective_services(box, kind))]


def resource_identifiers(connections):
    """One assigned box grants all of its current service connections."""
    result = defaultdict(set)
    for item in connections:
        identifier = str(item['identifier'])
        for key in (item.get('resource_key'), item.get('key'), item.get('name')):
            if key:
                result[str(key)].add(identifier)
    return result


def router_path(project, live, groups):
    """Configure this endpoint's router path without targeting other teams."""
    team = int(live['team_id'])
    network = int(live.get('network_id', team))
    parent = f'p{project:03d}t{team:03d}_router'
    parents = groups['staging_routers' if team == 254 else 'team_routers'].get('hosts', {})
    if parent not in parents:
        raise ValueError('The deployed box’s team router is missing from inventory.')
    hosts = list(groups['project_routers'].get('hosts', {})) + [parent]
    if network != team:
        custom = [name for name, values in groups.get('custom_routers', {}).get('hosts', {}).items()
                  if int(values.get('router_owner_team_id', -1)) == team
                  and values.get('router_team_network') == f'10.{project}.{network}.0/24']
        if len(custom) != 1:
            raise ValueError('The deployed box’s custom router is missing or ambiguous.')
        hosts += custom
    return hosts


def creation(value):
    import re
    from crange_templates import integer
    if isinstance(value,dict):value={'preset_key':'','node':'auto','preferred_node':'proxmox4' if value.get('node','auto') == 'auto' else '',**value}
    required={'preset_key','kind','name','team','template','node','preferred_node','network','cores','memory','ip','display_name','services','credential_ref','sealed_credentials'}
    if not isinstance(value,dict) or set(value)!=required:raise ValueError('Unexpected or missing jump-box settings.')
    if value['kind'] not in ('vm','container'):raise ValueError('Choose a VM or container jump box.')
    if not isinstance(value['name'],str) or not re.fullmatch('[a-z][a-z0-9-]{0,23}',value['name']):raise ValueError('Use a lowercase jump-box name, up to 24 characters.')
    integer(value['team'],0,253,'Team');integer(value['cores'],1,16,'CPU cores');integer(value['memory'],512,65536,'Memory MB')
    integer(value['ip'],0,100,'Host address');
    if value['ip']==1:raise ValueError('Host address .1 is reserved for the router.')
    from crange_scheduling import placement_intent
    placement_intent(value['node'],value['preferred_node'])
    for field in ('template','network','preset_key'):
        if not isinstance(value[field],str) or (not value[field] and field=='template') or (value[field] and not re.fullmatch('[a-z][a-z0-9_.-]{0,63}',value[field])):raise ValueError('Choose an approved template and project network.')
    settings(value['kind'],value['display_name'],True,value['services'])
    if not isinstance(value['credential_ref'],str) or not isinstance(value['sealed_credentials'],str):raise ValueError('Invalid protected credentials.')
    if bool(value['credential_ref'])!=bool(value['sealed_credentials']):raise ValueError('Custom credentials require a protected record.')
    if value['credential_ref'] and not re.fullmatch('[0-9a-f]{32}',value['credential_ref']):raise ValueError('Invalid credential reference.')
    if len(value['sealed_credentials'])>8192 or not re.fullmatch('[A-Za-z0-9_=-]*',value['sealed_credentials']):raise ValueError('Invalid protected guest credentials.')
    return value


def create(project,definition):
    from types import SimpleNamespace
    from crange import project_dir,json_config,guest_network_id,cmd_scenario_add,cmd_container_add,save_config,templates_for
    value=creation(definition);path=project_dir(str(project));original=json_config(path)
    if value['preset_key']:
        from crange import container_templates_for, template_identity
        preset=presets(project).get(value['preset_key'])
        if not preset or (value['kind'],value['template'])!=(preset['kind'],preset['template']):
            raise ValueError('The selected jump-box preset does not match this base image.')
        approved=(templates_for if value['kind']=='vm' else container_templates_for)(path)
        if preset['source']!=template_identity(approved.get(value['template'],{})):
            raise ValueError('The preset base image changed; review and save this preset again.')
    team='254' if value['team']==0 else str(value['team']);network=guest_network_id(original,team,value['network'])
    taken={1}
    for kind,field in FIELDS.items():
        for box in original.get(field,{}).values():
            if int(box.get('network_id',box['team_id']))==network:taken.add(10+int(box['jump_number']) if kind=='jumpbox' else int(box['ip_last_octet']))
    if network == int(team):
        taken.update(int(n['uplink_last_octet']) for n in original.get('custom_networks',{}).values() if str(n['owner_team_id'])==team)
    ip=value['ip'] or next((i for i in range(2,101) if i not in taken),None)
    if ip is None or ip in taken:raise ValueError('No free static address, or the selected address is already assigned.')
    key=f't{int(team):03d}-{value["name"]}'
    if any(key in original.get(field,{}) for field in FIELDS.values()):raise ValueError('A box with this name already exists in this team.')
    names=[b.get('display_name') for field in FIELDS.values() for b in original.get(field,{}).values() if str(b['team_id'])==team]
    if value['display_name'].strip() in names:raise ValueError('This team already has a box with that display name.')
    args=SimpleNamespace(project=str(project),name=value['name'],team=value['team'],ip=ip,template=value['template'],node=value['node'],preferred_node=value['preferred_node'],
                         network=value['network'],cores=value['cores'],memory=value['memory'],full_clone=None,health='[]',guac_protocol=value['services'][0])
    try:
        (cmd_scenario_add if value['kind']=='vm' else cmd_container_add)(args)
        cfg=json_config(path);box=cfg[FIELDS[value['kind']]][key]
        box.update(settings(value['kind'],value['display_name'],True,value['services']))
        if value['credential_ref']:
            from crange_guest_credentials import install
            install(int(project),value['credential_ref'],value['sealed_credentials']);box['guac_credentials']=value['credential_ref']
        save_config(path,cfg)
    except Exception:
        save_config(path,original);raise
    print(f'Custom jump box {key} declared at 10.{project}.{network}.{ip}. Stage and review changes before deploying.')


def presets(project):
    import json
    from crange import project_dir
    path=project_dir(str(project))/'jumpbox-templates.json'
    return json.loads(path.read_text()) if path.exists() else {}


def preset_settings(value):
    if isinstance(value,dict):value={'node':'auto','preferred_node':'proxmox4' if value.get('node','auto') == 'auto' else '',**value}
    if not isinstance(value,dict) or set(value)!={'key','display_name','kind','template','node','preferred_node','cores','memory','services'}:raise ValueError('Invalid jump-box template settings.')
    creation({'kind':value['kind'],'name':value['key'],'display_name':value['display_name'],'team':0,'template':value['template'],'node':value['node'],'preferred_node':value['preferred_node'],
              'network':'','cores':value['cores'],'memory':value['memory'],'services':value['services'],'ip':0,'credential_ref':'','sealed_credentials':''})
    return value


def save_preset(project,value):
    import json
    from crange import project_dir,templates_for,container_templates_for,template_identity
    value=preset_settings(value);path=project_dir(str(project));approved=(templates_for if value['kind']=='vm' else container_templates_for)(path,creation=True)
    if value['template'] not in approved:raise ValueError('Choose a template approved for this project.')
    data=presets(project)
    if value['key'] not in data and len(data)>=20:raise ValueError('This project has reached its 20 jump-box template limit.')
    data[value['key']]={**value,'source':template_identity(approved[value['template']])}
    file=path/'jumpbox-templates.json';temp=file.with_suffix('.tmp');temp.write_text(json.dumps(data,indent=2)+'\n');temp.replace(file)
    print('Saved project jump-box template '+value['display_name']+'. Existing boxes were unchanged.')


def remove_preset(project,key):
    import json
    from crange import project_dir
    data=presets(project)
    if key not in data:raise ValueError('Jump-box template does not exist in this project.')
    del data[key];file=project_dir(str(project))/'jumpbox-templates.json';temp=file.with_suffix('.tmp');temp.write_text(json.dumps(data,indent=2)+'\n');temp.replace(file)
    print('Removed jump-box template '+key+'. Existing boxes and source images were retained.')
