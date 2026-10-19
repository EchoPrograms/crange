"""Fixed Windows base-job schema, immutable-source guards, and eligibility."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from scripts import crange_jobs
import crange_templates
import crange_windows_base as base


BUILD_ID = "a" * 32


class FakeAPI:
    def __init__(self, config, status="stopped"):
        self.config = config
        self.vm_status = status
        self.requests = []
        self.storage = []

    def status(self, node, vmid):
        return self.vm_status

    def request(self, method, path, fields=None):
        self.requests.append((method, path, fields))
        if method == 'GET' and path.endswith('/get-osinfo'):
            return {'result': {'id': 'mswindows'}}
        if '/storage/local/content' in path:
            if method == 'DELETE':
                self.storage.clear()
                return None
            return list(self.storage)
        if path.endswith('/config') and method == 'PUT':
            self.config.update(fields or {})
            return None
        if method == "POST" and path.endswith("/exec"):
            if len([row for row in self.requests if row[0] == "POST"]) == 2:
                self.vm_status = "stopped"
            return {"pid": 12345}
        if method == "GET" and path.endswith("/exec-status"):
            return {"exited": True, "exitcode": 0, "out-data": "fixed-script-complete"}
        return {"exited": False}

    def vm(self, node, vmid):
        return self.config


def build_record(tags="crange-template-build;crange-build-" + BUILD_ID):
    return {
        "id": BUILD_ID,
        "vmid": 115,
        "guest_name": "crange-build-test-windows",
        "ownership_tag": "crange-build-" + BUILD_ID,
        "status": "installing",
        "definition": {"os": "windows", "node": "proxmox4", "windows_unattended": True},
        "private_answerfile": {
            "build_id": BUILD_ID,
            "vmid": 115,
            "volid": "local:iso/crange-win-" + BUILD_ID + ".iso",
            "sha256": "b" * 64,
        },
    }


class WindowsBaseWorkflowTests(unittest.TestCase):
    def test_identity_survives_seed_detach_but_tracks_hardware_changes(self):
        record = build_record()
        config = {'name': record['guest_name'], 'tags': record['ownership_tag'] + ';crange-template-build',
                  'template': 0, 'agent': '1', 'net0': 'e1000,bridge=craccess',
                  'digest': 'before-detach', 'ide0': 'local:iso/seed.iso,media=cdrom', 'memory': 4096}
        api = FakeAPI(config, status='running')
        with patch.object(crange_templates, 'owned', return_value=config), patch.object(base, '_private_answerfile'):
            before = base._identity(record, api)[1]
            config.update(ide0='none,media=cdrom', digest='after-detach')
            self.assertEqual(base._identity(record, api)[1], before)
            config['memory'] = 8192
            self.assertNotEqual(base._identity(record, api)[1], before)

    def test_guest_agent_unavailable_blocks_preparation_in_ui(self):
        record = build_record()
        config = {'name': record['guest_name'], 'tags': record['ownership_tag'] + ';crange-template-build',
                  'template': 0, 'agent': '1', 'net0': 'e1000,bridge=craccess'}
        api = FakeAPI(config, status='running')
        original = api.request
        def request(method, path, fields=None):
            if path.endswith('/get-osinfo'):
                raise RuntimeError('Guest agent is not connected')
            return original(method, path, fields)
        api.request = request
        with tempfile.TemporaryDirectory() as directory, patch.object(base, 'PRIVATE', Path(directory)), \
             patch.object(crange_templates, 'owned', return_value=config), patch.object(base, '_private_answerfile'):
            result = base.eligibility(record, api=api)
        self.assertFalse(result['can_prepare'])
        self.assertIn('installation is not ready', result['reason'])
        self.assertFalse(any(method == 'POST' for method, _, _ in api.requests))

    def test_admin_job_vectors_are_typed_and_string_only(self):
        for operation in ("template-windows-base-prepare", "template-windows-base-seal"):
            with self.subTest(operation=operation):
                vector = crange_jobs.command(operation, 0, {"build_id": BUILD_ID})
                self.assertEqual(vector, [operation, BUILD_ID])
                self.assertTrue(all(isinstance(item, str) for item in vector))
                with self.assertRaisesRegex(ValueError, "administrator-only"):
                    crange_jobs.command(operation, 100, {"build_id": BUILD_ID})
                with self.assertRaisesRegex(ValueError, "Unexpected or missing"):
                    crange_jobs.command(operation, 0, {"build_id": BUILD_ID, "vmid": 115})

    def test_sealed_candidate_is_rejected_before_private_answerfile_access(self):
        record = build_record("crange-template-build;crange-build-" + BUILD_ID + ";mhl-windows-sealed")
        api = FakeAPI({"name": record["guest_name"], "tags": record["ownership_tag"] + ";crange-template-build;mhl-windows-sealed",
                       "template": 0, "agent": "1", "net0": "e1000,bridge=craccess"})
        with patch.object(crange_templates, "owned", return_value=api.config):
            result = base.eligibility(record, api=api)
        self.assertFalse(result["eligible"])
        self.assertIn("sealed, published, or identity-mismatched", result["reason"])
        self.assertFalse(any(method == "POST" for method, _, _ in api.requests))

    def test_immutable_pve_template_is_rejected(self):
        record = build_record()
        config = {"name": record["guest_name"], "tags": record["ownership_tag"] + ";crange-template-build",
                  "template": 1, "agent": "1", "net0": "e1000,bridge=craccess"}
        api = FakeAPI(config)
        with patch.object(crange_templates, "owned", return_value=config):
            result = base.eligibility(record, api=api)
        self.assertFalse(result["eligible"])
        self.assertIn("sealed, published", result["reason"])

    def test_generic_builder_without_private_per_build_seed_is_actionably_ineligible(self):
        record = build_record()
        record.pop("private_answerfile")
        config = {"name": record["guest_name"], "tags": record["ownership_tag"] + ";crange-template-build",
                  "template": 0, "agent": "1", "net0": "e1000,bridge=craccess"}
        api = FakeAPI(config, status="running")
        with patch.object(crange_templates, "owned", return_value=config):
            result = base.eligibility(record, api=api)
        self.assertFalse(result["eligible"])
        self.assertIn("worker-generated local answerfile", result["reason"])

    def test_resume_timeout_keeps_the_same_qga_handle(self):
        with __import__("tempfile").TemporaryDirectory() as tmp:
            path = Path(tmp) / "build.json"
            record = {"operation": "seal", "state": "seal-running", "pid": 12345}
            with self.assertRaisesRegex(base.WindowsBaseError, "resume this build ID"):
                base._exec_status(FakeAPI({}), {"node": "proxmox4", "vmid": 115}, record, path, timeout=0)
            saved = __import__("json").loads(path.read_text())
            self.assertEqual(saved["pid"], 12345)
            self.assertEqual(saved["state"], "seal-running")

    def test_verified_setup_detaches_and_deletes_only_its_exact_answerfile_iso(self):
        volid = "local:iso/crange-win-" + BUILD_ID + ".iso"
        identity = {"build_id": BUILD_ID, "vmid": 115, "node": "proxmox4"}
        record = {"id": BUILD_ID, "vmid": 115,
                  "private_answerfile": {"build_id": BUILD_ID, "vmid": 115,
                                         "volid": volid, "sha256": "b" * 64}}
        api = FakeAPI({"ide0": volid + ",media=cdrom,size=376K"})
        api.storage = [{"volid": volid, "size": 4096}]
        def cleanup(request):
            self.assertEqual(request,{"command":"windows-seed-cleanup","build_id":BUILD_ID})
            api.storage.clear()
            return {"build_id":BUILD_ID,"node":"proxmox4","vmid":115,"volid":volid,
                    "state":"cleaned","verified_absent":True}
        with patch("crange_windows_seed._seed_cleanup_relay",side_effect=cleanup):
            base._cleanup_answerfile(api, identity, record)
        self.assertEqual(api.config["ide0"], "none,media=cdrom")
        self.assertEqual(api.storage, [])
        self.assertTrue(any(method == "PUT" and fields == {"ide0": "none,media=cdrom"}
                            for method, _, fields in api.requests))
        self.assertFalse(any(method == "DELETE" for method, _, _ in api.requests))

    def test_seed_cleanup_refuses_unrelated_attached_media(self):
        record = {"id": BUILD_ID, "vmid": 115,
                  "private_answerfile": {"build_id": BUILD_ID, "vmid": 115,
                                         "volid": "local:iso/crange-win-" + BUILD_ID + ".iso",
                                         "sha256": "b" * 64}}
        api = FakeAPI({"ide0": "local:iso/other.iso,media=cdrom"})
        with self.assertRaisesRegex(base.WindowsBaseError, "Unexpected installer media"):
            base._cleanup_answerfile(api, {"build_id": BUILD_ID, "vmid": 115, "node": "proxmox4"}, record)
        self.assertFalse(any(method in ("PUT", "DELETE") for method, _, _ in api.requests))

    def test_prepare_and_seal_execute_only_fixed_scripts_and_resume_without_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            secret_dir = home / ".config/crange/windows-build-secrets"
            secret_dir.mkdir(parents=True, mode=0o700)
            secret_path = secret_dir / (BUILD_ID + ".json")
            secret_path.write_text(json.dumps({
                "build_id": BUILD_ID, "vmid": 115,
                "answerfile_sha256": "b" * 64,
                "installer_admin_password": "synthetic-only-password-never-print",
            }))
            secret_path.chmod(0o600)
            record = build_record()
            config = {"name": record["guest_name"], "tags": record["ownership_tag"] + ";crange-template-build",
                      "template": 0, "agent": "1", "net0": "e1000,bridge=craccess",
                      "ide0": "local:iso/crange-win-" + BUILD_ID + ".iso,media=cdrom"}
            api = FakeAPI(config, status="running")
            api.storage = [{"volid": "local:iso/crange-win-" + BUILD_ID + ".iso", "size": 123}]
            journal_dir = base.PRIVATE
            base.PRIVATE = home / "private-journal"
            try:
                with patch.object(Path, "home", return_value=home), \
                        patch.object(crange_templates, "load", return_value=record), \
                        patch.object(crange_templates, "owned", return_value=config), \
                        patch("crange_windows_seed._seed_cleanup_relay",side_effect=lambda request: (
                            api.storage.clear() or {"build_id":BUILD_ID,"node":"proxmox4","vmid":115,
                                "volid":"local:iso/crange-win-"+BUILD_ID+".iso","state":"cleaned",
                                "verified_absent":True})):
                    prepared = base.prepare(BUILD_ID, api=api)
                    self.assertEqual(prepared["state"], "prepared")
                    self.assertNotIn("synthetic-only-password-never-print", json.dumps(prepared))
                    sealed = base.seal(BUILD_ID, api=api)
                    self.assertEqual(sealed["state"], "sealed")
                    prior_posts = len([row for row in api.requests if row[0] == "POST"])
                    self.assertEqual(base.seal(BUILD_ID, api=api)["state"], "sealed")
                    self.assertEqual(len([row for row in api.requests if row[0] == "POST"]), prior_posts)
                    for _, _, payload in (row for row in api.requests if row[0] == "POST"):
                        self.assertEqual(payload["command"][0], base.POWERSHELL)
                        self.assertIn("-EncodedCommand", payload["command"])
                        self.assertNotIn("synthetic-only-password-never-print", json.dumps(payload))
            finally:
                base.PRIVATE = journal_dir


if __name__ == "__main__":
    unittest.main()
