from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange


class ImageStorageTests(unittest.TestCase):
    def setUp(self):
        self.api = object.__new__(crange.ProxmoxAPI)
        self.api.request = Mock()
        self.api.task = Mock()
        self.api.vm = Mock(return_value={'scsi0': 'local-lvm:vm-100-disk-0,size=8G', 'template': 1})
        self.api.container = Mock(return_value={'rootfs': 'local-lvm:vm-100-disk-0,size=8G', 'template': 1})

    def test_absent_settings_keep_legacy_local_capture(self):
        with tempfile.TemporaryDirectory() as home, patch.object(crange.Path, 'home', return_value=Path(home)):
            self.assertEqual(self.api.image_datastore('proxmox4', 100, 'vm'), 'local-lvm')
        self.api.request.assert_not_called()

    def test_setting_requires_active_content_and_free_space(self):
        with tempfile.TemporaryDirectory() as home, patch.object(crange.Path, 'home', return_value=Path(home)):
            setting = Path(home) / '.config/crange/image-storage.json'
            setting.parent.mkdir(parents=True)
            setting.write_text('{"image_datastore":"crange-library"}')
            self.api.request.return_value = {'active': 1, 'content': 'images,rootdir', 'total': 400*1024**3, 'avail': 300*1024**3}
            self.assertEqual(self.api.image_datastore('proxmox4', 100, 'vm'), 'crange-library')
            self.api.request.return_value['avail'] = 21*1024**3
            with self.assertRaisesRegex(crange.CrangeError, 'headroom'):
                self.api.image_datastore('proxmox4', 100, 'container')
            self.api.request.return_value['active'] = 0
            with self.assertRaisesRegex(crange.CrangeError, 'inactive'):
                self.api.image_datastore('proxmox4', 100, 'vm')

    def test_capture_vm_uses_qcow2_library_and_ct_uses_same_target(self):
        self.api.image_datastore = Mock(return_value='crange-library')
        self.api.request.side_effect = lambda method, path, fields=None: 200 if path == '/cluster/nextid' else 'UPID:test'
        self.api.container_template = Mock()
        self.api.verify_container_image = Mock()
        self.assertEqual(self.api.capture_image('proxmox4', 100, 'release', 'capture'), 200)
        self.api.request.assert_any_call('POST', '/nodes/proxmox4/qemu/100/clone',
            {'newid': 200, 'name': 'release', 'description': 'capture', 'pool': 'crange-iac', 'full': 1, 'storage': 'crange-library', 'format': 'qcow2'})
        self.assertEqual(self.api.capture_container_image('proxmox4', 100, 'release', 'capture'), 200)
        self.api.request.assert_any_call('POST', '/nodes/proxmox4/lxc/100/clone',
            {'newid': 200, 'hostname': 'release', 'description': 'capture', 'pool': 'crange-iac', 'full': 1, 'storage': 'crange-library'})
        self.api.verify_container_image.assert_called_once_with('proxmox4', 200)

if __name__ == '__main__': unittest.main()
