#!/usr/bin/env python3
"""Host-only Unix relay for fixed container image identity reads."""
import json
import os
from pathlib import Path
import re
import shlex
import socket
import struct
import subprocess
import time
from decimal import Decimal, InvalidOperation, ROUND_CEILING

COMMANDS = {
    'systemd-readiness': ['/usr/bin/systemctl', 'is-system-running'],
    'openrc-bin-readiness': ['/bin/rc-status', '--runlevel'],
    'openrc-sbin-readiness': ['/sbin/rc-status', '--runlevel'],
    'machine-id': ['/bin/cat', '/etc/machine-id'],
    'ssh-public-key': ['/bin/cat', '/etc/ssh/ssh_host_ed25519_key.pub'],
    'linked-backing': [],
}


def validate_request(value, config, peer_uid):
    if peer_uid != config['peer_uid']:
        raise ValueError('Service user authentication failed.')
    if isinstance(value, dict) and value.get('command') == 'offline-config-snapshot':
        if set(value) != {'node', 'guest_node', 'kind', 'vmid', 'command'}:
            raise ValueError('Unexpected offline config snapshot request.')
        reader = config.get('offline_config_reader_node')
        guest_node, kind, vmid = value['guest_node'], value['kind'], value['vmid']
        if (not isinstance(reader, str) or reader not in {f'proxmox{i}' for i in range(1, 6)} or
                not isinstance(value['node'], str) or value['node'] != reader or
                not isinstance(guest_node, str) or value['node'] == guest_node or
                guest_node not in {f'proxmox{i}' for i in range(1, 6)} or
                not isinstance(kind, str) or kind not in ('lxc', 'qemu') or
                type(vmid) is not int or vmid < 100):
            raise ValueError('Unsupported offline config snapshot scope.')
        allowed = config.get('offline_config_guests', {}).get(guest_node, {}).get(str(vmid))
        if (not isinstance(allowed, dict) or allowed.get('kind') != kind or
                not isinstance(allowed.get('required_tags'), list) or
                not allowed['required_tags'] or
                any(not isinstance(tag, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', tag)
                    for tag in allowed['required_tags'])):
            raise ValueError('Guest is outside the protected offline config snapshot allowlist.')
        return reader, vmid, 'offline-config-snapshot', None
    fields = {'node', 'vmid', 'command'}
    if isinstance(value, dict) and value.get('command') == 'linked-backing': fields.add('source_id')
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('Unexpected identity-read request.')
    node, vmid, command = value['node'], value['vmid'], value['command']
    if (not isinstance(node, str) or not re.fullmatch('proxmox[1-5]', node) or
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


def validate_guest(config, members, node, vmid, marker):
    exact = [r for r in members if r.get('type') == 'lxc' and
             r.get('node') == node and r.get('vmid') == vmid]
    if len(exact) != 1:
        raise ValueError('Exact probe pool membership required.')
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
    allow = policy['offline_config_guests'][guest_node][str(vmid)]
    nodes_before = get('/nodes')
    node_rows = [row for row in nodes_before if row.get('node') == guest_node]
    if len(node_rows) != 1 or node_rows[0].get('status') != 'offline':
        raise ValueError('Offline config snapshot requires the exact guest node to be offline.')
    resources_before = get('/cluster/resources', '--type', 'vm')
    identity_before = _resource_identity(resources_before, guest_node, kind, vmid)
    required = set(allow['required_tags'])
    resource_tags = set(str(identity_before.get('tags') or '').split(';'))
    if not required <= resource_tags:
        raise ValueError('Cluster resource ownership tags do not match the offline snapshot allowlist.')
    config_dir = 'qemu-server' if kind == 'qemu' else 'lxc'
    path = Path(f'/etc/pve/nodes/{guest_node}/{config_dir}/{vmid}.conf')
    raw_before = read_bytes(path)
    try:
        text = raw_before.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError('Offline guest config encoding is invalid.') from exc
    config = {}
    for line in text.splitlines():
        if not line or line.lstrip().startswith('#') or ':' not in line:
            continue
        key, value = line.split(':', 1)
        key = key.strip()
        if key in config:
            raise ValueError('Offline guest config has duplicate fields.')
        config[key] = value.strip()
    configured_tags = set(config.get('tags', '').split(';'))
    if not required <= configured_tags:
        raise ValueError('Shared config ownership tags do not match the offline snapshot allowlist.')
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
    import hashlib
    captured_at = time.time() if now is None else now
    return {'guest_node': guest_node, 'kind': kind, 'vmid': vmid, 'status': 'offline',
            'tags': sorted(required), 'template': template, 'memory_bytes': memory_bytes,
            'cpu_cores': float(cpu_cores), 'disk_bytes': disk_bytes,
            'config_sha256': hashlib.sha256(raw_before).hexdigest(), 'captured_at': captured_at}


def host_read(request, marker, source_marker=None, source_pool='crange-probes', *, policy=None):
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
        return json.loads(subprocess.check_output(['pvesh', 'get', path, '--output-format', 'json'], text=True, timeout=15))
    before = get(f'/nodes/{node}/lxc/{vmid}/config')
    validate_guest(before, get('/pools/crange-probes')['members'], node, vmid, marker)
    if command == 'linked-backing':
        source_id = request['source_id']
        source = get(f'/nodes/{node}/lxc/{source_id}/config')
        if source_pool not in ('crange-probes', 'crange-iac'):
            raise ValueError('Unapproved backing source pool.')
        members = get('/pools/' + source_pool)['members']
        if (len([r for r in members if r.get('type') == 'lxc' and r.get('node') == node and r.get('vmid') == source_id]) != 1 or
                not source_marker or source_marker not in str(source.get('tags', '')).split(';') or
                'crange-image' not in str(source.get('tags', '')).split(';') or
                str(source.get('unprivileged')) != '1' or int(source.get('template', 0)) != 1 or
                get(f'/nodes/{node}/lxc/{source_id}/status/current').get('status') != 'stopped'):
            raise ValueError('Exact immutable owned backing source required.')
        if not all(str(c.get('rootfs', '')).startswith('local-lvm:') for c in (source, before)):
            raise ValueError('Only accepted local thin-LVM backing inspection is allowed.')
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
    else:
        value = subprocess.check_output(['pct', 'exec', str(vmid), '--'] + COMMANDS[command], text=True, timeout=30)
    after = get(f'/nodes/{node}/lxc/{vmid}/config')
    validate_guest(after, get('/pools/crange-probes')['members'], node, vmid, marker)
    if before != after:
        raise ValueError('Container changed during identity read.')
    if len(value.encode()) > 4096:
        raise ValueError('Identity read exceeds response bound.')
    return value


def dispatch(value, config, peer_uid, run=subprocess.run):
    node, vmid, command, marker = validate_request(value, config, peer_uid)
    if command == 'offline-config-snapshot':
        if socket.gethostname().split('.')[0] != node:
            raise ValueError('Offline config snapshots must use the local fixed reader.')
        # This command reads only shared PVE metadata from this online host.
        # It never accepts a path, command, or guest execution request.
        return {'value': host_read(value, marker, policy=config)}
    # The remote program is fixed source plus serialized validated data, never shell input.
    program = Path(__file__).read_text().split("\nif __name__ == '__main__':")[0]
    source_marker = config['allowed_guests'].get(node, {}).get(str(value.get('source_id')))
    source_pool = config.get('source_pools', {}).get(node, {}).get(str(value.get('source_id')), 'crange-probes')
    program += '\nprint(json.dumps({"value":host_read(' + repr(value) + ',' + repr(marker) + ',' + repr(source_marker) + ',' + repr(source_pool) + ')}))\n'
    target = config['node_addresses'][node]
    if not re.fullmatch(r'10\.0\.0\.10[1-5]', target):
        raise ValueError('Unapproved cluster SSH address.')
    result = run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                  '-o', 'StrictHostKeyChecking=yes', '-o', 'HostKeyAlias=' + node,
                  '-o', 'UserKnownHostsFile=/etc/pve/nodes/' + node + '/ssh_known_hosts',
                  'root@' + target, shlex.join(['python3', '-c', program])],
                 capture_output=True, text=True, timeout=60, check=True)
    response = json.loads(result.stdout)
    if set(response) != {'value'} or not isinstance(response['value'], str) or len(response['value'].encode()) > 4096:
        raise ValueError('Invalid fixed host response.')
    return response


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
                except Exception:
                    result = {'error': 'Fixed read-only relay request denied or failed.'}; status = 'failed'
                scope = ({key: request[key] for key in ('node', 'vmid', 'command')}
                         if status == 'passed' else {})
                if status == 'passed' and request.get('command') == 'offline-config-snapshot':
                    scope.update(guest_node=request['guest_node'], kind=request['kind'])
                with Path('/var/log/crange-ct-read-relay.jsonl').open('a') as audit:
                    audit.write(json.dumps({'time': time.time(), 'peer_uid': uid, 'status': status, **scope}) + '\n')
                conn.sendall(json.dumps(result).encode() + b'\n')
    finally:
        listener.close(); socket_path.unlink(missing_ok=True)


if __name__ == '__main__':
    import sys
    serve(sys.argv[1])
