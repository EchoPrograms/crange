import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import crange

class HealthRoutingTests(unittest.TestCase):
    def test_standard_and_custom_checks_use_their_own_subnet_router(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            inventory=root/'ansible/inventory/generated/project-003.json'
            inventory.parent.mkdir(parents=True)
            hosts={'p003c100_router':{'router_owner_team_id':'254','router_team_network':'10.3.100.0/24'},
                   'p003c101_router':{'router_owner_team_id':'1','router_team_network':'10.3.101.0/24'}}
            inventory.write_text(json.dumps({'all':{'children':{'routers':{'children':{'custom_routers':{'hosts':hosts}}}}}}))
            with patch.object(crange,'ROOT',root),patch.object(crange,'run') as run:
                crange.check_endpoint(3,'254','10.3.254.20',{'type':'ping'})
                self.assertEqual(run.call_args.args[0][4],'p003t254_router')
                crange.check_endpoint(3,'254','10.3.100.20',{'type':'tcp','port':22})
                self.assertEqual(run.call_args.args[0][4],'p003c100_router')
                run.reset_mock()
                for address in ['10.3.101.20','10.4.100.20']:
                    with self.assertRaises(crange.CrangeError):
                        crange.check_endpoint(3,'254',address,{'type':'ping'})
                run.assert_not_called()
                hosts['duplicate']={'router_owner_team_id':'254','router_team_network':'10.3.100.0/24'}
                inventory.write_text(json.dumps({'all':{'children':{'routers':{'children':{'custom_routers':{'hosts':hosts}}}}}}))
                with self.assertRaises(crange.CrangeError):
                    crange.check_endpoint(3,'254','10.3.100.20',{'type':'ping'})
                run.assert_not_called()
