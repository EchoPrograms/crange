"""Sanitized applied-definition checkpoints for the project UI change markers."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

_SCHEMA = 1
_FIELDS = {
    'scenario_box': ('team_id', 'template', 'node_name', 'cores', 'memory_mb', 'disk_gb',
                     'health_checks', 'guac_services', 'guac_protocol', 'ip_last_octet',
                     'network_id', 'network_key', 'display_name', 'release_scope'),
    'scenario_container': ('team_id', 'template', 'node_name', 'cores', 'memory_mb', 'disk_gb',
                           'health_checks', 'guac_services', 'guac_protocol', 'ip_last_octet',
                           'network_id', 'network_key', 'display_name', 'release_scope'),
    'custom_network': ('owner_team_id', 'network_id', 'uplink_last_octet', 'node_name',
                       'cores', 'memory_mb'),
}


def _safe_definition(kind: str, value: dict) -> dict:
    return {field: value[field] for field in _FIELDS[kind] if field in value}


def staging_definitions(config: dict) -> dict:
    """Return only editable Team 0 objects, excluding secret and runtime fields."""
    result = {kind: {} for kind in _FIELDS}
    for name, value in (config.get('scenario_boxes') or {}).items():
        if isinstance(value, dict) and str(value.get('team_id')) == '254':
            result['scenario_box'][name] = _safe_definition('scenario_box', value)
    for name, value in (config.get('scenario_containers') or {}).items():
        if isinstance(value, dict) and str(value.get('team_id')) == '254':
            result['scenario_container'][name] = _safe_definition('scenario_container', value)
    for name, value in (config.get('custom_networks') or {}).items():
        if isinstance(value, dict) and str(value.get('owner_team_id')) == '254':
            result['custom_network'][name] = _safe_definition('custom_network', value)
    return result


def load_applied_definitions(project_path: str | Path) -> dict | None:
    path = Path(project_path) / '.crange-applied-definitions.json'
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get('schema_version') != _SCHEMA:
        return None
    definitions = value.get('staging')
    if not isinstance(definitions, dict):
        return None
    return definitions


def record_applied_definitions(project_path: str | Path, config: dict, *, resource_keys=None, scope: str = '254') -> None:
    """Merge definitions actually covered by a successful reviewed staging apply."""
    if str(scope) != '254':
        raise ValueError('Only the reviewed Team 0 staging scope can be checkpointed.')
    path = Path(project_path) / '.crange-applied-definitions.json'
    current = staging_definitions(config)
    try:
        previous = json.loads(path.read_text())
        staging = previous.get('staging') if previous.get('schema_version') == _SCHEMA else None
    except (OSError, ValueError, AttributeError):
        staging = None
    first_checkpoint = not isinstance(staging, dict)
    staging = staging if isinstance(staging, dict) else {kind: {} for kind in _FIELDS}
    unknown = staging.get('unknown') if isinstance(staging.get('unknown'), dict) else {kind: [] for kind in _FIELDS}
    if first_checkpoint:
        # Legacy resources have no reliable applied-definition baseline. Keep
        # them explicitly unknown until a reviewed apply touches that identity.
        unknown = {kind: list(rows) for kind, rows in current.items()}
    keys = set(resource_keys or ())
    if resource_keys is None:
        keys = ({'vm:' + name for name in current['scenario_box']} |
                {'container:' + name for name in current['scenario_container']} |
                {'router:custom:' + name for name in current['custom_network']})
    key_map = {'scenario_box': 'vm:', 'scenario_container': 'container:', 'custom_network': 'router:custom:'}
    if resource_keys is None:
        keys.update(prefix + name for kind, prefix in key_map.items() for name in staging.get(kind, {}))
    for kind, prefix in key_map.items():
        for name, definition in current[kind].items():
            if prefix + name in keys:
                staging.setdefault(kind, {})[name] = definition
                unknown[kind] = [item for item in unknown.get(kind, []) if item != name]
        # A reviewed deletion removes its prior snapshot, but is not copied
        # back as current desired state.
        for name in list(staging.setdefault(kind, {})):
            if name not in current[kind] and prefix + name in keys:
                staging[kind].pop(name, None)
                unknown[kind] = [item for item in unknown.get(kind, []) if item != name]
    staging['unknown'] = unknown
    payload = {'schema_version': _SCHEMA, 'staging': staging}
    fd, temporary = tempfile.mkstemp(prefix='.crange-applied-definitions.', dir=Path(project_path))
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(json.dumps(payload, sort_keys=True, separators=(',', ':')) + '\n')
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def compare_staging_definitions(desired: dict, applied: dict | None) -> dict:
    """Classify exact desired/apply comparison, leaving uncheckpointed state unknown."""
    desired = staging_definitions(desired)
    if not isinstance(applied, dict):
        return {kind: {name: 'unknown' for name in rows} for kind, rows in desired.items()}
    result = {}
    for kind in _FIELDS:
        wanted, previous = desired[kind], applied.get(kind, {})
        if not isinstance(previous, dict):
            previous = {}
        unknown = set((applied.get('unknown') or {}).get(kind, []))
        result[kind] = {name: ('unchanged' if definition == previous[name] else 'edited') if name in previous else
                        'unknown' if name in unknown else 'added'
                        for name, definition in wanted.items()}
        result[kind].update({name: 'deleted' for name in previous if name not in wanted})
    return result
