#!/usr/bin/env python3
"""Host-only Unix relay for fixed container image identity reads."""
import json
import fcntl
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import struct
import subprocess
import time
import traceback
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import sys
if os.geteuid() == 0 and Path('/usr/local/lib/crange/crange_resource_identity.py').exists():
    sys.path.insert(0, '/usr/local/lib/crange')
import crange_resource_identity as resource_identity
from crange_resource_identity import automation_config, automation_response, display_tags

RANGE_STORAGE_SHA256 = '01666bd35eb315d829a39c52db9b03e0d1c75b3e7cdc4f5f5d7d8b673c888f63'

PRIVATE_BRIDGE_GUARD_SHA256 = '00c6bec611ba9a469a0ea04a88a794b29f67880111da5c48262a7dc1ae618e4d'

COMMANDS = {
    'systemd-readiness': ['/usr/bin/systemctl', 'is-system-running'],
    'failed-services': ['/usr/bin/systemctl', '--failed', '--no-pager', '--no-legend'],
    'postgres-status': ['/usr/bin/systemctl', 'status', 'postgresql@15-main.service', '--no-pager', '--lines=15'],
    'openrc-bin-readiness': ['/bin/rc-status', '--runlevel'],
    'openrc-sbin-readiness': ['/sbin/rc-status', '--runlevel'],
    'machine-id': ['/bin/cat', '/etc/machine-id'],
    'ssh-public-key': ['/bin/cat', '/etc/ssh/ssh_host_ed25519_key.pub'],
    'linked-backing': [],
}

SEALED_WINDOWS_FILES = {
    'inspection': ('/var/lib/crange-template-builds/windows-locale-candidate8912/inspection.json', 0o644),
    'preparation': ('/var/lib/crange-template-builds/windows-locale-candidate8912/preparation.json', 0o600),
    'probe8913': ('/var/lib/crange-template-builds/windows-locale-probe8913.json', 0o600),
    'probe8914': ('/var/lib/crange-template-builds/windows-locale-probe8914.json', 0o600),
    'qga8914': ('/var/lib/crange-template-builds/windows-locale-probe8914-qga-result.json', 0o600),
}
SEALED_WINDOWS_READER = 'proxmox4'
SEALED_WINDOWS_MAX_BYTES = 262144
SEALED_WINDOWS_RETIREMENT_PATH = '/var/lib/crange-template-builds/windows-locale-proof-retirement.json'
RELAY_RESPONSE_MAX_BYTES = 300000
NETWORK_PROBE_NODE = 'proxmox4'
RELAY_CODE_SHA256 = globals().get('_RELAY_SOURCE_DIGEST') or hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

NETWORK_GUAC_NODE = 'proxmox4'
NETWORK_GUAC_VMID = 110
NETWORK_GUAC_ADDRESS = '10.60.0.10'
ROUTER_HA_OPERATOR_SHA256 = '9697231ce5c180c95624a76fe813991ca352d2265c99835b001abd1e1c7b19ac'
WINDOWS_SEED_CLEANUP_DIR = Path('/var/lib/crange-windows-seed-cleanup')
WINDOWS_SEED_CLEANUP_NODE_IPS = {f'proxmox{i}': f'10.0.0.{100 + i}' for i in range(1, 7)}


def _network_config_digest(value):
    return hashlib.sha256(json.dumps(automation_config(value), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def identity_module_preamble():
    """Bundle trusted helper source so a returning host needs no new install."""
    source = Path(resource_identity.__file__).read_text()
    return ('import sys,types\n'
            '_identity_module=types.ModuleType("crange_resource_identity")\n'
            'sys.modules["crange_resource_identity"]=_identity_module\n'
            'exec(' + repr(source) + ',_identity_module.__dict__)\n')


def validate_request(value, config, peer_uid):
    if peer_uid != config['peer_uid']:
        raise ValueError('Service user authentication failed.')
    if isinstance(value,dict) and value.get('command') == 'router-ha-retire':
        if (set(value) != {'command','node','project_id','vmid'} or value.get('node') != 'proxmox4' or
                type(value.get('project_id')) is not int or not 1 <= value['project_id'] <= 255 or
                value['project_id'] in (60,61) or type(value.get('vmid')) is not int or value['vmid'] < 100):
            raise ValueError('Invalid fixed router HA retirement request.')
        return 'proxmox4',value['vmid'],value['command'],None
    if isinstance(value, dict) and value.get('command') == 'capture-export-unlock':
        if (set(value) != {'command','node','vmid','marker','upid','config_sha256'} or
                value.get('node') not in config.get('node_addresses', {}) or
                type(value.get('vmid')) is not int or value['vmid'] < 100 or
                not re.fullmatch(r'crange-capture-[a-f0-9]{12}', str(value.get('marker',''))) or
                not re.fullmatch(r'[a-f0-9]{64}', str(value.get('config_sha256',''))) or
                not re.fullmatch(r'UPID:' + re.escape(value['node']) + r':[A-Fa-f0-9]+:[A-Fa-f0-9]+:[A-Fa-f0-9]+:vzdump:' + str(value['vmid']) + r':[^\s]+:', str(value.get('upid','')))):
            raise ValueError('Invalid owned interrupted export request.')
        return value['node'], value['vmid'], value['command'], value['marker']
    if isinstance(value, dict) and value.get('command') in ('range-library-resources','range-library-recover'):
        if set(value)!={'command','node'} or value.get('node')!='proxmox5' or 'proxmox5' not in config.get('node_addresses',{}):
            raise ValueError('Invalid fixed library resource request.')
        return 'proxmox5',0,value['command'],None
    if isinstance(value, dict) and value.get('command') in ('range-storage-review', 'range-storage-apply'):
        reviewing = value['command'] == 'range-storage-review'
        fields = {'command', 'node', 'size_gb'} if reviewing else {'command', 'node', 'plan_id', 'review_sha256'}
        if (set(value) != fields or value.get('node') != 'proxmox5' or
                value['node'] not in config.get('node_addresses', {}) or
                (reviewing and (type(value.get('size_gb')) is not int or value['size_gb'] not in (256,512))) or
                (not reviewing and (not re.fullmatch('[a-f0-9]{32}', str(value.get('plan_id',''))) or
                 not re.fullmatch('[a-f0-9]{64}', str(value.get('review_sha256','')))))):
            raise ValueError('Invalid fixed storage preparation request.')
        return 'proxmox5', 0, value['command'], None
    if isinstance(value, dict) and value.get('command') in ('capture-bridge-prepare', 'capture-bridge-proof'):
        proof = value['command'] == 'capture-bridge-proof'
        compact=proof and str(value.get('marker','')).startswith('crange-cache-')
        fields = ({'command','node','vmid','marker','cache_job_id'} if compact else {'command', 'node', 'vmid', 'marker'}) if proof else {'command', 'node'}
        if (set(value) != fields or value.get('node') not in config.get('node_addresses', {}) or
                not re.fullmatch(r'proxmox[1-6]', str(value.get('node', ''))) or
                (proof and (type(value.get('vmid')) is not int or not 100 <= value['vmid'] <= 999999999 or
                 not ((compact and value.get('marker')==f"crange-cache-{value.get('vmid')}" and re.fullmatch(r'cache-[a-f0-9]{32}',str(value.get('cache_job_id','')))) or
                      (not compact and re.fullmatch(r'(?:crange-capture-[a-f0-9]{12}|crange-job-[a-zA-Z0-9-]{1,64})', str(value.get('marker', '')))))))):
            raise ValueError('Invalid fixed private-bridge request.')
        return value['node'], value.get('vmid', 0), value['command'], value.get('marker')
    if isinstance(value, dict) and value.get('command') == 'windows-seed-cleanup-ready':
        if set(value) != {'command'}:
            raise ValueError('Unexpected Windows seed cleanup readiness request.')
        return 'proxmox4', 0, 'windows-seed-cleanup-ready', None
    if isinstance(value, dict) and value.get('command') == 'windows-seed-cleanup':
        if set(value) != {'command', 'build_id'} or not re.fullmatch(r'[0-9a-f]{32}', str(value.get('build_id', ''))):
            raise ValueError('Invalid fixed Windows seed cleanup identity.')
        return 'proxmox4', 0, 'windows-seed-cleanup', {'build_id': value['build_id']}
    if isinstance(value, dict) and value.get('command') == 'network-probe-guac-origin':
        fields = {'command', 'node', 'probe_node', 'project_id', 'vnet', 'address', 'vmid', 'job_id'}
        if set(value) != fields or value.get('node') != NETWORK_GUAC_NODE or not re.fullmatch(r'proxmox[1-6]',str(value.get('probe_node',''))):
            raise ValueError('Unsupported fixed Guac origin request.')
        project, vnet, address, vmid, job = (value[key] for key in
            ('project_id', 'vnet', 'address', 'vmid', 'job_id'))
        if (type(project) is not int or not 1 <= project <= 255 or project in (60, 61) or
                not isinstance(vnet, str) or not re.fullmatch(fr'p{project:03d}[tc](?:00[1-9]|0[1-9][0-9]|1[0-9]{{2}}|2[0-4][0-9]|25[0-4])', vnet) or
                not isinstance(address, str) or not re.fullmatch(r'10\.[0-9]{1,3}\.[0-9]{1,3}\.(?:[0-9]{1,3})', address) or
                type(vmid) is not int or vmid < 900000 or
                not isinstance(job, str) or not re.fullmatch(r'[a-f0-9]{32}', job)):
            raise ValueError('Invalid probe address scope.')
        interface = ipaddress.ip_interface(address + '/24')
        subnet = ipaddress.ip_network(f'10.{project}.{int(vnet[-3:])}.0/24')
        if interface.network != subnet or not 2 <= int(interface.ip) - int(subnet.network_address) <= 100:
            raise ValueError('Invalid probe address scope.')
        expected = {'project_id': project, 'node': value['probe_node'], 'vnet': vnet,
                    'address': str(interface), 'vmid': vmid, 'job_id': 'network-' + job}
        return NETWORK_GUAC_NODE, NETWORK_GUAC_VMID, 'network-probe-guac-origin', expected
    if isinstance(value, dict) and value.get('command') == 'sealed-windows-source-proof':
        if (set(value) != {'node', 'command'} or value.get('node') != SEALED_WINDOWS_READER or
                config.get('sealed_windows_reader_node', SEALED_WINDOWS_READER) != SEALED_WINDOWS_READER):
            raise ValueError('Unsupported sealed Windows proof scope.')
        return SEALED_WINDOWS_READER, 8912, 'sealed-windows-source-proof', None
    if isinstance(value, dict) and value.get('command') == 'offline-config-snapshot':
        if set(value) != {'node', 'guest_node', 'kind', 'vmid', 'command'}:
            raise ValueError('Unexpected offline config snapshot request.')
        reader = config.get('offline_config_reader_node')
        guest_node, kind, vmid = value['guest_node'], value['kind'], value['vmid']
        if (not isinstance(reader, str) or reader not in {f'proxmox{i}' for i in range(1, 7)} or
                not isinstance(value['node'], str) or value['node'] != reader or
                not isinstance(guest_node, str) or value['node'] == guest_node or
                guest_node not in {f'proxmox{i}' for i in range(1, 7)} or
                not isinstance(kind, str) or kind not in ('lxc', 'qemu') or
                type(vmid) is not int or vmid < 100):
            raise ValueError('Unsupported offline config snapshot scope.')
        inventory_nodes = config.get('offline_config_inventory_nodes', [])
        if (not isinstance(inventory_nodes, list) or
                any(not isinstance(node, str) or node not in {f'proxmox{i}' for i in range(1, 7)} for node in inventory_nodes)):
            raise ValueError('Invalid protected offline inventory node policy.')
        if guest_node in inventory_nodes:
            # This authorizes only sanitized capacity metadata. host_read proves
            # current cluster membership, quorum and the exact offline identity.
            return reader, vmid, 'offline-config-snapshot', None
        allowed = config.get('offline_config_guests', {}).get(guest_node, {}).get(str(vmid))
        if (not isinstance(allowed, dict) or allowed.get('kind') != kind or
                not isinstance(allowed.get('required_tags'), list) or
                not allowed['required_tags'] or
                any(not isinstance(tag, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', tag)
                    for tag in allowed['required_tags'])):
            raise ValueError('Guest is outside the protected offline config snapshot allowlist.')
        return reader, vmid, 'offline-config-snapshot', None
    if isinstance(value, dict) and value.get('command') in ('owned-image-read', 'capture-container-generalize'):
        fields = {'node', 'vmid', 'command', 'marker'}
        if value['command'] == 'owned-image-read': fields.add('read')
        marker = value.get('marker')
        compact = str(marker).startswith('crange-cache-') and value['command']=='owned-image-read'
        if compact:
            fields.add('cache_job_id')
            if marker!=f"crange-cache-{value.get('vmid')}" or not re.fullmatch(r'cache-[a-f0-9]{32}',str(value.get('cache_job_id',''))):
                raise ValueError('Invalid compact probe identity.')
        pattern = r'crange-capture-[a-f0-9]{12}' if value['command'] == 'capture-container-generalize' else r'(?:crange-capture-[a-f0-9]{12}|crange-job-[a-zA-Z0-9-]{1,64})'
        if (set(value) != fields or not re.fullmatch(r'proxmox[1-6]', str(value.get('node', '')))
                or value.get('node') not in config.get('node_addresses', {})
                or type(value.get('vmid')) is not int or value['vmid'] < 100
                or not isinstance(marker, str) or not (compact or re.fullmatch(pattern, marker))
                or (value['command'] == 'owned-image-read' and value.get('read') not in COMMANDS.keys()-{'linked-backing'})):
            raise ValueError('Invalid owned image request scope.')
        return value['node'], value['vmid'], value['command'], marker
    if isinstance(value, dict) and value.get('command') == 'owned-linked-backing':
        fields = {'node','vmid','source_id','command','marker','source_marker'}
        compact = str(value.get('marker','')).startswith('crange-cache-')
        if compact:
            fields.add('cache_job_id')
            if (value.get('marker')!=f"crange-cache-{value.get('vmid')}" or
                    value.get('source_marker')!=f"crange-cache-{value.get('source_id')}" or
                    not re.fullmatch(r'cache-[a-f0-9]{32}',str(value.get('cache_job_id','')))):
                raise ValueError('Invalid compact backing identity.')
        pattern = r'(?:crange-capture-[a-f0-9]{12}|crange-job-[a-zA-Z0-9-]{1,64})'
        if (set(value)!=fields or not re.fullmatch(r'proxmox[1-6]',str(value.get('node','')))
                or value.get('node') not in config.get('node_addresses',{})
                or any(type(value.get(k)) is not int or value[k]<100 for k in ('vmid','source_id'))
                or value['vmid']==value['source_id']
                or any(not isinstance(value.get(k),str) or not (compact or re.fullmatch(pattern,value[k])) for k in ('marker','source_marker'))):
            raise ValueError('Invalid owned backing scope.')
        return value['node'],value['vmid'],value['command'],value['marker']
    fields = {'node', 'vmid', 'command'}
    if isinstance(value, dict) and value.get('command') == 'linked-backing': fields.add('source_id')
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('Unexpected identity-read request.')
    node, vmid, command = value['node'], value['vmid'], value['command']
    if (not isinstance(node, str) or not re.fullmatch('proxmox[1-6]', node) or
            type(vmid) is not int or vmid < 100 or command not in COMMANDS):
        raise ValueError('Unsupported identity-read scope.')
    marker = config['allowed_guests'].get(node, {}).get(str(vmid))
    if not isinstance(marker, str) or not re.fullmatch('crange-job-[a-zA-Z0-9-]{1,64}', marker):
        raise ValueError('Container ID is outside approved acceptance scope.')
    if command == 'linked-backing':
        source = value['source_id']
        if type(source) is not int or source == vmid or str(source) not in config['allowed_guests'].get(node, {}):
            raise ValueError('Exact approved backing source required.')
    return node, vmid, command, marker


def validate_guest(config, members, node, vmid, marker, cache_job_id=None):
    exact = [r for r in members if r.get('type') == 'lxc' and
             r.get('node') == node and r.get('vmid') == vmid]
    if len(exact) != 1:
        raise ValueError('Exact probe pool membership required.')
    if str(marker).startswith('crange-cache-'):
        if (marker!=f'crange-cache-{vmid}' or not re.fullmatch(r'cache-[a-f0-9]{32}',str(cache_job_id or '')) or
                str(config.get('description','')).rstrip('\r\n')!=f'CRANGE verification {cache_job_id}'):
            raise ValueError('Compact probe hidden identity differs.')
    tags = set(str(config.get('tags', '')).split(';'))
    if 'crange-image' not in tags or marker not in tags or str(config.get('unprivileged')) != '1':
        raise ValueError('Owned unprivileged probe required.')
    if int(config.get('template', 0)) or any(
            re.fullmatch(r'mp[0-9]+', str(k)) or str(k).startswith(('lxc.', 'dev')) for k in config):
        raise ValueError('Template, external mount or device denied.')
    networks = [str(v) for k, v in config.items() if re.fullmatch(r'net[0-9]+', k)]
    if not networks or any('bridge=crprobe1450' not in n.split(',') or 'link_down=1' not in n.split(',') for n in networks):
        raise ValueError('Probe must remain disconnected on the approved bridge.')


def _resource_identity(rows, node, kind, vmid):
    matches = [row for row in rows if row.get('type') == kind and row.get('node') == node and
               row.get('vmid') == vmid]
    if len(matches) != 1:
        raise ValueError('Exact offline cluster guest membership required.')
    row = matches[0]
    return {key: row.get(key) for key in ('node', 'type', 'vmid', 'status', 'tags')}


def _configured_disk_bytes(config):
    total = 0
    found = False
    for key, value in config.items():
        if not re.fullmatch(r'(?:scsi|sata|virtio|ide|efidisk|tpmstate|mp)\d+|rootfs', str(key)):
            continue
        value = str(value)
        if 'media=cdrom' in value or ':cloudinit' in value or value.startswith('none'):
            continue
        volume = value.split(',', 1)[0]
        if ':' not in volume or volume.startswith('/'):
            continue
        match = re.search(r'(?:^|,)size=([0-9]+(?:\.[0-9]+)?)([KMGT])(?:,|$)', value)
        if not match:
            raise ValueError('Configured offline guest disk size is missing.')
        amount = Decimal(match[1]) * {'K': Decimal(1024), 'M': Decimal(1024)**2,
                                      'G': Decimal(1024)**3, 'T': Decimal(1024)**4}[match[2]]
        total += int(amount.to_integral_value(rounding=ROUND_CEILING))
        found = True
    if not found or total <= 0:
        raise ValueError('Configured offline guest has no defensible disk reservation.')
    return total


def offline_config_snapshot(request, policy, *, get, read_bytes, now=None):
    """Return sanitized configured limits after exact before/after identity checks."""
    guest_node, kind, vmid = request['guest_node'], request['kind'], request['vmid']
    inventory_mode = guest_node in policy.get('offline_config_inventory_nodes', [])
    allow = policy.get('offline_config_guests', {}).get(guest_node, {}).get(str(vmid), {})
    if not inventory_mode and (allow.get('kind') != kind or not allow.get('required_tags')):
        raise ValueError('Guest is outside the protected offline snapshot policy.')
    def require_quorum():
        rows = get('/cluster/status')
        clusters = [row for row in rows if row.get('type') == 'cluster']
        if len(clusters) != 1 or clusters[0].get('quorate') != 1:
            raise ValueError('Offline capacity inventory requires cluster quorum.')
    if inventory_mode:
        require_quorum()
    nodes_before = get('/nodes')
    node_rows = [row for row in nodes_before if row.get('node') == guest_node]
    if len(node_rows) != 1 or node_rows[0].get('status') != 'offline':
        raise ValueError('Offline config snapshot requires the exact guest node to be offline.')
    resources_before = get('/cluster/resources', '--type', 'vm')
    identity_before = _resource_identity(resources_before, guest_node, kind, vmid)
    required = set(allow.get('required_tags', []))
    resource_tags = set(filter(None, str(identity_before.get('tags') or '').split(';')))
    categorical_required = set(filter(None, display_tags(';'.join(required)).split(';')))
    if not (required <= resource_tags or categorical_required <= resource_tags):
        raise ValueError('Cluster resource ownership tags do not match the offline snapshot allowlist.')
    config_dir = 'qemu-server' if kind == 'qemu' else 'lxc'
    path = Path(f'/etc/pve/nodes/{guest_node}/{config_dir}/{vmid}.conf')
    raw_before = read_bytes(path)
    try:
        text = raw_before.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError('Offline guest config encoding is invalid.') from exc
    config = {}
    description = []
    from urllib.parse import unquote
    for line in text.splitlines():
        if line.strip().startswith('['):
            # Snapshot sections contain historical copies, not active limits.
            break
        if line.startswith('#'):
            description.append(unquote(line[1:]) + '\n')
            continue
        if not line or line.lstrip().startswith('#') or ':' not in line:
            continue
        key, value = line.split(':', 1)
        key = key.strip()
        if key == 'description':
            description.append(unquote(value.lstrip()))
            continue
        if key not in ('cores', 'sockets', 'memory', 'tags', 'template') and not re.fullmatch(
                r'(?:scsi|sata|virtio|ide|efidisk|tpmstate|mp)\d+|rootfs', key):
            # LXC permits repeated low-level settings (e.g. mount entries).
            # They are neither capacity fields nor part of this response.
            continue
        if key in config:
            raise ValueError('Offline guest config has duplicate fields.')
        config[key] = value.strip()
    configured_tags = set(filter(None, config.get('tags', '').split(';')))
    if description:
        config['description'] = ''.join(description)
    logical_tags = set(filter(None, automation_config(config).get('tags', '').split(';')))
    if not required <= logical_tags:
        raise ValueError('Shared config ownership tags do not match the offline snapshot allowlist.')
    if inventory_mode and resource_tags != configured_tags:
        raise ValueError('Offline capacity inventory tags differ from shared configuration.')
    template = config.get('template', '0') == '1'
    def positive_number(field, default=None):
        value = config.get(field, default)
        if value is None:
            raise ValueError('Configured offline guest CPU or memory limit is missing.')
        try:
            number = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError('Configured offline guest CPU or memory limit is invalid.') from exc
        if not number.is_finite() or number <= 0:
            raise ValueError('Configured offline guest CPU or memory limit is invalid.')
        return number
    if template:
        memory_bytes = int((Decimal(config.get('memory', '0')) * Decimal(1024**2)).to_integral_value(rounding=ROUND_CEILING))
        cpu_cores = Decimal(config.get('cores', '0')) * Decimal(config.get('sockets', '1'))
    else:
        memory_bytes = int((positive_number('memory') * Decimal(1024**2)).to_integral_value(rounding=ROUND_CEILING))
        cpu_cores = positive_number('cores', '1') * positive_number('sockets', '1')
    disk_bytes = _configured_disk_bytes(config)
    raw_after = read_bytes(path)
    nodes_after = get('/nodes')
    node_after = [row for row in nodes_after if row.get('node') == guest_node]
    resources_after = get('/cluster/resources', '--type', 'vm')
    identity_after = _resource_identity(resources_after, guest_node, kind, vmid)
    if (len(node_after) != 1 or node_after[0].get('status') != 'offline' or
            identity_before != identity_after or raw_before != raw_after):
        raise ValueError('Offline node or shared guest config changed during snapshot.')
    if inventory_mode:
        require_quorum()
    import hashlib
    captured_at = time.time() if now is None else now
    return {'guest_node': guest_node, 'kind': kind, 'vmid': vmid, 'status': 'offline',
            'tags': sorted(configured_tags if inventory_mode else required),
            **({'scope': 'cluster-capacity'} if inventory_mode else {}),
            'template': template, 'memory_bytes': memory_bytes,
            'cpu_cores': float(cpu_cores), 'disk_bytes': disk_bytes,
            'config_sha256': hashlib.sha256(raw_before).hexdigest(), 'captured_at': captured_at}


def _sealed_config_identity(config, vmid):
    stable = {key: value for key, value in automation_config(config).items() if key not in {'template', 'lock', 'digest'}}
    volume = re.compile(rf'(?P<storage>[A-Za-z0-9_.-]+:)(?P<directory>{int(vmid)}/)?'
                        rf'(?:vm|base)-{int(vmid)}-disk-(?P<number>[0-9]+)'
                        r'(?P<format>\.(?:raw|qcow2|vmdk))?(?=,|$)')
    for key, value in stable.items():
        if isinstance(value, str):
            stable[key] = volume.sub(lambda match: f'{match["storage"]}{match["directory"] or ""}'
                f'vm-{int(vmid)}-disk-{match["number"]}{match["format"] or ""}', value)
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _sealed_protected_files():
    result = {}
    total = 0
    for name, (path, expected_mode) in SEALED_WINDOWS_FILES.items():
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
                    stat.S_IMODE(info.st_mode) != expected_mode):
                raise ValueError('Sealed proof file ownership or access mode is invalid.')
            raw = stream.read(SEALED_WINDOWS_MAX_BYTES + 1)
        total += len(raw)
        if total > SEALED_WINDOWS_MAX_BYTES:
            raise ValueError('Sealed proof bundle exceeds the fixed response limit.')
        try:
            document = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError('Sealed proof file is not valid JSON.') from exc
        if not isinstance(document, dict):
            raise ValueError('Sealed proof file is not an object.')
        result[name] = {'sha256': hashlib.sha256(raw).hexdigest(), 'document': document}
    return result


def _sealed_windows_retirement_receipt():
    """Read only the fixed root-owned historical proof retirement receipt."""
    try:
        fd = os.open(SEALED_WINDOWS_RETIREMENT_PATH, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
                stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('Sealed proof retirement receipt ownership or mode is invalid.')
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('Sealed proof retirement receipt exceeds its fixed bound.')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Sealed proof retirement receipt is not an object.')
    return value


def fixed_guac_qga_handshake(expected, *, run, persist, clock=time.monotonic, sleep=time.sleep):
    """One fixed VM110 command originating from its actual protected address."""
    address = expected['address'].split('/')[0]
    program = ("import socket,json; s=socket.socket(); s.settimeout(8); "
               "s.bind(('10.60.0.10',0)); s.connect((" + repr(address) + ",22)); "
               "banner=s.recv(256).decode('ascii').strip(); s.close(); "
               "print(json.dumps({'source':'10.60.0.10','target':" + repr(address) +
               ",'port':22,'banner':banner}))")
    base = '/nodes/proxmox4/qemu/110/agent/'
    command = ['pvesh', 'create', base+'exec', '--output-format', 'json']
    for argument in ['/usr/bin/python3', '-c', program]:
        command.extend(['--command', argument])
    persist('claim', {'scope':expected})
    submitted = run(command, capture_output=True, text=True, timeout=10, check=False)
    if len(submitted.stdout.encode()) > 4096 or len(submitted.stderr.encode()) > 4096:
        raise ValueError('Fixed Guac exec response exceeds its bound.')
    persist('submit', {'scope':expected,'returncode':submitted.returncode,
                       'stdout':submitted.stdout,'stderr':submitted.stderr})
    if submitted.returncode != 0:
        raise ValueError('Fixed Guac guest command submission failed.')
    response = json.loads(submitted.stdout)
    pid = response.get('pid')
    if type(pid) is not int or pid <= 0:
        raise ValueError('Fixed Guac guest command PID is invalid.')
    persist('pid', {'scope':expected,'pid':pid})
    deadline = clock()+20
    while clock() < deadline:
        polled = run(['pvesh','get',base+'exec-status','--pid',str(pid),'--output-format','json'],
                     capture_output=True,text=True,timeout=min(5,max(0.1,deadline-clock())),check=False)
        if len(polled.stdout.encode()) > 4096 or len(polled.stderr.encode()) > 4096:
            raise ValueError('Fixed Guac exec result exceeds its bound.')
        persist('result', {'scope':expected,'pid':pid,'returncode':polled.returncode,
                           'stdout':polled.stdout,'stderr':polled.stderr})
        if polled.returncode != 0:
            raise ValueError('Fixed Guac guest command polling failed.')
        result = json.loads(polled.stdout)
        if result.get('exited'):
            if result.get('exitcode') != 0 or result.get('out-truncated') or result.get('err-truncated'):
                raise ValueError('Fixed Guac-origin guest handshake failed.')
            value = json.loads(result.get('out-data',''))
            if set(value) != {'source','target','port','banner'} or value['source'] != '10.60.0.10' or value['target'] != address or value['port'] != 22 or not isinstance(value['banner'],str) or not value['banner'].startswith('SSH-2.0-') or len(value['banner']) > 255:
                raise ValueError('Fixed Guac-origin response differs from the owned probe.')
            return value['banner']
        sleep(min(0.2,max(0,deadline-clock())))
    raise ValueError('Fixed Guac-origin guest handshake timed out.')


def network_guac_origin_probe(expected, *, get, connect, bundle_root=Path('/var/lib/crange-network-probes'), owner_uid=0, guest_probe=None):
    """Verify and probe only a reviewed, currently owned network acceptance VM."""
    root = Path(bundle_root)
    bundle = root / expected['job_id']
    root_info, bundle_info = root.lstat(), bundle.lstat()
    if (not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != owner_uid or
            stat.S_IMODE(root_info.st_mode) != 0o700 or
            not stat.S_ISDIR(bundle_info.st_mode) or bundle_info.st_uid != owner_uid or
            stat.S_IMODE(bundle_info.st_mode) != 0o700):
        raise ValueError('Protected network acceptance bundle is unsafe.')
    def protected_json(path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o077:
                raise ValueError('Network ownership document is not protected.')
            with os.fdopen(fd, 'r', closefd=False) as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError('Network ownership document exceeds its bound.')
            return json.loads(raw)
        finally:
            os.close(fd)
    manifest = protected_json(bundle / 'review.json')
    if manifest.get('scope') != expected:
        raise ValueError('Protected network review scope differs from the relay request.')
    journal = protected_json(bundle / 'journal.json')
    if journal.get('scope') != expected or journal.get('state') != 'testing' or journal.get('active_check') != 'guacamole':
        raise ValueError('Network acceptance job is outside its live-check phase.')

    def live_identity():
        config = get(f"/nodes/{expected['node']}/qemu/{expected['vmid']}/config")
        status = get(f"/nodes/{expected['node']}/qemu/{expected['vmid']}/status/current").get('status')
        pool = get('/pools/crange-probes').get('members', [])
        members = [row for row in pool if row.get('type') == 'qemu' and
                   row.get('node') == expected['node'] and int(row.get('vmid', 0)) == expected['vmid']]
        tags = set(str(config.get('tags', '')).split(';'))
        expected_tags = {'crange-network-probe', expected['job_id']}
        net = str(config.get('net0', ''))
        initialization = dict(part.split('=', 1) for part in str(config.get('ipconfig0', '')).split(',') if '=' in part)
        if (len(members) != 1 or not expected_tags <= tags or config.get('template') not in (0, '0', False, None)
                or f"bridge={expected['vnet']}" not in net.split(',') or
                initialization != {'ip': expected['address'], 'gw': f"10.{expected['project_id']}.{int(expected['vnet'][-3:])}.1"}):
            raise ValueError('Live probe ownership or exact VNet/IP configuration differs.')
        source = get(f"/nodes/{expected['node']}/qemu/{manifest['source']['template_id']}/config")
        if _network_config_digest(source) != manifest['source']['config_sha256']:
            raise ValueError('Approved network probe source template changed.')
        origin = get(f'/nodes/{NETWORK_GUAC_NODE}/qemu/{NETWORK_GUAC_VMID}/config')
        origin_status = get(f'/nodes/{NETWORK_GUAC_NODE}/qemu/{NETWORK_GUAC_VMID}/status/current').get('status')
        resources = get('/cluster/resources', '--type', 'vm')
        origin_rows = [row for row in resources if row.get('type') == 'qemu' and
                       row.get('node') == NETWORK_GUAC_NODE and int(row.get('vmid', 0)) == NETWORK_GUAC_VMID]
        interfaces = get(f'/nodes/{NETWORK_GUAC_NODE}/qemu/{NETWORK_GUAC_VMID}/agent/network-get-interfaces')
        addresses = {item.get('ip-address') for iface in interfaces.get('result', [])
                     for item in iface.get('ip-addresses', [])}
        if status != 'running' or origin_status != 'running' or len(origin_rows) != 1 or NETWORK_GUAC_ADDRESS not in addresses:
            raise ValueError('Fixed Guac origin is not running at its protected address.')
        return {'config': config, 'status': status,
                'members': [(row['node'],row['type'],int(row['vmid'])) for row in members],
                'source': source, 'origin': origin, 'origin_status': origin_status,
                'origin_rows': [(row['node'],row['type'],int(row['vmid'])) for row in origin_rows],
                'addresses': sorted(addresses)}

    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != RELAY_CODE_SHA256:
        raise ValueError('Loaded relay source differs from its deployment file.')
    before = live_identity()
    if not callable(guest_probe):
        raise ValueError('Actual Guac-origin guest diagnostic is unavailable.')
    banner = guest_probe(expected)
    if not banner.startswith('SSH-2.0-') or len(banner) > 255:
        raise ValueError('Fixed Guac-origin SSH banner is invalid.')
    after = live_identity()
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != RELAY_CODE_SHA256:
        raise ValueError('Relay source changed during the actual-origin check.')
    after_journal = protected_json(bundle / 'journal.json')
    if after_journal.get('scope') != expected or after_journal.get('state') != 'testing' or after_journal.get('active_check') != 'guacamole':
        raise ValueError('Network acceptance phase changed during the actual-origin check.')
    if before != after:
        raise ValueError('Probe or Guac origin changed during the fixed TCP diagnostic.')
    return {'passed': True, 'origin': {'node': NETWORK_GUAC_NODE, 'vmid': NETWORK_GUAC_VMID,
            'address': NETWORK_GUAC_ADDRESS, 'kind': 'qemu'}, 'source_address': NETWORK_GUAC_ADDRESS,
            'target_address': expected['address'].split('/')[0], 'target_port': 22,
            'relay_sha256':RELAY_CODE_SHA256, 'output': banner + '\n'}


def sealed_windows_proof_snapshot(*, get, read_files=_sealed_protected_files):
    """Bounded P100 source/probe snapshot with lock and before/after revalidation."""
    files_before = read_files()
    retirement_before = _sealed_windows_retirement_receipt()
    if retirement_before is not None:
        source_config = get('/nodes/proxmox4/qemu/8912/config')
        source_status = get('/nodes/proxmox4/qemu/8912/status/current').get('status')
        resources = get('/cluster/resources', '--type', 'vm')
        remaining = [row for row in resources if row.get('type') == 'qemu' and
                     row.get('node') == 'proxmox4' and str(row.get('vmid')) in ('8913', '8914')]
        if (source_status != 'stopped' or remaining or
                _sealed_config_identity(source_config, 8912) !=
                '5e9fdb9337ec631b74100dc1c1f47ffdfb02f8873b32ee6119b49bc7cef27e9d' or
                read_files() != files_before or
                _sealed_windows_retirement_receipt() != retirement_before):
            raise ValueError('P100 frozen source or retired proof guests changed during snapshot.')
        return {'files': files_before,
                'live': {'source': {'config_sha256': _sealed_config_identity(source_config, 8912),
                                    'status': source_status}, 'probes': []},
                'retirement': retirement_before}
    second = files_before['probe8913']['document']
    third = files_before['probe8914']['document']
    upids = {8913: second.get('upid'), 8914: third.get('upid')}
    for vmid, upid in upids.items():
        if (not isinstance(upid, str) or not re.fullmatch(
                r'UPID:proxmox4:[A-Fa-f0-9]+:[A-Fa-f0-9]+:[A-Fa-f0-9]+:qmclone:8912:root@pam:', upid)):
            raise ValueError('Sealed clone task origin is outside the fixed P100 source scope.')

    def live():
        source_config = get('/nodes/proxmox4/qemu/8912/config')
        source_status = get('/nodes/proxmox4/qemu/8912/status/current').get('status')
        pool_members = get('/pools/crange-probes').get('members', [])
        probes = []
        for vmid in (8913, 8914):
            config = get(f'/nodes/proxmox4/qemu/{vmid}/config')
            status = get(f'/nodes/proxmox4/qemu/{vmid}/status/current').get('status')
            task = get('/nodes/proxmox4/tasks/' + upids[vmid] + '/status')
            members = [row for row in pool_members if row.get('type') == 'qemu' and
                       row.get('node') == 'proxmox4' and str(row.get('vmid')) == str(vmid)]
            probes.append({'vmid': vmid, 'config_sha256': _sealed_config_identity(config, vmid),
                'status': status, 'clone_upid': upids[vmid],
                'task_status': task.get('status'), 'task_exitstatus': task.get('exitstatus'),
                'probe_pool_member': len(members) == 1})
        return {'source': {'config_sha256': _sealed_config_identity(source_config, 8912),
                           'status': source_status},
                'probes': probes}

    before = live()
    files_after = read_files()
    after = live()
    if files_before != files_after or before != after:
        raise ValueError('P100 sealed source, clone task, or protected proof changed during snapshot.')
    if (before['source']['status'] != 'stopped' or
            any(probe['status'] != 'running' or probe['task_status'] != 'stopped' or
                probe['task_exitstatus'] != 'OK' or not probe['probe_pool_member'] for probe in before['probes'])):
        raise ValueError('P100 sealed source or independent clone state is not ready for capture.')
    return {'files': files_before, 'live': before}


def _read_ct_json(pid, relative, peer_uid, mode, limit):
    """Read one exact CT114 file without following any path component inside the CT."""
    parts = relative.split('/')
    if not parts or any(part in ('', '.', '..') for part in parts):
        raise ValueError('Invalid protected CT file identity.')
    fd = os.open(f'/proc/{pid}/root', os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) |
                            getattr(os, 'O_NOFOLLOW', 0), dir_fd=fd)
            os.close(fd); fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=fd)
        try:
            info = os.fstat(file_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != peer_uid or
                    stat.S_IMODE(info.st_mode) != mode or info.st_nlink != 1 or info.st_size > limit):
                raise ValueError('Protected CT build metadata ownership or mode is invalid.')
            with os.fdopen(file_fd, 'rb') as stream:
                file_fd = -1
                value = json.loads(stream.read(limit + 1))
        finally:
            if file_fd >= 0: os.close(file_fd)
    finally:
        os.close(fd)
    if not isinstance(value, dict): raise ValueError('Protected CT build metadata is invalid.')
    return value


def _cleanup_journal_path(build_id):
    return WINDOWS_SEED_CLEANUP_DIR / (build_id + '.json')


def _controller_ct_identity(peer_uid):
    pid = int(subprocess.check_output(['/usr/bin/lxc-info', '-n', '114', '-pH'], text=True, timeout=10).strip())
    if pid <= 1 or peer_uid != 101000:
        raise ValueError('Controller service container identity is invalid.')
    root_info = Path(f'/proc/{pid}/root').stat()
    if root_info.st_uid != 100000:
        raise ValueError('Controller mapped service UID differs from the fixed container identity.')
    return pid, (root_info.st_dev, root_info.st_ino, root_info.st_uid)


def _verify_controller_ct_identity(pid, identity):
    current_pid, current_identity = _controller_ct_identity(101000)
    if current_pid != pid or current_identity != identity:
        raise ValueError('Controller service container changed during private seed cleanup.')


def _read_root_cleanup_journal(path, identity):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
                stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > 8192):
            raise ValueError('Root cleanup task journal ownership or mode is invalid.')
        with os.fdopen(fd, 'r') as stream:
            fd = -1; journal = json.load(stream)
    finally:
        if fd >= 0: os.close(fd)
    if not isinstance(journal, dict) or any(journal.get(key) != value for key, value in identity.items()):
        raise ValueError('Root cleanup task journal identity changed.')
    return journal


def _write_root_cleanup_journal(path, journal):
    WINDOWS_SEED_CLEANUP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = WINDOWS_SEED_CLEANUP_DIR.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('Root cleanup journal directory ownership or mode is invalid.')
    temp = WINDOWS_SEED_CLEANUP_DIR / ('.' + path.name + '.' + os.urandom(8).hex() + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(journal, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
        dfd = os.open(WINDOWS_SEED_CLEANUP_DIR, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try: os.fsync(dfd)
        finally: os.close(dfd)
    finally:
        temp.unlink(missing_ok=True)


WINDOWS_SEED_REMOTE_SOURCE = r'''import hashlib,json,os,re,stat,subprocess,sys,time
sys.path.insert(0,'/usr/local/lib/crange')
from crange_resource_identity import automation_response
q=json.loads(sys.argv[1]); node=q['node']; build=q['build_id']; vmid=q['vmid']; volid=q['volid']; size=q['iso_size']; sha=q['iso_sha256']; created=int(q['created_at']); action=q['action']; upid=q.get('upid'); vm_mode=q['vm_mode']; guest_name=q.get('guest_name'); ownership_tag=q.get('ownership_tag')
if node not in {f'proxmox{i}' for i in range(1,7)} or not re.fullmatch(r'[0-9a-f]{32}',build) or volid!='local:iso/crange-win-'+build+'.iso' or type(vmid) is not int or not 100<=vmid<1000000 or type(size) is not int or not 1<=size<=67108864 or not re.fullmatch(r'[0-9a-f]{64}',sha): raise SystemExit(21)
def run(args, timeout=30): return subprocess.run(args,capture_output=True,text=True,timeout=timeout,check=True).stdout.strip()
def get(path,*args): return automation_response(path,json.loads(run(['pvesh','get',path,'--output-format','json',*args])))
def inspect():
 resources=get('/cluster/resources','--type','vm')
 owned=[r for r in resources if int(r.get('vmid',-1))==vmid]
 if vm_mode=='absent' and owned: raise SystemExit(22)
 if vm_mode=='detached':
  if len(owned)!=1 or owned[0].get('type')!='qemu' or owned[0].get('node')!=node or owned[0].get('status') not in ('running','stopped'): raise SystemExit(22)
  cfg=get(f'/nodes/{node}/qemu/{vmid}/config'); tags=set(str(cfg.get('tags','')).split(';'))
  if cfg.get('name')!=guest_name or 'crange-template-build' not in tags or ownership_tag not in tags or int(cfg.get('template',0) or 0)!=0 or str(cfg.get('ide0','')) not in ('','none,media=cdrom'): raise SystemExit(23)
 elif vm_mode!='absent': raise SystemExit(23)
 for r in resources:
  if r.get('type')!='qemu': continue
  cfg=get(f"/nodes/{r['node']}/qemu/{int(r['vmid'])}/config")
  if any(isinstance(v,str) and volid in v.split(',') for v in cfg.values()): raise SystemExit(23)
 tasks=get(f'/nodes/{node}/tasks','--limit','500')
 if any('upload' in str(t.get('type','')).lower() and t.get('status')=='running' and int(t.get('starttime',0))>=created-5 for t in tasks): raise SystemExit(24)
 rows=get(f'/nodes/{node}/storage/local/content','--content','iso')
 exact=[r for r in rows if r.get('volid')==volid]
 if not exact: return {'exists':False}
 if len(exact)!=1 or int(exact[0].get('size',-1))!=size: raise SystemExit(25)
 path=run(['pvesm','path',volid])
 if os.path.realpath(path)!=f'/var/lib/vz/template/iso/crange-win-{build}.iso': raise SystemExit(26)
 st=os.lstat(path)
 if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1: raise SystemExit(26)
 h=hashlib.sha256()
 with open(path,'rb') as f:
  for chunk in iter(lambda:f.read(1048576),b''): h.update(chunk)
 if h.hexdigest()!=sha: raise SystemExit(27)
 return {'exists':True}
if action=='inspect': result=inspect()
elif action=='delete':
 if not inspect().get('exists'): result={'exists':False,'upid':None}
 else:
  raw=run(['pvesh','delete',f'/nodes/{node}/storage/local/content/{volid}','--output-format','json'])
  try: data=json.loads(raw); raw=data.get('data',data) if isinstance(data,dict) else data
  except ValueError: pass
  result={'exists':True,'upid':raw if isinstance(raw,str) and raw.startswith('UPID:') else None}
elif action=='task':
 if not isinstance(upid,str) or not upid.startswith('UPID:'): raise SystemExit(28)
 p=f"/nodes/{node}/tasks/{upid}/status"
 result=get(p)
else: raise SystemExit(29)
print(json.dumps(result,separators=(',',':')))
'''


def _windows_seed_remote(node, request, run=subprocess.run):
    address = WINDOWS_SEED_CLEANUP_NODE_IPS.get(node)
    if not address or request.get('node') != node:
        raise ValueError('Windows seed cleanup node is outside the fixed cluster allowlist.')
    source = identity_module_preamble() + WINDOWS_SEED_REMOTE_SOURCE
    from datetime import datetime
    payload_value = dict(request)
    if isinstance(payload_value.get('created_at'), str):
        payload_value['created_at'] = int(datetime.fromisoformat(
            payload_value['created_at'].replace('Z', '+00:00')).timestamp())
    payload = json.dumps(payload_value, sort_keys=True, separators=(',', ':'))
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
        '-o', 'StrictHostKeyChecking=yes', '-o', 'HostKeyAlias=' + node,
        '-o', 'UserKnownHostsFile=/etc/pve/nodes/' + node + '/ssh_known_hosts',
        'root@' + address, shlex.join(['python3', '-c', source, payload])]
    result = run(command, capture_output=True, text=True, timeout=300, check=True)
    value = json.loads(result.stdout)
    if not isinstance(value, dict): raise ValueError('Remote Windows seed response is invalid.')
    return value


def windows_seed_cleanup(request, peer_uid, policy, *, run=subprocess.run):
    build_id = request['build_id']
    pid, ct_identity = _controller_ct_identity(peer_uid)
    peer = policy['peer_uid']
    if peer != peer_uid:
        raise ValueError('Controller service peer differs from the authenticated socket identity.')
    base = 'home/crange/.config/crange/windows-build-secrets/' + build_id
    secret = _read_ct_json(pid, base + '.json', peer, 0o600, 65536)
    record = _read_ct_json(pid, 'home/crange/crange/templates/builds/' + build_id + '.json', peer, 0o644, 262144)
    definition = record.get('definition')
    node = definition.get('node') if isinstance(definition,dict) else None
    vmid = record.get('vmid')
    volid = 'local:iso/crange-win-' + build_id + '.iso'
    created_at = secret.get('created_at')
    mode = None
    if record.get('status') in ('failed','cancelled'):
        mode = 'absent'
    elif record.get('status') == 'installing':
        base = _read_ct_json(pid, 'home/crange/.config/crange/windows-base-builds/' + build_id + '.json', peer, 0o600, 65536)
        binding = record.get('private_answerfile')
        if (base.get('build_id') != build_id or base.get('vmid') != vmid or base.get('node') != node or
                base.get('state') not in ('prepared','sealed') or not isinstance(binding,dict) or
                binding.get('build_id') != build_id or binding.get('vmid') != vmid or
                binding.get('volid') != 'local:iso/crange-win-' + build_id + '.iso' or
                binding.get('sha256') != secret.get('answerfile_sha256')):
            raise ValueError('Protected Windows base journal does not prove detached answerfile cleanup.')
        mode = 'detached'
    if (secret.get('build_id') != build_id or secret.get('vmid') != vmid or secret.get('node') != node or
            secret.get('volid') != volid or secret.get('artifact_name') != build_id + '.iso' or
            secret.get('state') not in ('uploading','upload_uncertain','uploaded','cleaned') or
            record.get('id') != build_id or mode not in ('absent','detached') or
            record.get('reference') is not None or
            not isinstance(definition,dict) or definition.get('windows_unattended') is not True or
            node not in WINDOWS_SEED_CLEANUP_NODE_IPS or type(vmid) is not int or vmid < 100 or
            type(secret.get('iso_size')) is not int or not 1 <= secret['iso_size'] <= 67108864 or
            not re.fullmatch(r'[0-9a-f]{64}',str(secret.get('iso_sha256',''))) or
            not isinstance(created_at,str)):
        raise ValueError('Protected build and private seed journal do not prove one owned failed build.')
    try:
        from datetime import datetime
        created = datetime.fromisoformat(created_at.replace('Z','+00:00')).timestamp()
    except (TypeError,ValueError): raise ValueError('Private seed creation timestamp is invalid.') from None
    if time.time() - created < 120: raise ValueError('Private answerfile upload may still be active; wait before cleanup.')
    identity = {'build_id':build_id,'node':node,'vmid':vmid,'volid':volid,
        'iso_size':secret['iso_size'],'iso_sha256':secret['iso_sha256'],'created_at':created_at,
        'vm_mode':mode,'guest_name':record.get('guest_name'),'ownership_tag':record.get('ownership_tag')}
    path = _cleanup_journal_path(build_id)
    journal = _read_root_cleanup_journal(path, identity)
    if journal and journal.get('state') == 'cleaned':
        result = _windows_seed_remote(node,{**identity,'action':'inspect'},run=run)
        if result.get('exists'): raise ValueError('Previously cleaned private seed volume has reappeared; preserve it for review.')
        _verify_controller_ct_identity(pid, ct_identity)
        return {'build_id':build_id,'node':node,'vmid':vmid,'volid':volid,'state':'cleaned','verified_absent':True}
    if journal and journal.get('state') == 'task_pending':
        result = _windows_seed_remote(node,{**identity,'action':'task','upid':journal.get('upid')},run=run)
        if result.get('status') != 'stopped':
            raise ValueError('Exact private seed cleanup task remains active; retry this build later.')
        if result.get('exitstatus') != 'OK': raise ValueError('Exact private seed cleanup task failed; preserve its handle for review.')
    elif journal and journal.get('state') == 'submitting':
        result = _windows_seed_remote(node,{**identity,'action':'inspect'},run=run)
        if result.get('exists'):
            raise ValueError('Cleanup submission outcome is uncertain; do not submit another delete for this build.')
    elif not journal or journal.get('state') in ('ready','submitting'):
        if not journal:
            result = _windows_seed_remote(node,{**identity,'action':'inspect'},run=run)
            if not result.get('exists'):
                _write_root_cleanup_journal(path,{**identity,'state':'cleaned','updated_at':time.time()})
                _verify_controller_ct_identity(pid, ct_identity)
                return {'build_id':build_id,'node':node,'vmid':vmid,'volid':volid,'state':'cleaned','verified_absent':True}
            _write_root_cleanup_journal(path,{**identity,'state':'ready','updated_at':time.time()})
        _write_root_cleanup_journal(path,{**identity,'state':'submitting','updated_at':time.time()})
        result = _windows_seed_remote(node,{**identity,'action':'delete'},run=run)
        upid = result.get('upid')
        if isinstance(upid,str) and upid.startswith('UPID:'):
            journal = {**identity,'state':'task_pending','upid':upid,'updated_at':time.time()}
            _write_root_cleanup_journal(path,journal)
            result = _windows_seed_remote(node,{**identity,'action':'task','upid':upid},run=run)
            if result.get('status') != 'stopped':
                raise ValueError('Exact private seed cleanup task remains active; retry this build later.')
            if result.get('exitstatus') != 'OK': raise ValueError('Exact private seed cleanup task failed; preserve its handle for review.')
        else:
            result = _windows_seed_remote(node,{**identity,'action':'inspect'},run=run)
            if result.get('exists'): raise ValueError('Private seed delete returned no durable task handle and storage still contains the ISO.')
    else:
        raise ValueError('Private seed cleanup journal is in an unknown state; preserve it for review.')
    result = _windows_seed_remote(node,{**identity,'action':'inspect'},run=run)
    if result.get('exists'): raise ValueError('Private seed ISO remains present after cleanup task completion.')
    _write_root_cleanup_journal(path,{**identity,'state':'cleaned','updated_at':time.time()})
    _verify_controller_ct_identity(pid, ct_identity)
    return {'build_id':build_id,'node':node,'vmid':vmid,'volid':volid,'state':'cleaned','verified_absent':True}


def library_resources(*, read_text=None, run=subprocess.run):
    """Raise only the fixed range NFS container's online resource floor."""
    read_text=read_text or (lambda path:Path(path).read_text())
    def config():
        return dict(line.split(': ',1) for line in read_text('/etc/pve/lxc/145.conf').splitlines()
                    if ': ' in line and not line.startswith('#'))
    before=config()
    if (before.get('hostname')!='crange-library' or
            not before.get('rootfs','').startswith('ssd-lvm:vm-145-disk-0,') or
            not before.get('mp0','').startswith('ssd-lvm:vm-145-disk-1,') or
            'mp=/srv/crange-library' not in before.get('mp0','').split(',') or
            'ip=10.0.0.249/24' not in before.get('net0','').split(',') or before.get('lock')):
        raise ValueError('Fixed library identity or backing storage changed.')
    current={k:int(before.get(k,0)) for k in ('memory','swap','cores')}
    target={k:max(current[k],v) for k,v in {'memory':2048,'swap':2048,'cores':2}.items()}
    mem=dict(line.split(':',1) for line in read_text('/proc/meminfo').splitlines() if ':' in line)
    available=int(mem['MemAvailable'].split()[0])//1024
    if available < target['memory']-current['memory']+4096:
        raise ValueError('Library memory increase would violate host headroom.')
    if current!=target:
        result=run(['pct','set','145','--memory',str(target['memory']),'--swap',str(target['swap']),'--cores',str(target['cores'])],
                   text=True,capture_output=True,timeout=45)
        if result.returncode:raise ValueError('Online library resource update failed; inspect host state before retry.')
    after=config()
    if any(int(after.get(k,0))!=v for k,v in target.items()):
        raise ValueError('Library resource readback differs; retain task for review.')
    return {'node':'proxmox5','vmid':145,'before':current,'after':target,'state':'ready','rebooted':False}


def library_recover(*, run=subprocess.run, read_text=None, recovery_path=None):
    """One journaled graceful NFS-only restart; repeated calls only observe it."""
    library_resources(run=run,read_text=read_text)
    path=recovery_path or Path('/var/lib/crange-library-service-recovery.json')
    def state():
        r=run(['pct','exec','145','--','systemctl','show','nfs-ganesha.service','-p','MainPID','-p','ActiveState','-p','SubState'],
              capture_output=True,text=True,timeout=15)
        if r.returncode:raise ValueError('Library service state is unavailable.')
        return dict(line.split('=',1) for line in r.stdout.splitlines() if '=' in line)
    before=state()
    if path.exists():
        record=json.loads(path.read_text())
        if record.get('state')=='ready':return record
        if record.get('state') not in ('submitting','observing'):
            raise ValueError('Library recovery journal requires review.')
    else:
        record={'state':'submitting','node':'proxmox5','vmid':145,'unit':'nfs-ganesha.service','before':before}
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream:json.dump(record,stream)
        r=run(['pct','exec','145','--','systemctl','restart','--no-block','nfs-ganesha.service'],
              capture_output=True,text=True,timeout=15)
        if r.returncode:raise ValueError('Library restart submission was not confirmed; retain its journal.')
        record['state']='observing';path.write_text(json.dumps(record))
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        current=state()
        if (current.get('ActiveState')=='active' and int(current.get('MainPID','0'))>0 and
                current.get('MainPID')!=record['before'].get('MainPID')):
            record.update(state='ready',after=current,rebooted=False);path.write_text(json.dumps(record));return record
        time.sleep(2)
    raise ValueError('Library service recovery remains pending; retry only observes the saved restart.')


def recover_export_lock(request, *, read=None, execute=None, boot_time=None, evidence_dir=None):
    """Unlock only an owned immutable capture whose backup died before this boot."""
    if read is None:
        def read(path, *args):
            result = subprocess.run(['pvesh','get',path,'--output-format','json',*args],
                                    capture_output=True,text=True,timeout=15,check=True)
            return automation_response(path, json.loads(result.stdout))
    if execute is None:
        def execute(argv):
            return subprocess.run(argv,capture_output=True,text=True,timeout=15,check=True)
    node, vmid, upid = request['node'], request['vmid'], request['upid']
    base = f'/nodes/{node}/qemu/{vmid}'
    config = read(base + '/config')
    status = read(f'/nodes/{node}/tasks/{upid}/status')
    boot_time = boot_time if boot_time is not None else next(
        int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines() if line.startswith('btime '))
    members = read('/pools/crange-probes').get('members', [])
    active = read(f'/nodes/{node}/tasks', '--source', 'active')
    if (int(config.get('template',0)) != 1 or config.get('lock') not in (None,'backup') or
            request['marker'] not in str(config.get('tags','')).split(';') or
            'crange-image' not in str(config.get('tags','')).split(';') or
            _sealed_config_identity(config,vmid) != request['config_sha256'] or
            read(base+'/status/current').get('status') != 'stopped' or
            not any(x.get('type')=='qemu' and x.get('node')==node and x.get('vmid')==vmid for x in members) or
            status.get('upid') != upid or status.get('type') != 'vzdump' or str(status.get('id')) != str(vmid) or
            status.get('status') != 'stopped' or status.get('exitstatus') != 'unexpected status' or
            not 0 < int(status.get('starttime',0)) < boot_time or
            any(str(x.get('id'))==str(vmid) and x.get('type')=='vzdump' for x in active)):
        raise ValueError('Interrupted export is not proven terminal, owned and unchanged after reboot.')
    evidence = {'state':'reviewed','node':node,'vmid':vmid,'upid':upid,
                'config_sha256':request['config_sha256'],'task':status,'boot_time':boot_time,
                'partial_files_preserved':True}
    directory = Path(evidence_dir or '/var/lib/crange-export-recovery');directory.mkdir(mode=0o700,exist_ok=True)
    path = directory/(hashlib.sha256(upid.encode()).hexdigest()+'.json')
    path.write_text(json.dumps(evidence));path.chmod(0o600)
    if config.get('lock') == 'backup': execute(['qm','unlock',str(vmid)])
    after = read(base+'/config')
    if after.get('lock') or _sealed_config_identity(after,vmid) != request['config_sha256']:
        raise ValueError('Export unlock did not preserve the captured configuration.')
    evidence['state']='unlocked';path.write_text(json.dumps(evidence))
    return evidence


def host_read(request, marker, source_marker=None, source_pool='crange-probes', *, policy=None):
    if request.get('command') == 'router-ha-retire':
        if socket.gethostname().split('.')[0] != 'proxmox4':raise ValueError('Router HA retirement must use the core.')
        helper=Path('/usr/local/lib/crange/router_ha_operator.py');info=helper.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or
                hashlib.sha256(helper.read_bytes()).hexdigest() != ROUTER_HA_OPERATOR_SHA256):
            raise ValueError('Router HA operator differs from its trusted source.')
        return subprocess.check_output(['/usr/bin/python3',str(helper),'retire',str(request['project_id']),
                                        '--vmid',str(request['vmid'])],text=True,timeout=55).strip()
    if request.get('command') == 'capture-export-unlock':
        if socket.gethostname().split('.')[0] != request['node']: raise ValueError('Export recovery reached another node.')
        return json.dumps(recover_export_lock(request))
    if request.get('command') in ('range-library-resources','range-library-recover'):
        if socket.gethostname().split('.')[0]!='proxmox5':raise ValueError('Library request reached a different node.')
        return json.dumps(library_recover() if request['command']=='range-library-recover' else library_resources())
    if request.get('command') in ('range-storage-review', 'range-storage-apply'):
        if socket.gethostname().split('.')[0] != 'proxmox5':
            raise ValueError('Storage preparation reached a different host.')
        helper = Path('/usr/local/lib/crange/range_storage.py')
        info = helper.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or
                hashlib.sha256(helper.read_bytes()).hexdigest() != RANGE_STORAGE_SHA256):
            raise ValueError('Storage helper differs from its trusted source.')
        args = (['review', '--size-gb', str(request['size_gb'])] if request['command'].endswith('review') else
                ['apply', '--plan-id', request['plan_id'], '--review-sha256', request['review_sha256']])
        result = subprocess.run(['/usr/bin/python3', str(helper), *args], text=True, capture_output=True, timeout=300)
        if result.returncode: raise ValueError((result.stderr or 'Storage helper failed').strip().splitlines()[-1])
        return result.stdout.strip()
    if request.get('command') in ('capture-bridge-prepare', 'capture-bridge-proof'):
        node = request['node']
        if socket.gethostname().split('.')[0] != node:
            raise ValueError('Private bridge request reached a different node.')
        script = Path('/usr/local/lib/crange/private_bridge_guard.py')
        info = script.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o644 or
                hashlib.sha256(script.read_bytes()).hexdigest() != PRIVATE_BRIDGE_GUARD_SHA256):
            raise ValueError('Private bridge guard differs from its trusted source.')
        args = ['prepare']
        if request['command'] == 'capture-bridge-proof':
            vmid = request['vmid']
            cfg = automation_config(json.loads(subprocess.check_output(['pvesh','get',f'/nodes/{node}/qemu/{vmid}/config','--output-format','json'],text=True,timeout=15)))
            pool = json.loads(subprocess.check_output(['pvesh','get','/pools/crange-probes','--output-format','json'],text=True,timeout=15))
            if (marker not in str(cfg.get('tags','')).split(';') or
                    not any(row.get('node') == node and row.get('type') == 'qemu' and int(row.get('vmid',-1)) == vmid
                            for row in pool.get('members',[]))):
                raise ValueError('Private bridge proof requires an owned capture/probe.')
            if request.get('cache_job_id') and str(cfg.get('description','')).rstrip('\r\n')!=f"CRANGE verification {request['cache_job_id']}":
                raise ValueError('Compact probe marker does not match its durable cache job.')
            args = ['proof', str(vmid)]
        return subprocess.check_output(['/usr/bin/python3',str(script),*args],text=True,timeout=45).strip()
    if request.get('command') in ('windows-seed-cleanup-ready', 'windows-seed-cleanup'):
        if socket.gethostname().split('.')[0] != 'proxmox4':
            raise ValueError('Windows seed cleanup relay must run on the fixed P4 host.')
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != RELAY_CODE_SHA256:
            raise ValueError('Loaded cleanup relay differs from its trusted source.')
        if request['command'] == 'windows-seed-cleanup-ready':
            return json.dumps({'schema_version':1,'cleanup_supported':True,
                               'relay_sha256':RELAY_CODE_SHA256},sort_keys=True,separators=(',',':'))
        return json.dumps(windows_seed_cleanup(request, policy['peer_uid'], policy),
                          sort_keys=True,separators=(',',':'))
    if request.get('command') == 'network-probe-guac-origin':
        if socket.gethostname().split('.')[0] != NETWORK_GUAC_NODE:
            raise ValueError('Guac-origin diagnostics must run on the fixed P4 host.')
        def get(path, *args):
            raw = subprocess.check_output(['pvesh', 'get', path, '--output-format', 'json', *args],
                                          text=True, timeout=15)
            if len(raw.encode()) > 65536:
                raise ValueError('Fixed network diagnostic response exceeds its bound.')
            value = json.loads(raw)
            if not isinstance(value, (dict, list)):
                raise ValueError('Fixed network diagnostic response has an invalid shape.')
            return automation_response(path, value)
        import socket as socket_module
        controller_pid = int(subprocess.check_output(['lxc-info', '-n', '114', '-pH'], text=True, timeout=10).strip())
        controller_root = Path(f'/proc/{controller_pid}/root')
        root_identity = controller_root.stat()
        mapped_root_uid = root_identity.st_uid
        if mapped_root_uid != 100000:
            raise ValueError('Fixed controller root mapping differs from the protected range.')
        bundle = controller_root/'var/lib/crange-network-probes'/marker['job_id']
        def persist(stage, document):
            destination = bundle/('guac-exec-'+stage+'.json')
            temporary = bundle/('guac-exec-'+stage+'.tmp')
            if stage in ('claim','submit','pid') and destination.exists():
                raise ValueError('Existing Guac command requires its recorded PID; never resubmit.')
            fd = os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            try:
                os.fchown(fd,mapped_root_uid,mapped_root_uid)
                with os.fdopen(fd,'w',closefd=False) as stream:
                    json.dump(document,stream,sort_keys=True);stream.flush();os.fsync(fd)
            finally:os.close(fd)
            os.replace(temporary,destination)
            parent_fd=os.open(bundle,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(parent_fd)
            finally:os.close(parent_fd)
        value = network_guac_origin_probe(
            marker, get=get, connect=lambda endpoint, timeout: socket_module.create_connection(endpoint, timeout),
            bundle_root=controller_root/'var/lib/crange-network-probes', owner_uid=mapped_root_uid,
            guest_probe=lambda expected:fixed_guac_qga_handshake(expected,run=subprocess.run,persist=persist))
        after_pid = int(subprocess.check_output(['lxc-info', '-n', '114', '-pH'], text=True, timeout=10).strip())
        after_root = controller_root.stat()
        if after_pid != controller_pid or (after_root.st_dev, after_root.st_ino, after_root.st_uid) != (root_identity.st_dev, root_identity.st_ino, mapped_root_uid):
            raise ValueError('Fixed controller root identity changed during the diagnostic.')
        return json.dumps(value, sort_keys=True, separators=(',', ':'))
    if request.get('command') == 'sealed-windows-source-proof':
        if socket.gethostname().split('.')[0] != SEALED_WINDOWS_READER:
            raise ValueError('Sealed Windows proof must be read on its fixed PVE host.')
        lock_fd = os.open('/run/lock/crange-sealed-windows-proof.lock',
                          os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(lock_fd)
            if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError('Sealed proof snapshot lock has unsafe ownership or mode.')
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            def get(path):
                raw = subprocess.check_output(['pvesh', 'get', path, '--output-format', 'json'],
                                              text=True, timeout=15)
                if len(raw.encode()) > 65536:
                    raise ValueError('PVE sealed proof response exceeds its fixed bound.')
                value = json.loads(raw)
                if not isinstance(value, (dict, list)):
                    raise ValueError('PVE sealed proof response has an invalid shape.')
                return automation_response(path, value)
            result = sealed_windows_proof_snapshot(get=get)
            encoded = json.dumps(result, sort_keys=True, separators=(',', ':'))
            if len(encoded.encode()) > SEALED_WINDOWS_MAX_BYTES:
                raise ValueError('Sealed proof snapshot exceeds its fixed response bound.')
            return encoded
        finally:
            os.close(lock_fd)
    node, vmid, command = request['node'], request['vmid'], request['command']
    if command == 'offline-config-snapshot':
        if socket.gethostname().split('.')[0] != node:
            raise ValueError('Offline config snapshot must run on its fixed reader node.')
        def get(path, *args):
            return json.loads(subprocess.check_output(
                ['pvesh', 'get', path, '--output-format', 'json', *args], text=True, timeout=15))
        value = offline_config_snapshot(request, policy or {}, get=get,
                                        read_bytes=lambda path: path.read_bytes())
        return json.dumps(value, sort_keys=True, separators=(',', ':'))
    if socket.gethostname().split('.')[0] != node:
        raise ValueError('Relay reached a different cluster node.')
    def get(path):
        return automation_response(path, json.loads(subprocess.check_output(['pvesh', 'get', path, '--output-format', 'json'], text=True, timeout=15)))
    before = get(f'/nodes/{node}/lxc/{vmid}/config')
    validate_guest(before, get('/pools/crange-probes')['members'], node, vmid, marker, request.get('cache_job_id'))
    if command in ('linked-backing','owned-linked-backing'):
        source_id = request['source_id']
        source = get(f'/nodes/{node}/lxc/{source_id}/config')
        if source_pool not in ('crange-probes', 'crange-iac'):
            raise ValueError('Unapproved backing source pool.')
        if request.get('cache_job_id'):
            description=str(source.get('description','')).rstrip('\r\n')
            if not description.startswith(f"CRANGE distribution {request['cache_job_id']} image "):
                raise ValueError('Compact source hidden identity differs.')
        members = get('/pools/' + source_pool)['members']
        if (len([r for r in members if r.get('type') == 'lxc' and r.get('node') == node and r.get('vmid') == source_id]) != 1 or
                not source_marker or source_marker not in str(source.get('tags', '')).split(';') or
                'crange-image' not in str(source.get('tags', '')).split(';') or
                str(source.get('unprivileged')) != '1' or int(source.get('template', 0)) != 1 or
                get(f'/nodes/{node}/lxc/{source_id}/status/current').get('status') != 'stopped'):
            raise ValueError('Exact immutable owned backing source required.')
        stores=[str(c.get('rootfs','')).split(':')[0] for c in (source,before)]
        if stores != ['local-lvm','local-lvm']:
            approved = {('proxmox5', 'crange-thin'): 'hdd-lvm',
                        ('proxmox6', 'local-hdd'): 'local-hdd'}
            storage = stores[0]
            vg = approved.get((node, storage))
            if not vg or stores != [storage, storage]:
                raise ValueError('Only accepted local thin-LVM backing inspection is allowed.')
            store=get('/storage/' + storage)
            if (store.get('type')!='lvmthin' or store.get('vgname')!=vg or
                    store.get('thinpool')!='crange_data' or store.get('nodes')!=node or store.get('shared',0)):
                raise ValueError('Dedicated range backing storage changed.')
        volumes = [str(c['rootfs']).split(',')[0] for c in (source, before)]
        paths = [subprocess.check_output(['pvesm', 'path', v], text=True, timeout=15).strip() for v in volumes]
        if any(not re.fullmatch(r'/dev/[a-zA-Z0-9_./-]+', path) for path in paths):
            raise ValueError('Unrecognized local backing volume.')
        rows = json.loads(subprocess.check_output(['lvs', '--reportformat', 'json', '-o', 'lv_name,origin', paths[1]], text=True, timeout=15))['report'][0]['lv']
        if len(rows) != 1 or rows[0].get('origin', '').strip(' []') != paths[0].split('/')[-1]:
            raise ValueError('Physical snapshot origin differs from the captured source.')
        if source != get(f'/nodes/{node}/lxc/{source_id}/config'):
            raise ValueError('Backing source changed during inspection.')
        value = json.dumps({'verified': True, 'source_id': source_id, 'probe_id': vmid,
                            'source_volume': volumes[0], 'probe_volume': volumes[1],
                            'origin': rows[0]['origin'].strip(' []')})
    elif command == 'capture-container-generalize':
        # Only a live, disconnected unprivileged capture clone passed the pool,
        # marker and mount checks above. This fixed program runs inside its LXC.
        program = r"""import os,stat,json,re
paths=['/etc/machine-id','/var/lib/dbus/machine-id'];old=None
for path in paths:
 try: info=os.lstat(path)
 except FileNotFoundError: continue
 if stat.S_ISLNK(info.st_mode):
  if path=='/var/lib/dbus/machine-id' and os.path.realpath(path)=='/etc/machine-id': continue
  raise RuntimeError('Unexpected machine-ID symlink')
 if not stat.S_ISREG(info.st_mode) or info.st_size>128: raise RuntimeError('Unexpected machine-ID file')
 fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
 try:
  current=os.read(fd,128).decode().strip()
  if current and not re.fullmatch('[a-f0-9]{32}',current): raise RuntimeError('Invalid machine-ID content')
  if path=='/etc/machine-id': old=current
  os.ftruncate(fd,0);os.fsync(fd)
 finally:os.close(fd)
if not old: raise RuntimeError('Source machine-ID is unavailable')
print(json.dumps({'generalized':True,'previous_machine_id':old,'machine_id_empty':os.stat('/etc/machine-id').st_size==0}))"""
        value = subprocess.check_output(['pct','exec',str(vmid),'--','/usr/bin/python3','-c',program],text=True,timeout=30)
    else:
        read = request['read'] if command == 'owned-image-read' else command
        args=['pct', 'exec', str(vmid), '--'] + COMMANDS[read]
        if read in ('systemd-readiness','postgres-status'):
            result=subprocess.run(args,text=True,capture_output=True,timeout=30)
            valid=(result.returncode in (0,1) and result.stdout.strip() in ('initializing','starting','running','degraded','maintenance','stopping','offline','unknown')) if read=='systemd-readiness' else result.returncode in (0,3)
            if not valid:
                result.check_returncode()
                raise ValueError('Unexpected systemd readiness response.')
            value=result.stdout
        else:
            value = subprocess.check_output(args, text=True, timeout=30)
    after = get(f'/nodes/{node}/lxc/{vmid}/config')
    validate_guest(after, get('/pools/crange-probes')['members'], node, vmid, marker, request.get('cache_job_id'))
    if before != after:
        raise ValueError('Container changed during identity read.')
    if len(value.encode()) > 4096:
        raise ValueError('Identity read exceeds response bound.')
    return value


def dispatch(value, config, peer_uid, run=subprocess.run):
    node, vmid, command, marker = validate_request(value, config, peer_uid)
    if command == 'router-ha-retire':
        if socket.gethostname().split('.')[0] != 'proxmox4':raise ValueError('Router HA retirement requires the local core relay.')
        return {'value':host_read(value,marker,policy=config)}
    if command in ('windows-seed-cleanup-ready', 'windows-seed-cleanup'):
        if socket.gethostname().split('.')[0] != 'proxmox4':
            raise ValueError('Windows seed cleanup requests must use the fixed local P4 relay.')
        raw = host_read(value, marker, policy=config)
        return {'value': json.loads(raw)}
    if command == 'sealed-windows-source-proof':
        if socket.gethostname().split('.')[0] != SEALED_WINDOWS_READER:
            raise ValueError('Sealed Windows proof requests must use the fixed local P4 reader.')
        return {'value': host_read(value, marker, policy=config)}
    if command == 'network-probe-guac-origin':
        if socket.gethostname().split('.')[0] != NETWORK_GUAC_NODE:
            raise ValueError('Guac-origin diagnostics must use the fixed local P4 reader.')
        raw=host_read(value, marker, policy=config)
        if not isinstance(raw,str) or len(raw.encode()) > 4096:
            raise ValueError('Fixed Guac response exceeds its bound.')
        diagnostic=json.loads(raw)
        if not isinstance(diagnostic,dict):
            raise ValueError('Fixed Guac response must be an object.')
        return {'value': diagnostic}
    if command == 'offline-config-snapshot':
        if socket.gethostname().split('.')[0] != node:
            raise ValueError('Offline config snapshots must use the local fixed reader.')
        # This command reads only shared PVE metadata from this online host.
        # It never accepts a path, command, or guest execution request.
        return {'value': host_read(value, marker, policy=config)}
    # The remote program is fixed source plus serialized validated data, never shell input.
    program = identity_module_preamble() + "_RELAY_SOURCE_DIGEST=" + repr(RELAY_CODE_SHA256) + "\n" + Path(__file__).read_text().split("\nif __name__ == '__main__':")[0]
    source_marker = value['source_marker'] if command == 'owned-linked-backing' else config['allowed_guests'].get(node, {}).get(str(value.get('source_id')))
    source_pool = ('crange-iac' if value.get('cache_job_id') and command=='owned-linked-backing'
                   else config.get('source_pools', {}).get(node, {}).get(str(value.get('source_id')), 'crange-probes'))
    program += '\nprint(json.dumps({"value":host_read(' + repr(value) + ',' + repr(marker) + ',' + repr(source_marker) + ',' + repr(source_pool) + ')}))\n'
    target = config['node_addresses'][node]
    if not re.fullmatch(r'10\.0\.0\.10[1-6]', target):
        raise ValueError('Unapproved cluster SSH address.')
    try:
        result = run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                          '-o', 'StrictHostKeyChecking=yes', '-o', 'HostKeyAlias=' + node,
                          '-o', 'UserKnownHostsFile=/etc/pve/nodes/' + node + '/ssh_known_hosts',
                          'root@' + target, shlex.join(['python3', '-c', program])],
                     capture_output=True, text=True, timeout=320 if command.startswith(('range-storage-','range-library-')) else 60, check=True)
    except subprocess.CalledProcessError as exc:
        if command.startswith('range-storage-'):
            lines=(exc.stderr or '').splitlines()
            detail=next((line for line in lines if line.startswith('ValueError: crange:')), 'Storage helper failed; inspect root diagnostics.')
            raise ValueError(detail[:500]) from None
        raise
    response = json.loads(result.stdout)
    limit = SEALED_WINDOWS_MAX_BYTES if command == 'sealed-windows-source-proof' else 4096
    if set(response) != {'value'} or not isinstance(response['value'], str) or len(response['value'].encode()) > limit:
        raise ValueError('Invalid fixed host response.')
    return response


def audit_scope(request, passed):
    if not passed:
        return {}
    fields = (('node','command','project_id','vmid') if request.get('command') == 'router-ha-retire'
              else ('node', 'command') if request.get('command','').startswith(('range-storage-','range-library-')) else ('node', 'command') if request.get('command') in ('sealed-windows-source-proof','capture-bridge-prepare')
              else ('command',) if request.get('command') == 'windows-seed-cleanup-ready'
              else ('command', 'build_id') if request.get('command') == 'windows-seed-cleanup'
              else ('node', 'probe_node', 'project_id', 'vnet', 'address', 'vmid', 'job_id', 'command')
              if request.get('command') == 'network-probe-guac-origin'
              else ('node', 'vmid', 'command'))
    scope = {key: request[key] for key in fields}
    if request.get('command') == 'offline-config-snapshot':
        scope.update(guest_node=request['guest_node'], kind=request['kind'])
    return scope


def persist_relay_exception(error, request):
    """Root-only diagnostic sink, never returned through the relay socket."""
    if not isinstance(request,dict) or request.get('command') not in ('range-storage-review','range-storage-apply','network-probe-guac-origin','capture-bridge-prepare','capture-bridge-proof'):
        return
    try:
        fd=os.open('/var/log/crange-network-relay-errors.log',os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(fd)
            if info.st_uid != 0 or not stat.S_ISREG(info.st_mode):return
            os.fchmod(fd,0o600)
            job=request.get('job_id','')
            if not isinstance(job,str) or not re.fullmatch(r'[a-f0-9]{32}',job):job='invalid'
            with os.fdopen(fd,'w',closefd=False) as stream:
                stream.write('job_id='+job+' exception='+type(error).__name__+'\n')
                stream.write(traceback.format_exc());stream.flush();os.fsync(fd)
        finally:os.close(fd)
    except OSError:pass


def serve(config_path):
    path = Path(config_path); st = path.stat()
    if st.st_uid != 0 or st.st_mode & 0o077:
        raise ValueError('Relay policy must be root-owned mode0600.')
    config = json.loads(path.read_text())
    if type(config.get('peer_uid')) is not int or config['peer_uid'] < 100000:
        raise ValueError('Exact unprivileged service UID required.')
    pid = int(subprocess.check_output(['lxc-info', '-n', '114', '-pH'], text=True).strip())
    root = Path(f'/proc/{pid}/root')
    subprocess.run(['pct', 'exec', '114', '--', 'install', '-d', '-m', '0700',
                    '-o', 'crange', '-g', 'crange', '/run/crange-ct-read'], check=True, timeout=15)
    socket_path = root / 'run/crange-ct-read/socket'
    if socket_path.exists():
        raise ValueError('Relay socket already exists; reconcile the service.')
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # CT /run is owned by its user namespace: create as the mapped worker
    # filesystem UID while retaining the host service's real/effective UID.
    import ctypes
    libc = ctypes.CDLL(None)
    libc.setfsuid.argtypes = [ctypes.c_uint]
    libc.setfsuid.restype = ctypes.c_uint
    libc.setfsgid.argtypes = [ctypes.c_uint]
    libc.setfsgid.restype = ctypes.c_uint
    cwd_fd = os.open('.', os.O_RDONLY | os.O_DIRECTORY)
    run_fd = os.open(str(root / 'run/crange-ct-read'), os.O_RDONLY | os.O_DIRECTORY)
    previous_fsgid = libc.setfsgid(config['peer_uid'])
    previous_fsuid = libc.setfsuid(config['peer_uid'])
    try:
        if libc.setfsuid(0xFFFFFFFF) != config['peer_uid']:
            raise ValueError('Mapped service filesystem UID unavailable.')
        os.fchdir(run_fd)
        listener.bind('socket')
        os.chmod('socket', 0o600)
    finally:
        libc.setfsuid(previous_fsuid)
        libc.setfsgid(previous_fsgid)
        os.fchdir(cwd_fd); os.close(cwd_fd); os.close(run_fd)
    listener.listen(4)
    import signal
    def stop(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    try:
        while True:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(5)
                _, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
                request = {}
                try:
                    data = b''
                    while not data.endswith(b'\n') and len(data) <= 4096:
                        chunk = conn.recv(4097 - len(data))
                        if not chunk: break
                        data += chunk
                    if len(data) > 4096 or not data.endswith(b'\n'):
                        raise ValueError('Invalid request framing.')
                    request = json.loads(data)
                    validate_request(request, config, uid)
                    result = dispatch(request, config, uid)
                    status = 'passed'
                except Exception as error:
                    persist_relay_exception(error, request)
                    result = {'error': 'Fixed read-only relay request denied or failed.'}; status = 'failed'
                encoded = json.dumps(result).encode()
                if len(encoded) > RELAY_RESPONSE_MAX_BYTES:
                    result = {'error': 'Fixed read-only relay response exceeded its bound.'}
                    status = 'failed'
                scope = audit_scope(request, status == 'passed')
                with Path('/var/log/crange-ct-read-relay.jsonl').open('a') as audit:
                    audit.write(json.dumps({'time': time.time(), 'peer_uid': uid, 'status': status, **scope}) + '\n')
                conn.sendall(json.dumps(result).encode() + b'\n')
    finally:
        listener.close(); socket_path.unlink(missing_ok=True)


if __name__ == '__main__':
    import sys
    serve(sys.argv[1])
