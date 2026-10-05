from pathlib import Path
import io
import sys
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange

class CaptureApiTests(unittest.TestCase):
    def test_null_ct_template_response_waits_for_actual_conversion(self):
        api=object.__new__(crange.ProxmoxAPI)
        api.request=Mock(return_value=None)
        api.task=Mock()
        api.container=Mock(side_effect=[{'template':0},{'template':1}])
        with patch.object(crange.time,'sleep') as sleep:
            api.container_template('proxmox4',132)
        api.task.assert_not_called()
        self.assertEqual(api.container.call_count,2)
        sleep.assert_called_once_with(2)

    def test_task_id_response_still_requires_actual_template_flag(self):
        api=object.__new__(crange.ProxmoxAPI)
        api.request=Mock(return_value='UPID:fixture')
        api.task=Mock()
        api.container=Mock(return_value={'template':0})
        with patch.object(crange.time,'monotonic',side_effect=[0,601]):
            with self.assertRaisesRegex(crange.CrangeError,'did not become a template'):
                api.container_template('proxmox4',132)
        api.task.assert_called_once_with('proxmox4','UPID:fixture',timeout=600)

    def test_missing_task_id_is_reported_as_a_cli_error(self):
        api=object.__new__(crange.ProxmoxAPI)
        api.request=Mock()
        with self.assertRaisesRegex(crange.CrangeError,'invalid task ID'):
            api.task('proxmox4',None)
        api.request.assert_not_called()

    def test_delete_parameters_are_query_fields_and_post_keeps_its_body(self):
        api=object.__new__(crange.ProxmoxAPI)
        api.endpoint='https://pve.invalid/api2/json'
        api.token='fixture'
        api.ssl_context=None
        for method in ['DELETE','POST']:
            with patch.object(crange.urllib.request,'urlopen',return_value=io.BytesIO(b'{"data":"UPID:fixture"}')) as request:
                self.assertEqual(api.request(method,'/nodes/proxmox4/lxc/132',{'purge':1}),'UPID:fixture')
                sent=request.call_args.args[0]
                self.assertEqual(sent.get_method(),method)
                if method=='DELETE':
                    self.assertIsNone(sent.data)
                    self.assertTrue(sent.full_url.endswith('?purge=1'))
                else:
                    self.assertEqual(sent.data,b'purge=1')
                    self.assertNotIn('?',sent.full_url)
