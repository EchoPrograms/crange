import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import crange_template_media as media


class TemplateMediaPendingTests(unittest.TestCase):
    def test_ignores_unrelated_protected_spool_records(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(media, 'SPOOL', Path(directory)):
            upload = media.begin('admin', 'test.iso', 1)['upload_id']
            unrelated = Path(directory) / 'server2025-primary-import.json'
            unrelated.write_text('{"source":"import audit"}')
            unrelated.chmod(0o600)
            self.assertEqual(media.pending('admin'), [
                {'id': upload, 'name': 'test.iso', 'size': 1, 'status': 'uploading'}])


if __name__ == '__main__':
    unittest.main()
