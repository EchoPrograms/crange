import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange_capture as capture
from crange_distribution import DistributionError


class CaptureInspectTests(unittest.TestCase):
    def inspect(self, *, wrong_owner=False, target_is_source=False, read_failure=False):
        project, version, machine = 100, '0.0.2', 't254-dc'
        slug=hashlib.sha256(f'{project}:{version}:{machine}'.encode()).hexdigest()[:12]
        receipt={'node':'proxmox5','template_id':128 if target_is_source else 131,
                 'source_id':128,'state':'allocated','marker':'crange-capture-'+slug,'capture_name':'restored'}
        config={'name':'restored','tags':f'crange-image;crange-capture-{slug};'+('p101' if wrong_owner else 'p100'),
                'net0':'e1000=AA:BB:CC:DD:EE:FF,bridge=isolated,link_down=1'}
        api=Mock();api.vm.return_value=config
        state={'value':'stopped'}
        api.status.side_effect=lambda *args:state['value']
        def request(method,path,fields=None):
            if path.endswith('/tasks'):return []
            if path=='/pools/crange-probes':return {'members':[{'node':'proxmox5','type':'qemu','vmid':131}]}
            if path.endswith('/get-osinfo'):return {'result':{'id':'mswindows'}}
            if method=='PUT' and path.endswith('/config'):return None
            if path.endswith('/status/start'):state['value']='running';return 'UPID:proxmox5:1:2:3:qmstart:131:user:'
            if path.endswith('/status/shutdown'):state['value']='stopped';return 'UPID:proxmox5:1:2:3:qmshutdown:131:user:'
            raise AssertionError(path)
        api.request.side_effect=request
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'capture-journal').mkdir()
            (root/'capture-journal'/f'capture-p100-{slug}.json').write_text(json.dumps(receipt))
            with patch.object(capture,'DATA',root), \
                 patch.object(capture,'read_settings',return_value=({'allowed_nodes':['proxmox5'],'capture_isolated_bridge':'isolated'},{})), \
                 patch.object(capture,'private_bridge_request',return_value={'verified':True}), \
                 patch('crange.json_config',return_value={'scenario_boxes':{machine:{'team_id':'254'}}}), \
                 patch('crange.output_json',return_value={'scenario_boxes':{'value':{machine:{'id':128}}}}), \
                 patch('crange_distribution._fixed_guest_read',return_value='{"directory":{"ok":false,"error_id":"ADServerDown"}}',
                       side_effect=DistributionError('read failed') if read_failure else None) as read, \
                 patch.object(capture,'node_locks',return_value=__import__('contextlib').nullcontext()):
                try:
                    result=capture.inspect_capture(api,project=project,version=version,machine=machine,path=root)
                except DistributionError:
                    if wrong_owner or target_is_source:
                        self.assertFalse(any(call.args[0]=='POST' for call in api.request.call_args_list))
                    else:
                        self.assertIn('/nodes/proxmox5/qemu/131/status/shutdown',[call.args[1] for call in api.request.call_args_list])
                    raise
        return result,api,read

    def test_only_copy_is_started_diagnosed_and_stopped(self):
        result,api,read=self.inspect()
        self.assertEqual(result['directory']['error_id'],'ADServerDown')
        mutations=[call.args[1] for call in api.request.call_args_list if call.args[0]=='POST']
        self.assertEqual(mutations,['/nodes/proxmox5/qemu/131/status/start','/nodes/proxmox5/qemu/131/status/shutdown'])
        self.assertIn('Get-ADRootDSE',read.call_args.args[4][-1])

    def test_wrong_project_or_live_source_is_never_started(self):
        with self.assertRaises(DistributionError):self.inspect(wrong_owner=True)
        with self.assertRaises(DistributionError):self.inspect(target_is_source=True)

    def test_failed_diagnostics_still_shut_down_copy(self):
        with self.assertRaisesRegex(DistributionError,'read failed'):self.inspect(read_failure=True)
