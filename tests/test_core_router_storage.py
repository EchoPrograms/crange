"""Host storage setup must fail closed before formatting any existing device."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('router_store', Path(__file__).resolve().parents[1] / 'scripts/setup-core-router-storage.py')
store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store)


class CoreRouterStorageTests(unittest.TestCase):
    def test_wrong_host_and_nonroot_never_touch_storage(self):
        for uid, host in ((1000, 'proxmox4'), (0, 'proxmox2')):
            with patch.object(store.os, 'geteuid', return_value=uid), \
                 patch.object(store.socket, 'gethostname', return_value=host), \
                 patch.object(store, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'designated surviving core'):
                    store.inspect()
                run.assert_not_called()

    def test_refuses_lost_quorum_wrong_pool_and_insufficient_physical_capacity(self):
        pool = {'lv_uuid': store.POOL_UUID, 'lv_size': str(800 * 2**30),
                'data_percent': '72', 'metadata_percent': '2'}
        for changes in ({'lv_uuid': 'different'}, {'data_percent': '90'}, {'metadata_percent': '11'}):
            result = json.dumps({'report': [{'lv': [{**pool, **changes}]}]})
            with patch.object(store.os, 'geteuid', return_value=0), \
                 patch.object(store.socket, 'gethostname', return_value='proxmox4'), \
                 patch.object(store, 'run', side_effect=['Quorate:          Yes', result]) as run:
                with self.assertRaisesRegex(ValueError, 'physical headroom'):
                    store.inspect()
                self.assertEqual(run.call_count, 2)
        with patch.object(store.os, 'geteuid', return_value=0), \
             patch.object(store.socket, 'gethostname', return_value='proxmox4'), \
             patch.object(store, 'run', return_value='Quorate:          No') as run:
            with self.assertRaisesRegex(ValueError, 'quorum'):
                store.inspect()
            run.assert_called_once_with('pvecm', 'status')

    def test_existing_device_or_interrupted_install_cannot_be_reformatted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'journal'
            mount = Path(directory) / 'mount'
            device = Path(directory) / 'device'
            with patch.object(store, 'ROOT', root), patch.object(store, 'MOUNT', mount), \
                 patch.object(store, 'DEVICE', str(device)), patch.object(store, 'run') as run:
                device.touch()
                with self.assertRaisesRegex(ValueError, 'uncertain'):
                    store.install({'existing_storage': None})
                device.unlink()
                root.mkdir()
                with self.assertRaisesRegex(ValueError, 'uncertain'):
                    store.install({'existing_storage': None})
                root.rmdir()
                with self.assertRaisesRegex(ValueError, 'uncertain'):
                    store.install({'existing_storage': {'storage': store.STORE}})
                run.assert_not_called()

    def test_controller_grant_cannot_expand_a_role_or_touch_other_paths(self):
        with patch.object(store, 'run', return_value=json.dumps({'Datastore.Audit': 1, 'Sys.Console': 1})) as run:
            with self.assertRaisesRegex(ValueError, 'unexpected privileges'):
                store.grant_controller_storage()
            self.assertEqual(run.call_count, 1)
        with patch.object(store, 'run', side_effect=[json.dumps({'Datastore.Audit': 1, 'Datastore.AllocateSpace': 1}), '', '']) as run:
            store.grant_controller_storage()
            grants = [c.args for c in run.call_args_list[1:]]
            self.assertEqual(len(grants), 2)
            for grant in grants:
                self.assertEqual(grant[:5], ('pvesh', 'set', '/access/acl', '--path', '/storage/crange-router-ha'))
                self.assertIn('PVEDatastoreUser', grant)


if __name__ == '__main__':
    unittest.main()
