import pathlib
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from crange_windows_answerfile import prepare_sealed_clone_locale, AnswerfileError, NS


class WindowsAnswerfileTests(unittest.TestCase):
    source = b'''<unattend xmlns="urn:schemas-microsoft-com:unattend">
<settings pass="specialize"><component name="Microsoft-Windows-Shell-Setup"><ComputerName>*</ComputerName></component></settings>
<settings pass="oobeSystem"><component name="Microsoft-Windows-Shell-Setup"><OOBE><HideEULAPage>true</HideEULAPage></OOBE><UserAccounts><AdministratorPassword><Value>private-fixture-only</Value><PlainText>true</PlainText></AdministratorPassword></UserAccounts></component></settings></unattend>'''

    def test_locale_only_preparation_preserves_private_settings_and_default_namespace(self):
        prepared, evidence = prepare_sealed_clone_locale(self.source)
        root = ET.fromstring(prepared)
        component = root.find("u:settings[@pass='oobeSystem']/u:component[@name='Microsoft-Windows-International-Core']", NS)
        self.assertEqual({x.tag.split('}')[-1]: x.text for x in component},
                         dict.fromkeys(('InputLocale', 'SystemLocale', 'UILanguage', 'UserLocale'), 'en-US'))
        self.assertEqual(root.find('.//u:AdministratorPassword/u:Value', NS).text, 'private-fixture-only')
        self.assertNotIn(b'ns0:', prepared)
        self.assertTrue(evidence['locale_only_semantic_diff_verified'])
        self.assertNotIn('private-fixture-only', str(evidence))
        self.assertNotEqual(evidence['original_sha256'], evidence['prepared_sha256'])

    def test_repeated_preparation_is_idempotent(self):
        first, _ = prepare_sealed_clone_locale(self.source)
        second, evidence = prepare_sealed_clone_locale(first)
        self.assertEqual(first, second)
        self.assertFalse(evidence['component_created'])

    def test_private_leaf_whitespace_is_preserved(self):
        source = self.source.replace(b'private-fixture-only', b' private-fixture-only ')
        prepared, _ = prepare_sealed_clone_locale(source)
        self.assertEqual(ET.fromstring(prepared).find('.//u:AdministratorPassword/u:Value', NS).text,
                         ' private-fixture-only ')

    def test_malformed_duplicate_and_missing_oobe_are_rejected(self):
        for source in (b'<unattend>', self.source.replace(b'oobeSystem', b'auditSystem'),
                       self.source.replace(b'<settings pass="oobeSystem">',
                           b'<settings pass="oobeSystem"/><settings pass="oobeSystem">')):
            with self.subTest(source=source):
                with self.assertRaises(AnswerfileError):
                    prepare_sealed_clone_locale(source)
