import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from crange_distribution import disconnected_container_nic,DistributionError
class DisconnectedNicTests(unittest.TestCase):
    def test_static_bind_address_survives_without_gateway_carrier_or_ipv6(self):
        net=disconnected_container_nic('name=eth0,ip=10.100.254.30/24,gw=10.100.254.1,host-managed=1',0,'crprobe1450')
        self.assertIn('ip=10.100.254.30/24',net)
        self.assertIn('link_down=1',net)
        self.assertIn('ip6=manual',net)
        self.assertNotIn('gw=',net)
        self.assertNotIn('host-managed',net)
    def test_dhcp_is_disconnected_and_non_private_or_invalid_addresses_are_rejected(self):
        self.assertIn('ip=manual',disconnected_container_nic('ip=dhcp',0,'crprobe1450'))
        for address in ('8.8.8.8/24','garbage','127.0.0.1/8'):
            with self.assertRaises(DistributionError):disconnected_container_nic('ip='+address,0,'crprobe1450')
if __name__=='__main__':unittest.main()
