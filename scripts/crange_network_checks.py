"""Fixed owned-probe network tests; never accepts shell or endpoint strings."""
import ipaddress
import re
import time
from crange_network_acceptance import require, scope
from crange_network_adapters import cluster_probe


def drop_counter(text):
    matches=[]
    for line in text.splitlines():
        match=re.match(r'\[(\d+):(\d+)\] (.+)$',line)
        if not match:continue
        fields=match[3].split()
        def option(key):
            return fields[fields.index(key)+1] if key in fields else None
        if (option('-A')=='ufw-before-forward' and option('-i')=='eth1'
                and option('-d')=='10.0.0.0/8' and option('-j')=='DROP'
                and set((option('--ctstate') or '').split(','))=={'NEW','INVALID','UNTRACKED'}):
            matches.append(int(match[1]))
    require(len(matches)==1,'Exact static UFW 10/8 isolation DROP rule not found.')
    return matches[0]


class FixedGuestNetworkChecks:
    def __init__(self, transport, router, *, router_node, other_team_id, sleep=time.sleep):
        require(router_node in {f'proxmox{i}' for i in range(1,6)},'Router must bind to an enrolled node.')
        require(type(other_team_id) is int and 1 <= other_team_id <= 253,'Bind one other regular team.')
        self.transport=transport
        self.router=router
        self.router_node=router_node
        self.other_team_id=other_team_id
        self.sleep=sleep

    def run(self,target,check,*,timeout=60):
        target=scope(**target)
        require(check in ('vxlan_mtu','dns','internet_egress','team_isolation')
                and type(timeout) is int and 1 <= timeout <= 60,'Unsupported or unbounded guest check.')
        deadline=time.monotonic()+timeout
        read=lambda method,path,fields=None:self.transport.request(method,path,fields,deadline=deadline)
        class DeadlineAPI:
            def request(self,method,path,fields=None):return read(method,path,fields)
        require(cluster_probe(DeadlineAPI(),target) is not None,'Owned network probe missing.')
        base=f'/nodes/{target["node"]}/qemu/{target["vmid"]}'
        logs=[]
        def observe(name):
            remaining=deadline-time.monotonic()
            require(remaining > 0, 'Router observation exceeded total check deadline.')
            return self.router.observe(name,timeout=min(5,remaining))
        def execute(command):
            response=read('POST',base+'/agent/exec',{'command':command})
            require(type(response.get('pid')) is int and response['pid']>0,'Invalid fixed check PID.')
            while time.monotonic()<deadline:
                result=read('GET',base+'/agent/exec-status',{'pid':response['pid']})
                if result.get('exited'):
                    out=str(result.get('out-data',''))
                    err=str(result.get('err-data',''))
                    require(len(out.encode())+len(err.encode()) <= 65536,'Fixed guest output exceeded bound.')
                    logs.append({'command':command,'exitcode':result.get('exitcode'),'stdout':out,'stderr':err})
                    return result.get('exitcode'),out,err
                self.sleep(min(.25,max(0,deadline-time.monotonic())))
            raise ValueError('Fixed guest check exceeded total deadline.')
        busy=lambda *args:['/bin/busybox',*args]
        ip=target['address'].split('/')[0]
        subnet=ipaddress.ip_interface(target['address']).network
        gateway=str(subnet.network_address+1)
        route=busy('ip','-4','route','get','1.1.1.1')
        def expected_route():
            code,out,_=execute(route)
            fields=out.split()
            require(code==0 and 'via' in fields and fields[fields.index('via')+1]==gateway
                    and 'dev' in fields and fields[fields.index('dev')+1]=='eth0'
                    and 'src' in fields and fields[fields.index('src')+1]==ip,'Probe route/source differs from desired state.')
        https=busy('timeout','12','/bin/busybox','wget','-q','-O','/dev/null','https://example.com/')
        if check=='vxlan_mtu':
            require(self.router_node != target['node'],'MTU acceptance needs a gateway on a different cluster node.')
            code,out,_=execute(busy('cat','/sys/class/net/eth0/mtu'))
            require(code==0 and out.strip()=='1450','Probe NIC MTU must be 1450.')
            expected_route()
            code,out,err=execute(['/usr/bin/env','ping','-V'])
            require(code==0 and 'iputils' in out+err,'Approved probe image lacks required iputils ping; no MTU downgrade allowed.')
            code,_,_=execute(['/usr/bin/env','ping','-M','do','-c','2','-W','3','-s','1422','1.1.1.1'])
            require(code==0,'1450-byte DF packet did not traverse cross-node routed path.')
            code,out,err=execute(['/usr/bin/env','ping','-M','do','-c','1','-W','3','-s','1423','1.1.1.1'])
            require(code != 0 and 'message too long' in (out+err).lower(),'1451-byte packet must fail locally without fragmentation.')
        elif check=='dns':
            code,out,_=execute(busy('timeout','10','/bin/busybox','nslookup','example.com','10.60.2.1'))
            require(code==0 and 'example.com' in out,'Declared OPNsense DNS resolution failed.')
            config=observe('dhcp_config')
            prefix=str(subnet.network_address).rsplit('.',1)[0]
            require(f'dhcp-range={prefix}.101,{prefix}.254,255.255.255.0,12h' in config
                    and 'dhcp-option=option:router,'+gateway in config
                    and 'dhcp-option=option:dns-server,10.60.2.1' in config,'Router DHCP desired range/options differ.')
            logs.append({'router_dhcp_config':config})
            try:
                code,out,err=execute(busy('timeout','12','/bin/busybox','udhcpc','-n','-q','-R','-i','eth0',
                                           '-s','/bin/true','-t','2','-T','3'))
                lease=re.search(r'lease of (10\.\d+\.\d+\.\d+) obtained',out+err)
                require(code==0 and lease is not None,'Bounded DHCP DORA failed.')
                address=ipaddress.ip_address(lease[1])
                require(address in subnet and 101 <= int(lease[1].split('.')[-1]) <= 254,'DHCP lease outside approved pool.')
            finally:
                # Fixed lease script never configures the NIC; restore declared state
                # explicitly even after failed DORA. -R releases the temporary lease.
                code,_,_=execute(busy('ip','address','replace',target['address'],'dev','eth0'))
                require(code==0,'Static probe address restoration failed.')
                code,_,_=execute(busy('ip','route','replace','default','via',gateway,'dev','eth0'))
                require(code==0,'Static probe route restoration failed.')
            expected_route()
        elif check=='internet_egress':
            expected_route()
            code,_,_=execute(https)
            require(code==0,'Bounded external HTTPS egress failed.')
        else:
            require(self.other_team_id != int(target['vnet'][-3:]),'Isolation comparison team must differ.')
            code,_,_=execute(https)
            require(code==0,'Isolation negative tests require a working public positive control.')
            addresses=[f'10.{target["project_id"]}.{self.other_team_id}.1',
                       f'10.{target["project_id"]}.0.1','10.60.0.20','10.60.0.10']
            for address in addresses:
                before=drop_counter(observe('firewall_counters'))
                code,_,_=execute(busy('timeout','3','/bin/busybox','nc',address,'22'))
                require(code != 0,'Probe reached an isolated protected target.')
                after=drop_counter(observe('firewall_counters'))
                require(0 < after-before <= 10,'Isolation needs a bounded static UFW deny-counter increment; resets/noisy background invalidate evidence.')
                logs.append({'protected_target':address,'port':22,'drop_before':before,'drop_after':after})
        require(time.monotonic() <= deadline,'Fixed network check exceeded total deadline.')
        return {'passed':True,'logs':logs}
