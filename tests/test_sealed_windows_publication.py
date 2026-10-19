import copy
import json
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from crange_capture import (capture_sealed_windows_source, _load_sealed_windows_proof,
                            convert_sealed_windows_p100)
from crange_distribution import (_source_config_identity, _sealed_windows_source_volumes, FileJournal, export_image,
    publish_exported_image, sealed_windows_verification)
import crange_templates


class SealedWindowsPublicationTests(unittest.TestCase):
    def test_proof_consumer_uses_only_the_fixed_unix_relay_operation(self):
        payload = {'files': {name: {'sha256': key * 64, 'document': {}}
                             for name, key in zip(('inspection', 'preparation', 'probe8913',
                                                   'probe8914', 'qga8914'), '12345')},
                   'live': {'source': {}, 'probes': []}}
        class FakeSocket:
            def __init__(self, *_args): pass
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def settimeout(self, _seconds): pass
            def connect(self, path): self.path = path
            def sendall(self, request): self.request = json.loads(request)
            def recv(self, _maximum): return json.dumps({'value': json.dumps(payload)}).encode() + b'\n'
        with unittest.mock.patch('crange_capture.socket.socket', return_value=FakeSocket()):
            proof = _load_sealed_windows_proof()
        self.assertEqual(set(proof), {'inspection', 'preparation', 'probe8913', 'probe8914', 'qga8914', 'live'})

    def test_fixed_capture_binds_protected_proof_to_live_pve_source_and_clones(self):
        preparation = json.loads((ROOT / 'docs/project-100/windows-locale-candidate-seal.json').read_text())
        # The protected root copy omits this redundant top-level digest; the
        # consumer must recompute it from its embedded frozen config.
        preparation.pop('frozen_candidate_config_sha256')
        clone_doc = json.loads((ROOT / 'docs/project-100/windows-locale-clone-acceptance.json').read_text())
        second_doc = json.loads((ROOT / 'docs/project-100/windows-locale-probe8914-acceptance.json').read_text())
        inspection = {'state': 'sealed-candidate-read-only-inspected', 'never_booted': True,
            'source115_stopped_unchanged': True, 'success_tag': True,
            'hive_sha256': preparation['hive_sha256_unchanged']}
        first_id = copy.deepcopy(clone_doc['first_clone']['identity'])
        second_id = copy.deepcopy(second_doc['second_probe']['identity'])
        qga = {'exitcode': 0, 'exited': True,
               'out-data': json.dumps({key: value for key, value in second_id.items() if key != 'os_family'})}
        first_upid = clone_doc['first_clone']['upid']
        second_upid = second_doc['second_probe']['clone_upid']
        proof = {}
        for name, document in (
                ('inspection', inspection), ('preparation', preparation),
                ('probe8913', {'source_vmid': 8912, 'guest_id': 8913, 'state': 'unattended-windows-ready',
                    'source_config_unchanged': True, 'clone_completed': True,
                    'isolation_before_boot_verified': True, 'manual_interventions': [],
                    'upid': first_upid, 'identity': first_id}),
                ('probe8914', {'source_vmid': 8912, 'guest_id': 8914, 'state': 'unattended-windows-ready',
                    'source_config_unchanged': True, 'clone_completed': True,
                    'isolation_before_boot_verified': True, 'manual_interventions': [],
                    'upid': second_upid, 'identity': second_id,
                    'recovery_result_sha256': '5' * 64}),
                ('qga8914', qga)):
            digest = '5' * 64 if name == 'qga8914' else (str(len(proof) + 1) * 64)[:64]
            proof[name] = {'sha256': digest, 'document': document}

        source_config = preparation['frozen_candidate_config']
        clone_configs = {}
        for vmid, identity, mac, guid, genid, upid in (
                (8913, first_id, 'BC:24:11:C7:52:9C', 'c852446c-7e14-44be-9e1f-eccabe5e0ef6',
                 '2138e7ee-2204-40cd-8a9e-47b544ad192f', first_upid),
                (8914, second_id, 'BC:24:11:3A:C4:7E', 'b667586e-69d3-4503-95ba-3b981d1b3a41',
                 'd1d60070-9d74-4e93-83c8-c6748b36b3e4', second_upid)):
            clone_configs[vmid] = {'name': f'crange-win-locale-proof-{vmid}', 'template': 0,
                'agent': '1', 'tags': 'crange-capture-windows-server2025-locale-20261008;crange-image;p100',
                'smbios1': f'uuid={guid}', 'vmgenid': genid,
                'net0': f'e1000={mac},bridge=crprobe1450,firewall=1,link_down=1'}

        live = {'source': {'config_sha256': _source_config_identity(source_config, 8912),
                           'status': 'stopped'},
                'probes': []}
        for vmid, record in ((8913, proof['probe8913']['document']),
                             (8914, proof['probe8914']['document'])):
            live['probes'].append({'vmid': vmid,
                'config_sha256': _source_config_identity(clone_configs[vmid], vmid),
                'status': 'running', 'clone_upid': record['upid'],
                'task_status': 'stopped', 'task_exitstatus': 'OK', 'probe_pool_member': True})
        proof['live'] = live

        api = Mock()
        def request(method, path, fields=None):
            if path == '/nodes/proxmox4/qemu/8912/config':
                return source_config
            if path == '/nodes/proxmox4/qemu/8912/status/current':
                return {'status': 'stopped'}
            for vmid in (8913, 8914):
                if path == f'/nodes/proxmox4/qemu/{vmid}/config':
                    return clone_configs[vmid]
                if path == f'/nodes/proxmox4/qemu/{vmid}/status/current':
                    return {'status': 'running'}
                if path.endswith('/status') and 'qmclone%3A8912%3Aroot%40pam%3A' in path:
                    return {'status': 'stopped', 'exitstatus': 'OK'}
            raise AssertionError(path)
        api.request.side_effect = request
        journal = Mock()
        capture = capture_sealed_windows_source(api, journal, proof_loader=lambda: proof)
        self.assertIsNone(capture['source_identity'])
        self.assertEqual({row['vmid'] for row in capture['probes']}, {8913, 8914})
        self.assertTrue(all(row['clone_task_ok'] for row in capture['probes']))
        journal.assert_called_once_with(capture)

        template_config = {**source_config, 'template': 1}
        for disk_key in ('sata0', 'efidisk0'):
            template_config[disk_key] = template_config[disk_key].replace('vm-8912-disk-', 'base-8912-disk-')
        self.assertTrue(_sealed_windows_source_volumes(template_config))
        self.assertEqual(_source_config_identity(template_config, 8912),
                         _source_config_identity(source_config, 8912))
        with tempfile.TemporaryDirectory() as tmp:
            source_journal_dir = Path(tmp) / 'source-journal'
            source_journal = FileJournal(source_journal_dir)
            source_journal(capture)
            converted_config = {**source_config, 'template': 1}
            convert_api = Mock()
            conversion_calls = []
            config_reads = 0
            def convert_request(method, path, fields=None):
                nonlocal config_reads
                conversion_calls.append((method, path))
                if path.endswith('/config'):
                    config_reads += 1
                    return {**source_config, 'template': 0} if config_reads == 1 else converted_config
                if path.endswith('/status/current'):
                    return {'status': 'stopped'}
                if path == '/nodes/proxmox4/qemu/8912/template':
                    return 'UPID:proxmox4:ABCDEF:ABCDEF:ABCDEF:qmtemplate:8912:root@pam:'
                if path.endswith('/status'):
                    return {'status': 'stopped', 'exitstatus': 'OK'}
                raise AssertionError(path)
            convert_api.request.side_effect = convert_request
            with unittest.mock.patch('crange_capture.capture_sealed_windows_source',
                                     return_value=capture):
                converted = convert_sealed_windows_p100(convert_api, source_journal,
                    proof_loader=Mock(), timeout=1, poll_interval=0)
            self.assertEqual(converted['state'], 'template-ready')
            self.assertEqual(sum(path.endswith('/template') for _, path in conversion_calls), 1)
            conversion_path = source_journal_dir / 'p100-sealed-windows-8912-template-conversion.json'
            conversion = json.loads(conversion_path.read_text())
            conversion['state'] = 'conversion-intent'
            conversion_path.write_text(json.dumps(conversion))
            with unittest.mock.patch('crange_capture.capture_sealed_windows_source',
                                     return_value=capture), self.assertRaisesRegex(Exception, 'uncertain'):
                convert_sealed_windows_p100(convert_api, source_journal,
                    proof_loader=Mock(), timeout=1, poll_interval=0)
            self.assertEqual(sum(path.endswith('/template') for _, path in conversion_calls), 1)
            export_journal = FileJournal(Path(tmp) / 'export-journal')
            api = Mock()
            content_reads = 0
            def export_request(method, path, fields=None):
                nonlocal content_reads
                if path.endswith('/config'):
                    return template_config
                if path.endswith('/status/current'):
                    return {'status': 'stopped'}
                if path.endswith('/storage/crange-library/content'):
                    content_reads += 1
                    if content_reads == 1:
                        return []
                    return [{'volid': 'crange-library:backup/vzdump-qemu-8912-2026_10_08-12_00_00.vma.zst',
                             'content': 'backup', 'vmid': 8912}]
                if path.endswith('/vzdump'):
                    return 'UPID:proxmox4:ABCDEF:ABCDEF:ABCDEF:vzdump:8912:root@pam:'
                raise AssertionError(path)
            api.request.side_effect = export_request
            api.task.return_value = None
            with unittest.mock.patch('crange_scheduling.DATA', Path(tmp)):
                exported = export_image(api, node='proxmox4', kind='vm', vmid=8912,
                    storage='crange-library', job_id='p100-sealed-export', journal=export_journal,
                    hash_volume=Mock(return_value='d' * 64),
                    approved_backup_storage=[('proxmox4', 'crange-library')], identity_record=capture)
            self.assertNotIn('source_identity', exported)
            image = publish_exported_image({'schema_version': 1, 'images': {}},
                'mhl-windows-server-2025-base-p100-v1', exported, architecture='amd64',
                source_revision='a' * 40, approved_projects=[100], datastore_id='crange-library',
                verification=sealed_windows_verification(capture))
            self.assertEqual(image['images']['mhl-windows-server-2025-base-p100-v1']['display_name'],
                             'Windows Server 2025 Desktop Experience')
            self.assertNotIn('source_identity', image['images']['mhl-windows-server-2025-base-p100-v1'])
            published = image['images']['mhl-windows-server-2025-base-p100-v1']
            replica_identity = copy.deepcopy(first_id)
            replica_identity.update(machine_guid='b1a4f11e-0b3d-49ea-8db3-759fab2d5065',
                machine_sid='S-1-5-21-111111111-222222222-333333333',
                local_administrator_sid='S-1-5-21-111111111-222222222-333333333-500')
            published['replicas'].append({'state': 'ready', 'artifact_sha256': published['artifact_sha256'],
                'cache_local': True, 'clone_modes_verified': ['linked'], 'template_id': 8991,
                'node': 'proxmox2', 'datastore_id': 'local-lvm',
                'verification': {'passed': True, 'identity_unique': True,
                                 'evidence': {'identity': replica_identity}}})
            project_dir = Path(tmp) / 'project-100'
            project_dir.mkdir()
            (project_dir / 'approved-scenario-templates.json').write_text(
                json.dumps({'existing': {'template_id': 88, 'display_name': 'Existing'}}))
            template_api = Mock()
            template_api.request.return_value = {'template': 1, 'name': 'mhl-windows-cache',
                                                  'tags': 'crange-cache-8991;crange-image'}
            with unittest.mock.patch('crange.project_dir', return_value=project_dir), \
                    unittest.mock.patch('crange_scheduling.read_settings',
                        return_value=({}, {'images': {'mhl-windows-server-2025-base-p100-v1': published}})):
                linked = crange_templates.approve_catalog_image(
                    'mhl-windows-server-2025-base-p100-v1', [100], template_api)
            linked_reference = json.loads((project_dir / 'approved-scenario-templates.json').read_text())[linked['alias']]
            self.assertEqual(linked_reference['display_name'], 'Windows Server 2025 Desktop Experience')


if __name__ == '__main__':
    unittest.main()
