import unittest
import test_network_acceptance as evidence_test
from crange_network_checks import FixedGuestNetworkChecks, drop_counter


class FixedNetworkChecksTests(unittest.TestCase):
    def setUp(self):
        fixture=evidence_test.NetworkAcceptanceTests()
        fixture.setUp()
        self.target=fixture.scope
        outer=self
        class Transport:
            command=None
            calls=[]
            iputils=True
            lease='10.240.254.101'
            nc_success=False
            dns_failure=False
            def request(self,method,path,fields,deadline):
                if path=='/cluster/resources':return [{'vmid':9911,'node':'proxmox2','type':'qemu'}]
                if path.endswith('/config'):return {'tags':'crange-network-probe;'+outer.target['job_id'],
                    'net0':'virtio=x,bridge=p240t254','ipconfig0':'ip=10.240.254.98/24,gw=10.240.254.1'}
                if path.endswith('/agent/exec'):
                    self.command=fields['command']
                    self.calls.append(self.command)
                    return {'pid':77}
                if path.endswith('/agent/exec-status'):
                    command=self.command
                    result={'exited':True,'exitcode':0,'out-data':'','err-data':''}
                    if 'route' in command and 'get' in command:result['out-data']='1.1.1.1 via 10.240.254.1 dev eth0 src 10.240.254.98'
                    if 'cat' in command:result['out-data']='1450\n'
                    if '-V' in command:
                        result['out-data']='ping from iputils' if self.iputils else 'BusyBox ping'
                    if '1423' in command:result.update(exitcode=1,**{'err-data':'ping: local error: message too long, mtu=1450'})
                    if 'nslookup' in command:result['out-data']='Name: example.com\nAddress: 93.184.215.14'
                    if 'nslookup' in command and self.dns_failure:
                        result.update(exitcode=1,**{'err-data':'private guest detail'})
                    if 'udhcpc' in command:result['err-data']='udhcpc: lease of '+self.lease+' obtained, lease time 43200'
                    if 'nc' in command:result['exitcode']=0 if self.nc_success else 124
                    return result
                raise AssertionError(path)
        self.transport=Transport()
        self.transport.calls=[]
        class Router:
            counter=10
            increment=True
            def observe(self,name,timeout):
                if name=='dhcp_config':return ('dhcp-range=10.240.254.101,10.240.254.254,255.255.255.0,12h\n'
                    'dhcp-option=option:router,10.240.254.1\ndhcp-option=option:dns-server,10.60.2.1')
                if self.increment:self.counter+=1
                return f'[{self.counter}:500] -A ufw-before-forward -i eth1 -d 10.0.0.0/8 -m conntrack --ctstate NEW,INVALID,UNTRACKED -j DROP'
        self.router=Router()
        self.checker=FixedGuestNetworkChecks(self.transport,self.router,router_node='proxmox1',other_team_id=1,sleep=lambda _:None)

    def test_cross_node_df_boundary_uses_real_1450_and_local_1451_failure(self):
        self.assertTrue(self.checker.run(self.target,'vxlan_mtu')['passed'])
        self.assertTrue(any('1422' in command and '-M' in command for command in self.transport.calls))
        self.assertTrue(any('1423' in command and '-M' in command for command in self.transport.calls))

    def test_local_gateway_uses_verified_remote_project_transit(self):
        self.checker.router_node = self.target['node']
        self.checker.project_router_node = 'proxmox1'
        original = self.router.observe
        def observe(name, timeout):
            if name == 'upstream_route':
                return '1.1.1.1 via 10.240.0.1 dev eth0 src 10.240.0.4'
            return original(name, timeout)
        self.router.observe = observe
        result = self.checker.run(self.target, 'vxlan_mtu')
        self.assertTrue(result['passed'])
        self.assertEqual(result['logs'][0]['cross_node_segment']['vnet'], 'p240tr')

    def test_local_gateway_wrong_upstream_cannot_claim_cross_node_mtu(self):
        self.checker.router_node = self.target['node']
        self.checker.project_router_node = 'proxmox1'
        self.router.observe = lambda name, timeout: '1.1.1.1 via 10.240.0.9 dev eth1'
        with self.assertRaises(ValueError):
            self.checker.run(self.target, 'vxlan_mtu')

    def test_musl_iputils_emsgsize_variant_is_bound_to_exact_1451_packet(self):
        original=self.transport.request
        def request(method,path,fields,deadline):
            result=original(method,path,fields,deadline)
            if path.endswith('/agent/exec-status') and '1423' in self.transport.command:
                result.update(exitcode=1,**{'err-data':'ping: sendmsg: Message too large\n',
                    'out-data':'PING 1.1.1.1 (1.1.1.1) 1423(1451) bytes of data.\n'})
            return result
        self.transport.request=request
        self.assertTrue(self.checker.run(self.target,'vxlan_mtu')['passed'])
        def loss(method,path,fields,deadline):
            result=request(method,path,fields,deadline)
            if path.endswith('/agent/exec-status') and '1423' in self.transport.command:
                result['err-data']=''
                result['out-data']='1423(1451) bytes of data. 100% packet loss'
            return result
        self.transport.request=loss
        with self.assertRaises(ValueError):self.checker.run(self.target,'vxlan_mtu')

    def test_failed_fixed_check_retains_bounded_command_context(self):
        self.transport.iputils=False
        with self.assertRaises(ValueError) as caught:
            self.checker.run(self.target,'vxlan_mtu')
        logs=caught.exception.network_check_log
        self.assertEqual(logs['check'],'vxlan_mtu')
        self.assertEqual(logs['commands'][-1]['command'],['/usr/bin/env','ping','-V'])
        self.assertIn('BusyBox',logs['commands'][-1]['stdout'])

    def test_same_node_or_missing_iputils_cannot_claim_cross_node_mtu(self):
        self.checker.router_node='proxmox2'
        with self.assertRaises(ValueError):self.checker.run(self.target,'vxlan_mtu')
        self.checker.router_node='proxmox1'
        self.transport.iputils=False
        with self.assertRaises(ValueError):self.checker.run(self.target,'vxlan_mtu')

    def test_dns_uses_opnsense_dhcp_dora_releases_and_restores_static(self):
        self.assertTrue(self.checker.run(self.target,'dns')['passed'])
        dns=next(command for command in self.transport.calls if 'nslookup' in command)
        self.assertEqual(dns[-1],'10.60.2.1')
        dhcp=next(command for command in self.transport.calls if 'udhcpc' in command)
        self.assertIn('-R',dhcp)
        self.assertIn('/bin/true',dhcp)
        self.assertTrue(any(command[1:4]==['ip','address','replace'] for command in self.transport.calls))
        self.assertTrue(any(command[1:4]==['ip','route','replace'] for command in self.transport.calls))

    def test_bad_dhcp_pool_still_restores_static_address_and_route(self):
        self.transport.lease='10.240.254.50'
        with self.assertRaises(ValueError) as caught:self.checker.run(self.target,'dns')
        self.assertEqual(caught.exception.safe_cause,'dns_dhcp_lease_outside_pool')
        self.assertTrue(any(command[1:4]==['ip','address','replace'] for command in self.transport.calls))
        self.assertTrue(any(command[1:4]==['ip','route','replace'] for command in self.transport.calls))

    def test_dns_resolution_failure_has_fixed_safe_category(self):
        self.transport.dns_failure=True
        with self.assertRaises(ValueError) as caught:self.checker.run(self.target,'dns')
        self.assertEqual(caught.exception.safe_cause,'dns_resolution_failed')
        self.assertNotIn('private guest detail',str(caught.exception))

    def test_isolation_requires_public_positive_control_and_four_counter_increments(self):
        result=self.checker.run(self.target,'team_isolation')
        self.assertTrue(result['passed'])
        counters=[row for row in result['logs'] if 'drop_before' in row]
        self.assertEqual(len(counters),4)
        self.assertTrue(all(row['drop_after']>row['drop_before'] for row in counters))
        self.assertTrue(any('https://example.com/' in command for command in self.transport.calls))

    def test_missing_deny_counter_or_reachable_internal_target_rejects_isolation(self):
        self.router.increment=False
        with self.assertRaises(ValueError):self.checker.run(self.target,'team_isolation')
        self.router.increment=True
        self.transport.nc_success=True
        with self.assertRaises(ValueError):self.checker.run(self.target,'team_isolation')

    def test_counter_rule_must_be_exact_static_drop(self):
        with self.assertRaises(ValueError):drop_counter('[1:64] -A ufw-before-forward -d 10.0.0.0/8 -j ACCEPT')
