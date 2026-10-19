import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import crange_guac
import crange_windows_guest_rdp as rdp
import crange


class WindowsGuacCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "private" / "rdp.json"
        self.env = patch.dict(os.environ, {
            "CRANGE_WINDOWS_GUEST_RDP_FILE": str(self.path),
            "GUACAMOLE_GUEST_USERNAME": "toor",
            "GUACAMOLE_GUEST_PASSWORD": "legacy-linux-default",
        })
        self.env.start()
        self.ref = rdp.reference(247, "t001-desktop", 73421, "approved-win-image-v4")
        rdp.store(247, "t001-desktop", 73421, "approved-win-image-v4", "private-rdp-password-unique")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_typed_windows_reference_resolves_only_rdp_account(self):
        record = {"project_id": 247, "crange_box_key": "t001-desktop",
                  "crange_guest_id": 73421, "crange_image_id": "approved-win-image-v4",
                  "credential_ref": self.ref, "guest_os": "windows", "protocol": "rdp",
                  "address": "10.247.1.20", "port": 3389}
        params = crange_guac.guest_parameters(record)
        self.assertEqual(params["username"], "user")
        self.assertEqual(params["password"], "private-rdp-password-unique")
        self.assertNotEqual(params["password"], "legacy-linux-default")
        self.assertEqual(params["hostname"], "10.247.1.20")

    def test_windows_rdp_fails_closed_without_private_binding(self):
        record = {"guest_os": "windows", "protocol": "rdp", "address": "10.247.1.20"}
        with self.assertRaisesRegex(crange_guac.GuacamoleError, "credentials are not configured"):
            crange_guac.guest_parameters(record)

    def test_captured_controller_infers_domain_without_using_management_credentials(self):
        image_id='p247-image-123456789abc'
        ref=rdp.store(247,'captured-dc',73422,image_id,'separate-synthetic-rdp-secret')
        record={'project_id':247,'crange_box_key':'captured-dc','crange_guest_id':73422,
                'crange_image_id':image_id,'credential_ref':ref,'guest_os':'windows',
                'protocol':'rdp','address':'10.247.1.10'}
        identity={'identity_mode':'windows-scenario-snapshot-v1','snapshot_project_id':247,
                  'domain_role':5,'domain_name':'example.test'}
        image={'verified':True,'source_identity':identity}
        with patch('crange_scheduling.read_settings',return_value=({}, {'images':{image_id:image}})), \
             patch('crange_windows_snapshot.validate_captured_identity') as validate:
            params=crange_guac.guest_parameters(record)
        validate.assert_called_once_with(identity)
        self.assertEqual(params['username'],'user@example.test')
        self.assertEqual(params['password'],'separate-synthetic-rdp-secret')

    def test_explicit_custom_reference_keeps_precedence(self):
        record = {"project_id": 247, "credential_ref": "a" * 32,
                  "guest_os": "windows", "protocol": "rdp", "address": "10.247.1.20"}
        with patch("crange_guest_credentials.resolve", return_value={
                "username": "assessment-user", "password": "custom-credential", "show_credentials": False}):
            params = crange_guac.guest_parameters(record)
        self.assertEqual(params["username"], "assessment-user")
        self.assertEqual(params["password"], "custom-credential")

    def test_guac_sync_emits_typed_ref_only_for_matching_applied_windows_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "terraform" / "environments" / "project-247"
            project.mkdir(parents=True)
            box_key = "t001-desktop"
            ref = rdp.reference(247, box_key, 73421, "approved-win-image-v4")
            rdp.store(247, box_key, 73421, "approved-win-image-v4", "private-rdp-password-unique")
            cfg = {"project_id": 247, "team_ids": ["1"], "jump_boxes": {},
                   "scenario_boxes": {box_key: {"team_id": "1", "template": "win",
                                                 "guac_protocol": "rdp", "name": "desktop"}},
                   "scenario_containers": {}}
            outputs = {"scenario_boxes": {"value": {box_key: {
                "id": 73421, "address": "10.247.1.20",
                "placement": {"image_id": "approved-win-image-v4", "template_id": 912,
                              "template_node": "proxmox4"}}}}}
            cfg['scenario_boxes']['pending']={**cfg['scenario_boxes'][box_key]}
            outputs['scenario_boxes']['value']['pending']={**outputs['scenario_boxes']['value'][box_key],'id':None,'address':'10.247.1.21'}
            cfg['jump_boxes']['pending-jump']={'team_id':'1','jump_number':1,'address':'10.247.1.22'}
            outputs['jump_boxes']={'value':{'pending-jump':{'id':None,'address':'10.247.1.22'}}}
            sidecar = {f"vm:{box_key}": {"guest_id": 73421, "image_id": "approved-win-image-v4",
                                         "declared_template": "win", "template_id": 912,
                                         "template_node": "proxmox4"}}
            (project / "crange-applied-placements.json").write_text(__import__("json").dumps(sidecar))
            class Users:
                def __init__(self, *_args, **_kwargs): pass
                def list_users(self, **_kwargs): return []
            class Connector:
                username = None
                password = None
                base_url = "http://guac"
            with patch.object(crange, "ROOT", root), \
                 patch.object(crange, "project_dir", return_value=project), \
                 patch.object(crange, "json_config", return_value=cfg), \
                 patch.object(crange, "output_json", return_value=outputs), \
                 patch.object(crange, "templates_for", return_value={"win": {"profile": {"os_type": "win11"}}}), \
                 patch("crange_auth.AuthStore", Users), \
                 patch("crange_guac.GuacamoleClient", return_value=Connector()):
                crange.cmd_guac_sync(type("Args", (), {"project": "247"})())
            manifest = __import__("json").loads((root / "guacamole/access/project-247.json").read_text())
            self.assertEqual(len(manifest["connections"]),1)
            row = manifest["connections"][0]
            self.assertEqual(row["credential_ref"], ref)
            self.assertEqual(row["guest_os"], "windows")
            self.assertNotIn("private-rdp-password-unique", __import__("json").dumps(manifest))

            sidecar[f"vm:{box_key}"]["image_id"] = "different-image"
            (project / "crange-applied-placements.json").write_text(__import__("json").dumps(sidecar))
            with patch.object(crange, "ROOT", root), \
                 patch.object(crange, "project_dir", return_value=project), \
                 patch.object(crange, "json_config", return_value=cfg), \
                 patch.object(crange, "output_json", return_value=outputs), \
                 patch.object(crange, "templates_for", return_value={"win": {"profile": {"os_type": "win11"}}}), \
                 patch("crange_auth.AuthStore", Users), \
                 patch("crange_guac.GuacamoleClient", return_value=Connector()):
                crange.cmd_guac_sync(type("Args", (), {"project": "247"})())
            manifest = __import__("json").loads((root / "guacamole/access/project-247.json").read_text())
            self.assertIsNone(manifest["connections"][0].get("credential_ref"))


if __name__ == "__main__":
    unittest.main()
