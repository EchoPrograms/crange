import copy,json,pathlib,sys,tempfile,unittest
from unittest.mock import patch,Mock
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import crange_cluster
from crange_jobs import command


class MemoryReserveTests(unittest.TestCase):
    def test_fixed_admin_command_rejects_project_scope_and_malformed_limits(self):
        fields={'node':'proxmox1','minimum_mb':2048,'fraction':.125,'expected_revision':'capture+network-abc'}
        self.assertEqual(command('template-memory-reserve-set',0,fields)[0],'cluster-memory-reserve-set')
        with self.assertRaises(ValueError):command('template-memory-reserve-set',100,fields)
        for field,value in [('minimum_mb',1024),('fraction',.05),('fraction',True),('expected_revision','x; shell')]:
            with self.subTest(field=field),self.assertRaises(ValueError):command('template-memory-reserve-set',0,{**fields,field:value})

    def run_policy(self, *, revision='current', online=True, physical=16384):
        policy={'revision':'current','allowed_nodes':['proxmox1'],'allowed_storages':['local-lvm'],
                'cpu_overcommit_ratio':6,'memory_overcommit_ratio':2,'local_linked_disk_overcommit_ratio':2,
                'memory_reserve_mb':4096,'memory_reserve_fraction':.25,
                'nodes':{'proxmox1':{'cpu_reserve_cores':2,'management':'keep'}},
                'network_attestations':{'proxmox1':{'vnets':['p100c100']}}}
        node={'online':online,'memory_mb':physical,'cpu_cores':4,
              'observed_memory_mb':10000 if physical==16384 else 1000,
              'committed_memory_mb':18000,'committed_cpu_cores':11}
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);path=root/'distribution-policy.json';path.write_text(json.dumps(policy))
            with patch('crange_scheduling.CONFIG',root),patch('crange_scheduling.read_settings',return_value=(policy,{})), \
                 patch('crange_scheduling.discover',return_value={'nodes':{'proxmox1':node}}),patch('crange.ProxmoxAPI'):
                result=crange_cluster.set_memory_reserve('proxmox1',2048,.125,revision)
            saved=json.loads(path.read_text())
        return policy,saved,result

    def test_changes_only_selected_memory_reserve_and_revision(self):
        old,saved,result=self.run_policy()
        expected=copy.deepcopy(old);expected['nodes']['proxmox1'].update(memory_reserve_mb=2048,memory_reserve_fraction=.125)
        expected['revision']=saved['revision']
        self.assertEqual(saved,expected)
        self.assertGreater(result['after_headroom_gb'],result['before_headroom_gb'])
        self.assertTrue(result['network_acceptance_unchanged'])

    def test_stale_offline_and_oversized_reserve_are_rejected(self):
        for changes in ({'revision':'stale'},{'online':False},{'physical':2048}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.run_policy(**changes)
