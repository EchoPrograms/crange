#!/usr/bin/env python3
"""Fixed, reviewed creation of a dedicated P5 thin pool; never touches existing LVs."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import time
import uuid

VG = 'hdd-lvm'
VG_UUID = 'owjdcT-tctf-scbC-Peiq-w3xw-De7r-yDrBkv'
POOL = 'crange_data'
STORAGE = 'crange-thin'
ROOT = Path('/var/lib/crange-storage')
GIB = 2**30


def run(args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=120)
    if result.returncode:
        raise ValueError((result.stderr.strip() or result.stdout.strip() or 'Storage command failed').splitlines()[-1])
    return result.stdout


def inventory():
    vg = json.loads(run(['vgs', '--reportformat', 'json', '--units', 'b', '--nosuffix', '-o', 'vg_name,vg_uuid,vg_size,vg_free', VG]))['report'][0]['vg']
    if len(vg) != 1 or vg[0]['vg_uuid'] != VG_UUID:
        raise ValueError('The reviewed unused HDD volume group identity changed.')
    lvs = json.loads(run(['lvs', '--reportformat', 'json', '--units', 'b', '--nosuffix', '-o', 'lv_name,lv_size,lv_attr,lv_tags,pool_lv', VG]))['report'][0]['lv']
    stores = json.loads(run(['pvesh', 'get', '/storage', '--output-format', 'json']))
    return {'vg': vg[0], 'lvs': lvs, 'storage': [s for s in stores if s['storage'] == STORAGE]}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def save(path, value):
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream); stream.flush(); os.fsync(stream.fileno())
    os.replace(tmp, path)


def review(size):
    before = inventory()
    if before['lvs'] or before['storage']:
        raise ValueError('This workflow only initializes an entirely unused HDD volume group.')
    total, free = (float(before['vg'][k]) for k in ('vg_size', 'vg_free'))
    if free - (size + 4)*GIB < max(total * .2, 64*GIB):
        raise ValueError('Thin pool would consume the protected HDD volume-group reserve.')
    identity = uuid.uuid4().hex
    plan = {'plan_id': identity, 'node': 'proxmox5', 'storage': STORAGE, 'vg': VG,
            'pool': POOL, 'size_gb': size, 'before': before, 'expires': time.time()+3600}
    plan['review_sha256'] = digest(plan)
    save(ROOT / (identity + '.json'), plan)
    return {k: plan[k] for k in ('plan_id', 'node', 'storage', 'vg', 'pool', 'size_gb', 'review_sha256')}


def snapshot_proof(identity):
    """Prove native COW using two tagged, disposable 64 MB LVs in the new pool."""
    tag = 'crange-storage-' + identity
    parent, child = ('crange_proof_' + identity[:12], 'crange_snap_' + identity[:12])
    before = inventory()
    existing = {lv['lv_name']:lv for lv in before['lvs'] if lv['lv_name'] in (parent,child)}
    for lv in existing.values():
        if (lv.get('pool_lv') != POOL or tag not in lv.get('lv_tags','').split(',') or
                not lv.get('lv_attr','').startswith('V') or float(lv['lv_size']) != 64*2**20):
            raise ValueError('Prior proof LV ownership or allocation differs; preserve it.')
    if child in existing and parent not in existing:
        raise ValueError('Prior proof has no parent; preserve it for inspection.')
    if parent not in existing:
        run(['lvcreate', '--yes', '-V', '64M', '-T', VG+'/'+POOL, '--addtag', tag, '-n', parent])
    def device(name, data=None):
        path = '/dev/'+VG+'/'+name
        fd = os.open(path, os.O_RDWR if data is not None else os.O_RDONLY)
        try:
            if not stat.S_ISBLK(os.fstat(fd).st_mode):
                raise ValueError('Proof LV is not a block device.')
            if data is not None:
                if os.pwrite(fd, data, 0) != len(data): raise ValueError('Incomplete proof write.')
                os.fsync(fd)
            return os.pread(fd, 4096, 0)
        finally: os.close(fd)
    original, replacement = b'A'*4096, b'B'*4096
    if parent not in existing: device(parent, original)
    elif device(parent) != original: raise ValueError('Retained proof parent content differs; preserve it.')
    if child not in existing:
        run(['lvcreate', '--yes', '--snapshot', '--addtag', tag, '-n', child, VG+'/'+parent])
    run(['lvchange', '--activate', 'y', '--ignoreactivationskip', VG+'/'+child])
    if device(child) not in (original, replacement):
        raise ValueError('Retained proof snapshot content differs; preserve it.')
    device(child, replacement)
    if device(parent) != original or device(child) != replacement:
        raise ValueError('New pool failed native snapshot COW verification.')
    for name in (child, parent):
        live = [lv for lv in inventory()['lvs'] if lv['lv_name'] == name]
        if (len(live) != 1 or live[0].get('pool_lv') != POOL or
                tag not in live[0].get('lv_tags','').split(',')):
            raise ValueError('Disposable proof LV ownership changed; preserve it.')
        run(['lvremove', '--yes', VG+'/'+name])
    if any(lv['lv_name'] in (parent, child) for lv in inventory()['lvs']):
        raise ValueError('Disposable storage proof cleanup is incomplete.')
    return True


def apply(identity, expected):
    if not re.fullmatch('[a-f0-9]{32}', identity) or not re.fullmatch('[a-f0-9]{64}', expected):
        raise ValueError('Invalid reviewed storage identity.')
    path = ROOT / (identity + '.json')
    plan = json.loads(path.read_text())
    unsigned = {k:v for k,v in plan.items() if k not in ('review_sha256', 'state')}
    if digest(unsigned) != expected or plan['review_sha256'] != expected:
        raise ValueError('Storage review differs from the approved digest.')
    tag = 'crange-storage-' + identity
    state = inventory()
    if plan.get('state') is None:
        if time.time() > plan['expires'] or state != plan['before']:
            raise ValueError('Storage inventory changed or review expired; create a fresh review.')
        plan['state'] = 'creating'; save(path, plan)
        run(['lvcreate', '--yes', '--type', 'thin-pool', '-L', str(plan['size_gb'])+'G',
             '--poolmetadatasize', '1G', '--addtag', tag, '-n', POOL, VG])
        state = inventory()
    pools = [lv for lv in state['lvs'] if lv['lv_name'] == POOL]
    if (len(pools) != 1 or not pools[0]['lv_attr'].startswith('t') or
            tag not in pools[0]['lv_tags'].split(',') or
            abs(float(pools[0]['lv_size']) - plan['size_gb']*GIB) > 4*2**20):
        raise ValueError('Pool creation is uncertain or ownership differs; preserve it for inspection.')
    if not state['storage']:
        run(['pvesm', 'add', 'lvmthin', STORAGE, '--vgname', VG, '--thinpool', POOL,
             '--nodes', 'proxmox5', '--content', 'images,rootdir'])
        state = inventory()
    storage = state['storage']
    if (len(storage) != 1 or storage[0].get('type') != 'lvmthin' or
            storage[0].get('vgname') != VG or storage[0].get('thinpool') != POOL or
            storage[0].get('nodes') != 'proxmox5' or storage[0].get('shared', 0)):
        raise ValueError('Proxmox storage definition differs from the reviewed local thin pool.')
    status = json.loads(run(['pvesh', 'get', '/nodes/proxmox5/storage/'+STORAGE+'/status', '--output-format', 'json']))
    if not status.get('active') or not status.get('total'):
        raise ValueError('Dedicated range storage is not active.')
    if plan.get('state') != 'ready':
        snapshot_proof(identity)
    # Grant only the controller's existing datastore role on this new pool.
    # User and privilege-separated token both need the exact storage ACL.
    acls=json.loads(run(['pveum','acl','list','--output-format','json']))
    for kind, principal, flag in (('user','crange-iac@pve','--users'),
                                  ('token','crange-iac@pve!terraform','--tokens')):
        def matches(row,path):
            return (row.get('path')==path and row.get('type')==kind and
                    row.get('ugid')==principal and row.get('roleid')=='PVEDatastoreUser' and
                    row.get('propagate')==1)
        if not any(matches(row,'/storage/local-lvm') for row in acls):
            raise ValueError('Controller datastore role differs from the audited baseline.')
        if not any(matches(row,'/storage/'+STORAGE) for row in acls):
            run(['pvesh','set','/access/acl','--path','/storage/'+STORAGE,
                 '--roles','PVEDatastoreUser',flag,principal,'--propagate','1'])
    verified=json.loads(run(['pveum','acl','list','--output-format','json']))
    for kind, principal in (('user','crange-iac@pve'),('token','crange-iac@pve!terraform')):
        if not any(row.get('path')=='/storage/'+STORAGE and row.get('type')==kind and
                   row.get('ugid')==principal and row.get('roleid')=='PVEDatastoreUser' and
                   row.get('propagate')==1 for row in verified):
            raise ValueError('New pool controller access is incomplete.')
    plan['state'] = 'ready'; save(path, plan)
    return {'plan_id': identity, 'node': 'proxmox5', 'storage': STORAGE,
            'size_gb': plan['size_gb'], 'state': 'ready',
            'snapshots_verified': True, 'proof_volumes_deleted': True,
            'note': 'Native snapshot COW verified; each image still requires its own clone acceptance.'}


def main():
    if os.geteuid() != 0 or socket.gethostname().split('.')[0] != 'proxmox5':
        raise ValueError('Storage preparation requires the fixed P5 root helper.')
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='action', required=True)
    r = sub.add_parser('review'); r.add_argument('--size-gb', type=int, choices=(256,512), required=True)
    a = sub.add_parser('apply'); a.add_argument('--plan-id', required=True); a.add_argument('--review-sha256', required=True)
    args = p.parse_args()
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if ROOT.is_symlink() or ROOT.stat().st_uid != 0 or ROOT.stat().st_mode & 0o077:
        raise ValueError('Storage journal must be private and root owned.')
    with open(ROOT / 'lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = review(args.size_gb) if args.action == 'review' else apply(args.plan_id, args.review_sha256)
    print(json.dumps(value))


if __name__ == '__main__':
    try: main()
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        import sys
        print('crange: '+str(exc).splitlines()[-1], file=sys.stderr); sys.exit(1)
