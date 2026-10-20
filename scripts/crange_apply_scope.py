"""Bind capacity and completion checks to the guests in the reviewed TF plan."""
import copy
import hashlib
import json
import re

from crange_placement import PlacementError


def guest_key(address):
    """Recognize only the fixed guest modules owned by project Terraform."""
    direct = re.fullmatch(
        r'module\.(project_router|staging_router)\.proxmox_virtual_environment_container\.router',
        address,
    )
    if direct:
        return 'router:' + direct[1].removesuffix('_router')
    indexed = re.fullmatch(
        r'module\.(team_router|custom_router|jumpbox|scenario_box|scenario_container)'
        r'\["([^"\\]+)"\]\.proxmox_virtual_environment_(vm|container)\.(router|jump|scenario)',
        address,
    )
    if not indexed:
        return None
    module, name, kind, resource = indexed.groups()
    expected = {
        'team_router': ('container', 'router', 'router:team'),
        'custom_router': ('container', 'router', 'router:custom'),
        'jumpbox': ('vm', 'jump', 'jumpbox'),
        'scenario_box': ('vm', 'scenario', 'vm'),
        'scenario_container': ('container', 'scenario', 'container'),
    }
    wanted_kind, wanted_resource, prefix = expected[module]
    return prefix + ':' + name if (kind, resource) == (wanted_kind, wanted_resource) else None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def reviewed_distribution(prepared, detail, actual):
    """Exclude untouched guests without discarding or refreshing their state.

    Call after Terraform review and again against the saved binary plan before
    applying. A changed untouched identity/configuration requires a new review.
    Capacity reservations must all belong to a guest actually in this plan.
    """
    if not prepared:
        return prepared
    keys, removed = set(), {}
    for resource in detail.get('resource_changes', []):
        if resource.get('mode') == 'data':
            continue
        address = resource.get('address', '')
        key = guest_key(address)
        if not key:
            if resource.get('type') in ('proxmox_virtual_environment_vm', 'proxmox_virtual_environment_container'):
                raise PlacementError('Reviewed plan contains an unrecognized guest resource.')
            continue
        if key in keys:
            raise PlacementError('Reviewed plan contains a duplicate guest identity.')
        keys.add(key)
        change = resource.get('change', {})
        if change.get('actions') == ['delete']:
            before = change.get('before') or {}
            if not before.get('node_name') or not str(before.get('vm_id', '')).isdigit():
                raise PlacementError('Reviewed guest deletion lacks an exact prior identity.')
            removed[key] = {'node_name': before['node_name'], 'guest_id': int(before['vm_id'])}
    if any(request['resource_key'] not in keys for request in prepared['requests']):
        raise PlacementError('Capacity reservations include guests outside the reviewed plan; restage.')
    scope = {'schema_version': 1, 'guest_keys': sorted(keys), 'removed_guests': removed,
             'untouched_state': {key: _digest(value) for key, value in actual.items() if key not in keys}}
    previous = prepared.get('apply_scope')
    if previous is not None and previous != scope:
        raise PlacementError('Reviewed guest scope or untouched Terraform state changed; restage.')
    result = copy.deepcopy(prepared)
    result['apply_scope'] = scope
    result['placements'] = [row for row in result['placements'] if row['resource_key'] in keys]
    return result


def verify_untouched(prepared, actual):
    scope = prepared.get('apply_scope')
    if scope is None:  # Historical unscoped reconciliation still verifies all placements.
        return
    if scope.get('schema_version') != 1:
        raise PlacementError('Unsupported reviewed apply scope; reservations retained.')
    keys = set(scope['guest_keys'])
    observed = {key: _digest(value) for key, value in actual.items() if key not in keys}
    if observed != scope['untouched_state']:
        raise PlacementError('Untouched Terraform guest state changed; reservations retained for reconciliation.')
    if any(row['resource_key'] not in keys for row in prepared['placements']):
        raise PlacementError('Placement is outside the reviewed guest scope.')
