"""Bind MHL service replay to the exact isolated disposable release cohort."""
import ipaddress

ROLES={'boxes':{'mhl-dc01':10,'mhl-file01':20},
       'containers':{'mhl-db01':30,'mhl-app01':40,'mhl-ci01':50,'mhl-bkp01':60}}


def select(cfg, release, record):
    if (cfg.get('project_id') != 100 or release.get('project_id') != 100 or
            not release.get('release_id') or record.get('release_id') != release['release_id']):
        raise ValueError('MHL test cohort has a different project or immutable release identity.')
    result={'release_id':release['release_id'],'keys':{}}
    networks=set()
    for group,roles in ROLES.items():
        sources={row['name']:row for row in release['scenario'].get(group,[])}
        if set(sources)!=set(roles) or len(record.get(group,{}))!=len(roles):
            raise ValueError('MHL test replay requires the complete six-server release cohort.')
        field='scenario_boxes' if group=='boxes' else 'scenario_containers'
        for key,info in record[group].items():
            box=cfg.get(field,{}).get(key,{})
            name=box.get('release_source_name');source=sources.get(name)
            if not source or name in result['keys']:
                raise ValueError('Disposable test source names are missing, foreign or duplicated.')
            pinned=release['scenario'].get('templates',{}).get(source['template'],{})
            net_key=box.get('network_key');net=cfg.get('custom_networks',{}).get(net_key,{})
            if (box.get('release_scope')!='test' or box.get('release_id')!=release['release_id'] or
                    str(box.get('team_id'))!='254' or box.get('template')!=source['template'] or
                    box.get('image')!=pinned.get('image_id',source['template']) or
                    box.get('ip_last_octet')!=roles[name] or source.get('ip_last_octet')!=roles[name] or
                    net_key not in record.get('networks',{}) or net!=record['networks'][net_key] or
                    net.get('release_scope')!='test' or net.get('release_id')!=release['release_id'] or
                    str(net.get('owner_team_id'))!='254' or box.get('network_id')!=net.get('network_id')):
                raise ValueError('Disposable test ownership, image or isolated network differs from its release record.')
            number=net['network_id']
            if type(number) is not int or not 1<=number<=253:
                raise ValueError('Test network must be a dedicated custom LAN.')
            expected=f'10.100.{number}.{roles[name]}'
            if info.get('address')!=expected or ipaddress.ip_address(expected).is_unspecified:
                raise ValueError('Disposable test address changed.')
            networks.add((net_key,number));result['keys'][name]=key
    if len(networks)!=1:
        raise ValueError('This MHL service profile requires one isolated six-server test LAN.')
    result['network_key'],result['network_id']=networks.pop()
    return result
