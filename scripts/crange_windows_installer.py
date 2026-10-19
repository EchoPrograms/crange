"""Answer the DVD boot prompt only for a fresh, owned Windows installer."""
import time
import copy
from datetime import datetime, timezone

import crange_templates as templates
import crange_windows_base as base
from crange_windows_seed import selected_node_upload_endpoint
from crange import ProxmoxAPI


def _scope(record, api):
    identity, _ = base._identity(record, api)
    if record.get('status') != 'installing' or identity['status'] != 'running':
        raise ValueError('Start only a running, unfinished Windows installer build.')
    current = api.request('GET', f'/nodes/{identity["node"]}/qemu/{identity["vmid"]}/status/current')
    if type(current.get('diskwrite')) not in (int, float) or current['diskwrite'] != 0:
        raise ValueError('Installation has written to disk; do not restart this installer.')
    if (type(current.get('mem')) not in (int, float) or not 0 <= current['mem'] < 256 * 1024**2
            or type(current.get('cpu')) not in (int, float) or not 0 <= current['cpu'] < .05):
        raise ValueError('The guest is loading its operating system; wait instead of restarting it.')
    created = datetime.fromisoformat(record['created']).timestamp()
    tasks = api.request('GET', f'/nodes/{identity["node"]}/tasks',
                        {'vmid': identity['vmid'], 'since': int(created) - 5, 'limit': 100})
    starts = [t for t in tasks if t.get('type') == 'qmstart' and t.get('status') == 'OK']
    if len(starts) != 1 or abs(time.time() - current['uptime'] - starts[0]['starttime']) > 30:
        raise ValueError('The original untouched power session cannot be verified; open the console.')
    return identity


def eligibility(record, api):
    try:
        claim = record.get('installer_boot', {})
        if claim and not claim.get('reset_response'):
            raise ValueError('DVD boot handling has already been requested; preserve its recorded task.')
        if claim.get('state') == 'boot-requested' and len(record.get('installer_boot_history', [])) >= 2:
            raise ValueError('Inspect the console; the bounded boot attempts are exhausted.')
        _scope(record, api)
        return {'can_start': True, 'resume': bool(claim) and claim.get('state') != 'boot-requested',
                'reason': 'Resume the recorded boot task.' if claim else 'Restart this untouched installer and answer the DVD boot prompt.'}
    except Exception as exc:
        return {'can_start': False, 'reason': str(exc)}


def start(build_id, api=None):
    api = api or ProxmoxAPI()
    record = templates.load(templates.identifier(build_id))
    identity = _scope(record, api)
    scope = {key: identity[key] for key in ('node', 'vmid', 'config_sha256')}
    claim = record.get('installer_boot')
    if claim and claim.get('scope') != scope:
        raise ValueError('Installer identity changed; do not repeat the reset.')
    if claim and not claim.get('reset_response'):
        raise ValueError('Reset submission is uncertain; retain the existing claim instead of resubmitting.')
    if claim and claim.get('state') == 'boot-requested':
        history = record.setdefault('installer_boot_history', [])
        if len(history) >= 2:
            raise ValueError('Repeated boot attempts did not start installation; inspect the console instead.')
        history.append(dict(claim))
        claim = None
    fresh = not claim
    claim = claim or {'state': 'reset-pending', 'scope': scope, 'requested_at': datetime.now(timezone.utc).isoformat()}
    record['installer_boot'] = claim
    templates.save(record)
    direct = copy.copy(api)
    direct.endpoint = selected_node_upload_endpoint(api, identity['node'])
    api = direct
    path = f'/nodes/{identity["node"]}/qemu/{identity["vmid"]}'
    result = claim.get('reset_response')
    if fresh:
        result = api.request('POST', path + '/status/reset', {})
        claim['reset_response'] = result
        templates.save(record)
    if not isinstance(result, str) or not result.startswith('UPID:'):
        raise ValueError('Reset outcome is uncertain; preserve this build and its task record.')
    api.task(identity['node'], result, timeout=120)
    claim['state'] = 'answering-prompt'
    templates.save(record)
    for count in range(claim.get('keys_sent', 0), 8):
        api.request('PUT', path + '/sendkey', {'key': 'ret'})
        claim['keys_sent'] = count + 1
        templates.save(record)
        time.sleep(1)
    claim['state'] = 'boot-requested'
    templates.save(record)
    print('DVD boot keys sent to this untouched build; installation readiness remains unverified.')
    return {'build_id': build_id, 'vmid': identity['vmid'], 'state': claim['state']}
