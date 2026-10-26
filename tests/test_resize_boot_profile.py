import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from crange_templates import media_tf_overrides


class ResizeBootProfileTests(unittest.TestCase):
    def test_growth_preserves_boot_devices_while_image_replacement_can_choose_new_ones(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            key = 'jumpbox:t001-j01'
            (path / 'crange-applied-placements.json').write_text(json.dumps({key: {'image_id': 'parrot'}}))
            live = {key: {'boot_order': ['scsi0', 'ide2', 'net0'], 'machine': 'pc-q35-9.2'}}
            row = {'resource_key': key, 'kind': 'vm', 'image': 'parrot', 'box': {}, 'reference': {}}
            prepared = {'placements': [{'resource_key': key, 'image': 'parrot', 'resolved': {'template_id': 169}}], 'requests': []}
            with patch('crange_scheduling.declarations', return_value=[row]), \
                 patch('crange_scheduling.applied_resources', return_value=live):
                resolved = media_tf_overrides(path, {}, prepared, api=Mock())['resolved_placements'][key]
                self.assertEqual(resolved['boot_order'], live[key]['boot_order'])
                self.assertEqual(resolved['machine'], 'pc-q35-9.2')
                row['image'] = 'replacement'
                resolved = media_tf_overrides(path, {}, prepared, api=Mock())['resolved_placements'][key]
                self.assertNotIn('boot_order', resolved)
