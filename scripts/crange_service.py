"""Install separate CRANGE web and automation services."""
from pathlib import Path
import grp
import os
import pwd
import shutil
import subprocess
import sys


def unit_text(root, username, home, executable, *, role='web', job_db='/var/lib/crange-jobs/jobs.sqlite3'):
    def quote(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
    if not username or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_-' for c in username):
        raise ValueError('Invalid service account.')
    if any(c.isspace() for c in str(root)):
        raise ValueError('Service checkout path cannot contain whitespace.')
    if role not in ('web', 'worker', 'guac-broker', 'proxmox-compat', 'cluster-refresh'):
        raise ValueError('Invalid service role.')
    # The service is reachable through the controller's SSH tunnel and its
    # management interface. The standalone CLI server remains loopback-only.
    action = {'web': 'serve --host 0.0.0.0 --port 8000',
              'worker': 'worker', 'guac-broker': 'guac-ticket-broker',
              'proxmox-compat': '', 'cluster-refresh': 'cluster-refresh'}[role]
    return '\n'.join([
        '[Unit]', f'Description=CRANGE {role}', 'After=network-online.target',
        'Wants=network-online.target', 'StartLimitIntervalSec=60', 'StartLimitBurst=5',
        '', '[Service]', f'Type={"oneshot" if role == "cluster-refresh" else "simple"}', f'User={username}',
        f'WorkingDirectory={root}',
        f'Environment={quote("HOME=" + str(home))}',
        'Environment=PYTHONUNBUFFERED=1',
        'Environment=CRANGE_SHARED_DB=1',
        'Environment=CRANGE_DISTRIBUTION_DATA=/var/lib/crange-distribution',
        'Environment=CRANGE_AUTH_DB=/var/lib/crange-web/.local/share/crange/auth.sqlite3',
        f'Environment={quote("CRANGE_JOB_DB=" + job_db)}',
        *(['Environment=CRANGE_WORKER_EXTERNAL=1'] if role == 'web' else []),
        *(['Environment=CRANGE_GUAC_JSON_KEY=/home/crange/.config/crange/guacamole-json.secret',
           'Environment=CRANGE_GUAC_TICKET_SOCKET=/run/crange-guac/ticket.sock',
           'Environment=CRANGE_WEB_USER=crange-web', 'RuntimeDirectory=crange-guac',
           'RuntimeDirectoryMode=0770'] if role == 'guac-broker' else []),
        f'ExecStart={quote(executable)} {quote(Path(root) / ("scripts/crange_proxmox_compat.py" if role == "proxmox-compat" else "scripts/crange.py"))} {action}',
        *(['TimeoutStartSec=600'] if role == 'cluster-refresh' else
          ['Restart=on-failure', 'RestartSec=5', 'TimeoutStopSec=30']),
        'KillMode=control-group', 'UMask=0007', 'NoNewPrivileges=true',
        '', '[Install]', 'WantedBy=multi-user.target', '',
    ])


def cluster_refresh_timer_text():
    # OnUnitInactiveSec waits until the prior refresh has finished, so a slow
    # full-cluster scan cannot overlap the next one.
    return '\n'.join([
        '[Unit]', 'Description=Refresh CRANGE cluster capacity inventory',
        '', '[Timer]', 'OnBootSec=90s', 'OnUnitInactiveSec=60s',
        'AccuracySec=15s', 'Unit=crange-cluster-refresh.service',
        '', '[Install]', 'WantedBy=timers.target', '',
    ])


def install(username, root):
    if os.geteuid() != 0:
        raise ValueError('Run crange served with sudo to install the system service.')
    account = pwd.getpwnam(username)
    if account.pw_uid == 0:
        raise ValueError('The dashboard must run as an unprivileged account.')
    root = Path(root).resolve()
    if not (root / 'scripts/crange.py').is_file():
        raise ValueError('Missing CLI entry point.')
    group = grp.getgrnam(username)
    try:
        web = pwd.getpwnam('crange-web')
    except KeyError:
        subprocess.run(['useradd', '--system', '--create-home', '--home-dir', '/var/lib/crange-web',
                        '--gid', username, '--shell', '/usr/sbin/nologin', 'crange-web'], check=True)
        web = pwd.getpwnam('crange-web')
    if web.pw_gid != group.gr_gid:
        raise ValueError('crange-web must have the CRANGE group as its primary group.')
    os.chmod(account.pw_dir, 0o710)
    web_home = Path(web.pw_dir)
    web_home.mkdir(parents=True, exist_ok=True)
    os.chown(web_home, web.pw_uid, group.gr_gid)
    os.chmod(web_home, 0o710)
    target_auth = web_home / '.local/share/crange/auth.sqlite3'
    target_auth.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in (web_home / '.local', web_home / '.local/share', target_auth.parent):
        os.chown(directory, web.pw_uid, group.gr_gid)
        os.chmod(directory, 0o710)
    if not target_auth.exists():
        original = Path(account.pw_dir) / '.local/share/crange/auth.sqlite3'
        if original.is_file():
            shutil.copyfile(original, target_auth)
            os.chown(target_auth, web.pw_uid, group.gr_gid)
            os.chmod(target_auth, 0o660)
    if target_auth.exists():
        os.chown(target_auth, web.pw_uid, group.gr_gid)
        os.chmod(target_auth, 0o660)
    job_dir = Path('/var/lib/crange-jobs')
    job_dir.mkdir(parents=True, exist_ok=True)
    os.chown(job_dir, web.pw_uid, group.gr_gid)
    os.chmod(job_dir, 0o2770)
    job_db = job_dir / 'jobs.sqlite3'
    job_db.touch(exist_ok=True)
    os.chown(job_db, web.pw_uid, group.gr_gid)
    os.chmod(job_db, 0o660)
    distribution_dir = Path('/var/lib/crange-distribution')
    distribution_dir.mkdir(parents=True, exist_ok=True)
    os.chown(distribution_dir, account.pw_uid, group.gr_gid)
    # Inherit the explicit shared group even when worker primary group differs.
    # The web user can traverse this directory and read only mode-0640 snapshots;
    # the ledger and cache journals retain owner-only permissions.
    os.chmod(distribution_dir, 0o2710)
    snapshot = distribution_dir / "cluster-inventory.json"
    if snapshot.is_file():
        os.chown(snapshot, account.pw_uid, group.gr_gid)
        os.chmod(snapshot, 0o640)
    ledger = distribution_dir / "reservations.sqlite3"
    if ledger.is_file():
        os.chown(ledger, account.pw_uid, group.gr_gid)
        os.chmod(ledger, 0o600)
    services = {
        'crange.service': unit_text(root, web.pw_name, web_home, sys.executable, role='web', job_db=str(job_db)),
        'crange-worker.service': unit_text(root, username, account.pw_dir, sys.executable, role='worker', job_db=str(job_db)),
        'crange-cluster-refresh.service': unit_text(root, username, account.pw_dir, sys.executable,
                                                   role='cluster-refresh', job_db=str(job_db)),
    }
    compat_config = Path(account.pw_dir) / '.config/crange/proxmox-compat.json'
    if compat_config.is_file():
        from crange_proxmox_compat import load_config
        config = load_config(compat_config, owner_uid=account.pw_uid)
        if config['port'] != 18766:
            raise ValueError('Installed compatibility service requires loopback port 18766.')
        compat_unit = unit_text(root, username, account.pw_dir, sys.executable, role='proxmox-compat', job_db=str(job_db))
        # Start the reviewed adapter before workers can resume queued jobs.
        services = {'crange-proxmox-compat.service': compat_unit, **services}
        services['crange-worker.service'] = services['crange-worker.service'].replace(
            'After=network-online.target',
            'After=network-online.target crange-proxmox-compat.service\nRequires=crange-proxmox-compat.service')
    json_key = Path(account.pw_dir) / '.config/crange/guacamole-json.secret'
    if json_key.is_file():
        if json_key.stat().st_mode & 0o077:
            raise ValueError('Guacamole JSON-auth key must not be accessible to group or other users.')
        if subprocess.run([sys.executable, '-c', 'import cryptography'], capture_output=True).returncode:
            raise ValueError('Install Python cryptography before enabling Guacamole launch tickets.')
        services['crange-guac-broker.service'] = unit_text(
            root, username, account.pw_dir, sys.executable, role='guac-broker', job_db=str(job_db))
    else:
        print('Guacamole launch tickets disabled; generate the protected shared key with crange guac-json-key.')
    for filename, content in services.items():
        destination = Path('/etc/systemd/system') / filename
        temporary = destination.with_suffix('.service.new')
        temporary.write_text(content)
        temporary.chmod(0o644)
        temporary.replace(destination)
    timer = Path('/etc/systemd/system/crange-cluster-refresh.timer')
    temporary_timer = timer.with_suffix('.timer.new')
    temporary_timer.write_text(cluster_refresh_timer_text())
    temporary_timer.chmod(0o644)
    temporary_timer.replace(timer)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    for filename in services:
        if filename == 'crange-cluster-refresh.service':
            continue
        subprocess.run(['systemctl', 'enable', '--now', filename], check=True)
        subprocess.run(['systemctl', 'restart', filename], check=True)
        subprocess.run(['systemctl', 'is-active', '--quiet', filename], check=True)
    subprocess.run(['systemctl', 'enable', '--now', timer.name], check=True)
    print('Dashboard and worker enabled; dashboard listening on loopback port 8000.')
