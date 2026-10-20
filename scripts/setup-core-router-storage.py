#!/usr/bin/env python3
"""Operator-only installation of the core's dedicated router HA disk store.

Run on Proxmox4. A review prints the exact prerequisites; --apply creates only
the new, journaled thin volume. Existing volumes/filesystems are never formatted.
This host administration command is not exposed through the web job runner.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess


POOL_UUID = 'mG5beI-dvYe-e3HC-V3Af-HfqX-0mMz-vQW6wm'
STORE = 'crange-router-ha'
LV = 'pve/crange_router_ha'
DEVICE = '/dev/pve/crange_router_ha'
MOUNT = Path('/srv/crange-router-ha')
ROOT = Path('/root/crange-resilience/router-storage')
SIZE_GIB = 96


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=90).stdout.strip()


def inspect():
    if os.geteuid() != 0 or socket.gethostname() != 'proxmox4':
        raise ValueError('Run only as root on the designated surviving core, proxmox4.')
    if 'Quorate:          Yes' not in run('pvecm', 'status'):
        raise ValueError('The cluster must have quorum.')
    pool = json.loads(run('lvs', 'pve/data', '--reportformat', 'json', '--units', 'b', '--nosuffix',
                          '-o', 'lv_uuid,lv_size,data_percent,metadata_percent'))['report'][0]['lv'][0]
    free = float(pool['lv_size']) * (1 - float(pool['data_percent']) / 100)
    if (pool['lv_uuid'] != POOL_UUID or float(pool['metadata_percent']) >= 10 or
            free < (SIZE_GIB + 64) * 2**30):
        raise ValueError('Core thin pool identity or physical headroom changed; review storage first.')
    storage = json.loads(run('pvesh', 'get', '/storage', '--output-format', 'json'))
    return {'core': 'proxmox4', 'pool_uuid': POOL_UUID, 'volume': LV, 'size_gib': SIZE_GIB,
            'physical_headroom_gib': round(free / 2**30, 2),
            'physical_headroom_after_max_growth_gib': round(free / 2**30 - SIZE_GIB, 2),
            'mount': str(MOUNT), 'storage': STORE,
            'existing_storage': next((row for row in storage if row['storage'] == STORE), None)}


def save(receipt):
    target = ROOT / 'receipt.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(receipt, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(target)


def install(review):
    # An interrupted installation is inspected, never replayed automatically.
    if ROOT.exists() or review['existing_storage'] or MOUNT.exists() or Path(DEVICE).exists():
        raise ValueError('Installation already exists or is uncertain; inspect its receipt before continuing.')
    for command in ('lvcreate', 'mkfs.ext4', 'mount', 'exportfs', 'pvesm', 'blkid'):
        run('which', command)
    fstab = Path('/etc/fstab')
    original = fstab.read_bytes()
    if STORE.encode() in original:
        raise ValueError('An existing router storage mount requires operator review.')
    export = Path('/etc/exports.d/crange-router-ha.exports')
    if export.exists():
        raise ValueError('An existing router export requires operator review.')
    ROOT.mkdir(mode=0o700, parents=True)
    (ROOT / 'fstab.before').write_bytes(original)
    receipt = {'review': review, 'phase': 'creating-volume', 'fstab_sha256': hashlib.sha256(original).hexdigest()}
    save(receipt)
    run('lvcreate', '--yes', '--virtualsize', f'{SIZE_GIB}G', '--thinpool', 'pve/data', '--name', 'crange_router_ha')
    receipt.update(phase='volume-created', volume_uuid=run('lvs', LV, '--noheadings', '-o', 'lv_uuid'))
    save(receipt)
    # Format exactly the just-created LV, and only when it has no signature.
    signature = subprocess.run(['blkid', DEVICE], capture_output=True, text=True, timeout=15)
    if signature.returncode != 2:
        raise ValueError('New volume unexpectedly has a filesystem; formatting refused.')
    run('mkfs.ext4', '-q', '-m', '0', '-L', STORE, DEVICE)
    uuid = run('blkid', '-s', 'UUID', '-o', 'value', DEVICE)
    receipt.update(phase='formatted', filesystem_uuid=uuid)
    save(receipt)
    MOUNT.mkdir(mode=0o755)
    if fstab.read_bytes() != original:
        raise ValueError('fstab changed concurrently; retaining new volume for inspection.')
    with fstab.open('a') as stream:
        stream.write(f'\n# CRANGE core router disks; {ROOT}/receipt.json\nUUID={uuid} {MOUNT} ext4 defaults,noatime 0 2\n')
    run('systemctl', 'daemon-reload')
    run('mount', str(MOUNT))
    mounted = json.loads(run('findmnt', '--json', '--target', str(MOUNT), '-o', 'UUID,TARGET'))['filesystems'][0]
    if mounted['uuid'] != uuid or mounted['target'] != str(MOUNT):
        raise ValueError('Router export mount identity differs; export refused.')
    receipt['phase'] = 'mounted'
    save(receipt)
    clients = ' '.join(f'10.0.0.{100+i}(rw,sync,no_subtree_check,no_root_squash)' for i in range(1, 7))
    export.write_text(f'# Dedicated core storage for HA routers; fixed cluster hosts only.\n{MOUNT} {clients}\n')
    export.chmod(0o644)
    run('exportfs', '-ra')
    receipt['phase'] = 'exported'
    save(receipt)
    run('pvesm', 'add', 'nfs', STORE, '--server', '10.0.0.104', '--export', str(MOUNT),
        '--content', 'rootdir', '--options', 'vers=4.2',
        '--nodes', ','.join(f'proxmox{i}' for i in range(1, 7)))
    receipt['phase'] = 'controller-permissions'
    save(receipt)
    grant_controller_storage()
    receipt['phase'] = 'installed'
    save(receipt)
    print(json.dumps(receipt, indent=2))


def grant_controller_storage():
    """Reuse only the existing disk-consumer role on this one new datastore."""
    role = json.loads(run('pvesh', 'get', '/access/roles/PVEDatastoreUser', '--output-format', 'json'))
    if {key for key, value in role.items() if value} != {'Datastore.Audit', 'Datastore.AllocateSpace'}:
        raise ValueError('Storage consumer role has unexpected privileges; grant refused.')
    for field, principal in (('--users', 'crange-iac@pve'), ('--tokens', 'crange-iac@pve!terraform')):
        run('pvesh', 'set', '/access/acl', '--path', '/storage/' + STORE,
            field, principal, '--roles', 'PVEDatastoreUser', '--propagate', '1')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    review = inspect()
    if args.apply:
        install(review)
    else:
        print(json.dumps(review, indent=2))
