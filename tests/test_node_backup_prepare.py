import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import crange_jobs
spec=importlib.util.spec_from_file_location('node_backup_prepare',ROOT/'ops/proxmox/node_backup_prepare.py')
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)


class NodeBackupPreparationTests(unittest.TestCase):
    def setUp(self):
        self.version='9.0.16';self.calls=[];self.active=[];self.simulation='Inst qemu-server [9.0.16] (9.0.18 Proxmox)\n';self.bad_download=False
        p=patch.object(helper.socket,'gethostname',return_value='proxmox6');p.start();self.addCleanup(p.stop)
        p=patch.object(helper,'PACKAGE_SHA256',hashlib.sha256(b'official-package').hexdigest());p.start();self.addCleanup(p.stop)
    def command(self,args,**kwargs):
        self.calls.append(args);output='';code=0
        if args[:2]==['dpkg-query','-W']:
            output=self.version if args[-1]=='qemu-server' else 'pve-manager 9.1.1\nqemu-server '+self.version+'\n'
        elif args[0]=='dpkg':code=0 if self.version=='9.0.18' else 1
        elif args[0]=='pvesh':output=json.dumps(self.active)
        elif args[:2]==['apt-get','-s']:output=self.simulation
        elif args[:2]==['apt-get','download']:(Path(kwargs['cwd'])/'package.deb').write_bytes(b'tampered' if self.bad_download else b'official-package')
        elif args[:2]==['apt-get','-y']:self.version='9.0.18'
        elif args[0]!='systemctl':raise AssertionError(args)
        return subprocess.CompletedProcess(args,code,output,'')
    def prepare(self):return helper.prepare('proxmox6',run=self.command)
    def test_only_pinned_package_updates_then_reloads_management(self):
        result=self.prepare();self.assertEqual(result['changed_packages'],['qemu-server']);self.assertEqual(result['version'],'9.0.18');self.assertFalse(result['guests_rebooted'])
        installs=[a for a in self.calls if a[:2]==['apt-get','-y']];self.assertEqual(len(installs),1);self.assertIn('--only-upgrade',installs[0]);self.assertEqual([a for a in self.calls if a[0]=='systemctl'],[['systemctl','restart','pvedaemon.service'],['systemctl','is-active','--quiet','pvedaemon.service']])
    def test_extra_dependency_or_removal_fails_before_install(self):
        for simulation in [self.simulation+'Inst pve-cluster (new)\n',self.simulation+'Remv existing\n']:
            self.simulation=simulation
            with self.assertRaisesRegex(ValueError,'dependencies'):self.prepare()
        self.assertFalse(any(a[:2]==['apt-get','-y'] for a in self.calls))
    def test_active_node_task_prevents_package_work(self):
        self.active=[{'type':'qmrestore','id':'160'}]
        with self.assertRaisesRegex(ValueError,'active node tasks'):self.prepare()
        self.assertFalse(any(a[0]=='apt-get' for a in self.calls))
    def test_package_hash_mismatch_never_installed(self):
        self.bad_download=True
        with self.assertRaisesRegex(ValueError,'reviewed artifact'):self.prepare()
        self.assertEqual(self.version,'9.0.16');self.assertFalse(any(a[0]=='systemctl' for a in self.calls))
    def test_up_to_date_node_is_read_only(self):
        self.version='9.0.18';result=self.prepare();self.assertFalse(result['updated']);self.assertFalse(any(a[0] in ('apt-get','systemctl') for a in self.calls))
    def test_wrong_node_or_unknown_old_version_refused(self):
        with self.assertRaisesRegex(ValueError,'different node'):helper.prepare('proxmox5',run=self.command)
        self.version='9.0.15'
        with self.assertRaisesRegex(ValueError,'administrator review'):self.prepare()
    def test_exact_admin_job_scope(self):
        self.assertEqual(crange_jobs.command('template-node-backup-prepare',0,{'node':'proxmox6'}),['node-backup-prepare','--node','proxmox6'])
        for project,options in [(100,{'node':'proxmox6'}),(0,{'node':'auto'}),(0,{'node':'proxmox6','package':'other'})]:
            with self.assertRaises(ValueError):crange_jobs.command('template-node-backup-prepare',project,options)
    def test_relay_has_no_arbitrary_package_or_command_fields(self):
        spec=importlib.util.spec_from_file_location('node_backup_relay',ROOT/'ops/proxmox/node_backup_relay.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        module.validate({'action':'prepare','node':'proxmox6'},101001,101001)
        for request,uid in [({'action':'prepare','node':'proxmox6'},100000),({'action':'prepare','node':'proxmox7'},101001),({'action':'prepare','node':'proxmox6','package':'other'},101001)]:
            with self.assertRaises(ValueError):module.validate(request,uid,101001)


class ContainerTemplateSupportTests(unittest.TestCase):
    def setUp(self):
        self.version='6.0.9';self.calls=[];self.active=[]
        self.simulation='Inst pve-container [6.0.9] (6.0.10 Proxmox)\n'
        self.bad_download=False
        p=patch.object(helper.socket,'gethostname',return_value='proxmox6');p.start();self.addCleanup(p.stop)
        p=patch.object(helper,'CONTAINER_SHA256',hashlib.sha256(b'official-container').hexdigest());p.start();self.addCleanup(p.stop)
    def command(self,args,**kwargs):
        self.calls.append(args);out='';code=0
        if args[:2]==['dpkg-query','-W']:
            out=self.version if args[-1]=='pve-container' else 'pve-manager 9.0.3\npve-container '+self.version+'\n'
        elif args[0]=='dpkg':code=0 if self.version!='6.0.9' else 1
        elif args[0]=='pvesh':out=json.dumps(self.active)
        elif args[:2]==['apt-get','-s']:out=self.simulation
        elif args[:2]==['apt-get','download']:
            (Path(kwargs['cwd'])/'package.deb').write_bytes(b'wrong' if self.bad_download else b'official-container')
        elif args[:2]==['apt-get','-y']:self.version='6.0.10'
        elif args[0]!='systemctl':raise AssertionError(args)
        return subprocess.CompletedProcess(args,code,out,'')
    def test_single_pinned_package_fix_and_no_guest_reboot(self):
        result=helper.prepare_containers('proxmox6',run=self.command)
        self.assertEqual(result['changed_packages'],['pve-container'])
        self.assertEqual(result['version'],'6.0.10')
        self.assertFalse(result['guests_rebooted'])
    def test_reject_cluster_dependency_changes_and_manager_removal(self):
        for suffix in ['Inst pve-cluster (new)\n','Remv pve-manager\n']:
            self.simulation='Inst pve-container [6.0.9] (6.0.10 Proxmox)\n'+suffix
            with self.assertRaisesRegex(ValueError,'dependencies'):
                helper.prepare_containers('proxmox6',run=self.command)
        self.assertFalse(any(c[:2]==['apt-get','-y'] for c in self.calls))
    def test_busy_node_never_installs(self):
        self.active=[{'id':'170','type':'vzrestore'}]
        with self.assertRaisesRegex(ValueError,'active node tasks'):
            helper.prepare_containers('proxmox6',run=self.command)
        self.assertFalse(any(c[0]=='apt-get' for c in self.calls))
    def test_wrong_download_never_installs(self):
        self.bad_download=True
        with self.assertRaisesRegex(ValueError,'reviewed artifact'):
            helper.prepare_containers('proxmox6',run=self.command)
        self.assertEqual(self.version,'6.0.9')
    def test_current_package_is_read_only_and_wrong_node_refused(self):
        self.version='6.0.18'
        result=helper.prepare_containers('proxmox6',run=self.command)
        self.assertFalse(result['updated'])
        self.assertFalse(any(c[0] in ('apt-get','systemctl') for c in self.calls))
        with self.assertRaisesRegex(ValueError,'different node'):
            helper.prepare_containers('proxmox4',run=self.command)
