"""Keep Proxmox tags categorical while retaining exact automation provenance.

The controller's API boundary presents the original logical configuration to
ownership checks. Native Proxmox displays only the categorical tags. Notes keep
an exact, reversible ownership record; inherited or altered tags never acquire
another template's ownership merely by retaining its notes.
"""
import copy
import json
import re

PREFIX = '\n\n<!-- CRANGE-OWNERSHIP-V1 '
SUFFIX = ' -->'
IDENTITY = re.compile(r'crange-(?:cache-[0-9]+|(?:build|capture|job)-[A-Za-z0-9-]{1,96})\Z')


def category(marker):
    if marker.startswith('crange-build-'):
        return 'crange-template-build'
    return 'crange-capture' if marker.startswith('crange-capture-') else 'crange-cache'


def _tags(value):
    return {tag for tag in str(value or '').split(';') if tag}


def _record(description):
    description = str(description or '')
    if PREFIX not in description:
        return description, None
    # PVE serializes notes as comment lines and adds a final newline.
    description = description.rstrip('\n')
    if description.count(PREFIX) != 1 or not description.endswith(SUFFIX):
        raise ValueError('Malformed CRANGE ownership metadata; no tag changes are safe.')
    body, encoded = description.split(PREFIX, 1)
    if len(encoded) > 8192:
        raise ValueError('CRANGE ownership metadata exceeds its limit.')
    try:
        value = json.loads(encoded[:-len(SUFFIX)])
    except ValueError:
        raise ValueError('Invalid CRANGE ownership metadata.') from None
    if (not isinstance(value, dict) or set(value) != {'tags', 'description_present'}
            or not isinstance(value['tags'], str) or len(value['tags']) > 4096
            or type(value['description_present']) is not bool
            or not any(IDENTITY.fullmatch(t) for t in _tags(value['tags']))):
        raise ValueError('Invalid CRANGE ownership metadata fields.')
    return body, value


def display_tags(logical_tags):
    tags = _tags(logical_tags)
    markers = {t for t in tags if IDENTITY.fullmatch(t)}
    return ';'.join(sorted((tags - markers) | {category(t) for t in markers}))


def automation_config(config):
    if not isinstance(config, dict):
        return config
    body, record = _record(config.get('description', ''))
    if record is None:
        return config
    # A scenario clone may inherit its template's notes while receiving its own
    # project tags. Those notes must never grant it the template's identity.
    if _tags(config.get('tags')) != _tags(display_tags(record['tags'])):
        return config
    result = copy.deepcopy(config)
    result['tags'] = record['tags']
    if record['description_present']:
        result['description'] = body
    else:
        result.pop('description', None)
    return result


def category_updates(fields, previous=None):
    """Translate an ownership-bearing tag update without changing its meaning."""
    previous = automation_config(previous or {})
    result = copy.deepcopy(fields)
    logical = result.get('tags', previous.get('tags', ''))
    if not any(IDENTITY.fullmatch(t) for t in _tags(logical)):
        return result
    if 'tags' not in result and 'description' not in result:
        return result
    description_present = 'description' in result or 'description' in previous
    body, _ = _record(result.get('description', previous.get('description', '')))
    record = {'tags': logical, 'description_present': description_present}
    result['tags'] = display_tags(logical)
    result['description'] = body + PREFIX + json.dumps(record, sort_keys=True, separators=(',', ':')) + SUFFIX
    return result


CONFIG_PATH = re.compile(r'/nodes/proxmox[1-6]/(?:qemu|lxc)/[0-9]+/config\Z')
CREATE_PATH = re.compile(r'/nodes/proxmox[1-6]/(?:qemu|lxc)\Z')


def needs_previous(fields):
    # A description-only edit must retain any recorded ownership. A complete
    # tag/description replacement, or an ordinary scenario tag update, doesn't
    # need another API round trip.
    if 'tags' not in fields:
        return 'description' in fields
    return ('description' not in fields
            and any(IDENTITY.fullmatch(t) for t in _tags(fields['tags'])))


def automation_response(path, value):
    return automation_config(value) if CONFIG_PATH.fullmatch(path) else value
