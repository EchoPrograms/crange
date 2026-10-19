import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec=importlib.util.spec_from_file_location('guard',Path(__file__).resolve().parents[1]/'ops/proxmox/private_bridge_guard.py')
guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)


class PrivateBridgeGuardTests(unittest.TestCase):
    def document(self):
        entries=[{'chain':{'family':'inet','table':guard.TABLE,'name':hook,'hook':hook,'type':'filter','prio':-210,'policy':'accept'}} for hook in ('input','forward','output')]
        entries += [{'rule':{'chain':chain,'expr':[{'match':{'op':'==','left':{'meta':{'key':key}},'right':guard.BRIDGE}},{'drop':None}]}} for chain,key in guard.RULES]
        return {'nftables':entries}

    def test_only_exact_private_interface_drop_rules_are_accepted(self):
        document=self.document();guard.check_nft(document)
        document['nftables'][-1]['rule']['expr'][0]['match']['right']='vmbr0'
        with self.assertRaisesRegex(ValueError,'rules changed'):guard.check_nft(document)

    def test_extra_rules_or_changed_priority_are_rejected(self):
        document=self.document();document['nftables'].append(document['nftables'][-1])
        with self.assertRaisesRegex(ValueError,'shape changed'):guard.check_nft(document)
        document=self.document();document['nftables'][0]['chain']['prio']=0
        with self.assertRaisesRegex(ValueError,'hook changed'):guard.check_nft(document)

    def topology(self, addresses=(), ports=(), *, preparing=False, vmid=None):
        data=[{'linkinfo':{'info_kind':'bridge'},'addr_info':list(addresses)}]
        with patch.object(guard,'run',side_effect=[Mock(stdout=json.dumps(data)),Mock(stdout=json.dumps(list(ports)))]):
            return guard.topology(vmid,preparing=preparing)

    def test_host_addresses_are_rejected_and_only_link_local_can_be_removed(self):
        link={'family':'inet6','scope':'link'}
        self.topology([link],preparing=True)
        with self.assertRaisesRegex(ValueError,'host address'):self.topology([link])
        with self.assertRaisesRegex(ValueError,'host address'):self.topology([{'family':'inet','scope':'global'}],preparing=True)

    def test_uplinks_other_guests_and_active_ports_during_preparation_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'uplink'):self.topology(ports=[{'ifname':'eno1'}],vmid=131)
        with self.assertRaisesRegex(ValueError,'uplink'):self.topology(ports=[{'ifname':'tap132i0'}],vmid=131)
        with self.assertRaisesRegex(ValueError,'uplink'):self.topology(ports=[{'ifname':'tap131i0'}],preparing=True)
        self.topology(ports=[{'ifname':'tap131i0'}],vmid=131)
