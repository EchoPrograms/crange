#!/usr/bin/python3
"""Recover an explicitly configured host address without bouncing guest bridges."""
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

CONFIG = Path('/etc/crange/management-guard.json')
STATE = Path('/var/lib/crange-management-guard/state.json')
EVIDENCE = Path('/var/log/crange-management-guard')


class RecoveryFailure(RuntimeError):
    """Only a demonstrated local network failure can request a reboot."""


def run(*args, timeout=8):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout


def load_config():
    cfg = json.loads(CONFIG.read_text())
    if cfg['hostname'] != socket.gethostname().split('.')[0]:
        raise ValueError('guard configuration belongs to another host')
    for name in ('bridge', 'nic'):
        if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', cfg[name]):
            raise ValueError('invalid interface')
    address = ipaddress.IPv4Interface(cfg['address'])
    gateway = ipaddress.IPv4Address(cfg['gateway'])
    if gateway not in address.network or address.ip == gateway:
        raise ValueError('invalid gateway')
    for peer in cfg['peers']:
        ipaddress.IPv4Address(peer)
    # An administrator changing persistent addressing takes precedence over us.
    text = Path('/etc/network/interfaces').read_text()
    match = re.search(r'(?m)^iface\s+' + re.escape(cfg['bridge']) + r'\s+inet\s+static\s*\n((?:[ \t]+[^\n]*\n|\n)*)', text)
    if not match:
        raise ValueError('configured static management bridge not found')
    stanza = match.group(1)
    for key, value in (('address', cfg['address']), ('gateway', cfg['gateway']), ('bridge-ports', cfg['nic'])):
        if not re.search(r'(?m)^\s+' + key + r'\s+' + re.escape(value) + r'\s*$', stanza):
            raise ValueError('persistent management configuration changed; refusing repair')
    return cfg


def save_state(state):
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(state))
    temporary.chmod(0o600)
    os.replace(temporary, STATE)


def read_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def recent_hang(cfg):
    try:
        output = run('journalctl', '-b', '-k', '--since=-5min', '--no-pager', '-n', '400', '-o', 'cat', timeout=12)
    except (OSError, subprocess.SubprocessError):
        return False
    return any(re.search(r'(?<![\w.-])' + re.escape(cfg['nic']) + r'(?![\w.-])', line)
               and ('Detected Hardware Unit Hang' in line or 'NETDEV WATCHDOG' in line)
               for line in output.splitlines())


def ping(cfg, target):
    try:
        run('ping', '-n', '-I', cfg['bridge'], '-c', '1', '-W', '1', target, timeout=2)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def evidence(cfg, reason):
    EVIDENCE.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Bound disk use even if a fault persists for days.
    previous = sorted(EVIDENCE.glob('incident-*.json'))
    for old in previous[:-19]:
        old.unlink()
    data = {'time': time.time(), 'reason': reason, 'hostname': cfg['hostname']}
    for name, cmd in (
        ('addresses', ('ip', '-j', 'address')),
        ('routes', ('ip', '-j', 'route')),
        ('kernel', ('journalctl', '-b', '-k', '-n', '100', '--no-pager', '-o', 'cat')),
    ):
        try:
            data[name] = run(*cmd)[:32000]
        except (OSError, subprocess.SubprocessError) as exc:
            data[name] = type(exc).__name__
    target = EVIDENCE / f'incident-{time.time_ns()}.json'
    target.write_text(json.dumps(data, indent=2))
    target.chmod(0o600)


def mitigate(cfg):
    if not cfg.get('mitigate_e1000e'):
        return
    driver = Path('/sys/class/net') / cfg['nic'] / 'device/driver'
    if not driver.exists():
        return  # Let address/link recovery handle a disappeared physical NIC.
    if driver.resolve().name != 'e1000e':
        raise ValueError('NIC driver changed; refusing offload changes')
    features = run('ethtool', '-k', cfg['nic'])
    if any(re.search(r'(?m)^' + name + r': on\b', features)
           for name in ('tcp-segmentation-offload', 'generic-segmentation-offload')):
        run('ethtool', '-K', cfg['nic'], 'tso', 'off', 'gso', 'off')
        print('management-guard: applied e1000e TSO/GSO mitigation', flush=True)


def repair(cfg):
    try:
        links = json.loads(run('ip', '-j', 'address', 'show', 'dev', cfg['bridge']))
        nic = json.loads(run('ip', '-j', 'link', 'show', 'dev', cfg['nic']))
    except (OSError, subprocess.SubprocessError) as exc:
        raise RecoveryFailure('management interface unavailable') from exc
    if not links or not nic:
        raise RecoveryFailure('management interface missing')
    expected = ipaddress.IPv4Interface(cfg['address'])
    ipv4 = [a for a in links[0].get('addr_info', []) if a.get('family') == 'inet']
    present = any(a.get('local') == str(expected.ip) and a.get('prefixlen') == expected.network.prefixlen for a in ipv4)
    if not present and ipv4:
        raise ValueError('different management address is present; refusing overwrite')
    try:
        for interface, link in ((cfg['nic'], nic[0]), (cfg['bridge'], links[0])):
            if 'UP' not in link.get('flags', []):
                run('ip', 'link', 'set', 'dev', interface, 'up')
        if not present:
            evidence(cfg, 'management address missing')
            run('ip', 'address', 'add', cfg['address'], 'dev', cfg['bridge'])
            print('management-guard: restored management address', flush=True)
        routes = json.loads(run('ip', '-j', 'route', 'show', 'default'))
        if not routes:
            run('ip', 'route', 'add', 'default', 'via', cfg['gateway'], 'dev', cfg['bridge'])
            print('management-guard: restored missing default route', flush=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RecoveryFailure('local address/link/route recovery failed') from exc
    current = json.loads(run('ip', '-j', 'address', 'show', 'dev', cfg['bridge']))
    if not any(a.get('local') == str(expected.ip) and a.get('prefixlen') == expected.network.prefixlen for a in current[0]['addr_info']):
        raise RecoveryFailure('management address remains missing')
    if not ping(cfg, cfg['gateway']) and recent_hang(cfg) and not any(ping(cfg, peer) for peer in cfg['peers']):
        raise RecoveryFailure('gateway and cluster peers unreachable with recent local NIC transmit hang')


def escalate(cfg, reason):
    state = read_state()
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    failures = state.get('failures', 0) + 1 if state.get('boot') == boot else 1
    now = time.time()
    state.update(boot=boot, failures=failures, reason=reason)
    uptime = float(Path('/proc/uptime').read_text().split()[0])
    reboot = failures >= 6 and uptime >= 300 and now - state.get('last_reboot', 0) >= 3600
    if failures == 1 or reboot:
        evidence(cfg, reason)
    if reboot:
        state['last_reboot'] = now
    save_state(state)
    print(f'management-guard: recovery failure {failures}/6: {reason}', flush=True)
    if reboot:
        # Native Proxmox HA/watchdog remains responsible for fencing. Never force
        # reset a host, alter quorum, or move guest identities from this service.
        run('systemctl', 'reboot', timeout=15)


def main():
    cfg = load_config()
    with open('/run/crange-management-guard.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # A mitigation/config/tool failure must not masquerade as a reboot reason.
        try:
            mitigate(cfg)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f'management-guard: mitigation could not run: {type(exc).__name__}', flush=True)
        try:
            repair(cfg)
        except RecoveryFailure as exc:
            escalate(cfg, str(exc))
        else:
            state = read_state()
            if state.get('failures'):
                state['failures'] = 0
                save_state(state)


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['--mitigate']:
            mitigate(load_config())
        elif sys.argv[1:]:
            raise ValueError('unsupported guard arguments')
        else:
            main()
    except BlockingIOError:
        pass
