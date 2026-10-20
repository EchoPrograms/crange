import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange_project_lifecycle as lifecycle
from crange_jobs import JobStore


class DeferredDeletionTests(unittest.TestCase):
    def test_required_nodes_come_from_recorded_guests_not_unrelated_offline_hosts(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            (path/'terraform.tfstate').write_text(json.dumps({'resources':[{'type':'proxmox_virtual_environment_vm','instances':[{'attributes':{'node_name':'proxmox4'}}]}]}))
            api=Mock();api.request.return_value=[]
            self.assertEqual(lifecycle.required_guest_nodes(path,100,api),{'proxmox4'})
            api.request.return_value=[{'node':'proxmox4','status':'online'},{'node':'proxmox1','status':'offline'}]
            lifecycle.require_nodes(api,{'proxmox4'})
            with self.assertRaises(lifecycle.WaitingForNodes):lifecycle.require_nodes(api,{'proxmox1'})

    def test_waiting_jobs_survive_restart_do_not_block_other_projects_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            store=JobStore(Path(directory)/'jobs.sqlite3')
            with store.connect() as db:
                db.execute("INSERT INTO jobs(id,actor,project,operation,options,status,created,waiting_nodes) VALUES('deferred','manager',100,'project-delete','{}','waiting-for-node',1,'[\"proxmox1\"]')")
            self.assertEqual(store.list([],admin=True)[0]['status'],'waiting-for-node')
            page=store.list_page([],admin=True);self.assertEqual(page['total'],0);self.assertEqual(len(page['jobs']),1)
            self.assertFalse(store.run_next(),'waiting jobs do not occupy the worker')
            restarted=JobStore(store.path);api=Mock();api.request.return_value=[{'node':'proxmox1','status':'offline'}]
            restarted.resume_waiting(api=api)
            self.assertEqual(restarted.list([],admin=True)[0]['status'],'waiting-for-node')
            api.request.return_value=[{'node':'proxmox1','status':'online'}]
            restarted.resume_waiting(api=api)
            self.assertEqual(restarted.list([],admin=True)[0]['status'],'queued')

    def test_worker_persists_deferral_and_runs_another_project(self):
        import crange
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            envs=Path(directory)/'envs'
            for project in (100,101):(envs/f'project-{project:03d}').mkdir(parents=True)
            store=JobStore(Path(directory)/'jobs.sqlite3')
            with patch.object(crange,'ENVS',envs):
                waiting=store.submit('manager',100,'project-delete',{'confirmation':'p100'})
                other=store.submit('manager',101,'inventory',{})
                def run(*args,**kwargs):
                    if 'project-delete' in args[0]:
                        lifecycle.write_state(envs/'project-100',{'state':'deleting','job_id':waiting,'waiting_nodes':['proxmox1']})
                        return subprocess.CompletedProcess(args[0],75,'Waiting for proxmox1','')
                    return subprocess.CompletedProcess(args[0],0,'Inventory complete','')
                with patch('crange_jobs.subprocess.run',side_effect=run):
                    self.assertTrue(store.run_next());self.assertTrue(store.run_next())
                self.assertEqual(store.get(waiting,[],admin=True)['status'],'waiting-for-node')
                self.assertEqual(store.get(other,[],admin=True)['status'],'succeeded')

    def test_failed_status_read_never_requeues_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            store=JobStore(Path(directory)/'jobs.sqlite3')
            with store.connect() as db:db.execute("INSERT INTO jobs(id,actor,project,operation,options,status,created,waiting_nodes) VALUES('waiting','manager',100,'project-delete','{}','waiting-for-node',1,'[\"proxmox1\"]')")
            api=Mock();api.request.side_effect=TimeoutError()
            store.resume_waiting(api=api)
            self.assertEqual(store.list([],admin=True)[0]['status'],'waiting-for-node')

    def test_delete_resumes_for_each_returning_node_but_purge_waits_for_all(self):
        with tempfile.TemporaryDirectory() as directory:
            store=JobStore(Path(directory)/'jobs.sqlite3')
            with store.connect() as db:
                for job, operation in [('delete','project-delete'),('purge','project-purge')]:
                    db.execute("INSERT INTO jobs(id,actor,project,operation,options,status,created,waiting_nodes) VALUES(?, 'admin',100,?,'{}','waiting-for-node',1,?)",
                               (job,operation,json.dumps(['proxmox1','proxmox2'])))
            api=Mock();api.request.return_value=[{'node':'proxmox2','status':'online'}]
            store.resume_waiting(api=api)
            self.assertEqual(store.get('delete',[],admin=True)['status'],'queued')
            self.assertEqual(store.get('purge',[],admin=True)['status'],'waiting-for-node')

if __name__=='__main__':unittest.main()
