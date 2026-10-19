"""Fixed administrator-only locale preparation for sealed Windows images.

This module transforms private XML bytes; callers must enforce stopped guest
and exact owned disk scope. It never accepts commands, credentials or paths
from project managers, and never returns XML in public evidence.
"""
import copy
import hashlib
import xml.etree.ElementTree as ET

UNATTEND = 'urn:schemas-microsoft-com:unattend'
NS = {'u': UNATTEND}
LOCALES = ('InputLocale', 'SystemLocale', 'UILanguage', 'UserLocale')
COMPONENT = 'Microsoft-Windows-International-Core'


class AnswerfileError(ValueError):
    pass


def _semantic(element):
    return (element.tag, tuple(sorted(element.attrib.items())),
            ((element.text or '').strip() if len(element) else (element.text or '')),
            tuple(_semantic(child) for child in element))


def prepare_sealed_clone_locale(content):
    """Add only en-US OOBE locale fields while preserving private settings."""
    if not isinstance(content, bytes) or not 1 <= len(content) <= 2 * 1024 * 1024:
        raise AnswerfileError('Private Windows answer file size is invalid.')
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise AnswerfileError('Private Windows answer file is malformed.') from exc
    if root.tag != '{' + UNATTEND + '}unattend':
        raise AnswerfileError('Windows answer file namespace is invalid.')
    settings = root.findall("u:settings[@pass='oobeSystem']", NS)
    if len(settings) != 1:
        raise AnswerfileError('Exactly one existing OOBE settings pass is required.')
    oobe = settings[0]
    if len(oobe.findall("u:component[@name='Microsoft-Windows-Shell-Setup']", NS)) != 1:
        raise AnswerfileError('Existing Windows shell OOBE settings are required.')
    original = copy.deepcopy(root)
    components = oobe.findall("u:component[@name='" + COMPONENT + "']", NS)
    if len(components) > 1:
        raise AnswerfileError('Duplicate Windows locale components are invalid.')
    created = not components
    if created:
        component = ET.SubElement(oobe, '{' + UNATTEND + '}component', {
            'name': COMPONENT, 'processorArchitecture': 'amd64',
            'publicKeyToken': '31bf3856ad364e35', 'language': 'neutral',
            'versionScope': 'nonSxS'})
    else:
        component = components[0]
        if component.get('processorArchitecture') != 'amd64':
            raise AnswerfileError('Windows locale component architecture is invalid.')
    previous = {}
    for name in LOCALES:
        nodes = component.findall('u:' + name, NS)
        if len(nodes) > 1:
            raise AnswerfileError('Duplicate Windows locale values are invalid.')
        previous[name] = nodes[0].text if nodes else None
        node = nodes[0] if nodes else ET.SubElement(component, '{' + UNATTEND + '}' + name)
        node.text = 'en-US'
    # Prove a semantic locale-only edit independently of XML serialization.
    reverted = copy.deepcopy(root)
    roobe = reverted.findall("u:settings[@pass='oobeSystem']", NS)[0]
    rcomponent = roobe.findall("u:component[@name='" + COMPONENT + "']", NS)[0]
    if created:
        roobe.remove(rcomponent)
    else:
        for name, value in previous.items():
            node = rcomponent.find('u:' + name, NS)
            if value is None:
                rcomponent.remove(node)
            else:
                node.text = value
    if _semantic(reverted) != _semantic(original):
        raise AnswerfileError('Windows preparation changed non-locale settings.')
    ET.register_namespace('', UNATTEND)
    ET.register_namespace('wcm', 'http://schemas.microsoft.com/WMIConfig/2002/State')
    ET.register_namespace('xsi', 'http://www.w3.org/2001/XMLSchema-instance')
    result = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    if _semantic(ET.fromstring(result)) != _semantic(root):
        raise AnswerfileError('Windows locale serialization changed settings.')
    return result, {'original_sha256': hashlib.sha256(content).hexdigest(),
                    'prepared_sha256': hashlib.sha256(result).hexdigest(),
                    'locale_only_semantic_diff_verified': True,
                    'locale': 'en-US', 'component_created': created}
