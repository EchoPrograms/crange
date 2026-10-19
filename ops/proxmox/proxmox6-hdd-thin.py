#!/usr/bin/env python3
"""Root maintenance: initialize only Proxmox6's confirmed-empty local-hdd VG.

Keeps the storage ID; refuses existing LVs or guest references. No disks, VGs,
existing volumes or guest configs are erased. Retains the review if interrupted.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import time
import uuid

VG = 'local-hdd'
POOL = 'crange_data'
NODE = 'proxmox6'


def run(args):
    p = subprocess.run(args, capture_output=True, text=True, timeout=180)
    if p.returncode:
        raise ValueError('Storage command failed: ' + args[0] + ': ' + p.stderr.strip().splitlines()[-1])
    return p.stdout


def initialize():
    if os.geteuid() != 0 or socket.gethostname().split('.')[0] != NODE:
        raise ValueError('Run only as root on Proxmox6.')
    store = json.loads(run(['pvesh', 'get', '/storage/'+VG, '--output-format', 'json']))
    if (store.get('type') != 'lvm' or store.get('vgname') != VG or
            store.get('nodes') != NODE or store.get('shared', 0)):
        raise ValueError('Expected the original local, node-6-only thick LVM definition.')
    vg = json.loads(run(['vgs', '--reportformat', 'json', '--units', 'b', '--nosuffix',
                        '-o', 'vg_name,vg_uuid,vg_size,vg_free', VG]))['report'][0]['vg']
    lvs = json.loads(run(['lvs', '--reportformat', 'json', '-o', 'lv_name', VG]))['report'][0]['lv']
    if len(vg) != 1 or lvs or float(vg[0]['vg_free']) != float(vg[0]['vg_size']):
        raise ValueError('Volume group is not completely unused; preserve it.')
    if json.loads(run(['pvesh', 'get', '/nodes/'+NODE+'/storage/'+VG+'/content', '--output-format', 'json'])):
        raise ValueError('Storage still has content.')
    for pattern in ('*/qemu-server/*.conf', '*/lxc/*.conf'):
        if any(VG+':' in p.read_text() for p in Path('/etc/pve/nodes').glob(pattern)):
            raise ValueError('A guest or snapshot references this storage.')
    identity = uuid.uuid4().hex
    evidence = Path('/var/lib/crange-storage')/('p6-hdd-'+identity)
    evidence.mkdir(parents=True, mode=0o700)
    shutil.copy2('/etc/pve/storage.cfg', evidence/'storage-before.cfg')
    (evidence/'review.json').write_text(json.dumps({'node':NODE,'vg':vg[0],
        'storage':store,'empty_lvs_verified':True,'guest_references_absent':True}, indent=2))
    # Keep 10% of the VG free for pool/metadata repair and future extension.
    run(['lvcreate', '--yes', '--type', 'thin-pool', '-l', '90%FREE',
         '--poolmetadatasize', '4G', '--chunksize', '256K',
         '--addtag', 'crange-storage-'+identity, '-n', POOL, VG])
    run(['lvchange', '--monitor', 'y', VG+'/'+POOL])
    # Native COW proof writes only two newly-created, randomly named proof LVs.
    parent, child = 'crange_proof_'+identity[:12], 'crange_snap_'+identity[:12]
    run(['lvcreate', '--yes', '-V', '64M', '-T', VG+'/'+POOL, '-n', parent])
    def block(name, data=None):
        fd = os.open('/dev/'+VG+'/'+name, os.O_RDWR if data is not None else os.O_RDONLY)
        try:
            if not stat.S_ISBLK(os.fstat(fd).st_mode): raise ValueError('Expected proof block device.')
            if data is not None:
                if os.pwrite(fd, data, 0) != len(data): raise ValueError('Incomplete proof write.')
                os.fsync(fd)
            return os.pread(fd, 4096, 0)
        finally: os.close(fd)
    block(parent, b'A'*4096)
    run(['lvcreate', '--yes', '-s', '-n', child, VG+'/'+parent])
    run(['lvchange', '--activate', 'y', '--ignoreactivationskip', VG+'/'+child])
    if block(child) != b'A'*4096: raise ValueError('Snapshot does not match parent.')
    block(child, b'B'*4096)
    if block(parent) != b'A'*4096 or block(child) != b'B'*4096:
        raise ValueError('Snapshot copy-on-write proof failed; preserve evidence.')
    for name in (child, parent): run(['lvremove', '--yes', VG+'/'+name])
    # Replace only the empty storage registration, keeping its ID and content.
    run(['pvesm', 'remove', VG])
    try:
        run(['pvesm', 'add', 'lvmthin', VG, '--vgname', VG, '--thinpool', POOL,
             '--nodes', NODE, '--content', store['content']])
    except Exception:
        run(['pvesm', 'add', 'lvm', VG, '--vgname', VG, '--nodes', NODE,
             '--content', store['content'], '--shared', '0'])
        raise
    status = json.loads(run(['pvesh', 'get', '/nodes/'+NODE+'/storage/'+VG+'/status', '--output-format', 'json']))
    if not status.get('active') or status.get('shared', 0): raise ValueError('Thin storage unavailable.')
    proof = {'node':NODE,'storage':VG,'vg':VG,'thinpool':POOL,'state':'ready',
             'snapshots_verified':True,'proof_volumes_deleted':True,
             'total_gb':status['total']/2**30,'evidence':str(evidence),
             'created_at':time.time(),'vg_uuid':vg[0]['vg_uuid']}
    (evidence/'receipt.json').write_text(json.dumps(proof, indent=2))
    return proof


if __name__ == '__main__':
    try: print(json.dumps(initialize()))
    except Exception as error: raise SystemExit('crange: '+str(error).splitlines()[0]) from None
