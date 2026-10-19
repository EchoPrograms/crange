"""Non-blocking, read-only template inventory for the administrator interface.

The broker authorizes each caller before returning this shared observation.
Workers always revalidate live identity/media; this cache grants no operations.
"""
import copy
import json
import threading
import time

_LOCK = threading.Lock()
_CACHE = {'value': None, 'updated': 0.0, 'attempted': 0.0, 'loading': False, 'error': None}
TTL = 30


def _initial():
    from crange_templates import BUILDS, NODES, library_status
    from crange_platform_bases import BASES
    builds = []
    for path in sorted(BUILDS.glob('*.json')):
        try:
            record = json.loads(path.read_text())
            if not isinstance(record, dict) or not isinstance(record.get('definition'), dict):
                continue
            builds.append({**record, 'console_available': False, 'console_node': None,
                           'error': 'Checking installer availability…'})
        except (OSError, ValueError):
            continue
    return {'nodes': [{'name': n, 'isos': [], 'container_archives': [], 'unavailable': True} for n in sorted(NODES)],
            'builds': builds, 'media': [], 'library': library_status(), 'logical_images': None,
            'maintained_bases': [{'key': k, 'name': v['display_name'], 'kind': v['kind']} for k, v in BASES.items()],
            'cache_recovery': [], 'restored_caches': [], 'library_storage': 'crange-library',
            'mirror_storages': ['media-mirror1', 'media-mirror4']}


def _refresh():
    from crange_templates import options
    from crange import ProxmoxAPI
    try:
        api = ProxmoxAPI()
        api.request_timeout = 3
        api.read_attempts = 1
        value = options(api)
        with _LOCK:
            _CACHE.update(value=value, updated=time.time(), error=None)
    except Exception:
        with _LOCK:
            _CACHE['error'] = 'Live template inventory could not be refreshed. Use Refresh templates to retry.'
    finally:
        with _LOCK:
            _CACHE.update(loading=False, attempted=time.monotonic())


def snapshot(*, force=False):
    with _LOCK:
        if not _CACHE['loading'] and (force or time.monotonic() - _CACHE['attempted'] >= TTL):
            _CACHE['loading'] = True
            threading.Thread(target=_refresh, name='template-inventory', daemon=True).start()
        value = copy.deepcopy(_CACHE['value']) if _CACHE['value'] is not None else _initial()
        value.update(inventory_loading=_CACHE['loading'], inventory_updated_at=_CACHE['updated'] or None,
                     inventory_error=_CACHE['error'])
        return value
