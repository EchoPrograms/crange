import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('p6_hdd', Path(__file__).resolve().parents[1]/'ops/proxmox/proxmox6-hdd-thin.py')
hdd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hdd)


class EmptyGroupGuards(unittest.TestCase):
    def check_refusal(self, *, store=None, lvs=None, free='100', content=None, references=False):
        calls = []
        def run(args):
            calls.append(args)
            if args[0:3] == ['pvesh', 'get', '/storage/local-hdd']:
                return json.dumps(store or {'type':'lvm','vgname':'local-hdd','nodes':'proxmox6','content':'images,rootdir','shared':0})
            if args[0] == 'vgs':return json.dumps({'report':[{'vg':[{'vg_size':'100','vg_free':free,'vg_uuid':'fixed'}]}]})
            if args[0] == 'lvs':return json.dumps({'report':[{'lv':lvs or []}]})
            if args[0] == 'pvesh':return json.dumps(content or [])
            raise AssertionError('Unexpected mutation: '+str(args))
        with patch.object(hdd.os,'geteuid',return_value=0), patch.object(hdd.socket,'gethostname',return_value='proxmox6'), patch.object(hdd,'run',side_effect=run), patch.object(hdd.Path,'glob',return_value=[Path('/fake')] if references else []), patch.object(hdd.Path,'read_text',return_value='scsi0: local-hdd:vm-42-disk-0'):
            with self.assertRaises(ValueError):hdd.initialize()
        self.assertFalse(any(c[0] in ('lvcreate','lvremove','pvesm') for c in calls))

    def test_existing_lvs_are_never_touched(self):
        self.check_refusal(lvs=[{'lv_name':'important'}])

    def test_partially_allocated_vg_is_refused(self):
        self.check_refusal(free='99')

    def test_existing_content_or_guest_reference_is_refused(self):
        self.check_refusal(content=[{'volid':'local-hdd:vm-42-disk-0'}])
        self.check_refusal(references=True)

    def test_existing_thin_or_shared_storage_is_not_reinitialized(self):
        self.check_refusal(store={'type':'lvmthin','vgname':'local-hdd','nodes':'proxmox6'})
        self.check_refusal(store={'type':'lvm','vgname':'local-hdd','nodes':'proxmox6','shared':1})

    def test_wrong_host_cannot_run(self):
        with patch.object(hdd.os,'geteuid',return_value=0), patch.object(hdd.socket,'gethostname',return_value='proxmox5'), patch.object(hdd,'run') as run:
            with self.assertRaises(ValueError):hdd.initialize()
            run.assert_not_called()
