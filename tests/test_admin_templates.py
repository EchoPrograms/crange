from pathlib import Path
import sys, json, tempfile, unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch, call
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange_templates as t
import crange_template_media as media
import crange_guac_broker as broker
import crange_windows_base as windows_base
import crange_windows_cache_recovery as cache_recovery
from crange_jobs import command

DEFINITION={'name':'custom-windows-v1','node':'proxmox4','iso':'local:iso/windows.iso','driver_iso':'local:iso/virtio.iso',
            'os':'windows','cores':2,'memory':4096,'disk':40,'disk_bus':'sata','bios':'ovmf','machine':'q35',
            'cpu':'host','nic_models':['e1000','virtio'],'agent':False,'cloud_init':False,'tpm':True}

class AdminTemplateTests(unittest.TestCase):
    def test_scoped_media_preserves_live_team_and_skips_unplanned_archive(self):
        rows = [
            {'resource_key': 'container:live', 'reference': {}, 'box': {'node_name': 'auto'}},
            {'resource_key': 'container:pending', 'reference': {'template_file_id': 'library:vztmpl/os.tar.zst'}, 'box': {'node_name': 'auto'}},
            {'resource_key': 'container:stage', 'reference': {}, 'box': {'node_name': 'auto'}}]
        prepared = {'placements': [{'resource_key': 'container:stage', 'resolved': {
            'node_name': 'proxmox2', 'template_file_id': 'library:vztmpl/os.tar.zst'}}]}
        with patch('crange_scheduling.declarations', return_value=rows), \
             patch('crange_scheduling.applied_resources', return_value={'container:live': {'node_name': 'proxmox1'}}), \
             patch.object(t, 'resolve_archive', return_value='library:vztmpl/os.tar.zst') as resolve:
            result = t.media_tf_overrides(Path('/tmp/project'), {}, prepared, api=Mock(), scoped=True)
        self.assertEqual(result['resolved_placements']['container:live']['node_name'], 'proxmox1')
        self.assertNotIn('container:pending', result['resolved_placements'])
        resolve.assert_called_once()

    def test_seed_reconcile_button_has_fixed_admin_job(self):
        build_id = 'a' * 32
        argv = command('template-cancel', 0, {'build_id': build_id})
        self.assertEqual(argv[-2:], ['template-cancel', build_id])
        with self.assertRaises(ValueError):
            command('template-cancel', 100, {'build_id': build_id})

    def test_legacy_seed_reconcile_releases_only_proven_absent_build(self):
        import crange_scheduling as scheduling
        import crange_windows_seed as seed
        from crange_placement import ReservationLedger
        for obstruction in (None, 'vm', 'iso', 'upload', 'wrong-lease'):
            with self.subTest(obstruction=obstruction), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                build_id = 'b' * 32
                record = {'id': build_id, 'status': 'failed', 'vmid': 123,
                          'ownership_tag': 'crange-build-' + build_id,
                          'created': '2020-01-01T00:00:00+00:00',
                          'capacity_job_id': 'failed-job',
                          'definition': {'node': 'proxmox5', 'windows_unattended': True},
                          'private_answerfile': {'state': 'journal_unavailable'}}
                allocation = {'build_id': 'wrong' if obstruction == 'wrong-lease' else build_id,
                              'vmid': 123, 'operation': 'template-create',
                              'resolved': {'node_name': 'proxmox5'}}
                ledger = ReservationLedger(root / 'reservations.sqlite3')
                ledger.db.execute('INSERT INTO jobs(job_id,project_id,state,allocations) VALUES(?,?,?,?)',
                                  ('failed-job', 0, 'running', json.dumps([allocation])))
                ledger.db.close()
                def request(method, path, params=None):
                    self.assertEqual(method, 'GET')
                    if path == '/cluster/resources':
                        return [{'vmid': 123}] if obstruction == 'vm' else []
                    if path == '/cluster/status':
                        return [{'name': n, 'type': 'node', 'online': 1} for n in seed.NODE_MANAGEMENT_IPS]
                    if path.endswith('/tasks'):
                        self.assertEqual(params, {'source': 'active'})
                        return [{'type': 'imgcopy'}] if obstruction == 'upload' else []
                    if path.endswith('/content'):
                        return [{'volid': 'local:iso/crange-win-' + build_id + '.iso'}] if obstruction == 'iso' else []
                    self.fail('Unexpected provider request: ' + path)
                api = Mock(); api.request.side_effect = request
                with patch.object(scheduling, 'DATA', root), patch.object(t, 'save'), \
                     patch.object(seed, '_journal_path', return_value=root/'absent.json'), \
                     patch.object(seed, '_artifact_path', return_value=root/'absent.iso'):
                    if obstruction:
                        with self.assertRaises(ValueError):
                            t._reconcile_legacy_seed(record, api)
                    else:
                        self.assertTrue(t._reconcile_legacy_seed(record, api)['cleanup_verified'])
                check = ReservationLedger(root / 'reservations.sqlite3')
                self.assertEqual(check.db.execute('SELECT state FROM jobs').fetchone()[0],
                                 'running' if obstruction else 'released')
                check.db.close()

    def test_cache_recovery_uses_protected_guest_ids_not_fixed_vmid_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); journal=root/'cache-journal'; journal.mkdir(mode=0o700)
            job_id='cache-alternate-generation'
            record={'job_id':job_id,'image_id':cache_recovery.IMAGE_ID,
                    'artifact_sha256':cache_recovery.ARTIFACT_SHA256,'kind':'qemu',
                    'node':'proxmox3','allocation_node':'proxmox3','datastore_id':'ssd-lvm',
                    'template_id':1765,'probe_id':1766,'state':'verifying'}
            path=journal/(job_id+'.json'); path.write_text(json.dumps(record)); path.chmod(0o600)
            capture={'provenance_type':'sealed-windows-source-v1'}
            policy={'enabled':True,'approved_storage':[['proxmox3','ssd-lvm']],
                    'cache':{'tested_paths':['vm-archive-restore','local-linked-vm']}}
            image={'artifact_sha256':cache_recovery.ARTIFACT_SHA256,'approved_projects':[100]}
            with patch.object(cache_recovery,'_root',return_value=root), \
                    patch.object(cache_recovery,'_settings',return_value=(policy,{'images':{cache_recovery.IMAGE_ID:image}})), \
                    patch.object(cache_recovery,'_cache_probe_identity',return_value=(capture,'windows',True)), \
                    patch('crange_catalog.validate_sealed_windows_capture'):
                result=cache_recovery._load_locked_job(cache_recovery.IMAGE_ID,job_id)
                with self.assertRaisesRegex(cache_recovery.DistributionError,'does not exist'):
                    cache_recovery._load_locked_job(cache_recovery.IMAGE_ID,'cache-not-owned')
            self.assertEqual(result[4]['template_id'],1765)
            self.assertEqual(result[4]['probe_id'],1766)

    def test_cache_recovery_summary_is_sanitized_and_requires_live_p100_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); journal=root/'cache-journal'; journal.mkdir()
            journal.chmod(0o700)
            job_id='cache-recoverable-123'
            reservation={'image':cache_recovery.IMAGE_ID,'artifact_sha256':cache_recovery.ARTIFACT_SHA256,
                         'resolved':{'node_name':'proxmox2','datastore_id':'local-lvm'}}
            record={'job_id':job_id,'image_id':cache_recovery.IMAGE_ID,
                    'artifact_sha256':cache_recovery.ARTIFACT_SHA256,'state':'failed-verification',
                    'node':'proxmox2','datastore_id':'local-lvm','template_id':8914,'probe_id':8915,
                    'reservation':reservation,'qga_evidence':{'secret':'do-not-return'},
                    'private_password':'do-not-return'}
            (journal/(job_id+'.json')).write_text(json.dumps(record));(journal/(job_id+'.json')).chmod(0o600)
            active=Mock();active.get_job.return_value={'state':'running','project_id':100,
                'allocations':[reservation]}
            with patch.object(cache_recovery,'_root',return_value=root), \
                    patch.object(cache_recovery,'ReservationLedger',return_value=active):
                rows=cache_recovery.public_recovery_status()
            active.close.assert_called_once()
            self.assertEqual(rows,[{'image_id':cache_recovery.IMAGE_ID,'source_job_id':job_id,
                'state':'failed-verification','node':'proxmox2','datastore_id':'local-lvm',
                'template_id':8914,'probe_id':8915,
                'reason':'Verification stopped before completion; exact reservation is retained.'}])
            serialized=json.dumps(rows)
            self.assertNotIn('secret',serialized)
            self.assertNotIn('password',serialized)
            active.get_job.return_value={'state':'released','project_id':100,'allocations':[reservation]}
            with patch.object(cache_recovery,'_root',return_value=root), \
                    patch.object(cache_recovery,'ReservationLedger',return_value=active):
                self.assertEqual(cache_recovery.public_recovery_status(),[])

    def test_server_2025_unattend_run_path_is_within_windows_schema_limit(self):
        source=Path(__file__).resolve().parents[1]/'templates/windows/server2025-autounattend.xml.in'
        root=ET.parse(source).getroot()
        path=next(node.text or '' for node in root.iter() if node.tag.endswith('Path'))
        self.assertLessEqual(len(path),259)
        self.assertIn('powershell.exe -NoProfile -ExecutionPolicy Bypass -Command',path)
        self.assertIn('65..90',path)
        self.assertIn('crange-win-setup.ps1',path)
        self.assertIn('Test-Path',path)

    def test_server_2025_unattend_serialization_keeps_default_component_namespace(self):
        source=Path(__file__).resolve().parents[1]/'templates/windows/server2025-autounattend.xml.in'
        raw=t.serialize_windows_unattend(ET.parse(source).getroot())
        self.assertIn(b'<unattend xmlns="urn:schemas-microsoft-com:unattend"',raw)
        self.assertIn(b'<component name="Microsoft-Windows-Setup"',raw)
        self.assertIn(b'wcm:action="add"',raw)
        self.assertNotRegex(raw.decode(),r'<(?:ns\d+|ms\d+):(?:unattend|settings|component)')

    def test_typed_jobs_reject_arbitrary_options(self):
        self.assertEqual(command('template-create',0,{'definition':DEFINITION})[:1],['template-create'])
        for patch_value in ({'node':'outside-range'},{'iso':'local:iso/../../etc/passwd.iso'}, {'nic_models':['virtio,bridge=vmbr0']}, {'memory':True}, {'tpm':'yes'}, {'cores':999}, {'shell':'touch /root/hacked'}):
            with self.subTest(patch=patch_value), self.assertRaises(ValueError):t.validate({**DEFINITION,**patch_value})
        with self.assertRaises(ValueError):command('plan',0,{})

    def test_installer_uses_only_range_pool_and_build_network(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(t,'BUILDS',Path(directory)):
            api=Mock()
            resources=[]
            def request(method,path,fields=None):
                if path.endswith('/content'):return [{'volid':DEFINITION['iso'],'size':4096},{'volid':DEFINITION['driver_iso'],'size':4096}]
                if path=='/cluster/sdn/vnets':return [{'vnet':'craccess','zone':'crange'}]
                if path=='/cluster/resources':return list(resources)
                if path=='/cluster/nextid':return '3333'
                if method=='POST' and path.endswith('/qemu'):
                    resources.append({'vmid':3333,'node':'proxmox4','type':'qemu','name':'crange-build-custom-windows-v1'})
                return 'UPID:created'
            api.request.side_effect=request
            api.vm.side_effect=lambda *args:{'name':'crange-build-custom-windows-v1','tags':next(c.args[2]['tags'] for c in api.request.call_args_list if c.args[0]=='POST')}
            with patch('crange_scheduling.discover', return_value={'captured_at': 1}), \
                    patch('crange_placement.ReservationLedger') as ledger_type:
                ledger_type.return_value.reserve_external_capacity.return_value={'reservation_id':'test'}
                record=t.create(DEFINITION,api)
            fields=next(c.args[2] for c in api.request.call_args_list if c.args[0]=='POST')
            self.assertEqual(fields['pool'],'crange-iac')
            self.assertEqual(fields['net0'],'e1000,bridge=craccess,firewall=1')
            self.assertEqual(fields['net1'],'virtio,bridge=craccess,firewall=1')
            self.assertEqual(fields['sata0'],'local-lvm:40')
            self.assertIn('tpmstate0',fields)
            self.assertEqual(t.load(record['id'])['status'],'installing')

    def test_fixed_server_2025_answerfile_is_generated_per_build_and_attached_as_third_cdrom(self):
        definition={**DEFINITION,'name':'mhl-server-2025-eval','node':'proxmox4',
                    'iso':t.WINDOWS_SERVER_EVAL_ISO,'driver_iso':'crange-library:iso/virtio-win.iso',
                    'cores':4,'memory':8192,'disk':64,'disk_bus':'sata','cpu':'x86-64-v2-AES',
                    'nic_models':['e1000'],'tpm':False,'agent':True,'windows_unattended':True}
        with self.assertRaisesRegex(ValueError,'only approved'):
            t.validate({**definition,'iso':'crange-library:iso/other-windows.iso'})
        with tempfile.TemporaryDirectory() as directory, patch.object(t,'BUILDS',Path(directory)):
            api=Mock();resources=[]
            def request(method,path,fields=None):
                if path.endswith('/content'):
                    return [{'volid':definition['iso'],'size':4096},
                            {'volid':definition['driver_iso'],'size':4096}]
                if path=='/cluster/sdn/vnets':return [{'vnet':'craccess','zone':'crange'}]
                if path=='/cluster/resources':return list(resources)
                if path=='/cluster/nextid':return '3334'
                if method=='POST' and path.endswith('/qemu'):
                    resources.append({'vmid':3334,'node':'proxmox4','type':'qemu','name':'crange-build-mhl-server-2025-eval'})
                return 'UPID:created'
            api.request.side_effect=request
            api.vm.side_effect=lambda *args:{'name':'crange-build-mhl-server-2025-eval',
                'tags':next(c.args[2]['tags'] for c in api.request.call_args_list if c.args[0]=='POST')}
            with patch('crange_windows_seed.create', side_effect=lambda build_id, vmid, node, driver_iso, api: {
                    'build_id':build_id,'vmid':vmid,'volid':'local:iso/crange-win-'+build_id+'.iso',
                    'sha256':'d'*64,'iso_sha256':'e'*64,'driver_iso':driver_iso}):
                with patch('crange_scheduling.discover', return_value={'captured_at': 1}), \
                        patch('crange_placement.ReservationLedger') as ledger_type:
                    ledger_type.return_value.reserve_external_capacity.return_value={'reservation_id':'test'}
                    t.create(definition,api)
            fields=next(c.args[2] for c in api.request.call_args_list if c.args[0]=='POST')
            self.assertEqual(fields['ide2'],definition['iso']+',media=cdrom')
            self.assertEqual(fields['ide1'],definition['driver_iso']+',media=cdrom')
            self.assertRegex(fields['ide0'],r'^local:iso/crange-win-[0-9a-f]{32}\.iso,media=cdrom$')

    def test_publish_requires_stopped_owned_guest_and_preserves_other_approvals(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(t,'BUILDS',Path(directory)/'builds'):
            root=Path(directory)/'project';root.mkdir()
            approved={'existing':{'template_id':112,'template_node':'proxmox4'}}
            (root/'approved-scenario-templates.json').write_text(json.dumps(approved))
            record={'id':'a'*32,'definition':DEFINITION,'vmid':3333,'guest_name':'crange-build-custom-windows-v1','ownership_tag':'crange-build-'+'a'*32,'status':'installing'};t.save(record)
            api=Mock();api.request.return_value=[{'vmid':3333,'node':'proxmox4','type':'qemu'}];api.vm.return_value={'name':record['guest_name'],'tags':'crange-template-build;'+record['ownership_tag'],'template':0};api.status.return_value='running'
            with patch('crange.project_dir',return_value=root):
                with self.assertRaisesRegex(ValueError,'Shut down'):t.publish(record['id'],[3],True,api)
                self.assertFalse(any(call.args[0] in ('PUT','POST') for call in api.request.call_args_list))
                api.status.return_value='stopped';api.vm.side_effect=[{'name':record['guest_name'],'tags':'crange-template-build;'+record['ownership_tag'],'template':0},{'name':record['guest_name'],'tags':'crange-template-build;'+record['ownership_tag'],'template':1}]
                t.publish(record['id'],[3],True,api)
            result=json.loads((root/'approved-scenario-templates.json').read_text());self.assertEqual(result['existing'],approved['existing'])
            self.assertFalse(result['custom-custom-windows-v1']['profile']['cloud_init'])
            self.assertEqual(result['custom-custom-windows-v1']['profile']['boot_order'],['sata0'])
            api.vm.side_effect=None;api.vm.return_value={'name':'unrelated-server','tags':'crange-template-build;'+record['ownership_tag']}
            with self.assertRaisesRegex(ValueError,'identity'):t.owned(api,record)

    def test_unattended_publish_requires_sealed_receipt_and_detached_answerfile(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(t,'BUILDS',Path(directory)/'builds'):
            root=Path(directory)/'project';root.mkdir()
            (root/'approved-scenario-templates.json').write_text('{"existing": {"template_id": 112, "template_node": "proxmox4"}}')
            definition={**DEFINITION,'name':'mhl-server-2025-eval','iso':t.WINDOWS_SERVER_EVAL_ISO,
                        'windows_unattended':True}
            record={'id':'e'*32,'definition':definition,'vmid':3344,
                    'guest_name':'crange-build-mhl-server-2025-eval',
                    'ownership_tag':'crange-build-'+'e'*32,'status':'installing'}
            record['private_answerfile']={'build_id':record['id'],'vmid':3344,
                'volid':'local:iso/crange-win-'+record['id']+'.iso','sha256':'d'*64}
            t.save(record)
            api=Mock()
            def request(method,path,fields=None):
                if method=='GET' and path=='/cluster/resources':
                    return [{'vmid':3344,'node':'proxmox4','type':'qemu'}]
                return 'UPID:template'
            api.request.side_effect=request
            api.vm.side_effect=[{'name':record['guest_name'],'tags':'crange-template-build;'+record['ownership_tag'],'template':0},
                                {'name':record['guest_name'],'tags':'crange-template-build;'+record['ownership_tag'],'template':0},
                                {'name':record['guest_name'],'tags':'crange-template-build;'+record['ownership_tag'],'template':1}]
            api.status.return_value='stopped'
            journal_dir=Path(directory)/'windows-base-journal'
            with patch.object(windows_base,'PRIVATE',journal_dir), patch('crange.project_dir',return_value=root):
                with self.assertRaisesRegex(ValueError,'preparation and sealing'):
                    t.publish(record['id'],[100],True,api)
                windows_base._save(windows_base._journal_path(record['id']),{'state':'sealed'})
                t.publish(record['id'],[100],True,api)
            updates=[c.args[2] for c in api.request.call_args_list if c.args[0]=='PUT']
            self.assertEqual(len(updates),1)
            self.assertEqual(updates[0]['delete'],'ide1')

    def test_stale_build_vmid_reused_by_library_container_is_not_linked(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(t,'BUILDS',Path(directory)):
            record={'id':'b'*32,'definition':DEFINITION,'vmid':145,'guest_name':'crange-build-validation-builder',
                    'ownership_tag':'crange-build-'+'b'*32,'status':'installing'}
            t.save(record)
            api=Mock()
            def request(method,path,fields=None):
                if path.endswith('/content'):return []
                if path=='/cluster/resources':return [{'vmid':145,'node':'proxmox5','type':'lxc','name':'crange-library'}]
                return []
            api.request.side_effect=request
            result=t.options(api)
            build=result['builds'][0]
            self.assertFalse(build['console_available'])
            self.assertIsNone(build['console_node'])
            self.assertIn('missing or no longer matches',build['error'])
            self.assertEqual(json.loads((Path(directory)/(record['id']+'.json')).read_text())['vmid'],145)
            api.vm.assert_not_called()
            with patch('crange.project_dir',return_value=Path(directory)/'project'), self.assertRaisesRegex(ValueError,'missing or no longer matches'):
                t.publish(record['id'],[3],True,api)
            api.vm.assert_not_called()
            self.assertTrue((Path(directory)/(record['id']+'.json')).exists())

    def test_proxmox_console_uses_public_origin_and_authenticated_gui_route(self):
        self.assertEqual(broker.public_proxmox_origin(''), 'https://proxmox.brogano.dev')
        self.assertEqual(broker.public_proxmox_origin('https://PVE.example.test/'), 'https://pve.example.test')
        self.assertEqual(broker.proxmox_console_url('https://proxmox.brogano.dev',145),
                         'https://proxmox.brogano.dev/#v1:0:=qemu%2F145:4:::::=console')
        for invalid in ('http://pve.example.test','https://pve.example.test:8006','https://user@pve.example.test',
                        'https://127.0.0.1','https://pve.example.test/path','https://proxmox'):
            with self.subTest(invalid=invalid),self.assertRaisesRegex(Exception,'public HTTPS origin'):
                broker.public_proxmox_origin(invalid)

    def test_iso_upload_offsets_ownership_limits_and_completion(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(media,'SPOOL',Path(directory)):
            created=media.begin('admin','my-os.iso',6);upload=created['upload_id']
            self.assertEqual(media.chunk('admin',upload,0,b'abc'),{'offset':3})
            for actor,offset,data in [('other',3,b'def'),('admin',0,b'def'),('admin',3,b'toolong')]:
                with self.assertRaises(ValueError):media.chunk(actor,upload,offset,data)
            with self.assertRaises(ValueError):media.finish('admin',upload)
            media.chunk('admin',upload,3,b'def');self.assertEqual(media.finish('admin',upload)['status'],'ready')
            with self.assertRaises(ValueError):media.begin('admin','../../bad.iso',6)
            self.assertEqual(media.pending('admin')[0]['status'],'ready')
            with self.assertRaises(ValueError):media.cancel('other',upload)
            media.cancel('admin',upload);self.assertEqual(media.pending('admin'),[])
            self.assertEqual(t.validate({**DEFINITION,'iso':'local:iso/WINDOWS.ISO'})['os'],'windows')

    def test_cancel_removes_only_exact_unfinished_build_and_keeps_audit_record(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(t,'BUILDS',Path(directory)):
            record={'id':'c'*32,'definition':DEFINITION,'vmid':3333,
                    'guest_name':'crange-build-custom-windows-v1',
                    'ownership_tag':'crange-build-'+'c'*32,'status':'installing'}
            t.save(record)
            api=Mock();api.request.side_effect=[
                [{'vmid':3333,'node':'proxmox4','type':'qemu'}],
                'UPID:proxmox4:stop',
                'UPID:proxmox4:delete',
                []]
            api.vm.return_value={'name':record['guest_name'],
                                 'tags':'crange-template-build;'+record['ownership_tag'],
                                 'template':0}
            api.status.side_effect=['running','stopped']
            self.assertEqual(t.cancel(record['id'],api),
                             {'build_id':record['id'],'vmid':3333,'status':'cancelled'})
            delete_call=[call for call in api.request.call_args_list if call.args[0]=='DELETE']
            self.assertEqual(len(delete_call),1)
            self.assertEqual(delete_call[0].args[1],'/nodes/proxmox4/qemu/3333')
            self.assertEqual(delete_call[0].args[2],{'purge':1,'destroy-unreferenced-disks':1})
            saved=json.loads((Path(directory)/(record['id']+'.json')).read_text())
            self.assertEqual(saved['status'],'cancelled')
            self.assertIn('cancelled_at',saved)

    def test_cancel_refuses_template_or_unowned_reused_vmid(self):
        for template, actual in ((1,{'name':'crange-build-custom-windows-v1','tags':'crange-template-build;crange-build-'+'d'*32}),
                                 (0,{'name':'other-vm','tags':'crange-template-build;crange-build-'+'d'*32})):
            with self.subTest(template=template), tempfile.TemporaryDirectory() as directory, patch.object(t,'BUILDS',Path(directory)):
                record={'id':'d'*32,'definition':DEFINITION,'vmid':3333,
                        'guest_name':'crange-build-custom-windows-v1',
                        'ownership_tag':'crange-build-'+'d'*32,'status':'installing'}
                t.save(record)
                api=Mock();api.request.return_value=[{'vmid':3333,'node':'proxmox4','type':'qemu'}]
                api.vm.return_value={**actual,'template':template}
                with self.assertRaises(ValueError):t.cancel(record['id'],api)
                self.assertFalse(any(call.args[0] in ('POST','DELETE') for call in api.request.call_args_list))

    def test_reused_same_name_vmid_requires_exact_build_generation_tag(self):
        record={'id':'e'*32,'definition':{**DEFINITION,'node':'proxmox4'},'vmid':113,
                'guest_name':'crange-build-mhl-server-2025-eval',
                'ownership_tag':'crange-build-'+'e'*32,'status':'installing'}
        api=Mock()
        api.request.return_value=[{'vmid':113,'node':'proxmox4','type':'qemu'}]
        api.vm.return_value={'name':record['guest_name'],
                             'tags':'crange-template-build;crange-build-'+'f'*32,
                             'template':0}
        with self.assertRaisesRegex(ValueError,'identity changed'):
            t.owned(api,record)

if __name__=='__main__':unittest.main()

class SharedLibraryTests(unittest.TestCase):
    def test_shared_iso_allowed_but_unapproved_storage_rejected(self):
        t.validate({**DEFINITION,'iso':'crange-library:iso/windows.iso','driver_iso':'media-mirror1:iso/virtio.iso'})
        with self.assertRaises(ValueError):t.validate({**DEFINITION,'iso':'arbitrary:iso/windows.iso'})

    def test_mirror_fallback_requires_matching_manifest_hash_and_size(self):
        api=Mock()
        api.request.side_effect=lambda method,path,fields: ([{'volid':'media-mirror4:iso/windows.iso','size':4096}]
            if '/media-mirror4/' in path else [])
        with patch.object(t,'library_status',return_value={}):
            with self.assertRaisesRegex(ValueError,'no verified mirror'):t.resolve_iso(api,'proxmox4','crange-library:iso/windows.iso')
        status={'files':[{'content':'iso','name':'windows.iso','size':4096,'sha256':'abc',
                         'copies':[{'storage':'media-mirror4','sha256':'abc'}]}]}
        with patch.object(t,'library_status',return_value=status):
            self.assertEqual(t.resolve_iso(api,'proxmox4','crange-library:iso/windows.iso'),'media-mirror4:iso/windows.iso')
        status['files'][0]['copies'][0]['sha256']='different'
        with patch.object(t,'library_status',return_value=status),self.assertRaises(ValueError):
            t.resolve_iso(api,'proxmox4','crange-library:iso/windows.iso')

    def test_zero_byte_images_are_not_offered(self):
        api=Mock();api.request.return_value=[{'volid':'crange-library:iso/empty.iso','size':0}]
        self.assertEqual(t.media_inventory(api,'proxmox1'),[])
