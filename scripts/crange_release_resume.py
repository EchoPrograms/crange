"""Prove selected applied release identities before configuration-only recovery."""
import re


def validate_applied(path, cfg, teams, api=None):
    from crange import CrangeError, NODES, ProxmoxAPI, output_json
    from crange_rollouts import endpoint, verify_owned
    api = api or ProxmoxAPI()
    outputs = output_json(path)
    resources = api.request('GET', '/cluster/resources', {'type': 'vm'})
    if not isinstance(resources, list):
        raise CrangeError('Live release inventory is unavailable; no configuration started.')
    project = int(cfg['project_id'])

    def check_guest(actual, frozen, kind, name, bridge):
        if not frozen or endpoint(actual) != frozen or type(actual.get('id')) is not int:
            raise CrangeError('Applied release endpoint changed; review infrastructure before configuration.')
        matches = [row for row in resources if str(row.get('vmid')) == str(actual['id'])]
        if (len(matches) != 1 or matches[0].get('type') != kind
                or matches[0].get('node') not in NODES or matches[0].get('pool') != 'crange-iac'
                or matches[0].get('node') != frozen.get('node_name')
                or matches[0].get('status') != 'running'):
            raise CrangeError('Release guest is missing, stopped or outside its recorded ownership; no configuration started.')
        node = matches[0]['node']
        config = api.vm(node, actual['id']) if kind == 'qemu' else api.container(node, actual['id'])
        if (config.get('name', config.get('hostname')) != name or int(config.get('template', 0))
                or not any(re.search(r'(?:^|,)bridge=' + re.escape(bridge) + r'(?:,|$)', str(value))
                           for key, value in config.items() if re.fullmatch(r'net\d+', key))):
            raise CrangeError('Live release guest identity or network differs; no configuration started.')

    for team in map(str, teams):
        record = cfg.get('release_rollouts', {}).get(team)
        if not record or not record.get('applied') or record['applied'] != record.get('desired'):
            raise CrangeError(f'Team {team} has no matching applied release to configure.')
        try:
            verify_owned(cfg, team, record['desired'])
        except ValueError as exc:
            raise CrangeError(str(exc)) from exc
        saved = record.get('applied_endpoints', {})
        for field, kind, prefix in (('scenario_boxes', 'qemu', 'vm:'),
                                    ('scenario_containers', 'lxc', 'container:')):
            for key, box in record['desired']['resources'][field].items():
                live = outputs.get(field, {}).get('value', {}).get(key, {})
                network_id = int(box.get('network_id', team))
                address = f'10.{project}.{network_id}.{box["ip_last_octet"]}'
                if live.get('address') != address:
                    raise CrangeError('Applied release address differs from its declaration.')
                bridge = f'p{project:03d}c{network_id:03d}' if box.get('network_key') else f'p{project:03d}t{int(team):03d}'
                check_guest(live, saved.get(prefix + key), kind, bridge + '-' + box['name'], bridge)
        for key, network in record['desired']['resources']['custom_networks'].items():
            live = outputs.get('routers', {}).get('value', {}).get('custom_networks', {}).get(key, {})
            if (str(live.get('owner_team_id')) != team
                    or live.get('lan') != f'10.{project}.{network["network_id"]}.0/24'
                    or live.get('upstream_address') != f'10.{project}.{team}.{network["uplink_last_octet"]}'):
                raise CrangeError('Applied release router topology differs from its declaration.')
            check_guest(live, saved.get('network:' + key), 'lxc',
                        f'p{project:03d}-custom-{key}-router', f'p{project:03d}c{network["network_id"]:03d}')
