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
    if role not in ('web', 'worker'):
        raise ValueError('Invalid service role.')
    # The service is reachable through the controller's SSH tunnel and its
    # management interface. The standalone CLI server remains loopback-only.
    action = 'serve --host 0.0.0.0 --port 8000' if role == 'web' else 'worker'
    return '\n'.join([
        '[Unit]', f'Description=CRANGE {role}', 'After=network-online.target',
        'Wants=network-online.target', 'StartLimitIntervalSec=60', 'StartLimitBurst=5',
        '', '[Service]', 'Type=simple', f'User={username}',
        f'WorkingDirectory={root}',
        f'Environment={quote("HOME=" + str(home))}',
        'Environment=PYTHONUNBUFFERED=1',
        'Environment=CRANGE_SHARED_DB=1',
        'Environment=CRANGE_AUTH_DB=/var/lib/crange-web/.local/share/crange/auth.sqlite3',
        f'Environment={quote("CRANGE_JOB_DB=" + job_db)}',
        *(['Environment=CRANGE_WORKER_EXTERNAL=1'] if role == 'web' else []),
        f'ExecStart={quote(executable)} {quote(Path(root) / "scripts/crange.py")} {action}',
        'Restart=on-failure', 'RestartSec=5', 'TimeoutStopSec=30',
        'KillMode=control-group', 'UMask=0007', 'NoNewPrivileges=true',
        '', '[Install]', 'WantedBy=multi-user.target', '',
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
    services = {
        'crange.service': unit_text(root, web.pw_name, web_home, sys.executable, role='web', job_db=str(job_db)),
        'crange-worker.service': unit_text(root, username, account.pw_dir, sys.executable, role='worker', job_db=str(job_db)),
    }
    for filename, content in services.items():
        destination = Path('/etc/systemd/system') / filename
        temporary = destination.with_suffix('.service.new')
        temporary.write_text(content)
        temporary.chmod(0o644)
        temporary.replace(destination)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    for filename in services:
        subprocess.run(['systemctl', 'enable', '--now', filename], check=True)
        subprocess.run(['systemctl', 'restart', filename], check=True)
        subprocess.run(['systemctl', 'is-active', '--quiet', filename], check=True)
    print('Dashboard and worker enabled; dashboard listening on loopback port 8000.')
