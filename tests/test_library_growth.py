import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import importlib.util
spec=importlib.util.spec_from_file_location('library_grow',Path(__file__).resolve().parents[1]/'ops/proxmox/library_grow.py')
relay=importlib.util.module_from_spec(spec);spec.loader.exec_module(relay)
import crange_jobs


class LibraryGrowthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'grow.json';self.size=400;self.fs_size=400;self.free=203*2**30;self.calls=[]
        self.source='/dev/mapper/ssd--lvm-vm--145--disk--1';self.fail_resize=False
    def read(self,path):
        self.assertEqual(path,'/etc/pve/lxc/145.conf')
        return f'hostname: crange-library\nrootfs: ssd-lvm:vm-145-disk-0,size=8G\nmp0: ssd-lvm:vm-145-disk-1,mp=/srv/crange-library,size={self.size}G\nnet0: name=eth0,ip=10.0.0.249/24\n'
    def command(self,args,**kwargs):
        self.calls.append(args)
        if args[0]=='vgs':out={'report':[{'vg':[{'vg_uuid':'XjBFk8-GlBE-HKNP-u1DE-XZSr-05cZ-Kz72mZ','vg_size':str(932*2**30),'vg_free':str(self.free)}]}]}
        elif args[:2]==['pct','exec']:out={'filesystems':[{'target':'/srv/crange-library','source':self.source,'fstype':'ext4','size':int(self.fs_size*2**30*.98)}]}
        elif args[:2]==['pct','resize']:
            if self.fail_resize:return subprocess.CompletedProcess(args,1,'','simulated failure')
            self.size=int(args[-1][:-1]);self.fs_size=self.size;out={}
        else:raise AssertionError(args)
        return subprocess.CompletedProcess(args,0,json.dumps(out),'')
    def grow(self):return relay.library_grow(run=self.command,read_text=self.read,journal_path=self.path)
    def test_online_growth_and_readback_without_service_restart(self):
        r=self.grow();self.assertEqual((r['before_gb'],r['after_gb']),(400,464));self.assertFalse(r['nfs_restarted']);self.assertFalse(r['guests_rebooted'])
        self.assertEqual([a for a in self.calls if a[:2]==['pct','resize']],[['pct','resize','145','mp0','464G']]);self.assertEqual(json.loads(self.path.read_text())['state'],'ready')
    def test_protected_host_reserve_prevents_mutation(self):
        self.free=120*2**30
        with self.assertRaisesRegex(ValueError,'reserve'):self.grow()
        self.assertFalse(any(a[:2]==['pct','resize'] for a in self.calls));self.assertFalse(self.path.exists())
    def test_wrong_mount_cannot_grow_any_disk(self):
        self.source='/dev/mapper/unrelated'
        with self.assertRaisesRegex(ValueError,'exact mounted'):self.grow()
        self.assertFalse(any(a[:2]==['pct','resize'] for a in self.calls))
    def test_completed_resize_after_interruption_is_not_submitted_twice(self):
        self.path.write_text(json.dumps({'volume':'ssd-lvm:vm-145-disk-1','before_gb':400,'target_gb':464,'state':'prepared'}));self.path.chmod(0o600);self.size=self.fs_size=464
        self.assertEqual(self.grow()['after_gb'],464);self.assertFalse(any(a[:2]==['pct','resize'] for a in self.calls))
    def test_failed_resize_preserves_exact_pending_growth(self):
        self.fail_resize=True
        with self.assertRaisesRegex(ValueError,'preserve'):self.grow()
        r=json.loads(self.path.read_text());self.assertEqual((r['state'],r['target_gb']),('prepared',464))
    def test_fixed_dashboard_command_has_no_disk_or_size_options(self):
        self.assertEqual(crange_jobs.command('template-library-grow',0,{}),['library-grow'])
        with self.assertRaises(ValueError):crange_jobs.command('template-library-grow',100,{})
        with self.assertRaises(ValueError):crange_jobs.command('template-library-grow',0,{'disk':'other'})

class LibraryGrowthRelayTests(unittest.TestCase):
    def test_only_exact_storage_action_from_worker_uid_is_accepted(self):
        spec=importlib.util.spec_from_file_location('grow_relay',Path(__file__).resolve().parents[1]/'ops/proxmox/library_grow_relay.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        module.validate({'action':'grow'},101001,101001)
        for request,uid in [({'action':'grow'},100000),({'action':'grow','command':'rm'},101001),({'action':'grow','size':1024},101001),({'action':'recover'},101001)]:
            with self.assertRaises(ValueError):module.validate(request,uid,101001)
