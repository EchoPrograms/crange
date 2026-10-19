import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from crange_cache_jobs import _probe_absent_or_reused


class ProbeReuseTests(unittest.TestCase):
    def setUp(self):
        self.upid='UPID:proxmox6:ABCD:EF01:1234:qmdestroy:156:crange-iac@pve!terraform:'
        self.record={'node':'proxmox6','kind':'qemu','probe_id':156,
                     'provider_tasks':[{'upid':self.upid,'state':'confirmed-terminal'}]}
        self.inventory={'guests':{'156':{'node_name':'proxmox6','kind':'lxc','template':True}}}

    def test_different_kind_reuse_requires_successful_exact_owned_deletion(self):
        self.assertTrue(_probe_absent_or_reused(self.record,self.inventory,{self.upid}))
        self.assertFalse(_probe_absent_or_reused(self.record,self.inventory,set()))
        for key,value in [('node','proxmox5'),('probe_id',157),('kind','lxc')]:
            # ID 157 itself must remain present to exercise mismatch, not absence.
            record={**self.record,key:value};inventory={'guests':{str(record['probe_id']):{'kind':'lxc'}}}
            self.assertFalse(_probe_absent_or_reused(record,inventory,{self.upid}))
        self.record['provider_tasks'][0]['state']='running'
        self.assertFalse(_probe_absent_or_reused(self.record,self.inventory,{self.upid}))

    def test_same_or_unknown_guest_kind_remains_blocked(self):
        for kind in ('qemu',None,'unknown'):
            self.inventory['guests']['156']['kind']=kind
            self.assertFalse(_probe_absent_or_reused(self.record,self.inventory,{self.upid}))

    def test_absent_probe_accepts_inventory_but_missing_identity_does_not(self):
        self.assertTrue(_probe_absent_or_reused(self.record,{'guests':{}},set()))
        self.assertFalse(_probe_absent_or_reused({**self.record,'probe_id':None},{'guests':{}},set()))
