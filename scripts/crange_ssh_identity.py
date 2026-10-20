"""Refresh cached SSH identities only for owned guests created by an applied plan."""
import ipaddress
import json
from pathlib import Path
import re
import subprocess


def refresh_created_guests(path, network_id, *, api=None, output=None, home=None, runner=subprocess.run, resource_keys=None):
    from crange import ProxmoxAPI, output_json
    from crange_scheduling import atomic_json
    project = int(path.name.split('-')[-1])
    home = home or Path.home()
    directory = home / '.local/share/crange/plans' / f'project-{project:03d}'
    records = []
    for manifest in directory.glob('*.json'):
        record = json.loads(manifest.read_text())
        if record.get('project') == project and record.get('applied_at'):
            records.append((record['applied_at'], manifest, record))
    if not records:
        return []
    _, manifest, record = max(records, key=lambda row: row[0])
    output = output if output is not None else output_json(path)
    applied = json.loads((path / 'crange-applied-placements.json').read_text())
    api = api or ProxmoxAPI()
    refreshed = set(record.get('ssh_refreshed_resources', []))
    selected = []
    for change in record.get('changes', []):
        if 'create' not in change['actions'] or change['address'] in refreshed:
            continue
        router_match = re.fullmatch(r'module\.custom_router\["([a-z0-9_-]+)"\]\..+', change['address'])
        if router_match:
            key = router_match[1]
            if resource_keys is not None and 'router:custom:' + key not in resource_keys:
                continue
            router = output.get('routers', {}).get('value', {}).get('custom_networks', {}).get(key)
            if not router or int(router.get('owner_team_id', -1)) != network_id:
                continue
            placement = applied.get('router:custom:' + key, {})
            node, guest_id = placement.get('node_name'), placement.get('guest_id')
            if guest_id != router.get('id') or not re.fullmatch(r'proxmox[1-6]', str(node)):
                raise ValueError('Applied router identity differs from the SSH refresh scope.')
            number = router.get('network_id')
            if type(number) is not int or not 1 <= number <= 253:
                raise ValueError('Applied router network differs from the SSH refresh scope.')
            parent_vnet, vnet = f'p{project:03d}t{network_id:03d}', f'p{project:03d}c{number:03d}'
            address = ipaddress.ip_address(router['address'])
            config = api.request('GET', f'/nodes/{node}/lxc/{guest_id}/config')
            tags = set(str(config.get('tags', '')).split(';'))
            nics = [dict(part.split('=', 1) for part in str(config.get(name, '')).split(',')
                         if '=' in part) for name in ('net0', 'net1')]
            if (address not in ipaddress.ip_network(f'10.{project}.{network_id}.0/24')
                    or not 2 <= int(str(address).split('.')[-1]) <= 100
                    or router.get('upstream_address') != str(address) or router.get('vnet') != vnet
                    or not {f'p{project:03d}', vnet} <= tags or int(config.get('template', 0))
                    or nics[0].get('bridge') != parent_vnet or nics[1].get('bridge') != vnet
                    or nics[0].get('ip') != f'{address}/24'
                    or nics[1].get('ip') != f'10.{project}.{number}.1/24'):
                raise ValueError('Live router ownership or interfaces differ from the SSH refresh scope.')
            selected.append((change['address'], str(address)))
            continue
        match = re.fullmatch(r'module\.(scenario_container|scenario_box|jumpbox)\["([a-z0-9_-]+)"\]\..+', change['address'])
        if not match:
            continue
        module, key = match.groups()
        field, kind, prefix = {'scenario_container': ('scenario_containers', 'lxc', 'container'),
                               'scenario_box': ('scenario_boxes', 'qemu', 'vm'),
                               'jumpbox': ('jump_boxes', 'qemu', 'jumpbox')}[module]
        if resource_keys is not None and prefix + ':' + key not in resource_keys:
            continue
        guest = output.get(field, {}).get('value', {}).get(key)
        if not guest or int(guest['team_id']) != network_id:
            continue
        placement = applied.get(prefix + ':' + key, {})
        node, guest_id = placement.get('node_name'), placement.get('guest_id')
        if guest_id != guest['id'] or not re.fullmatch(r'proxmox[1-6]', str(node)):
            raise ValueError('Applied guest identity differs from the SSH refresh scope.')
        address = str(ipaddress.ip_address(guest['address']))
        subnet = ipaddress.ip_network(f'10.{project}.{int(guest.get("network_id", network_id))}.0/24')
        vnet = guest.get('network_vnet') or f'p{project:03d}t{network_id:03d}'
        config = api.request('GET', f'/nodes/{node}/{kind}/{guest_id}/config')
        tags = str(config.get('tags', '')).split(';')
        nic = dict(part.split('=', 1) for part in str(config.get('net0', '')).split(',') if '=' in part)
        if (ipaddress.ip_address(address) not in subnet or f'p{project:03d}' not in tags
                or vnet not in tags or nic.get('bridge') != vnet or int(config.get('template', 0))):
            raise ValueError('Live guest network or ownership differs from the SSH refresh scope.')
        if kind == 'lxc' and ipaddress.ip_interface(nic.get('ip', '0.0.0.0/32')).ip != ipaddress.ip_address(address):
            raise ValueError('Live container address differs from its applied SSH identity.')
        selected.append((change['address'], address))
    known_hosts = home / '.ssh/known_hosts'
    for resource, address in selected:
        if known_hosts.exists():
            runner(['ssh-keygen', '-f', str(known_hosts), '-R', address], check=True, capture_output=True, text=True)
        refreshed.add(resource)
        record['ssh_refreshed_resources'] = sorted(refreshed)
        atomic_json(manifest, record, 0o600)
    return [address for _, address in selected]
