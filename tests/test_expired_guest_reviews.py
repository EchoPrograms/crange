import hashlib,json,pathlib,sys,tempfile,unittest
from contextlib import nullcontext
from unittest.mock import Mock,patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import crange_scheduling as s
from crange_placement import ReservationLedger

class ExpiredGuestReviewTests(unittest.TestCase):
    def run_case(self,*,guest=False,active=False,owner='admin',state='reviewed',changed=False,applied=False,expired=True,jumpbox=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp);plans=root/'private';plans.mkdir();job='a'*32
            cfg={'project_id':100};file=root/'crange.auto.tfvars.json';file.write_text(json.dumps(cfg))
            key='jumpbox:assessor' if jumpbox else 'container:test-db';resolved={'node_name':'proxmox5','datastore_id':'crange-thin'}
            allocation={'resource_key':key,'image':'immutable-db','resolved':resolved,'resources':{'memory_mb':1024,'cpu_cores':1,'disk_gb':8}}
            record={'project':100,'plan_id':job,'created':0,'inputs_sha256':hashlib.sha256(file.read_bytes()).hexdigest(),
                    'placements':[{'resource_key':key,'image':'immutable-db','resolved':resolved,'existing':False}]}
            (plans/(job+'.json')).write_text(json.dumps(record))
            ledger=ReservationLedger(root/'ledger.sqlite3');ledger.db.execute('INSERT INTO jobs (job_id,project_id,owner,state,expires,heartbeat,allocations) VALUES (?,?,?,?,?,?,?)',(job,100,owner,state,100 if expired else 9000,0,json.dumps([allocation])))
            api=Mock()
            def request(method,path,fields):
                if path=='/cluster/resources':return [{'name':'p100c100-jump-01' if jumpbox else 'p100c100-db'}] if guest else []
                if path.endswith('/tasks'):return [{'upid':'active'}] if active else []
                raise AssertionError(path)
            api.request.side_effect=request
            if changed:file.write_text(json.dumps({**cfg,'changed':True}))
            row={'resource_key':key,'image':'immutable-db','box':{'name':'db','team_id':'254','network_id':100,'network_key':'test'}}
            if jumpbox: row['box'].pop('name');row['box']['jump_number']=1
            with patch.object(s,'declarations',return_value=[row]),patch.object(s,'applied_resources',return_value={key:{}} if applied else {}),patch.object(s,'node_locks',return_value=nullcontext()):
                released=s.purge_expired_guest_reviews(root,cfg,'admin',api,ledger,now=5000,review_directory=plans)
            final=ledger.get_job(job)['state'];ledger.close()
            self.assertTrue(all(call.args[0]=='GET' for call in api.request.call_args_list))
            return released,final

    def test_expired_unapplied_guest_review_is_released_after_negative_provider_evidence(self):
        released,state=self.run_case();self.assertEqual(released,['a'*32]);self.assertEqual(state,'released')

    def test_existing_or_active_or_changed_or_unexpired_allocations_are_retained(self):
        for kwargs in ({'guest':True},{'active':True},{'owner':'someone-else'},{'changed':True},{'applied':True},{'expired':False}):
            with self.subTest(**kwargs):
                released,state=self.run_case(**kwargs);self.assertEqual(released,[]);self.assertEqual(state,'reviewed')

    def test_jumpbox_review_derives_provider_name_and_retains_real_guest(self):
        released,state=self.run_case(jumpbox=True)
        self.assertEqual((released,state),(['a'*32],'released'))
        released,state=self.run_case(jumpbox=True,guest=True)
        self.assertEqual((released,state),([],'reviewed'))

    def test_running_apply_is_never_expired(self):
        released,state=self.run_case(state='running');self.assertEqual(released,[]);self.assertEqual(state,'running')
