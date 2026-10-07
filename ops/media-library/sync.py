#!/usr/bin/env python3
"""Mirror only cluster ISO/LXC archives; preserve removed versions until explicit cleanup."""
from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import shlex
import socket
import subprocess
import tempfile

PRIMARY = pathlib.Path('/mnt/pve/crange-library')
STATUS = pathlib.Path('/var/lib/crange-media-library/status.json')
MIRRORS = [
    ('media-mirror1', 'proxmox1', '/mnt/pve/ssd-dir/cluster-media'),
    ('media-mirror4', 'proxmox4', '/var/lib/vz/cluster-media'),
]
AUDIT_INTERVAL = datetime.timedelta(hours=24)


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=1800, **kwargs)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def parse_time(value):
    try:
        return datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def same_fingerprint(record, stat):
    return all(record.get(key) == stat[key] for key in ('size', 'mtime_ns', 'inode'))


def remote_stats(node, paths, *, local_node):
    """Return stat fingerprints without reading file contents."""
    if node == local_node:
        result = {}
        for path in paths:
            try:
                st = pathlib.Path(path).stat()
                result[path] = {'size': st.st_size, 'mtime_ns': st.st_mtime_ns, 'inode': st.st_ino}
            except OSError:
                result[path] = None
        return result

    script = (
        'import json,os,sys; paths=json.load(sys.stdin); out={}; '
        '[(out.__setitem__(p, (lambda s: {"size":s.st_size,"mtime_ns":s.st_mtime_ns,"inode":s.st_ino})(os.stat(p)))) '
        'if os.path.isfile(p) else out.__setitem__(p,None) for p in paths]; '
        'print(json.dumps(out))'
    )
    command = shlex.join(['python3', '-c', script])
    result = subprocess.run(
        ['ssh', 'root@' + node, command], input=json.dumps(paths), text=True,
        capture_output=True, check=True, timeout=120,
    )
    return json.loads(result.stdout)


def check_hashes(node, checks, *, local_node):
    """Hash only files whose stat changed or whose daily audit is due."""
    if not checks:
        return {}
    data = ''.join(f"{sha}  {path}\n" for path, sha in checks.items())
    command = ['sha256sum', '--check']
    if node != local_node:
        command = ['ssh', 'root@' + node, *command]
    result = subprocess.run(command, input=data, text=True, capture_output=True, timeout=1800)
    verified = {}
    for line in result.stdout.splitlines():
        if line.endswith(': OK'):
            verified[line[:-4]] = True
        elif line.endswith(': FAILED'):
            verified[line[:-8]] = False
    return {path: verified.get(path, False) for path in checks}


def force_copy(source, destination, *, node, local_node):
    if node == local_node:
        run(['rsync', '-a', '--checksum', str(source), str(destination)])
    else:
        run(['rsync', '-a', '--checksum', str(source), f'root@{node}:{destination}'])


def write_status(value):
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=STATUS.parent, delete=False) as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        temporary = stream.name
    os.chmod(temporary, 0o644)
    os.replace(temporary, STATUS)
    # The controller reads this report without host credentials.
    run(['ssh', 'root@proxmox4', 'pct', 'exec', '114', '--', 'mkdir', '-p', '/var/lib/crange-media-library'])
    subprocess.run(
        ['ssh', 'root@proxmox4', 'pct', 'exec', '114', '--', 'tee', str(STATUS)],
        input=json.dumps(value), text=True, capture_output=True, check=True, timeout=30,
    )


def main():
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    with (STATUS.parent / 'sync.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with socket.create_connection(('10.0.0.249', 2049), timeout=3):
            pass
        if not os.path.ismount(PRIMARY):
            raise RuntimeError('Primary library is not mounted')

        previous = json.loads(STATUS.read_text()) if STATUS.exists() else {}
        old = {(row['content'], row['name']): row for row in previous.get('files', [])}
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        now = now_dt.isoformat()
        local_node = socket.gethostname().split('.')[0]
        files = []

        for content, folder in [('iso', 'template/iso'), ('vztmpl', 'template/cache')]:
            for path in sorted((PRIMARY / folder).glob('*')):
                if not path.is_file() or not path.stat().st_size or path.name.startswith('.'):
                    continue
                stat = path.stat()
                identity = {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'inode': stat.st_ino}
                prior = old.get((content, path.name), {})
                sha = prior.get('sha256') if same_fingerprint(prior, identity) else None
                if not sha:
                    sha = digest(path)
                files.append({
                    'content': content, 'name': path.name, **identity, 'sha256': sha,
                    'copies': [{
                        'storage': 'crange-library', 'sha256': sha, **identity, 'checked_at': now,
                    }],
                })

        mirrors = []
        for storage, node, destination in MIRRORS:
            entry = {'storage': storage, 'node': node, 'checked_at': now, 'status': 'failed'}
            try:
                run(['ssh', 'root@' + node, 'mkdir', '-p', destination + '/template/iso', destination + '/template/cache'])
                # No delete: missing primary files never erase older mirror versions.
                for folder in ['template/iso', 'template/cache']:
                    run([
                        'rsync', '-a', '--delay-updates', '--exclude=.*',
                        str(PRIMARY / folder) + '/', f'root@{node}:{destination}/{folder}/',
                    ])

                source_paths = {
                    (PRIMARY / ('template/cache' if item['content'] == 'vztmpl' else 'template/iso') / item['name']).as_posix(): item
                    for item in files
                }
                destination_paths = {
                    f'{destination}/template/{"cache" if item["content"] == "vztmpl" else "iso"}/{item["name"]}': item
                    for item in files
                }
                stats = remote_stats(node, list(destination_paths), local_node=local_node)
                old_copies = {
                    (item['content'], item['name']): {
                        copy['storage']: copy for copy in item.get('copies', [])
                    }
                    for item in old.values()
                }
                due = {}
                for dest_path, item in destination_paths.items():
                    current = stats.get(dest_path)
                    prior_copy = old_copies.get((item['content'], item['name']), {}).get(storage, {})
                    audit_at = parse_time(prior_copy.get('checked_at'))
                    audit_due = not audit_at or now_dt - audit_at >= AUDIT_INTERVAL
                    if current is None or not same_fingerprint(prior_copy, current) or audit_due:
                        due[dest_path] = item['sha256']

                checks = check_hashes(node, due, local_node=local_node)
                failures = [path for path, verified in checks.items() if not verified]
                for dest_path in failures:
                    item = destination_paths[dest_path]
                    key = (PRIMARY / ('template/cache' if item['content'] == 'vztmpl' else 'template/iso') / item['name']).as_posix()
                    force_copy(source_paths[key], dest_path, node=node, local_node=local_node)
                if failures:
                    fixed_paths = {path: destination_paths[path]['sha256'] for path in failures}
                    fixed = check_hashes(node, fixed_paths, local_node=local_node)
                    if not all(fixed.values()):
                        raise RuntimeError('Mirror SHA256 mismatch after repair')
                    stats = remote_stats(node, failures, local_node=local_node)

                for dest_path, item in destination_paths.items():
                    current = stats.get(dest_path)
                    if current is None:
                        raise RuntimeError('Mirror file is missing: ' + dest_path)
                    prior_copy = old_copies.get((item['content'], item['name']), {}).get(storage, {})
                    item['copies'].append({
                        'storage': storage, 'sha256': item['sha256'], **current,
                        'checked_at': now if dest_path in due else prior_copy.get('checked_at', now),
                    })
                entry['status'] = 'ready'
            except (subprocess.SubprocessError, OSError, RuntimeError, KeyError, ValueError) as error:
                entry['error'] = str(error)[:200]
            mirrors.append(entry)

        result = {
            'updated_at': now, 'primary_storage': 'crange-library',
            'files': files, 'mirrors': mirrors,
        }
        write_status(result)
        print(json.dumps({'files': len(files), 'mirrors': mirrors}))
        if any(mirror['status'] != 'ready' for mirror in mirrors):
            raise RuntimeError('One or more library mirrors failed verification')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit('media-library: ' + str(error))
