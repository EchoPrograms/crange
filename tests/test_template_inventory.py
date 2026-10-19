import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange_template_inventory as inventory


class TemplateInventoryTests(unittest.TestCase):
    def setUp(self):
        inventory._CACHE.update(value=None,updated=0,attempted=0,loading=False,error=None)

    def test_first_response_does_not_wait_for_proxmox_and_concurrent_reads_share_refresh(self):
        entered,finish=threading.Event(),threading.Event()
        def collect(api):entered.set();finish.wait(2);return {'nodes':[],'builds':[],'media':[]}
        with tempfile.TemporaryDirectory() as directory,patch('crange_templates.BUILDS',Path(directory)),patch('crange.ProxmoxAPI'),patch('crange_templates.options',side_effect=collect) as options:
            start=time.monotonic();first=inventory.snapshot();self.assertLess(time.monotonic()-start,.2)
            self.assertTrue(first['inventory_loading']);self.assertTrue(entered.wait(1))
            second=inventory.snapshot();self.assertTrue(second['inventory_loading']);options.assert_called_once()
            finish.set()
            for _ in range(100):
                if not inventory._CACHE['loading']:break
                time.sleep(.005)
            result=inventory.snapshot();self.assertFalse(result['inventory_loading']);self.assertIsNotNone(result['inventory_updated_at']);options.assert_called_once()
            result['nodes'].append('changed');self.assertEqual(inventory.snapshot()['nodes'],[])

    def test_failure_keeps_cached_data_and_finishes_loading(self):
        inventory._CACHE.update(value={'nodes':['known']},loading=True)
        with patch('crange.ProxmoxAPI',side_effect=OSError()):inventory._refresh()
        result=inventory.snapshot();self.assertEqual(result['nodes'],['known']);self.assertFalse(result['inventory_loading']);self.assertIn('could not',result['inventory_error'])

if __name__=='__main__':unittest.main()
