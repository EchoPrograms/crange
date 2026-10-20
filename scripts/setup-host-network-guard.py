#!/usr/bin/python3
"""Install host-local recovery on the four explicitly requested Proxmox nodes."""
import json
import os
from pathlib import Path
import socket
import subprocess

HOSTS = {
    'proxmox1': ('10.0.0.101/24', 'eno1', True),
    'proxmox3': ('10.0.0.103/24', 'enp2s0', False),
    'proxmox4': ('10.0.0.104/24', 'enp3s0', False),
    'proxmox5': ('10.0.0.105/24', 'enp0s31f6', True),
}


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run as root on the intended Proxmox node')
    hostname = socket.gethostname().split('.')[0]
    if hostname not in HOSTS:
        raise SystemExit('Host is outside the requested installation scope')
    address, nic, mitigation = HOSTS[hostname]
    source = Path(__file__).resolve().parents[1] / 'ops/proxmox'
    config = {'hostname': hostname, 'bridge': 'vmbr0', 'address': address, 'nic': nic,
              'gateway': '10.0.0.1', 'mitigate_e1000e': mitigation,
              'peers': [f'10.0.0.{number}' for number in (101, 103, 104, 105) if f'10.0.0.{number}/24' != address]}
    # Review persistent identity before installing a service that can repair it.
    import importlib.util
    spec = importlib.util.spec_from_file_location('management_guard', source / 'management_guard.py')
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    config_path = Path('/etc/crange/management-guard.json')
    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if config_path.exists() and json.loads(config_path.read_text()) != config:
        raise SystemExit('Existing guard configuration differs; review required')
    # Validate against persistent interfaces before publishing the configuration.
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        staged = Path(directory) / 'config.json'
        staged.write_text(json.dumps(config))
        guard.CONFIG = staged
        guard.load_config()
    config_path.write_text(json.dumps(config, indent=2) + '\n')
    config_path.chmod(0o600)
    files = {'management_guard.py': ('/usr/local/sbin/crange-management-guard', 0o755),
             'cluster_rejoin_guard.py': ('/usr/local/sbin/crange-cluster-rejoin', 0o755)}
    for unit in ('crange-management-guard.service', 'crange-management-guard.timer',
                 'crange-cluster-rejoin.service', 'crange-cluster-rejoin.timer', 'crange-nic-mitigation.service'):
        files[unit] = ('/etc/systemd/system/' + unit, 0o644)
    for filename, (destination, mode) in files.items():
        target = Path(destination)
        temporary = target.with_suffix(target.suffix + '.crange-new')
        temporary.write_bytes((source / filename).read_bytes())
        temporary.chmod(mode)
        os.replace(temporary, target)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'start', 'crange-nic-mitigation.service', 'crange-management-guard.service', 'crange-cluster-rejoin.service'], check=True, timeout=190)
    subprocess.run(['systemctl', 'enable', '--now', 'crange-nic-mitigation.service', 'crange-management-guard.timer', 'crange-cluster-rejoin.timer'], check=True)
    print(json.dumps({'hostname': hostname, 'installed': True, 'e1000e_mitigation': mitigation}))


if __name__ == '__main__':
    main()
