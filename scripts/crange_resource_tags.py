"""Idempotent migration of owned image/probe tags to categorical display tags.

This maintenance action changes only tags and notes, with a Proxmox digest
precondition. Offline nodes remain pending and are retried on the next pass.
The API boundary retains the original logical identity for ownership checks.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import time

from crange_resource_identity import IDENTITY, category_updates


def candidates(nodes, resources):
    online = {row['node'] for row in nodes if row.get('status') == 'online'}
    pending, ready = [], []
    for row in resources:
        tags = set(str(row.get('tags') or '').split(';'))
        if not any(IDENTITY.fullmatch(tag) for tag in tags):
            continue
        if (row.get('type') not in ('qemu', 'lxc')
                or row.get('node') not in {f'proxmox{i}' for i in range(1, 7)}
                or type(row.get('vmid')) is not int
                or row.get('pool') not in ('crange-iac', 'crange-probes')
                or not tags.intersection({'crange-image', 'crange-template-build'})):
            continue  # No generic rewriting of user-managed guests or tags.
        (ready if row['node'] in online else pending).append(row)
    return ready, pending


def migrate(api, resource, *, save):
    node, kind, vmid = resource['node'], resource['type'], resource['vmid']
    path = f'/nodes/{node}/{kind}/{vmid}/config'
    before = api.request('GET', path)
    if before.get('lock'):
        return {'node': node, 'vmid': vmid, 'status': 'locked'}
    if (not before.get('digest') or before.get('name', before.get('hostname')) != resource['name']
            or set(str(before.get('tags') or '').split(';')) != set(resource['tags'].split(';'))):
        raise ValueError('Image identity changed before tag migration.')
    fields = category_updates({'tags': before['tags']}, before)
    if fields.get('tags') == before['tags']:
        return {'node': node, 'vmid': vmid, 'status': 'unchanged'}
    # Persist recovery evidence BEFORE the single compare-and-set write.
    save({'node': node, 'kind': kind, 'vmid': vmid, 'state': 'prepared', 'before': before})
    api.request('PUT', path, {**fields, 'digest': before['digest']})
    after = api.request('GET', path)
    stable = lambda value: {k: v for k, v in value.items() if k != 'digest'}
    if stable(after) != stable(before):
        raise ValueError('Logical image configuration changed during tag migration; inspect the journal.')
    save({'node': node, 'kind': kind, 'vmid': vmid, 'state': 'verified', 'before': before})
    return {'node': node, 'vmid': vmid, 'status': 'verified', 'tags': fields['tags']}


def write_journal(root, resource, value):
    target = root / f'{resource["node"]}-{resource["vmid"]}.json'
    temporary = target.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    from crange import ProxmoxAPI
    root = Path.home() / '.local/state/crange/resource-tags'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    with (root / 'maintenance.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        api = ProxmoxAPI()
        api.request_timeout, api.read_attempts = 12, 1
        ready, pending = candidates(api.request('GET', '/nodes'),
                                    api.request('GET', '/cluster/resources', {'type': 'vm'}))
        report = {'created_at': time.time(), 'apply': args.apply, 'results': [],
                  'pending': [{'node': r['node'], 'vmid': r['vmid']} for r in pending]}
        for resource in ready:
            if not args.apply:
                report['results'].append({'node': resource['node'], 'vmid': resource['vmid'], 'status': 'planned'})
                continue
            def save(value):
                write_journal(root, resource, value)
            try:
                report['results'].append(migrate(api, resource, save=save))
            except (RuntimeError, ValueError, OSError) as exc:
                report['results'].append({'node': resource['node'], 'vmid': resource['vmid'],
                                          'status': 'retry', 'error_type': type(exc).__name__})
        # This public summary contains no guest configuration or credentials.
        print(json.dumps(report, sort_keys=True))
        if any(r['status'] == 'retry' for r in report['results']):
            raise SystemExit(1)


if __name__ == '__main__':
    main()
