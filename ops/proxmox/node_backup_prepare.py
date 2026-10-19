#!/usr/bin/env python3
"""Prepare native template backups with the smallest reviewed Proxmox package fix."""
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time

MINIMUM='9.0.18'
PACKAGE_SHA256='48391539c5f6bbdec567693dc6c62f4dfc1af32d5b8ca203e580efaa943a00fe'
CONTAINER_MINIMUM='6.0.10'
CONTAINER_SHA256='8b782027b45e78ae3bf6830746c35d00795c0d339f1555693d5788b2bf5f6519'


def prepare_containers(expected_node, *, run=subprocess.run):
    """Fix Debian minor-version rejection without upgrading cluster services."""
    def call(args, **kwargs):
        result=run(args,capture_output=True,text=True,timeout=180,**kwargs)
        if result.returncode:raise ValueError((result.stderr or result.stdout or 'Container preparation failed.').strip().splitlines()[-1])
        return result.stdout
    node=socket.gethostname().split('.')[0]
    if not re.fullmatch(r'proxmox[1-6]',str(expected_node)) or node!=expected_node:
        raise ValueError('Container-template request reached a different node.')
    installed=call(['dpkg-query','-W','-f','${Version}','pve-container']).strip()
    if run(['dpkg','--compare-versions',installed,'ge',CONTAINER_MINIMUM],capture_output=True,text=True,timeout=10).returncode==0:
        return {'node':node,'state':'ready','version':installed,'updated':False}
    if installed!='6.0.9':
        raise ValueError('This node needs administrator review of its pve-container version.')
    if json.loads(call(['pvesh','get',f'/nodes/{node}/tasks','--source','active','--limit','1000','--output-format','json'])):
        raise ValueError('Wait for active node tasks before preparing container templates.')
    simulation=call(['apt-get','-s','--only-upgrade','--no-install-recommends','install','pve-container='+CONTAINER_MINIMUM])
    if re.findall(r'^Inst (\S+)',simulation,re.M)!=['pve-container'] or re.search(r'^Remv ',simulation,re.M):
        raise ValueError('Container preparation would change dependencies; no update performed.')
    def packages():
        return sorted(line for line in call(['dpkg-query','-W','-f','${binary:Package} ${Version}\n']).splitlines()
                      if not line.startswith('pve-container '))
    before=packages()
    with tempfile.TemporaryDirectory(prefix='crange-container-support-') as directory:
        call(['apt-get','download','-o','Acquire::ForceIPv4=true','pve-container='+CONTAINER_MINIMUM],cwd=directory)
        archives=list(Path(directory).glob('*.deb'))
        if len(archives)!=1 or hashlib.sha256(archives[0].read_bytes()).hexdigest()!=CONTAINER_SHA256:
            raise ValueError('Container package differs from the reviewed artifact.')
        call(['apt-get','-y','--only-upgrade','--no-install-recommends','install',str(archives[0])],
             env={**os.environ,'DEBIAN_FRONTEND':'noninteractive','NEEDRESTART_MODE':'l'})
    after=call(['dpkg-query','-W','-f','${Version}','pve-container']).strip()
    if after!=CONTAINER_MINIMUM or packages()!=before:
        raise ValueError('Container package readback differs; inspect host state before retry.')
    call(['systemctl','restart','pvedaemon.service'])
    call(['systemctl','is-active','--quiet','pvedaemon.service'])
    return {'node':node,'state':'ready','before_version':installed,'version':after,
            'updated':True,'changed_packages':['pve-container'],'guests_rebooted':False}


def prepare(expected_node, *, run=subprocess.run):
    def call(args, **kwargs):
        result=run(args,capture_output=True,text=True,timeout=180,**kwargs)
        if result.returncode:raise ValueError((result.stderr or result.stdout or 'Node preparation failed.').strip().splitlines()[-1])
        return result.stdout
    node=socket.gethostname().split('.')[0]
    if not re.fullmatch(r'proxmox[1-6]',str(expected_node)) or node!=expected_node:
        raise ValueError('Template-backup request reached a different node.')
    installed=call(['dpkg-query','-W','-f','${Version}','qemu-server']).strip()
    comparison=run(['dpkg','--compare-versions',installed,'ge',MINIMUM],capture_output=True,text=True,timeout=10)
    if comparison.returncode==0:
        return {'node':node,'state':'ready','version':installed,'updated':False,'verified_at':time.time()}
    if installed not in ('9.0.16','9.0.17'):
        raise ValueError('This node needs administrator review of its qemu-server package version.')
    active=json.loads(call(['pvesh','get',f'/nodes/{node}/tasks','--source','active','--limit','1000','--output-format','json']))
    if active:raise ValueError('Wait for active node tasks before preparing template backups.')
    simulation=call(['apt-get','-s','--only-upgrade','--no-install-recommends','install','qemu-server='+MINIMUM])
    if re.findall(r'^Inst (\S+)',simulation,re.M)!=['qemu-server'] or re.search(r'^Remv ',simulation,re.M):
        raise ValueError('Package preparation would change dependencies; no update performed.')
    def packages():
        lines=call(['dpkg-query','-W','-f','${binary:Package} ${Version}\n']).splitlines()
        return sorted(line for line in lines if not line.startswith('qemu-server '))
    before=packages()
    with tempfile.TemporaryDirectory(prefix='crange-node-backup-') as directory:
        call(['apt-get','download','-o','Acquire::ForceIPv4=true','qemu-server='+MINIMUM],cwd=directory)
        archives=list(Path(directory).glob('*.deb'))
        if len(archives)!=1 or hashlib.sha256(archives[0].read_bytes()).hexdigest()!=PACKAGE_SHA256:
            raise ValueError('Downloaded Proxmox package differs from the reviewed artifact.')
        call(['apt-get','-y','--only-upgrade','--no-install-recommends','install',str(archives[0])],
             env={**os.environ,'DEBIAN_FRONTEND':'noninteractive','NEEDRESTART_MODE':'l'})
    after=call(['dpkg-query','-W','-f','${Version}','qemu-server']).strip()
    if after!=MINIMUM or packages()!=before:
        raise ValueError('Package readback differs; inspect host package state before retry.')
    # Reload management code after the package update. No VM/container power,
    # networking, storage, kernel, or unrelated package operation is permitted.
    call(['systemctl','restart','pvedaemon.service'])
    call(['systemctl','is-active','--quiet','pvedaemon.service'])
    return {'node':node,'state':'ready','before_version':installed,'version':after,
            'updated':True,'changed_packages':['qemu-server'],'guests_rebooted':False,
            'management_service_reloaded':True,'verified_at':time.time()}


if __name__=='__main__':
    try:
        node=globals().get('CRANGE_EXPECTED_NODE',socket.gethostname().split('.')[0])
        result=prepare(node)
        result['container_templates']=prepare_containers(node)
        print(json.dumps(result))
    except (ValueError,OSError,subprocess.SubprocessError) as exc:
        raise SystemExit('crange: '+str(exc).splitlines()[-1]) from None
