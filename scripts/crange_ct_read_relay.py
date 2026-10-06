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

COMMANDS = {
    'systemd-readiness': ['/usr/bin/systemctl', 'is-system-running'],
    'openrc-bin-readiness': ['/bin/rc-status', '--runlevel'],
    'openrc-sbin-readiness': ['/sbin/rc-status', '--runlevel'],
    'machine-id': ['/bin/cat', '/etc/machine-id'],
    'ssh-public-key': ['/bin/cat', '/etc/ssh/ssh_host_ed25519_key.pub'],
}


def validate_request(value, config, peer_uid):
    if peer_uid != config['peer_uid']:
        raise ValueError('Service user authentication failed.')
    if not isinstance(value, dict) or set(value) != {'node', 'vmid', 'command'}:
        raise ValueError('Unexpected identity-read request.')
    node, vmid, command = value['node'], value['vmid'], value['command']
    if (not isinstance(node, str) or not re.fullmatch('proxmox[1-5]', node) or
            type(vmid) is not int or vmid < 100 or command not in COMMANDS):
        raise ValueError('Unsupported identity-read scope.')
    marker = config['allowed_guests'].get(node, {}).get(str(vmid))
    if not isinstance(marker, str) or not re.fullmatch('crange-job-[a-zA-Z0-9-]{1,64}', marker):
        raise ValueError('Container ID is outside approved acceptance scope.')
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


def host_read(request, marker):
    node, vmid, command = request['node'], request['vmid'], request['command']
    if socket.gethostname().split('.')[0] != node:
        raise ValueError('Relay reached a different cluster node.')
    def get(path):
        return json.loads(subprocess.check_output(['pvesh', 'get', path, '--output-format', 'json'], text=True, timeout=15))
    before = get(f'/nodes/{node}/lxc/{vmid}/config')
    validate_guest(before, get('/pools/crange-probes')['members'], node, vmid, marker)
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
    # The remote program is fixed source plus serialized validated data, never shell input.
    program = Path(__file__).read_text().split("\nif __name__ == '__main__':")[0]
    program += '\nprint(json.dumps({"value":host_read(' + repr(value) + ',' + repr(marker) + ')}))\n'
    target = config['node_addresses'][node]
    if not re.fullmatch(r'10\.0\.0\.10[1-5]', target):
        raise ValueError('Unapproved cluster SSH address.')
    result = run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                  '-o', 'StrictHostKeyChecking=yes', '-o', 'HostKeyAlias=' + node,
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
    socket_path = root / 'run/crange-host-read.sock'
    if socket_path.exists():
        raise ValueError('Relay socket already exists; reconcile the service.')
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # CT /run is owned by its user namespace: create as the mapped worker
    # filesystem UID while retaining the host service's real/effective UID.
    import ctypes
    libc = ctypes.CDLL(None)
    libc.setfsuid.argtypes = [ctypes.c_uint]
    libc.setfsuid.restype = ctypes.c_uint
    cwd_fd = os.open('.', os.O_RDONLY | os.O_DIRECTORY)
    run_fd = os.open(str(root / 'run'), os.O_RDONLY | os.O_DIRECTORY)
    previous_fsuid = libc.setfsuid(config['peer_uid'])
    try:
        if libc.setfsuid(0xFFFFFFFF) != config['peer_uid']:
            raise ValueError('Mapped service filesystem UID unavailable.')
        os.fchdir(run_fd)
        listener.bind('crange-host-read.sock')
        os.chmod('crange-host-read.sock', 0o600)
    finally:
        libc.setfsuid(previous_fsuid)
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
                    result = {'error': 'Fixed container identity read denied or failed.'}; status = 'failed'
                scope = ({key: request[key] for key in ('node', 'vmid', 'command')}
                         if status == 'passed' else {})
                with Path('/var/log/crange-ct-read-relay.jsonl').open('a') as audit:
                    audit.write(json.dumps({'time': time.time(), 'peer_uid': uid, 'status': status, **scope}) + '\n')
                conn.sendall(json.dumps(result).encode() + b'\n')
    finally:
        listener.close(); socket_path.unlink(missing_ok=True)


if __name__ == '__main__':
    import sys
    serve(sys.argv[1])
