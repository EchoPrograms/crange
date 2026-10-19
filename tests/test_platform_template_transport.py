import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange_capture as c
from crange_distribution import DistributionError


class TemplateTransportTests(unittest.TestCase):
    def fixture(self, kind):
        api=Mock();marker='crange-capture-123456abcdef-transport'
        volume='rootfs' if kind=='lxc' else 'scsi0'
        network='name=eth0,type=veth,bridge=crprobe1450,link_down=1' if kind=='lxc' else 'virtio=02:00:00:00:00:01,bridge=craccess'
        configs={21000:{'template':1,'tags':marker+';crange-image;p001',volume:'local-hdd:base-21000-disk-0,size=4G','net0':network}}
        tasks=[]
        def request(method,path,fields=None):
            if path=='/cluster/resources':return [{'vmid':v} for v in configs]
            vmid=int(path.split('/')[4])
            if path.endswith('/status/current'):return {'status':'stopped'}
            if method=='GET':return copy.deepcopy(configs[vmid])
            if method=='PUT':configs[vmid].update(fields);return None
            raise AssertionError((method,path,fields))
        def task(method,path,fields):
            tasks.append((method,path,fields))
            if path.endswith('/clone'):
                self.assertEqual(fields['full'],1)
                configs[175]={**configs[21000],'template':0,volume:'local-hdd:vm-175-disk-0,size=4G'}
            elif method=='DELETE':del configs[21000]
            else:raise AssertionError((method,path,fields))
        api.request.side_effect=request
        receipt={'marker':'crange-capture-123456abcdef','capture_name':'crange-debian-base',
                 'reservation':{'project_id':1},'transport':{'template_id':21000}}
        return api,configs,receipt,task,tasks

    def test_vm_and_container_full_clones_are_regular_and_temporary_template_is_retired(self):
        for kind in ['qemu','lxc']:
            with self.subTest(kind=kind):
                api,configs,receipt,task,tasks=self.fixture(kind)
                with patch.object(c,'_require_probe_pool') as pool:
                    c._clone_transport_template(api,node='proxmox6',kind=kind,source_id=21000,target_id=175,
                                                storage='local-hdd',receipt=receipt,save=Mock(),task=task)
                self.assertNotIn(21000,configs)
                self.assertEqual(configs[175]['template'],0)
                self.assertIn('link_down=1',configs[175]['net0'])
                self.assertIn(receipt['marker'],configs[175]['tags'].split(';'))
                self.assertTrue(receipt['transport']['template_retired'])
                self.assertEqual([item[0] for item in tasks],['POST','DELETE'])
                self.assertEqual(pool.call_count,2)

    def test_foreign_or_linked_volume_is_retained_and_never_deleted(self):
        api,configs,receipt,task,tasks=self.fixture('lxc')
        def bad_task(method,path,fields):
            task(method,path,fields)
            if path.endswith('/clone'):configs[175]['rootfs']='local-hdd:base-21000-disk-0,size=4G'
        with patch.object(c,'_require_probe_pool'),self.assertRaisesRegex(DistributionError,'exclusively owned'):
            c._clone_transport_template(api,node='proxmox6',kind='lxc',source_id=21000,target_id=175,
                storage='local-hdd',receipt=receipt,save=Mock(),task=bad_task)
        self.assertIn(21000,configs)
        self.assertFalse(any(method=='DELETE' for method,_,_ in tasks))

    def test_existing_transport_identity_never_causes_another_restore(self):
        receipt={'transport':{'template_id':21000}}
        api=Mock()
        with self.assertRaisesRegex(DistributionError,'no second restore'):
            c._transport_template_id(api,receipt,Mock())
        api.request.assert_not_called()

    def test_allocator_skips_live_and_retained_transport_ids(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'capture-journal').mkdir()
            (root/'capture-journal/old.json').write_text(json.dumps({'transport':{'template_id':21001}}))
            api=Mock();api.request.side_effect=[[{'vmid':21000}],21002]
            receipt={'transport':{}}
            with patch.object(c,'DATA',root):
                self.assertEqual(c._transport_template_id(api,receipt,Mock()),21002)
