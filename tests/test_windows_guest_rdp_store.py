import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import crange_windows_guest_rdp as rdp


class WindowsRdpCredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "private" / "rdp.json"
        self.env = patch.dict(os.environ, {"CRANGE_WINDOWS_GUEST_RDP_FILE": str(self.path)})
        self.env.start()
        self.ref = rdp.reference(247, "t254-workstation", 73421, "approved-win-image-v4")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_typed_binding_is_nonsecret_private_and_exact_guest_image_bound(self):
        rdp.store(247, "t254-workstation", 73421, "approved-win-image-v4", "private-rdp-password-unique")
        value = rdp.resolve(self.ref, project_id=247, box_key="t254-workstation",
                            guest_id=73421, image_id="approved-win-image-v4")
        self.assertEqual(value, {"username": "user", "password": "private-rdp-password-unique",
                                 "show_credentials": False})
        serialized_ref = json.dumps(self.ref)
        self.assertNotIn(value["password"], serialized_ref)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertRaises(ValueError, rdp.resolve, self.ref, project_id=247,
                          box_key="t254-workstation", guest_id=73421, image_id="other-image")

    def test_private_store_does_not_rotate_or_rebind_existing_identity(self):
        rdp.store(247, "t254-workstation", 73421, "approved-win-image-v4", "first-private-password")
        with self.assertRaisesRegex(ValueError, "already bound"):
            rdp.store(247, "t254-workstation", 73421, "approved-win-image-v4", "second-private-password")
        self.assertEqual(rdp.resolve(self.ref, project_id=247, box_key="t254-workstation",
                                     guest_id=73421, image_id="approved-win-image-v4")["password"],
                         "first-private-password")
        replacement = rdp.reference(247, "t254-workstation", 73422, "approved-win-image-v4")
        rdp.store(247, "t254-workstation", 73422, "approved-win-image-v4", "replacement-private-password")
        self.assertEqual(rdp.resolve(replacement, project_id=247, box_key="t254-workstation",
                                     guest_id=73422, image_id="approved-win-image-v4")["password"],
                         "replacement-private-password")


if __name__ == "__main__":
    unittest.main()
