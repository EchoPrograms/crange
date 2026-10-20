#!/usr/bin/python3
"""Repair only an older local Corosync copy after agreement from quorate peers."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import time

PEERS = tuple(f'proxmox{i}' for i in range(1, 7))
LOCAL = Path('/etc/corosync/corosync.conf')
BACKUPS = Path('/var/lib/crange-cluster-rejoin')
SNAPSHOT = """import json,subprocess,socket
from pathlib import Path
p=subprocess.run(['pvecm','status'],capture_output=True,text=True,timeout=10)
print(json.dumps({'host':socket.gethostname().split('.')[0], 'quorate':p.returncode==0 and bool(__import__('re').search(r'(?m)^Quorate:\\s+Yes\\s*$',p.stdout)), 'configuration':Path('/etc/pve/corosync.conf').read_text()}))
"""


def run(*args, timeout=12):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=timeout).stdout


def version(configuration):
    values = re.findall(r'(?m)^\s*config_version:\s*(\d+)\s*$', configuration)
    if len(values) != 1:
        raise ValueError('expected exactly one configuration version')
    return int(values[0])


def compatible(old, new):
    """Only version/vote changes qualify; never change members, links or identity."""
    if version(new) <= version(old):
        return False
    def normalized(value):
        return re.sub(r'(?m)^\s*(?:config_version|quorum_votes):\s*\d+\s*$', '', value)
    return normalized(old) == normalized(new)


def agreed_configuration(snapshots, hostname):
    usable = [s for s in snapshots if s.get('host') in PEERS and s['host'] != hostname and s.get('quorate') is True]
    # Distinct authenticated hosts must agree byte for byte. No quorum overrides.
    if len({s['host'] for s in usable}) < 2:
        raise ValueError('two quorate peers are required')
    configurations = {s['configuration'] for s in usable}
    if len(configurations) != 1:
        raise ValueError('healthy peers disagree; manual investigation required')
    return configurations.pop()


def peer_snapshot(peer):
    if peer not in PEERS:
        raise ValueError('unknown cluster peer')
    # Proxmox maintains these pins independently of interactive SSH histories.
    # Never use accept-new or disable host-key checking for automatic recovery.
    output = run('ssh', '-oBatchMode=yes', '-oConnectTimeout=3', '-oStrictHostKeyChecking=yes',
                 '-oUserKnownHostsFile=/etc/pve/nodes/' + peer + '/ssh_known_hosts',
                 '-oGlobalKnownHostsFile=/dev/null', '-oHostKeyAlias=' + peer,
                 'root@' + peer, shlex.join(['python3', '-c', SNAPSHOT]), timeout=18)
    snapshot = json.loads(output)
    if snapshot.get('host') != peer:
        raise ValueError('peer hostname differs from SSH target')
    return snapshot


def recover(hostname):
    if hostname not in PEERS:
        raise ValueError('not a configured cluster host')
    state = subprocess.run(['systemctl', 'is-active', 'corosync'], capture_output=True, text=True, timeout=8).stdout.strip()
    if state != 'failed':
        return  # Never interfere with active Corosync, quorum loss or HA fencing.
    if run('systemctl', 'is-active', 'pve-cluster').strip() != 'active':
        return
    old = LOCAL.read_text()
    snapshots = []
    for peer in PEERS:
        if peer == hostname:
            continue
        try:
            snapshots.append(peer_snapshot(peer))
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    candidate = agreed_configuration(snapshots, hostname)
    if not compatible(old, candidate):
        print('cluster-rejoin: configuration is not an older compatible copy; no action', flush=True)
        return
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    # One attempt per five minutes, retained across service invocations.
    attempt = BACKUPS / 'last-attempt'
    if attempt.exists() and time.time() - attempt.stat().st_mtime < 300:
        return
    attempt.touch(mode=0o600)
    backup = BACKUPS / f'corosync-{time.time_ns()}.before'
    backup.write_text(old)
    backup.chmod(0o600)
    staged = LOCAL.with_name('corosync.conf.crange-rejoin')
    staged.write_text(candidate)
    staged.chmod(0o644)
    try:
        run('corosync', '-t', '-c', str(staged), timeout=20)
        if LOCAL.read_text() != old:
            raise ValueError('local configuration changed during review')
        state = subprocess.run(['systemctl', 'is-active', 'corosync'], capture_output=True, text=True, timeout=8).stdout.strip()
        if state != 'failed':
            raise ValueError('Corosync state changed during review')
        os.replace(staged, LOCAL)
        run('systemctl', 'start', 'corosync', timeout=40)
        print(json.dumps({'action': 'started-corosync-with-verified-local-copy', 'version': version(candidate),
                          'sha256': hashlib.sha256(candidate.encode()).hexdigest()}), flush=True)
    finally:
        staged.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        with open('/run/crange-cluster-rejoin.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            recover(socket.gethostname().split('.')[0])
    except BlockingIOError:
        pass
