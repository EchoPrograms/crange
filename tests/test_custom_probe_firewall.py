import pathlib,sys,unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import crange_network_adapters as adapters


class CustomProbeFirewallTests(unittest.TestCase):
    def test_nested_probe_installs_and_cleans_exact_rule_on_both_routers(self):
        parent, child = Mock(), Mock()
        parent.temporary_rules_absent.return_value = child.temporary_rules_absent.return_value = True
        firewall = adapters.RoutedRouterUFW(parent, child)
        target, rule = {'owned': 'scope'}, {'owner': 'job', 'destination': '10.100.100.2/32'}
        firewall.install_management_rule(target, rule)
        firewall.remove_management_rule(target, rule)
        for router in (parent, child):
            router.install_management_rule.assert_called_once_with(target, rule)
            router.remove_management_rule.assert_called_once_with(target, rule)
        self.assertTrue(firewall.temporary_rules_absent(target))
        firewall.observe('dhcp_config', timeout=4)
        child.observe.assert_called_once_with('dhcp_config', timeout=4)
        parent.observe.assert_not_called()

    def test_partial_cleanup_still_attempts_other_router_and_fails_closed(self):
        parent, child = Mock(), Mock()
        child.remove_management_rule.side_effect = ValueError('Owned rule differs')
        firewall = adapters.RoutedRouterUFW(parent, child)
        with self.assertRaisesRegex(ValueError, 'both routers'):
            firewall.remove_management_rule({'owned':'scope'}, {'owner':'job'})
        parent.remove_management_rule.assert_called_once()
        parent.temporary_rules_absent.return_value = False
        child.temporary_rules_absent.return_value = True
        self.assertFalse(firewall.temporary_rules_absent({'owned':'scope'}))
        child.temporary_rules_absent.assert_called_once()

    def test_factory_uses_only_bound_router_addresses(self):
        child, parent = Mock(), Mock()
        target = {'project_id':100,'vnet':'p100c100'}
        routers = {'project_router':'10.60.2.2','team_router':'10.100.254.2','parent_team_router':'10.100.0.7'}
        with patch.object(adapters,'FixedRouterUFW',side_effect=[child,parent]) as factory:
            result = adapters.router_firewall(target, routers)
        self.assertIsInstance(result, adapters.RoutedRouterUFW)
        self.assertEqual(factory.call_args_list[1].kwargs, {'project_router':'10.60.2.2',
            'team_router':'10.100.0.7','parent_team_router':'10.100.0.7','forwarding_parent':True})
