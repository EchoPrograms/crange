import json
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


class BrandingAssetTests(unittest.TestCase):
    def test_control_panel_declares_its_crange_favicon(self):
        page = (ROOT / 'gui/index.html').read_text()
        icon = ET.parse(ROOT / 'gui/favicon.svg').getroot()
        self.assertIn('rel="icon" type="image/svg+xml" href="/favicon.svg"', page)
        self.assertEqual(icon.attrib.get('viewBox'), '0 0 48 48')

    def test_guacamole_favicon_is_packaged_as_an_extension_resource(self):
        manifest = json.loads((ROOT / 'guacamole/extension/guac-manifest.json').read_text())
        self.assertEqual(manifest['resources']['assets/remote-access.svg'], 'image/svg+xml')
        icon = ET.parse(ROOT / 'guacamole/extension/assets/remote-access.svg').getroot()
        self.assertEqual(icon.attrib.get('viewBox'), '0 0 48 48')
        self.assertIn('app/ext/crange-theme/assets/remote-access.svg',
                      (ROOT / 'guacamole/extension/ui.js').read_text())


if __name__ == '__main__':
    unittest.main()
