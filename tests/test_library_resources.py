import pathlib,sys,subprocess,unittest,tempfile,json
from unittest.mock import Mock,patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from crange_ct_read_relay import library_resources,library_recover,validate_request
from crange_jobs import command
class LibraryResourcesTests(unittest.TestCase):
    def fixture(self):
        config={'hostname':'crange-library','rootfs':'ssd-lvm:vm-145-disk-0,size=8G',
          'mp0':'ssd-lvm:vm-145-disk-1,mp=/srv/crange-library,backup=1,size=400G',
          'net0':'name=eth0,ip=10.0.0.249/24','memory':'768','swap':'0','cores':'1'}
        def read(path):
            if path=='/proc/meminfo':return 'MemAvailable: 10485760 kB\n'
            return '\n'.join(k+': '+v for k,v in config.items())
        def apply(argv,**kwargs):
            self.assertEqual(argv[:3],['pct','set','145'])
            config.update(memory=argv[4],swap=argv[6],cores=argv[8])
            return subprocess.CompletedProcess(argv,0,'','')
        return config,read,Mock(side_effect=apply)
    def test_online_increase_and_idempotent_no_shrink(self):
        config,read,run=self.fixture();result=library_resources(read_text=read,run=run)
        self.assertEqual(result['after'],{'memory':2048,'swap':2048,'cores':2});self.assertFalse(result['rebooted'])
        run.assert_called_once();run.reset_mock()
        config['memory']='4096';library_resources(read_text=read,run=run);run.assert_not_called()
    def test_foreign_storage_identity_lock_and_host_pressure_are_denied(self):
        for key,value in [('hostname','foreign'),('mp0','local-lvm:vm-145-disk-1,mp=/srv/crange-library'),
                          ('net0','ip=10.60.0.99/24'),('lock','backup')]:
            config,read,run=self.fixture();config[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):library_resources(read_text=read,run=run)
            run.assert_not_called()
        config,read,run=self.fixture()
        with self.assertRaises(ValueError):
            library_resources(read_text=lambda p:'MemAvailable: 1024 kB\n' if p=='/proc/meminfo' else read(p),run=run)
        run.assert_not_called()
    def test_fixed_admin_request_and_peer_scope(self):
        self.assertEqual(command('template-library-resources',0,{}),['library-resources'])
        with self.assertRaises(ValueError):command('template-library-resources',100,{})
        policy={'peer_uid':101000,'node_addresses':{'proxmox5':'10.0.0.105'}}
        value={'command':'range-library-resources','node':'proxmox5'}
        validate_request(value,policy,101000)
        for request,uid in [(value,0),({**value,'vmid':103},101000),({**value,'memory':1},101000),({**value,'node':'proxmox1'},101000)]:
            with self.assertRaises(ValueError):validate_request(request,policy,uid)

    def test_service_restart_is_journaled_and_only_observed_when_pending(self):
        config,read,_=self.fixture();config.update(memory='2048',swap='2048',cores='2')
        pid=['10'];restarts=[]
        def run(argv,**kwargs):
            if 'restart' in argv:restarts.append(argv);pid[0]='20';return subprocess.CompletedProcess(argv,0,'','')
            return subprocess.CompletedProcess(argv,0,'MainPID='+pid[0]+'\nActiveState=active\nSubState=running\n','')
        with tempfile.TemporaryDirectory() as directory:
            path=pathlib.Path(directory)/'journal.json'
            result=library_recover(run=run,read_text=read,recovery_path=path)
            self.assertEqual(result['state'],'ready');self.assertEqual(len(restarts),1)
            result['state']='observing';path.write_text(json.dumps(result))
            library_recover(run=run,read_text=read,recovery_path=path)
            self.assertEqual(len(restarts),1)
            self.assertEqual(restarts[0],['pct','exec','145','--','systemctl','restart','--no-block','nfs-ganesha.service'])
