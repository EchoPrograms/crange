import json
import os
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch

sys_path = Path(__file__).resolve().parents[1] / "scripts"
import sys
sys.path.insert(0, str(sys_path))
import crange_windows_guest_admin as store


class WindowsGuestAdminStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "worker" / "windows-guest-admin.json"
        self.project = 100
        self.box = "t001-windows-jump"
        self.guest = 30120
        self.image = "mhl-windows-server-v1"
        self.username = ".\\Administrator"
        self.password = secrets.token_urlsafe(32)
        env = patch.dict(os.environ, {"CRANGE_WINDOWS_GUEST_ADMIN_FILE": str(self.path)})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.temporary.cleanup()

    def test_private_store_roundtrip_and_preserves_other_records(self):
        ref = store.store(self.project, self.box, self.guest, self.image,
                          self.username, self.password)
        ref2 = store.store(self.project, "t002-windows-jump", 30121,
                           self.image, "Administrator", secrets.token_urlsafe(32))
        self.assertEqual(ref, {"project_id": self.project, "box_key": self.box})
        self.assertEqual(set(ref), {"project_id", "box_key"})
        resolved = store.resolve(ref, project_id=self.project, box_key=self.box,
                                 guest_id=self.guest, image_id=self.image)
        self.assertEqual(resolved["username"], self.username)
        self.assertTrue(secrets.compare_digest(resolved["password"], self.password))
        self.assertEqual(ref2["box_key"], "t002-windows-jump")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        data = json.loads(self.path.read_text())
        self.assertEqual(len(data["records"]), 2)
        self.assertEqual(data["records"]["100:t001-windows-jump"]["guest_id"], self.guest)
        self.assertNotIn("password", ref)
        self.assertFalse(list(self.path.parent.glob(".windows-guest-admin.*")))

    def test_wrong_project_box_guest_or_image_cannot_resolve(self):
        ref = store.store(self.project, self.box, self.guest, self.image,
                          self.username, self.password)
        for values in (
            dict(project_id=101, box_key=self.box, guest_id=self.guest, image_id=self.image),
            dict(project_id=self.project, box_key="t002-windows-jump", guest_id=self.guest, image_id=self.image),
            dict(project_id=self.project, box_key=self.box, guest_id=self.guest + 1, image_id=self.image),
            dict(project_id=self.project, box_key=self.box, guest_id=self.guest, image_id="different-image"),
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                store.resolve(ref, **values)
        with self.assertRaisesRegex(ValueError, "reference"):
            store.resolve({**ref, "password": self.password}, project_id=self.project,
                          box_key=self.box, guest_id=self.guest, image_id=self.image)
        with self.assertRaisesRegex(ValueError, "reference"):
            store.resolve({"project_id": True, "box_key": self.box}, project_id=1,
                          box_key=self.box, guest_id=self.guest, image_id=self.image)

    def test_invalid_input_and_insecure_store_permissions_fail_closed(self):
        cases = (
            (True, self.box, self.guest, self.image, self.username, self.password),
            (self.project, "../outside", self.guest, self.image, self.username, self.password),
            (self.project, self.box, True, self.image, self.username, self.password),
            (self.project, self.box, self.guest, "", self.username, self.password),
            (self.project, self.box, self.guest, self.image, "", self.password),
            (self.project, self.box, self.guest, self.image, self.username, "short"),
        )
        for values in cases:
            with self.assertRaises(ValueError):
                store.store(*values)

        store.store(self.project, self.box, self.guest, self.image,
                    self.username, self.password)
        os.chmod(self.path, 0o644)
        with self.assertRaisesRegex(ValueError, "mode 0600"):
            store.resolve(store.reference(self.project, self.box), project_id=self.project,
                          box_key=self.box, guest_id=self.guest, image_id=self.image)

    def test_insecure_lock_permissions_are_rejected(self):
        store.store(self.project, self.box, self.guest, self.image,
                    self.username, self.password)
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        os.chmod(lock, 0o644)
        with self.assertRaisesRegex(ValueError, "lock.*mode 0600"):
            store.resolve(store.reference(self.project, self.box), project_id=self.project,
                          box_key=self.box, guest_id=self.guest, image_id=self.image)

    def test_insecure_parent_directory_is_rejected(self):
        store.store(self.project, self.box, self.guest, self.image,
                    self.username, self.password)
        os.chmod(self.path.parent, 0o755)
        with self.assertRaisesRegex(ValueError, "mode 0700"):
            store.store(self.project, "t001-second", self.guest + 1,
                        self.image, self.username, self.password)


if __name__ == "__main__":
    unittest.main()
