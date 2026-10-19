#!/usr/bin/env python3
"""Single-action peer-UID checked relay; independent of signed network diagnostics."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import time


def validate(request, uid, expected_uid):
    if uid != expected_uid or request != {'action': 'grow'}:
        raise ValueError('Library growth request is not authorized.')


def serve(policy_path, expected_digest):
    policy_path=Path(policy_path)
    if policy_path.stat().st_uid!=0 or stat.S_IMODE(policy_path.stat().st_mode)!=0o600:
        raise ValueError('Storage relay policy must be root-owned mode0600.')
    policy=json.loads(policy_path.read_text());uid=policy['peer_uid']
    if type(uid) is not int or uid<100000 or policy['node_addresses'].get('proxmox5')!='10.0.0.105':
        raise ValueError('Storage relay worker or destination identity differs.')
    helper=Path('/usr/local/lib/crange/library_grow.py');info=helper.stat();source=helper.read_bytes()
    if (info.st_uid!=0 or info.st_mode&0o022 or not re.fullmatch('[a-f0-9]{64}',expected_digest) or
            hashlib.sha256(source).hexdigest()!=expected_digest):
        raise ValueError('Fixed storage helper source differs from its reviewed digest.')
    pid=subprocess.check_output(['lxc-info','-n','114','-pH'],text=True).strip()
    directory=Path('/proc')/pid/'root/run/crange-ct-read';path=directory/'library-grow.sock'
    if path.exists():path.unlink()
    listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    libc=ctypes.CDLL(None)
    for name in ('setfsuid','setfsgid'):
        fn=getattr(libc,name);fn.argtypes=[ctypes.c_uint];fn.restype=ctypes.c_uint
    cwd=os.open('.',os.O_RDONLY|os.O_DIRECTORY);runfd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
    old_gid=libc.setfsgid(uid);old_uid=libc.setfsuid(uid)
    try:
        if libc.setfsuid(0xFFFFFFFF)!=uid:raise ValueError('Worker filesystem UID is unavailable.')
        os.fchdir(runfd);listener.bind('library-grow.sock');os.chmod('library-grow.sock',0o600)
    finally:
        libc.setfsuid(old_uid);libc.setfsgid(old_gid);os.fchdir(cwd);os.close(cwd);os.close(runfd)
    def stop(*_):raise SystemExit(0)
    signal.signal(signal.SIGTERM,stop);listener.listen(4);listener.settimeout(5)
    try:
        while True:
            if not directory.exists():raise RuntimeError('Controller namespace changed; rebind storage relay.')
            try:connection,_=listener.accept()
            except socket.timeout:continue
            with connection:
                connection.settimeout(5)
                try:
                    _,peer,_=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,struct.calcsize('3i')))
                    raw=b''
                    while not raw.endswith(b'\n') and len(raw)<256:
                        part=connection.recv(256-len(raw))
                        if not part:break
                        raw+=part
                    validate(json.loads(raw),peer,uid)
                    result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=5','-o','StrictHostKeyChecking=yes',
                        '-o','HostKeyAlias=proxmox5','-o','UserKnownHostsFile=/etc/pve/nodes/proxmox5/ssh_known_hosts',
                        'root@10.0.0.105','python3','-'],input=source.decode(),capture_output=True,text=True,timeout=210)
                    if result.returncode:
                        detail=next((line for line in result.stderr.splitlines() if line.startswith('crange: ')), 'crange: Library expansion failed; inspect root service logs.')
                        raise ValueError(detail[:400])
                    reply=json.loads(result.stdout)
                    if reply.get('node')!='proxmox5' or reply.get('vmid')!=145 or reply.get('state')!='ready':
                        raise ValueError('Library expansion readback is incomplete.')
                    print(json.dumps({'event':'library-grow','peer_uid':peer,'status':'ready','after_gb':reply.get('after_gb'),'time':time.time()}),flush=True)
                except (ValueError,OSError,KeyError,subprocess.SubprocessError) as exc:
                    reply={'error':str(exc).splitlines()[-1]};print(json.dumps({'event':'library-grow','status':'failed','reason':reply['error']}),flush=True)
                connection.sendall(json.dumps(reply).encode()+b'\n')
    finally:
        listener.close();path.unlink(missing_ok=True)


if __name__=='__main__':
    serve(sys.argv[1],sys.argv[2])
