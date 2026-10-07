#!/usr/bin/env python3
"""Build an administrator-owned Linux LXC base on a Proxmox node.

Never invoked by dashboard jobs. Sources come from Proxmox's signed appliance
catalog; credentials stay in a protected external file. Approval is separate
and requires fresh-clone verification.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

class BuildError(Exception):
    pass


def find_signed_archive(args, checksum):
    """Return the first approved copy matching the signed appliance checksum."""
    for storage in ('crange-library', 'media-mirror1', 'media-mirror4', 'local'):
        candidate = storage + ':vztmpl/' + args.archive
        try:
            path = Path(command(['pvesm', 'path', candidate]).strip())
            if not path.is_file() or path.stat().st_size <= 0:
                continue
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha512').hexdigest() == checksum:
                    return path, candidate
        except (BuildError, OSError):
            continue
    return None, None


def command(argv, data=None, timeout=900):
    result = subprocess.run(argv, input=data, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        lines = (result.stderr or result.stdout).strip().splitlines()
        raise BuildError(f'{argv[0]} failed: {lines[-1] if lines else result.returncode}')
    return result.stdout


def private_file(path):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise BuildError('Password file must be owned by the operator with mode 0600.')
    password = path.read_text().rstrip('\n')
    if not password or any(c in password for c in '\n\r:'):
        raise BuildError('Invalid guest password file.')
    return password


def build(args):
    if os.geteuid() != 0:
        raise BuildError('Run this administrator tool as root on the selected Proxmox node.')
    if not 100 <= args.vmid <= 999999:
        raise BuildError('Template ID must be 100–999999.')
    if not re.fullmatch(r'(debian-13|ubuntu-24\.04|alpine-[0-9]+\.[0-9]+)-(standard|default)_[a-zA-Z0-9_.-]+_amd64\.tar\.(zst|xz|gz)', args.archive):
        raise BuildError('Choose a Debian 13, Ubuntu 24.04 or Alpine amd64 appliance archive.')
    password = private_file(args.password_file)
    key = args.ssh_key_file.read_text().strip()
    if '\n' in key or not key.startswith(('ssh-ed25519 ', 'ssh-rsa ', 'ecdsa-sha2-')):
        raise BuildError('Provide one valid SSH public key.')
    resources = json.loads(command(['pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json']))
    if any(int(r['vmid']) == args.vmid for r in resources):
        raise BuildError(f'Guest ID {args.vmid} is already in use; nothing was changed.')
    if not Path('/sys/class/net/craccess').exists():
        raise BuildError('The approved craccess build bridge is unavailable.')
    command(['pveam', 'update'], timeout=180)
    code = 'use PVE::APLInfo; use JSON; my $d=PVE::APLInfo::load_data(); print encode_json($d->{all}->{"' + args.archive + '"});'
    metadata = json.loads(command(['perl', '-e', code]))
    if not metadata or metadata.get('architecture') != 'amd64' or not re.fullmatch(r'[a-f0-9]{128}', metadata.get('sha512sum', '')):
        raise BuildError('Archive is missing from the signed catalog or has no SHA-512 checksum.')
    # Prefer the shared cluster library. Read-only mirrors remain usable when
    # its server is unavailable; signed appliance metadata verifies every copy.
    archive, volume = find_signed_archive(args, metadata['sha512sum'])
    if archive is None:
        # Writable primary only: a new archive must be distributed by the
        # mirror service rather than silently creating divergent mirror files.
        command(['pveam', 'download', 'crange-library', args.archive])
        volume = 'crange-library:vztmpl/' + args.archive
        archive = Path(command(['pvesm', 'path', volume]).strip())
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha512').hexdigest()
    if digest != metadata['sha512sum']:
        raise BuildError('Archive checksum differs from the signed catalog; nothing was created.')
    distro = metadata['os']
    hostname = 'crange-base-' + distro.replace('.', '-')
    record = {'schema_version': 1, 'template_id': args.vmid, 'node': os.uname().nodename,
        'hostname': hostname, 'kind': 'container', 'archive': args.archive, 'archive_volume':volume, 'archive_sha512': digest,
        'source': metadata['location'], 'catalog_version': metadata['version'],
        'builder_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'built_at': datetime.now(timezone.utc).isoformat(), 'status': 'building'}
    records = Path('/var/lib/crange-template-builds'); records.mkdir(mode=0o750, parents=True, exist_ok=True)
    record_file = records / f'{args.vmid}.json'
    def save():
        record_file.write_text(json.dumps(record, indent=2) + '\n'); record_file.chmod(0o640)
    save()
    def guest(*argv, data=None):
        return command(['pct', 'exec', str(args.vmid), '--', *argv], data=data)
    def put(path, text, mode='0644'):
        guest('sh', '-c', 'cat > ' + path, data=text)
        guest('chmod', mode, path)
    try:
        command(['pct', 'create', str(args.vmid), volume,
            '--hostname', hostname, '--rootfs', 'local-lvm:4', '--cores', '1', '--memory', '512', '--swap', '0',
            '--unprivileged', '1', '--onboot', '0', '--pool', 'crange-iac', '--tags', 'range-lan;crange-base',
            '--net0', 'name=eth0,bridge=craccess,ip=dhcp,mtu=1450,firewall=1', '--nameserver', '10.60.2.1'])
        command(['pct', 'start', str(args.vmid)])
        # pct start can return before guest networking has finished booting.
        for attempt in range(60):
            try:
                guest('sh', '-c', 'test -n "$(ip -4 route show default)"')
                break
            except BuildError:
                if attempt == 59: raise BuildError('Build guest did not acquire a default route.')
                time.sleep(2)
        if distro.startswith('alpine'):
            guest('apk', 'update')
            guest('apk', 'add', 'openssh', 'sudo', 'curl', 'ca-certificates', 'iputils', 'iproute2', 'python3')
            guest('adduser', '-D', '-s', '/bin/ash', 'user')
        else:
            guest('env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'update')
            guest('env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y', 'openssh-server', 'sudo', 'curl', 'ca-certificates', 'iputils-ping', 'iproute2', 'python3')
            guest('useradd', '-m', '-s', '/bin/bash', 'user')
        guest('chpasswd', data='user:' + password + '\n')
        guest('mkdir', '-p', '/home/user/.ssh', '/etc/sudoers.d')
        put('/home/user/.ssh/authorized_keys', key + '\n', '0600')
        guest('chmod', '0700', '/home/user/.ssh'); guest('chown', '-R', 'user:user', '/home/user/.ssh')
        put('/etc/sudoers.d/crange-user', 'user ALL=(ALL) NOPASSWD: ALL\n', '0440')
        guest('visudo', '-cf', '/etc/sudoers.d/crange-user')
        if distro.startswith('alpine'):
            guest('rc-update', 'add', 'sshd', 'default')
            put('/etc/init.d/crange-hostkeys', '#!/sbin/openrc-run\ndepend() { need localmount; before sshd; }\nstart() { /usr/bin/ssh-keygen -A; }\n', '0755')
            guest('rc-update', 'add', 'crange-hostkeys', 'boot')
            guest('rc-service', 'sshd', 'start')
        else:
            put('/etc/systemd/system/crange-hostkeys.service', '[Unit]\nDescription=Generate unique SSH keys for a CRANGE clone\nBefore=ssh.service\n[Service]\nType=oneshot\nExecStart=/usr/bin/ssh-keygen -A\nRemainAfterExit=yes\n[Install]\nWantedBy=multi-user.target\n')
            guest('systemctl', 'enable', 'crange-hostkeys.service', 'ssh.service')
            guest('systemctl', 'start', 'ssh.service')
        record['os_release'] = guest('cat', '/etc/os-release')
        guest('sh', '-c', 'ss -lnt | awk \'$4 ~ /:22$/ {found=1} END {exit !found}\'')
        record['status'] = 'configured';save()
        # Stop before sanitizing so template host identities cannot be reused.
        command(['pct', 'shutdown', str(args.vmid), '--timeout', '90'])
        command(['pct', 'mount', str(args.vmid)])
        try:
            root = Path(f'/var/lib/lxc/{args.vmid}/rootfs')
            if not (root/'etc/os-release').is_file():
                raise BuildError('Mounted guest root filesystem could not be identified.')
            for path in (root/'etc/ssh').glob('ssh_host_*'): path.unlink()
            for name in ('etc/machine-id', 'var/lib/dbus/machine-id'):
                p = root/name
                if p.is_file() and not p.is_symlink():p.write_text('')
            for path in (root/'var/lib/dhcp').glob('*leases*'):path.unlink()
            for path in root.glob('root/.*history'):path.unlink()
        finally:
            command(['pct', 'unmount', str(args.vmid)])
        command(['pct', 'set', str(args.vmid), '--net0', 'name=eth0,bridge=craccess,ip=manual,mtu=1450,firewall=1'])
        command(['pct', 'template', str(args.vmid)])
        record['status'] = 'template-created'; save()
        print(json.dumps(record, indent=2))
    except Exception as exc:
        record['status'] = 'failed'; record['error'] = str(exc); save();raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vmid', type=int, required=True)
    parser.add_argument('--archive', required=True)
    parser.add_argument('--ssh-key-file', type=Path, required=True)
    parser.add_argument('--password-file', type=Path, required=True)
    args = parser.parse_args()
    try:
        build(args)
    except (BuildError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print('crange: container base build failed: ' + str(exc), file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
