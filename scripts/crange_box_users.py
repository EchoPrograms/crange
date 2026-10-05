"""Fixed, project-owned Linux guest account operations; no caller shell commands."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ACTIONS = ('list', 'create', 'delete', 'grant-sudo', 'revoke-sudo', 'password', 'access')
FIELDS = {'jumpbox': 'jump_boxes', 'vm': 'scenario_boxes', 'container': 'scenario_containers'}


def box_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}', value):
        raise ValueError('Choose a declared box.')
    return value


def username(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', value):
        raise ValueError('Use a Linux username of 1–32 lowercase letters, digits, underscores or hyphens.')
    if value in ('root', 'user', 'crange', 'nobody', 'daemon'):
        raise ValueError('This platform or controller-access account is protected.')
    return value


def binding(kind, name, login):
    return 'guest-' + hashlib.sha256(json.dumps([kind, name, login]).encode()).hexdigest()


def resolve(project, kind, name):
    from crange import project_dir, json_config, output_json, ProxmoxAPI
    path = project_dir(str(project))
    cfg = json_config(path)
    field = FIELDS[kind]
    box = cfg.get(field, {}).get(name)
    live = output_json(path).get(field, {}).get('value', {}).get(name)
    if not box or not live:
        raise ValueError('Choose a deployed box in this project.')
    network = cfg.get('custom_networks', {}).get(box.get('network_key', ''))
    nid = int(network['network_id']) if network else int(box['team_id'])
    vnet = f'p{int(project):03d}{"c" if network else "t"}{nid:03d}'
    expected = f'{vnet}-jump-{int(box["jump_number"]):02d}' if kind == 'jumpbox' else f'{vnet}-{box["name"]}'
    node = live.get('node_name', box['node_name'])
    if node not in {f'proxmox{i}' for i in range(1, 7)}:
        raise ValueError('Applied box has no verified physical node; refresh its applied outputs.')
    api = ProxmoxAPI()
    actual = (api.container if kind == 'container' else api.vm)(node, int(live['id']))
    if actual.get('name', actual.get('hostname')) != expected or int(actual.get('template', 0)):
        raise ValueError('Guest identity does not match this project; refusing account operation.')
    if kind=='vm' and actual.get('ostype','l26')!='l26':
        raise ValueError('Guest user and sudo management requires a managed Linux image; configure Windows/BSD accounts inside the guest.')
    if (api.container_status if kind == 'container' else api.status)(node, int(live['id'])) != 'running':
        raise ValueError('Start this box before managing its users.')
    address = ipaddress.IPv4Address(live['address'])
    if address not in ipaddress.IPv4Network(f'10.{int(project)}.{nid}.0/24'):
        raise ValueError('Guest address does not match its declared project subnet.')
    return str(address), f'crange-{int(project)}-{kind}-{int(live["id"])}-{expected}'


def execute(args):
    from crange import ROOT, CrangeError
    try:
        if args.action not in ACTIONS or args.kind not in FIELDS:
            raise ValueError('Invalid guest account operation.')
        if args.action not in ('list', 'access'):
            username(args.username)
        address, alias = resolve(args.project, args.kind, args.name)
        key = Path.home() / '.ssh/id_ed25519_range'
        public = key.with_suffix('.pub').read_text().strip()
        try:
            probe = subprocess.run(['ssh', '-i', str(key), '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'HostKeyAlias=' + alias, 'root@' + address, 'true'], capture_output=True, timeout=10)
            ssh_user = 'root' if probe.returncode == 0 else 'user'
        except subprocess.TimeoutExpired:
            ssh_user = 'user'
        values = {'account_action': args.action, 'account_name': args.username,
                  'controller_public_key': public, 'account_password_hash': ''}
        if args.action in ('create', 'password'):
            from crange_guac_users import open_password
            secret = Path(os.environ.get('CRANGE_GUAC_JSON_KEY', '/home/crange/.config/crange/guacamole-json.secret')).read_text().strip()
            password = open_password(args.sealed_password, secret, int(args.project), binding(args.kind, args.name, args.username))
            if '\n' in password or '\r' in password or '\x00' in password:
                raise ValueError('Passwords cannot contain line breaks or null characters.')
            hashed = subprocess.run(['openssl', 'passwd', '-6', '-stdin'], input=password + '\n', text=True, capture_output=True, check=True)
            values['account_password_hash'] = hashed.stdout.strip()
        with tempfile.TemporaryDirectory(prefix='crange-account-') as tmp:
            inventory = Path(tmp) / 'inventory.json'
            variables = Path(tmp) / 'vars.json'
            inventory.write_text(json.dumps({'all': {'hosts': {'selected_box': {
                'ansible_host': address, 'ansible_user': ssh_user, 'ansible_become': ssh_user != 'root',
                'ansible_ssh_private_key_file': str(key), 'ansible_ssh_common_args': '-o HostKeyAlias=' + alias + ' -o StrictHostKeyChecking=accept-new', 'ansible_python_interpreter': '/usr/bin/python3'}}}}))
            variables.write_text(json.dumps(values)); variables.chmod(0o600)
            env = {**os.environ, 'LC_ALL': 'C.UTF-8', 'ANSIBLE_HOST_KEY_CHECKING': 'True'}
            proc = subprocess.run(['ansible-playbook', '-i', str(inventory), str(ROOT / 'ansible/box-users.yml'), '-e', '@' + str(variables)], text=True, capture_output=True, timeout=180, env=env)
            print(proc.stdout)
            if proc.returncode:
                raise ValueError('Guest account action failed. Check the task output above; no other box was targeted.')
            root_check = subprocess.run(['ssh', '-i', str(key), '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'HostKeyAlias=' + alias, 'root@' + address, 'id -u'], capture_output=True, text=True, timeout=10)
            if root_check.returncode or root_check.stdout.strip() != '0':
                raise ValueError('Account action completed, but controller root-key access verification failed.')
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise CrangeError(str(exc)) from exc
