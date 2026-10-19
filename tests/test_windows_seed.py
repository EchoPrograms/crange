"""Private per-build unattended-seed generation tests (synthetic credentials)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import crange_windows_seed as seed
import crange_templates as templates


class WindowsSeedTests(unittest.TestCase):
    def test_selected_node_upload_endpoint_uses_unique_approved_cluster_identity(self):
        class API:
            def __init__(self, rows):
                self.rows = rows
            def request(self, method, path):
                self.asserted = (method, path)
                return self.rows
        row = {"type": "node", "name": "proxmox5", "ip": "10.0.0.105", "online": 1}
        api = API([row])
        self.assertEqual(seed.selected_node_upload_endpoint(api, "proxmox5"),
                         "https://10.0.0.105:8006/api2/json")
        self.assertEqual(api.asserted, ("GET", "/cluster/status"))
        for rows, node in [([row, row], "proxmox5"), ([{**row, "ip": "10.0.0.101"}], "proxmox5"),
                           ([{**row, "online": 0}], "proxmox5"), ([row], "proxmox6")]:
            with self.subTest(rows=rows, node=node), self.assertRaises(seed.WindowsSeedError):
                seed.selected_node_upload_endpoint(API(rows), node)

    def test_seed_upload_targets_selected_node_api_without_cluster_proxy(self):
        class API:
            endpoint="https://10.0.0.101:8006/api2/json"
            token="synthetic-test-token"
            ssl_context=object()
            def __init__(self): self.content_calls=0
            def request(self,method,path,params=None):
                if path=="/cluster/status":
                    return [{"type":"node","name":"proxmox5","ip":"10.0.0.105","online":1}]
                if path=="/nodes/proxmox5/storage/local/content":
                    self.content_calls+=1
                    return [] if self.content_calls==1 else [{"volid":"local:iso/crange-win-test.iso","size":4}]
                raise AssertionError((method,path))
        class Response:
            status=200
            def read(self,limit): return b'{"data":null}'
        class Connection:
            instances=[]
            def __init__(self,*args,**kwargs): self.args=args;self.kwargs=kwargs;self.calls=[];self.__class__.instances.append(self)
            def putrequest(self,*args): self.calls.append(("request",args))
            def putheader(self,*args): self.calls.append(("header",args))
            def endheaders(self): pass
            def send(self,data): pass
            def getresponse(self): return Response()
            def close(self): pass
        with tempfile.TemporaryDirectory() as tmp:
            image=Path(tmp)/"seed.iso";image.write_bytes(b"seed")
            api=API()
            with patch.object(seed.http.client,"HTTPSConnection",Connection):
                self.assertEqual(seed._upload_iso(api,"proxmox5","crange-win-test.iso",image,
                    __import__("hashlib").sha256(b"seed").hexdigest()),"local:iso/crange-win-test.iso")
            connection=Connection.instances[-1]
            self.assertEqual(connection.args[0],"10.0.0.105")
            self.assertEqual(connection.args[1],8006)
            self.assertEqual(connection.calls[0][1],
                             ("POST", "/api2/json/nodes/proxmox5/storage/local/upload"))

    def test_schema_requires_fixed_windows_eval_media_and_driver_iso(self):
        value = {
            "name": "windows-seed-test", "node": "proxmox4", "iso": templates.WINDOWS_SERVER_EVAL_ISO,
            "driver_iso": "crange-library:iso/virtio-win.iso", "os": "windows", "cores": 2,
            "memory": 4096, "disk": 40, "disk_bus": "sata", "bios": "ovmf", "machine": "q35",
            "cpu": "host", "nic_models": ["e1000"], "agent": True, "cloud_init": False,
            "tpm": False, "windows_unattended": True,
        }
        self.assertTrue(templates.validate(value)["windows_unattended"])
        with self.assertRaisesRegex(ValueError, "VirtIO driver ISO"):
            templates.validate({**value, "driver_iso": ""})
        with self.assertRaisesRegex(ValueError, "QEMU Guest Agent setting"):
            templates.validate({**value, "agent": False})

    def test_seed_is_generated_per_build_and_secret_is_private_and_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            captured = {}

            def fake_xorriso(args, **kwargs):
                captured["xml"] = (Path(kwargs["cwd"]) / "Autounattend.xml").read_bytes()
                output = Path(args[args.index("-o") + 1])
                output.write_bytes(b"synthetic ISO bytes")

            def fake_upload(api, node, filename, image, digest):
                captured["filename"] = filename
                self.assertEqual(node, "proxmox4")
                self.assertEqual(digest, __import__("hashlib").sha256(b"synthetic ISO bytes").hexdigest())
                return "local:iso/" + filename

            with patch.object(Path, "home", return_value=home), \
                    patch.object(seed.shutil, "which", return_value="/usr/bin/xorriso"), \
                    patch.object(seed.subprocess, "run", side_effect=fake_xorriso), \
                    patch.object(seed, "seed_cleanup_relay_ready", return_value=True), \
                    patch.object(seed, "_upload_iso", side_effect=fake_upload):
                binding = seed.create("a" * 32, 3333, "proxmox4", "crange-library:iso/virtio-win.iso", object())
            self.assertEqual(binding["volid"], "local:iso/crange-win-" + "a" * 32 + ".iso")
            root = ET.fromstring(captured["xml"])
            password = root.find(".//{urn:schemas-microsoft-com:unattend}AdministratorPassword/{urn:schemas-microsoft-com:unattend}Value").text
            self.assertGreaterEqual(len(password), 30)
            self.assertEqual(root.tag, "{urn:schemas-microsoft-com:unattend}unattend")
            secret_path = home / ".config/crange/windows-build-secrets" / ("a" * 32 + ".json")
            secret = json.loads(secret_path.read_text())
            self.assertEqual(secret["installer_admin_password"], password)
            self.assertEqual(secret["answerfile_sha256"], binding["sha256"])
            self.assertEqual(secret["vmid"], 3333)
            self.assertEqual(os.stat(secret_path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(secret_path.parent).st_mode & 0o777, 0o700)
            self.assertFalse(seed._artifact_path("a" * 32).exists())
            self.assertNotIn(password, json.dumps(binding))

    def test_existing_build_credential_binding_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            secret_dir = home / ".config/crange/windows-build-secrets"
            secret_dir.mkdir(parents=True, mode=0o700)
            (secret_dir / ("a" * 32 + ".json")).write_text("synthetic")
            (secret_dir / ("a" * 32 + ".json")).chmod(0o600)
            with patch.object(Path, "home", return_value=home):
                with self.assertRaisesRegex(seed.WindowsSeedError, "already exist"):
                    seed.create("a" * 32, 3333, "proxmox4", "crange-library:iso/virtio-win.iso", object())

    def test_upload_accepted_then_eof_keeps_private_seed_for_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            def fake_xorriso(args, **kwargs):
                Path(args[args.index("-o") + 1]).write_bytes(b"synthetic ISO bytes")
            with patch.object(Path, "home", return_value=home), \
                    patch.object(seed.shutil, "which", return_value="/usr/bin/xorriso"), \
                    patch.object(seed.subprocess, "run", side_effect=fake_xorriso), \
                    patch.object(seed, "seed_cleanup_relay_ready", return_value=True), \
                    patch.object(seed, "_upload_iso", side_effect=seed.SeedUploadFailure(True, "EOF")):
                with self.assertRaises(seed.SeedUploadUncertain):
                    seed.create("b" * 32, 3334, "proxmox4", "crange-library:iso/virtio-win.iso", object())
                path = home / ".config/crange/windows-build-secrets" / ("b" * 32 + ".json")
                journal = json.loads(path.read_text())
                self.assertEqual(journal["state"], "upload_uncertain")
                self.assertTrue(journal["installer_admin_password"])
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                artifact = seed._artifact_path("b" * 32)
                self.assertEqual(artifact.read_bytes(), b"synthetic ISO bytes")
                self.assertEqual(os.stat(artifact).st_mode & 0o777, 0o600)
                status = seed.public_status("b" * 32)
            self.assertTrue(status["reconcile_required"])
            self.assertNotIn(journal["installer_admin_password"], json.dumps(status))

    def test_reconcile_unavailable_provider_retains_private_seed_and_never_deletes(self):
        class UnavailableAPI:
            def __init__(self):
                self.calls = []
            def request(self, method, path, params=None):
                self.calls.append((method, path))
                raise OSError("provider unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / ".config/crange/windows-build-secrets" / ("c" * 32 + ".json")
            seed._private_dir(path.parent)
            artifact = path.with_suffix(".iso")
            artifact.write_bytes(b"synthetic ISO bytes")
            artifact.chmod(0o600)
            password = "Synthetic-Only-Password-42!"
            seed._atomic_secret(path, {
                "build_id": "c" * 32, "vmid": 3335, "node": "proxmox5",
                "volid": "local:iso/crange-win-" + "c" * 32 + ".iso",
                "artifact_name": "c" * 32 + ".iso",
                "iso_sha256": __import__("hashlib").sha256(b"synthetic ISO bytes").hexdigest(),
                "iso_size": len(b"synthetic ISO bytes"),
                "state": "upload_uncertain",
                "created_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                "installer_admin_password": password,
            })
            record = {"vmid": 3335, "definition": {"node": "proxmox5"}}
            api = UnavailableAPI()
            with patch.object(Path, "home", return_value=home):
                with self.assertRaises(OSError):
                    seed.reconcile_upload("c" * 32, record, api)
                self.assertTrue(path.exists())
                self.assertTrue(artifact.exists())
                self.assertEqual(seed._read_secret(path)["installer_admin_password"], password)
            self.assertFalse(any(method == "DELETE" for method, _ in api.calls))

    def test_reconcile_exact_seed_then_release_keeps_journal_until_finalize(self):
        class ExactAPI:
            def __init__(self, volid):
                self.volid = volid
                self.deleted = False
            def request(self, method, path, params=None):
                if path == "/cluster/resources":
                    return []
                if path == "/nodes/proxmox5/tasks":
                    return []
                if path == "/nodes/proxmox5/storage/local/content":
                    return [] if self.deleted else [{"volid": self.volid, "size": len(b"synthetic ISO bytes")}]
                if method == "DELETE" and path.endswith(self.volid.replace(":", "%3A").replace("/", "%2F")):
                    self.deleted = True
                    return None
                raise AssertionError((method, path))
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            build_id = "e" * 32
            volid = "local:iso/crange-win-" + build_id + ".iso"
            path = home / ".config/crange/windows-build-secrets" / (build_id + ".json")
            api = ExactAPI(volid)
            record = {"vmid": 3336, "definition": {"node": "proxmox5"}}
            def fake_cleanup(request):
                api.deleted = True
                return {"build_id": build_id, "node": "proxmox5", "vmid": 3336,
                        "volid": volid, "state": "cleaned", "verified_absent": True}
            with patch.object(Path, "home", return_value=home), \
                    patch.object(seed, "_seed_cleanup_relay", side_effect=fake_cleanup):
                seed._private_dir(path.parent)
                artifact = seed._artifact_path(build_id)
                artifact.write_bytes(b"synthetic ISO bytes")
                artifact.chmod(0o600)
                seed._atomic_secret(path, {
                    "build_id": build_id, "vmid": 3336, "node": "proxmox5", "volid": volid,
                    "artifact_name": build_id + ".iso",
                    "iso_sha256": __import__("hashlib").sha256(b"synthetic ISO bytes").hexdigest(),
                    "iso_size": len(b"synthetic ISO bytes"), "state": "upload_uncertain",
                    "created_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                    "installer_admin_password": "Synthetic-Only-Password-43!",
                })
                outcome = seed.reconcile_upload(build_id, record, api)
                self.assertEqual(outcome["state"], "cleaned")
                self.assertTrue(path.exists(), "lease release happens before deleting the private credential journal")
                self.assertEqual(seed._read_secret(path)["state"], "cleaned")
                seed.finalize_cleanup(build_id)
                self.assertFalse(path.exists())
                self.assertFalse(artifact.exists())
            self.assertTrue(api.deleted)


if __name__ == "__main__":
    unittest.main()
