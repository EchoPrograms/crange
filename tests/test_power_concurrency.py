import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange
from crange_jobs import JobStore


class PowerConcurrencyTests(unittest.TestCase):
    def test_unrelated_power_runs_while_deployment_or_other_guest_is_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            store=JobStore(Path(directory)/'jobs.sqlite3')
            busy=store.submit('admin',100,'inventory',{})
            with store.connect() as db:
                db.execute("UPDATE jobs SET status='running',operation='configure' WHERE id=?",(busy,))
            blocked=store.submit('admin',100,'guac-box-control',{'resource_key':'vm:a','action':'start'})
            ready=store.submit('admin',101,'guac-box-control',{'resource_key':'vm:b','action':'restart'})
            with patch('crange_jobs.subprocess.run',return_value=subprocess.CompletedProcess([],0,'done','')):
                self.assertTrue(store.run_next(force_only=False,power_only=True))
            self.assertEqual(store.get(ready,['101'])['status'],'succeeded')
            self.assertEqual(store.get(blocked,['100'])['status'],'queued')

    def test_four_controls_run_concurrently_but_same_guest_waits(self):
        with tempfile.TemporaryDirectory() as directory:
            store=JobStore(Path(directory)/'jobs.sqlite3')
            jobs=[store.submit('alice',3,'guac-box-control',{'resource_key':'vm:'+name,'action':'start'}) for name in ('a','a','b','c','d')]
            entered=[];gate=threading.Event();ready=threading.Event();stop=threading.Event();lock=threading.Lock()
            def execute(args,**kwargs):
                with lock:
                    entered.append(args[-2])
                    if len(entered)==4:ready.set()
                if not gate.wait(4):raise RuntimeError('controls were serialized')
                return subprocess.CompletedProcess(args,0,'done','')
            with patch('crange_jobs.subprocess.run',side_effect=execute):
                worker=threading.Thread(target=store.worker,args=(stop,),daemon=True);worker.start()
                try:
                    self.assertTrue(ready.wait(3))
                    self.assertEqual(set(entered),{'vm:a','vm:b','vm:c','vm:d'})
                    self.assertEqual(store.get(jobs[1],['3'])['status'],'queued')
                finally:stop.set();gate.set();worker.join(5)
                self.assertFalse(worker.is_alive())

    def test_power_flags_and_configuration_edits_do_not_overwrite_each_other(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(crange,'validate_project_quota'):
            path=Path(directory);file=path/'crange.auto.tfvars.json'
            file.write_text(json.dumps({'scenario_boxes':{'a':{'started':True,'cores':1},'b':{'started':True,'cores':1}}}))
            edit=crange.json_config(path);edit['scenario_boxes']['a']['cores']=2
            crange.save_power_state(path,'scenario_boxes','a',False)
            crange.save_config(path,edit)
            data=json.loads(file.read_text())
            self.assertEqual(data['scenario_boxes']['a'],{'started':False,'cores':2})
            crange.save_power_state(path,'scenario_boxes','b',False)
            data=json.loads(file.read_text())
            self.assertFalse(data['scenario_boxes']['a']['started'])
            self.assertFalse(data['scenario_boxes']['b']['started'])
            self.assertEqual(data['scenario_boxes']['a']['cores'],2)
