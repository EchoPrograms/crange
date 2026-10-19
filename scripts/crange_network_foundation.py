"""Reviewed creation of custom routers before exact LAN acceptance.

This phase never admits scenario guests or records network acceptance.
"""
import json


def pending(cfg, existing):
    return sorted(key for key, net in cfg.get('custom_networks', {}).items()
                  if str(net['owner_team_id']) == '254' and 'router:custom:' + key not in existing)


def targets(keys):
    result = {'proxmox_sdn_applier.apply', 'terraform_data.sdn_revision'}
    for key in keys:
        encoded = json.dumps(key)
        result.update({f'module.custom_router[{encoded}]', f'proxmox_sdn_vnet.custom[{encoded}]'})
    return sorted(result)


def validate_plan(detail, cfg, keys, prepared):
    """Reject every mutation outside the exact new subnet/router foundation."""
    if not keys or len(set(keys)) != len(keys):
        raise ValueError('Network foundation must name exact new custom LANs.')
    placements = {row['resource_key']: row['resolved'] for row in prepared['placements']}
    requests = {row['resource_key']: row for row in prepared['requests']}
    expected = {}
    for key in keys:
        net = cfg['custom_networks'][key]
        if str(net['owner_team_id']) != '254':
            raise ValueError('Staging network foundation cannot modify another team.')
        expected[f'module.custom_router[{json.dumps(key)}].proxmox_virtual_environment_container.router'] = ('router', key)
        expected[f'proxmox_sdn_vnet.custom[{json.dumps(key)}]'] = ('vnet', key)
    seen = set()
    for row in detail.get('resource_changes', []):
        address = row.get('address', '')
        change = row.get('change', {})
        actions = change.get('actions', [])
        if actions == ['no-op'] or row.get('mode') == 'data':
            continue
        after = change.get('after') or {}
        if address == 'terraform_data.sdn_revision' and actions in (['create'], ['update']):
            continue
        if address == 'proxmox_sdn_applier.apply':
            # This provider resource performs SDN reload, not guest deletion.
            if (actions not in (['create'], ['delete', 'create'], ['create', 'delete'])
                    or after.get('on_destroy') is not False or after.get('on_create') is not True
                    or (change.get('before') or {}).get('on_destroy', False) is not False):
                raise ValueError('Network foundation SDN applier is not safe.')
            continue
        if address not in expected or actions != ['create'] or change.get('before') is not None:
            raise ValueError('Network foundation would alter an existing or unrelated resource: ' + address)
        kind, key = expected[address]
        net = cfg['custom_networks'][key]
        pid, nid = int(cfg['project_id']), int(net['network_id'])
        prefix = f'p{pid:03d}'
        if kind == 'vnet':
            if (after.get('id') != f'{prefix}c{nid:03d}' or after.get('zone') != 'crange'
                    or after.get('tag') != 100000 + pid * 256 + nid):
                raise ValueError('Network foundation VNet differs from its declaration.')
        else:
            resolved = placements['router:custom:' + key]
            nics = {nic.get('name'): nic for nic in after.get('network_interface', [])}
            ips = (after.get('initialization') or [{}])[0].get('ip_config', [])
            addresses = [(item.get('ipv4') or [{}])[0] for item in ips]
            if (after.get('node_name') != resolved['node_name'] or after.get('pool_id') != 'crange-iac'
                    or not {prefix, f'{prefix}c{nid:03d}'} <= set(after.get('tags') or [])
                    or nics.get('eth0', {}).get('bridge') != prefix + 't254'
                    or nics.get('eth1', {}).get('bridge') != f'{prefix}c{nid:03d}'
                    or len(addresses) != 2
                    or addresses[0].get('address') != f'10.{pid}.254.{net["uplink_last_octet"]}/24'
                    or addresses[0].get('gateway') != f'10.{pid}.254.1'
                    or addresses[1].get('address') != f'10.{pid}.{nid}.1/24'):
                raise ValueError('Network foundation router differs from its reviewed topology.')
            disks, os = after.get('disk') or [], after.get('operating_system') or []
            if (len(disks) != 1 or disks[0].get('datastore_id') != resolved['datastore_id']
                    or disks[0].get('size') != requests['router:custom:' + key]['disk_gb']
                    or len(os) != 1 or os[0].get('template_file_id') != resolved.get('template_file_id')):
                raise ValueError('Network foundation router storage or source differs from review.')
        seen.add(address)
    if seen != set(expected):
        raise ValueError('Network foundation plan is missing declared routers or VNets.')
    return True
