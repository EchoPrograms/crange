#!/usr/bin/env python3
"""Administrator-only Linux VM base builder; approval follows clone tests."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import urllib.request


class BuildError(Exception):
    pass


SOURCES = {
    'alpine-3-24': ('https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/cloud/', 'alpine-3.24.2-x86_64-cloudinit-r0.qcow2', 'sha512'),
    'debian-13': ('https://cloud.debian.org/images/cloud/trixie/', 'debian-13-genericcloud-amd64.qcow2', 'sha512'),
    'ubuntu-server-24-04': ('https://cloud-images.ubuntu.com/releases/noble/', 'ubuntu-24.04-server-cloudimg-amd64.img', 'sha256'),
}


def command(argv, timeout=1800):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        lines = (result.stderr or result.stdout).strip().splitlines()
        raise BuildError(f'{argv[0]} failed: {lines[-1] if lines else result.returncode}')
    return result.stdout


def validate_source(os_name, url, checksum):
    prefix, filename, algorithm = SOURCES[os_name]
    if not url.startswith(prefix) or url.rsplit('/', 1)[-1] != filename or any(c in url for c in ('?', '#', '..')):
        raise BuildError('Use the official amd64 cloud-image source for this OS.')
    if not re.fullmatch('[0-9a-f]{' + str(hashlib.new(algorithm).digest_size * 2) + '}', checksum):
        raise BuildError(f'Provide the published {algorithm.upper()} checksum.')
    return algorithm


def private_file(path):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise BuildError('Guest password file must be operator-owned with mode 0600.')
    if not path.read_text().strip():
        raise BuildError('Guest password file is empty.')


def alpine_machine_identity_service():
    """Use the clone hardware UUID as Alpine's persistent machine identity."""
    return '#!/sbin/openrc-run\ndepend() { need localmount; before cloud-init-local sshd; }\nstart() { tr -cd "[:xdigit:]" < /sys/class/dmi/id/product_uuid | tr "A-F" "a-f" > /etc/machine-id; }\n'


def build(args):
    if os.geteuid() != 0:
        raise BuildError('Run as root on the selected Proxmox node.')
    if not 100 <= args.vmid <= 999999:
        raise BuildError('Use a valid unused cluster guest ID.')
    algorithm = validate_source(args.os, args.source, args.checksum)
    private_file(args.password_file)
    key = args.ssh_key_file.read_text().strip()
    if '\n' in key or not key.startswith(('ssh-ed25519 ', 'ssh-rsa ', 'ecdsa-sha2-')):
        raise BuildError('Provide one SSH public key.')
    for tool in ('qm', 'pvesh', 'qemu-img', 'virt-customize'):
        if not shutil.which(tool):
            raise BuildError(f'Required administrator tool is missing: {tool}.')
    if not Path('/sys/class/net/craccess').exists():
        raise BuildError('Approved craccess build bridge is unavailable.')
    def unused():
        resources = json.loads(command(['pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json']))
        if any(int(r['vmid']) == args.vmid for r in resources):
            raise BuildError('Guest ID is occupied; refusing to overwrite it.')
    unused()
    directory = Path('/var/lib/crange-template-builds'); directory.mkdir(mode=0o750, parents=True, exist_ok=True)
    image = directory / f'{args.vmid}-build.qcow2'
    if image.exists():
        raise BuildError('A previous build image exists; inspect and resume or remove it explicitly.')
    record = {'schema_version': 1, 'template_id': args.vmid, 'node': os.uname().nodename,
              'kind': 'vm', 'os': args.os, 'source': args.source, 'source_checksum': args.checksum,
              'checksum_algorithm': algorithm, 'builder_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'built_at': datetime.now(timezone.utc).isoformat(), 'status': 'downloading'}
    record_path = directory / f'{args.vmid}.json'
    def save():
        record_path.write_text(json.dumps(record, indent=2) + '\n'); record_path.chmod(0o640)
    save()
    try:
        digest = hashlib.new(algorithm)
        with urllib.request.urlopen(args.source, timeout=60) as response, image.open('xb') as stream:
            image.chmod(0o600)
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 5 * 1024**3:
                    raise BuildError('Official image exceeds the build size limit.')
                digest.update(chunk); stream.write(chunk)
        if digest.hexdigest() != args.checksum:
            raise BuildError('Image differs from the published checksum; no guest was created.')
        info = json.loads(command(['qemu-img', 'info', '--output=json', str(image)]))
        if info['format'] != 'qcow2' or info.get('backing-filename'):
            raise BuildError('Use a standalone QCOW2 cloud image.')
        record['status'] = 'customizing'; save()
        hostname = 'crange-base-' + args.os
        # Fixed reviewed commands only. Password contents never enter process arguments.
        if args.os == 'alpine-3-24':
            command(['virt-customize', '-a', str(image), '--hostname', hostname,
                     '--run-command', 'apk add --no-cache openssh sudo qemu-guest-agent qemu-guest-agent-openrc curl ca-certificates iputils iproute2 python3 shadow',
                     '--run-command', 'id user >/dev/null 2>&1 || adduser -D -s /bin/ash user',
                     '--password', 'user:file:' + str(args.password_file),
                     '--ssh-inject', 'user:file:' + str(args.ssh_key_file),
                     '--run-command', "printf '%s\\n' 'ssh_pwauth: true' 'system_info:' '  default_user:' '    name: user' '    lock_passwd: false' '    doas: [permit nopass user]' > /etc/cloud/cloud.cfg.d/99-crange-user.cfg",
                     '--run-command', "mkdir -p /etc/sudoers.d; printf '%s\\n' 'user ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/crange-user; chmod 0440 /etc/sudoers.d/crange-user; visudo -cf /etc/sudoers.d/crange-user",
                     '--run-command', "printf '%s\\n' 'PasswordAuthentication yes' >> /etc/ssh/sshd_config; rc-update add sshd default; rc-update add qemu-guest-agent default",
                     '--run-command', 'mkdir -p /usr/lib/cloud-init; ln -s /usr/libexec/cloud-init/write-ssh-key-fingerprints /usr/lib/cloud-init/write-ssh-key-fingerprints',
                     '--write', '/etc/init.d/crange-machine-id:' + alpine_machine_identity_service(),
                     '--run-command', 'chmod 0755 /etc/init.d/crange-machine-id; rc-update add crange-machine-id boot',
                     '--run-command', 'cloud-init clean --logs; rm -f /etc/ssh/ssh_host_* /var/lib/dhcp/*leases* /etc/machine-id /var/lib/dbus/machine-id; rm -f /root/.*history /home/user/.*history'])
        else:
            command(['virt-customize', '-a', str(image), '--hostname', hostname,
                     '--install', 'openssh-server,qemu-guest-agent,sudo,curl,ca-certificates,iputils-ping,python3',
                     '--run-command', 'id user >/dev/null 2>&1 || useradd -m -s /bin/bash user',
                     '--password', 'user:file:' + str(args.password_file),
                     '--ssh-inject', 'user:file:' + str(args.ssh_key_file),
                     '--run-command', "printf '%s\\n' 'system_info:' '  default_user:' '    name: user' '    lock_passwd: false' > /etc/cloud/cloud.cfg.d/99-crange-user.cfg",
                     '--run-command', "printf '%s\\n' 'user ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/crange-user; chmod 0440 /etc/sudoers.d/crange-user; visudo -cf /etc/sudoers.d/crange-user",
                     '--run-command', "mkdir -p /etc/ssh/sshd_config.d; printf '%s\\n' 'PasswordAuthentication yes' > /etc/ssh/sshd_config.d/00-crange-access.conf; systemctl enable ssh qemu-guest-agent",
                     '--run-command', 'cloud-init clean --logs; rm -f /etc/ssh/ssh_host_* /var/lib/dhcp/*leases*; truncate -s 0 /etc/machine-id; rm -f /var/lib/dbus/machine-id; ln -s /etc/machine-id /var/lib/dbus/machine-id',
                     '--run-command', 'rm -f /root/.*history /home/user/.*history'])
        with image.open('rb') as stream:
            record['customized_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
        unused()
        command(['qm', 'create', str(args.vmid), '--name', hostname, '--memory', '2048', '--cores', '2',
                 '--bios', 'seabios', '--machine', 'q35', '--scsihw', 'virtio-scsi-single', '--ostype', 'l26',
                 '--agent', 'enabled=1,fstrim_cloned_disks=1', '--serial0', 'socket', '--vga', 'serial0',
                 '--net0', 'virtio,bridge=craccess,firewall=1,mtu=1450', '--onboot', '0',
                 '--pool', 'crange-iac', '--tags', 'range-lan;crange-base'])
        record['status'] = 'guest-created'; save()
        command(['qm', 'disk', 'import', str(args.vmid), str(image), 'local-lvm'])
        cfg = json.loads(command(['pvesh', 'get', f'/nodes/{os.uname().nodename}/qemu/{args.vmid}/config', '--output-format', 'json']))
        volume = cfg.get('unused0')
        if not volume or not volume.startswith(f'local-lvm:vm-{args.vmid}-disk-'):
            raise BuildError('Imported disk ownership differs from this build.')
        command(['qm', 'set', str(args.vmid), '--scsi0', volume + ',discard=on,ssd=1', '--ide2', 'local-lvm:cloudinit',
                 '--boot', 'order=scsi0', '--ciupgrade', '0', '--ciuser', 'user', '--sshkeys', str(args.ssh_key_file),
                 '--ipconfig0', 'ip=dhcp', '--nameserver', '10.60.2.1'])
        command(['qm', 'resize', str(args.vmid), 'scsi0', '20G'])
        command(['qm', 'template', str(args.vmid)])
        record['status'] = 'template-created'; record['hostname'] = hostname; save()
        image.unlink()
        print(json.dumps(record, indent=2))
    except Exception as exc:
        record['status'] = 'failed'; record['error'] = str(exc); save(); raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--os', choices=SOURCES, required=True)
    parser.add_argument('--vmid', type=int, required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--checksum', required=True)
    parser.add_argument('--ssh-key-file', type=Path, required=True)
    parser.add_argument('--password-file', type=Path, required=True)
    try:
        build(parser.parse_args())
    except (BuildError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print('crange: VM base build failed: ' + str(exc), file=sys.stderr); return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
