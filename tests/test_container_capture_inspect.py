import hashlib,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange_capture as capture
from crange_distribution import DistributionError

class ContainerInspectTests(unittest.TestCase):
    def test_only_owned_copy_is_started_read_and_stopped_even_on_read_failure(self):
        for fail in (False,True):
            with tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);(root/'capture-journal').mkdir()
                key='t254-db';slug=hashlib.sha256(f'100:v1:{key}'.encode()).hexdigest()[:12]
                receipt={'node':'proxmox3','template_id':137,'source_id':127,'state':'allocated','marker':'crange-capture-'+slug}
                (root/'capture-journal'/f'capture-p100-{slug}.json').write_text(json.dumps(receipt))
                config={'tags':'crange-image;crange-capture-'+slug+';p100','net0':'name=eth0,bridge=isolated,link_down=1'}
                api=Mock();api.container.return_value=config;api.container_status.return_value='stopped';api.request.return_value=[]
                reader=Mock(side_effect=DistributionError('read failed') if fail else ['degraded','mysql.service failed'])
                with patch.object(capture,'DATA',root),patch.object(capture,'read_settings',return_value=({'allowed_nodes':['proxmox3'],'capture_isolated_bridge':'isolated'},{})), \
                     patch.object(capture,'_require_probe_pool'),patch.object(capture,'node_locks',return_value=__import__('contextlib').nullcontext()), \
                     patch('crange_distribution.SSHContainerExec',return_value=reader):
                    args=dict(project=100,version='v1',machine=key,cfg={'scenario_containers':{key:{'team_id':'254'}}},outputs={'scenario_containers':{'value':{key:{'id':127}}}})
                    if fail:
                        with self.assertRaisesRegex(DistributionError,'read failed'):capture.inspect_container_capture(api,**args)
                    else:
                        result=capture.inspect_container_capture(api,**args);self.assertEqual(result['system_state'],'degraded')
                api.container_start.assert_called_once_with('proxmox3',137)
                api.container_stop.assert_called_once_with('proxmox3',137)

if __name__=='__main__':unittest.main()
