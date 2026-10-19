#!/usr/bin/python3
"""Fixed host guard for CRANGE's addressless, unrouted capture bridge."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

BRIDGE = 'crprobe1450'
TABLE = 'crange_capture_isolation'
SCRIPT = '/usr/local/lib/crange/private_bridge_guard.py'
STATE = Path('/var/lib/crange-private-bridge/owner.json')
KEYS = {f'net.ipv4.conf.{BRIDGE}.{key}': '0' for key in ('forwarding','proxy_arp','accept_redirects','send_redirects')}
KEYS[f'net.ipv6.conf.{BRIDGE}.disable_ipv6'] = '1'
RULES = [('input','iifname'),('forward','iifname'),('forward','oifname'),('output','oifname')]
HOOK = f'''#!/bin/sh
[ "${{IFACE:-}}" = "{BRIDGE}" ] || exit 0
exec /usr/bin/python3 {SCRIPT} prepare
'''
UNIT = f'''[Unit]
Description=CRANGE private capture bridge isolation
After=networking.service
Before=pve-guests.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {SCRIPT} prepare
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
'''


def run(args, *, check=True, input=None):
    return subprocess.run(args,input=input,text=True,capture_output=True,check=check,timeout=30)


def topology(vmid=None, *, preparing=False):
    addresses=json.loads(run(['ip','-d','-j','addr','show','dev',BRIDGE]).stdout)
    ports=json.loads(run(['bridge','-j','link','show','master',BRIDGE]).stdout)
    if len(addresses)!=1 or addresses[0].get('linkinfo',{}).get('info_kind')!='bridge':
        raise ValueError('Fixed capture interface is not a Linux bridge.')
    for address in addresses[0].get('addr_info',[]):
        if not (preparing and address.get('family')=='inet6' and address.get('scope')=='link'):
            raise ValueError('Capture bridge has a host address.')
    if any(vmid is None or not re.fullmatch(fr'tap{vmid}i[0-9]+',port.get('ifname','')) for port in ports):
        raise ValueError('Capture bridge has an uplink or another guest port.')
    return {'ports':[port['ifname'] for port in ports],'host_addresses':[]}


def check_nft(document):
    entries=document.get('nftables',[])
    chains=[x['chain'] for x in entries if 'chain' in x]
    rules=[x['rule'] for x in entries if 'rule' in x]
    if len(chains)!=3 or len(rules)!=4:
        raise ValueError('Private bridge firewall shape changed.')
    for hook in ('input','forward','output'):
        selected=[x for x in chains if x.get('name')==hook]
        if (len(selected)!=1 or selected[0].get('table')!=TABLE or selected[0].get('family')!='inet'
                or selected[0].get('hook')!=hook or selected[0].get('type')!='filter'
                or selected[0].get('prio')!=-210 or selected[0].get('policy')!='accept'):
            raise ValueError('Private bridge firewall hook changed.')
    expected=[]
    for chain,key in RULES:
        expected.append({'chain':chain,'expr':[{'match':{'op':'==','left':{'meta':{'key':key}},'right':BRIDGE}},{'drop':None}]})
    actual=[{key:row.get(key) for key in ('chain','expr')} for row in rules]
    if sorted(actual,key=str)!=sorted(expected,key=str):
        raise ValueError('Private bridge firewall rules changed.')


def verify(vmid=None):
    result=topology(vmid)
    if (not STATE.is_file() or STATE.is_symlink() or STATE.stat().st_uid!=0
            or stat.S_IMODE(STATE.stat().st_mode)!=0o600):
        raise ValueError('Prepare the private capture network before use.')
    if json.loads(STATE.read_text())!={'bridge':BRIDGE,'table':TABLE,'schema_version':1}:
        raise ValueError('Private bridge ownership record differs.')
    for path, text, mode in [('/etc/network/if-up.d/crange-capture-isolation',HOOK,0o755),
                              ('/etc/systemd/system/crange-capture-isolation.service',UNIT,0o644)]:
        file=Path(path);info=file.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=mode
                or file.read_text()!=text):
            raise ValueError('Private bridge persistence differs from its owned configuration.')
    if run(['systemctl','is-enabled','crange-capture-isolation.service']).stdout.strip()!='enabled':
        raise ValueError('Private bridge boot guard is not enabled.')
    check_nft(json.loads(run(['nft','-j','list','table','inet',TABLE]).stdout))
    for key,value in KEYS.items():
        if run(['sysctl','-n',key]).stdout.strip()!=value:
            raise ValueError('Private bridge routing/IPv6 isolation changed.')
    return {**result,'bridge':BRIDGE,'host_and_routed_traffic_blocked':True,'verified':True}


def write_owned(path, text, mode):
    path=Path(path)
    if path.exists() or path.is_symlink():
        info=path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or path.read_text()!=text):
            raise ValueError('Refusing to overwrite an unmanaged guard file.')
    else:
        path.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
        with os.fdopen(fd,'w') as handle:handle.write(text)
    os.chmod(path,mode)


def prepare():
    topology(preparing=True)
    current=run(['nft','-j','list','table','inet',TABLE],check=False)
    if current.returncode==0:
        if not STATE.is_file() or STATE.is_symlink() or STATE.stat().st_uid!=0:
            raise ValueError('Refusing to replace an unmanaged firewall table.')
        check_nft(json.loads(current.stdout))
    commands=[f'add table inet {TABLE}']
    commands += [f'add chain inet {TABLE} {hook} {{ type filter hook {hook} priority -210; policy accept; }}' for hook in ('input','forward','output')]
    commands += [f'add rule inet {TABLE} {chain} {key} "{BRIDGE}" drop' for chain,key in RULES]
    if current.returncode!=0:
        run(['nft','-f','-'],input='\n'.join(commands)+'\n')
    for key,value in KEYS.items():run(['sysctl','-q','-w',key+'='+value])
    write_owned(STATE,json.dumps({'bridge':BRIDGE,'table':TABLE,'schema_version':1},sort_keys=True),0o600)
    write_owned('/etc/network/if-up.d/crange-capture-isolation',HOOK,0o755)
    write_owned('/etc/systemd/system/crange-capture-isolation.service',UNIT,0o644)
    run(['systemctl','daemon-reload']);run(['systemctl','enable','crange-capture-isolation.service'])
    return verify()


if __name__=='__main__':
    try:
        if len(sys.argv)==2 and sys.argv[1]=='prepare':result=prepare()
        elif len(sys.argv)==3 and sys.argv[1]=='proof' and re.fullmatch(r'[0-9]{3,9}',sys.argv[2]):result=verify(int(sys.argv[2]))
        else:raise ValueError('Unsupported fixed private-bridge action.')
        print(json.dumps(result,sort_keys=True))
    except Exception as exc:
        raise SystemExit('crange: private bridge guard: '+str(exc).splitlines()[0]) from None
