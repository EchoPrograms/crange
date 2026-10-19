#!/usr/bin/env python3
"""Root-owned fixed online growth for the range NFS data volume only."""
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time

def library_grow(*, run=subprocess.run, read_text=None, journal_path=None):
    path=Path(journal_path or '/var/lib/crange-library-grow.json')
    fd=os.open(path.with_suffix('.lock'),os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return _library_grow_locked(run=run,read_text=read_text,journal_path=path)
    finally:os.close(fd)


def _library_grow_locked(*, run=subprocess.run, read_text=None, journal_path=None):
    """Grow only the fixed range library data volume by 64 GiB, without restarting NFS."""
    read_text=read_text or (lambda path:Path(path).read_text())
    path=Path(journal_path or '/var/lib/crange-library-grow.json')
    gib=2**30
    def call(args):
        result=run(args,text=True,capture_output=True,timeout=180)
        if result.returncode:raise ValueError('Library disk operation failed; preserve its grow journal and inspect root logs.')
        return result.stdout
    def config():
        cfg=dict(line.split(': ',1) for line in read_text('/etc/pve/lxc/145.conf').splitlines() if ': ' in line and not line.startswith('#'))
        parts=cfg.get('mp0','').split(',')
        sizes=[v.removeprefix('size=') for v in parts if v.startswith('size=')]
        if (cfg.get('hostname')!='crange-library' or cfg.get('lock') or
                not cfg.get('rootfs','').startswith('ssd-lvm:vm-145-disk-0,') or
                parts[0]!='ssd-lvm:vm-145-disk-1' or 'mp=/srv/crange-library' not in parts or
                'ip=10.0.0.249/24' not in cfg.get('net0','').split(',') or
                len(sizes)!=1 or not re.fullmatch(r'[0-9]+G',sizes[0])):
            raise ValueError('Fixed library data volume or container identity changed.')
        return int(sizes[0][:-1])
    def mount():
        rows=json.loads(call(['pct','exec','145','--','findmnt','--bytes','--json','--output','TARGET,SOURCE,FSTYPE,SIZE','--target','/srv/crange-library'])).get('filesystems',[])
        if (len(rows)!=1 or rows[0].get('target')!='/srv/crange-library' or
                rows[0].get('source')!='/dev/mapper/ssd--lvm-vm--145--disk--1' or rows[0].get('fstype')!='ext4'):
            raise ValueError('Library filesystem is not the exact mounted SSD data volume.')
        return int(rows[0]['size'])
    def save(record):
        temporary=path.with_name('.'+path.name+'.'+os.urandom(8).hex())
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            with os.fdopen(fd,'w') as stream:
                json.dump(record,stream);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,path)
            directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:temporary.unlink(missing_ok=True)
    if path.is_symlink() or (path.exists() and (path.stat().st_uid!=os.getuid() or stat.S_IMODE(path.stat().st_mode)!=0o600)):
        raise ValueError('Library growth journal ownership or mode differs.')
    current=config();mount()
    vg=json.loads(call(['vgs','--reportformat','json','--units','b','--nosuffix','-o','vg_name,vg_uuid,vg_size,vg_free','ssd-lvm']))['report'][0]['vg']
    if len(vg)!=1 or vg[0]['vg_uuid']!='XjBFk8-GlBE-HKNP-u1DE-XZSr-05cZ-Kz72mZ':
        raise ValueError('Reviewed SSD volume-group identity changed.')
    pending=json.loads(path.read_text()) if path.exists() else None
    if pending and pending.get('state')!='ready':
        if (pending.get('volume')!='ssd-lvm:vm-145-disk-1' or
                type(pending.get('before_gb')) is not int or
                pending.get('target_gb')!=pending['before_gb']+64 or
                current not in (pending['before_gb'],pending['target_gb'])):
            raise ValueError('Prior library growth is ambiguous; no second resize submitted.')
        record=pending
    else:record={'volume':'ssd-lvm:vm-145-disk-1','before_gb':current,'target_gb':current+64,'state':'prepared'}
    if not 400<=record['before_gb']<record['target_gb']<=1024:
        raise ValueError('Library growth is outside its reviewed capacity bounds.')
    extra=max(record['target_gb']-current,0)*gib
    if float(vg[0]['vg_free'])-extra < max(float(vg[0]['vg_size'])*.10,100*gib):
        raise ValueError('Library growth would consume the protected SSD host reserve.')
    save(record)
    if current!=record['target_gb']:
        call(['pct','resize','145','mp0',str(record['target_gb'])+'G'])
    if config()!=record['target_gb'] or mount()<record['target_gb']*gib*.95:
        raise ValueError('Library volume/filesystem expansion is unconfirmed; keep the grow journal.')
    record.update(state='ready',completed_at=time.time())
    save(record)
    return {'node':'proxmox5','vmid':145,'state':'ready','before_gb':record['before_gb'],
            'after_gb':record['target_gb'],'nfs_restarted':False,'guests_rebooted':False}



if __name__ == '__main__':
    try:print(json.dumps(library_grow()))
    except (ValueError,OSError,subprocess.SubprocessError) as exc:raise SystemExit('crange: '+str(exc).splitlines()[-1]) from None
