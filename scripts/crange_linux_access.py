"""Provision the declared Guacamole login on applied Linux containers."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def access_inventory(cfg, inventory, *, team=None, parameters=None):
    from crange_guac import guest_parameters
    from crange_jumpboxes import connection_records, effective_services
    parameters = parameters or guest_parameters
    group = inventory.get('all', {}).get('children', {}).get('scenario_containers', {})
    selected = {}
    addresses = set()
    for host, values in group.get('hosts', {}).items():
        key = values.get('crange_box_key')
        box = cfg.get('scenario_containers', {}).get(key)
        if not box or (team is not None and str(box['team_id']) != str(team)):
            continue
        if 'ssh' not in effective_services(box, 'container'):
            continue
        project = int(cfg['project_id'])
        network = int(box.get('network_id', box['team_id']))
        if box.get('network_key'):
            custom = cfg.get('custom_networks', {}).get(box['network_key'], {})
            if str(custom.get('owner_team_id')) != str(box['team_id']):
                raise ValueError('Container access network must belong to its declared team.')
            network = int(custom['network_id'])
        address = f"10.{project}.{network}.{int(box['ip_last_octet'])}"
        if (values.get('crange_project_id') != project or values.get('ansible_host') != address
                or type(values.get('crange_guest_id')) is not int
                or values['crange_guest_id'] < 100 or address in addresses):
            raise ValueError('Linux access requires unique applied container identities and addresses.')
        ipaddress.IPv4Address(address)
        row = next(r for r in connection_records(project, 'container', key, box, address) if r['protocol'] == 'ssh')
        credentials = parameters(row)
        login, password = credentials.get('username', ''), credentials.get('password', '')
        if (not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', login)
                or login in ('root', 'nobody', 'daemon', 'crange')
                or not isinstance(password, str) or not password
                or any(ord(c) < 32 for c in password)):
            raise ValueError('Declared container SSH access needs a non-system login and a protected password.')
        salt = hashlib.sha256(f'{project}:{key}:{values["crange_guest_id"]}'.encode()).hexdigest()[:16]
        hashed = subprocess.run(['openssl', 'passwd', '-6', '-salt', salt, '-stdin'],
                                input=password + '\n', text=True, capture_output=True, check=True).stdout.strip()
        if not hashed.startswith('$6$'):
            raise ValueError('Could not prepare the protected Linux access credential.')
        selected[host] = {**group.get('vars', {}), **values,
                          'crange_access_login': login, 'crange_access_password_hash': hashed}
        addresses.add(address)
    return {'all': {'hosts': selected}}


def configure(root, cfg, inventory, *, team=None):
    document = access_inventory(cfg, inventory, team=team)
    if not document['all']['hosts']:
        return
    with tempfile.TemporaryDirectory(prefix='crange-linux-access-') as temporary:
        os.chmod(temporary, 0o700)
        path = Path(temporary) / 'inventory.json'
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(document, stream)
        subprocess.run(['ansible-playbook', '-f', '3', '-i', str(path),
                        str(root / 'ansible/linux-access.yml')], check=True, cwd=root,
                       env={**os.environ, 'LC_ALL': 'C.UTF-8', 'LANG': 'C.UTF-8'})
