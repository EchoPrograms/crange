#!/usr/bin/python3
"""Restore only P2's configured management address; never bounce bridges."""
import fcntl,json,pathlib,socket,subprocess,sys,time

def run(*args):
    return subprocess.check_output(args,text=True)

STATE=pathlib.Path('/var/lib/proxmox2-management-guard/state.json')

def escalate(reason):
    STATE.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    state=json.loads(STATE.read_text()) if STATE.exists() else {}
    boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    failures=state.get('failures',0)+1 if state.get('boot')==boot else 1
    now=time.time()
    state.update(boot=boot,failures=failures,reason=reason)
    uptime=float(pathlib.Path('/proc/uptime').read_text().split()[0])
    reboot=failures>=6 and uptime>=300 and now-state.get('last_reboot',0)>=3600
    if reboot: state['last_reboot']=now
    STATE.write_text(json.dumps(state));STATE.chmod(0o600)
    print(f'management-guard: recovery failed ({failures}/6): {reason}',flush=True)
    if reboot:
        print('management-guard: requesting graceful host reboot; recovery exhausted',flush=True)
        subprocess.run(['systemctl','reboot'],check=True,timeout=15)


def check_recovery():
    links=json.loads(run('ip','-j','address','show','dev','vmbr0'))
    if not any(a.get('local')=='10.0.0.102' and a.get('prefixlen')==24 for a in links[0]['addr_info']):
        raise RuntimeError('management address remains missing')
    # A gateway outage alone must not reboot a healthy node. Require a recent
    # local NIC hang as well, from this boot only.
    reachable=subprocess.run(['ping','-I','vmbr0','-c','2','-W','2','10.0.0.1'],
                             stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=6).returncode==0
    if not reachable:
        kernel=run('journalctl','-b','-k','--since','5 minutes ago','--no-pager')
        if any('eno1' in line and 'Detected Hardware Unit Hang' in line for line in kernel.splitlines()):
            raise RuntimeError('gateway unreachable with recent eno1 hardware transmit hang')
    if STATE.exists():
        state=json.loads(STATE.read_text());state['failures']=0
        STATE.write_text(json.dumps(state))


def main():
    if socket.gethostname().split('.')[0] != 'proxmox2':
        raise RuntimeError('refusing to run on another host')
    lock=open('/run/proxmox2-management-guard.lock','w')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
        repair()
        check_recovery()
    except Exception as exc:
        escalate(str(exc))

def repair():
    links=json.loads(run('ip','-j','address','show','dev','vmbr0'))
    addresses=links[0]['addr_info']
    missing=not any(a.get('local')=='10.0.0.102' and a.get('prefixlen')==24 for a in addresses)
    if missing:
        # Preserve evidence before restoration, without collecting secrets.
        out=pathlib.Path('/var/log/proxmox2-management-guard');out.mkdir(mode=0o700,exist_ok=True)
        import time
        evidence={'time':time.time(),'addresses':links,'routes':json.loads(run('ip','-j','route')),
                  'kernel':run('journalctl','-k','-n','100','--no-pager')}
        (out/f'incident-{time.time_ns()}.json').write_text(json.dumps(evidence,indent=2))
        subprocess.run(['ip','address','add','10.0.0.102/24','dev','vmbr0'],check=True)
        print('management-guard: restored 10.0.0.102/24 on vmbr0',flush=True)
    routes=json.loads(run('ip','-j','route','show','default'))
    if not routes:
        subprocess.run(['ip','route','add','default','via','10.0.0.1','dev','vmbr0'],check=True)
        print('management-guard: restored missing default route',flush=True)

if __name__=='__main__':
    try: main()
    except BlockingIOError: pass
    except Exception as exc:
        print(f'management-guard: {exc}',file=sys.stderr);sys.exit(1)
