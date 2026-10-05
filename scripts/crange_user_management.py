"""Project user preferences and controlled account/jump-box enrollment."""
import copy
import json
import re
import sqlite3
import string
import uuid
from types import SimpleNamespace
from urllib.parse import quote

from crange_guac_users import ProjectUsers, valid_username
from crange_guac_ticket import request_broker, request_sealed_password

DEFAULT_FORMAT = 'p{project-number:03d}-t{team-number}u{user-number}'
DEFAULT_BOXES = [{'network': '', 'node': 'auto', 'preferred_node': 'proxmox4', 'cores': 2, 'memory': 2048}]


def expand_format(pattern, project, team, number):
    if not isinstance(pattern, str) or not 1 <= len(pattern) <= 256:
        raise ValueError('A format must contain 1–256 characters.')
    values = {'project-number': int(project), 'project-id': int(project),
              'team-number': 0 if int(team) == 254 else int(team), 'user-number': int(number)}
    result = []
    try:
        for text, field, spec, conversion in string.Formatter().parse(pattern):
            result.append(text)
            if field is not None:
                if field not in values or conversion or not re.fullmatch(r'(0?[1-8])?d?', spec):
                    raise ValueError('Use {user-number}, {team-number}, {project-number}, optionally :02d for padding.')
                result.append(format(values[field], spec))
    except (ValueError, KeyError) as exc:
        raise ValueError('Invalid format. Use {user-number}, {team-number}, {project-number} or {user-number:02d}.') from exc
    return ''.join(result)


def preferences(auth, project, private=False):
    with auth.connect() as db:
        row = db.execute('SELECT * FROM guacamole_project_settings WHERE project=?', (project,)).fetchone()
    value = dict(row) if row else {'display_name': '', 'username_template': DEFAULT_FORMAT,
                                  'sealed_password_template': None, 'jumpboxes': json.dumps(DEFAULT_BOXES)}
    value['jumpboxes'] = json.loads(value['jumpboxes'])
    value['password_template_set'] = bool(value.get('sealed_password_template'))
    if not private:
        value.pop('sealed_password_template', None)
    return value


def validate_boxes(cfg, boxes):
    from crange_jobs import command
    if not isinstance(boxes, list) or len(boxes) > 10:
        raise ValueError('Choose up to 10 jump boxes per user.')
    result = []
    for box in boxes:
        if not isinstance(box, dict):
            raise ValueError('Invalid default jump box.')
        if set(box) - set(DEFAULT_BOXES[0]):
            raise ValueError('Unexpected default jump-box settings.')
        value = {key: box.get(key, default) for key, default in DEFAULT_BOXES[0].items()}
        if 'preferred_node' not in box and value['node'] != 'auto':
            value['preferred_node'] = ''
        command('box-add', int(cfg['project_id']), {'name': 'default-j01', 'team': 0, **value})
        if value['network'] and value['network'] not in cfg.get('custom_networks', {}):
            raise ValueError('Choose a declared custom network.')
        result.append(value)
    return result


def save_preferences(auth, session, project, data, cfg):
    current = preferences(auth, project, private=True)
    display = data.get('display_name', current['display_name'])
    if not isinstance(display, str) or len(display) > 80 or any(ord(c) < 32 for c in display):
        raise ValueError('Project display name must be at most 80 characters on one line.')
    pattern = data.get('username_template', current['username_template'])
    valid_username(expand_format(pattern, project, 1, 1))
    boxes = validate_boxes(cfg, data.get('jumpboxes', current['jumpboxes']))
    sealed = current.get('sealed_password_template')
    if data.get('password_template'):
        password = data['password_template']
        if len(expand_format(password, project, 1, 1)) < 6:
            raise ValueError('The generated password must have at least 6 characters.')
        sealed = request_sealed_password(session, project, 'defaults', password)
    with auth.connect() as db:
        db.execute('INSERT INTO guacamole_project_settings VALUES (?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET '
                   'display_name=excluded.display_name,username_template=excluded.username_template,'
                   'sealed_password_template=excluded.sealed_password_template,jumpboxes=excluded.jumpboxes',
                   (project, display.strip(), pattern, sealed, json.dumps(boxes)))
    return preferences(auth, project)


def next_number(auth, project, team):
    numbers = [r['user_number'] or 0 for r in ProjectUsers(auth).list(project) if r['home_team'] == team]
    return max(numbers, default=0) + 1


def plan_boxes(cfg, users, specs):
    """Build all declarations in memory and validate quota before persisting."""
    from crange import mutate_box, validate_project_quota, project_dir
    from crange_jobs import command
    planned = copy.deepcopy(cfg)
    result = []
    for user in users:
        team = int(user['home_team'])
        for slot, spec in enumerate(specs, 1):
            network = planned.get('custom_networks', {}).get(spec['network']) if spec['network'] else None
            if network and str(network['owner_team_id']) != str(254 if team == 0 else team):
                raise ValueError('Default jump-box network belongs to another team. Choose this team’s subnet.')
            network_id = int(network['network_id']) if network else (254 if team == 0 else team)
            occupied = {10 + int(b['jump_number']) for b in planned.get('jump_boxes', {}).values()
                        if int(b.get('network_id', b['team_id'])) == network_id}
            occupied |= {int(b['ip_last_octet']) for field in ('scenario_boxes', 'scenario_containers')
                         for b in planned.get(field, {}).values() if int(b.get('network_id', b['team_id'])) == network_id}
            occupied |= {int(n['uplink_last_octet']) for n in planned.get('custom_networks', {}).values()
                         if int(n['owner_team_id']) == network_id}
            number = next((n for n in range(1, 91) if 10 + n not in occupied), None)
            if number is None:
                raise ValueError('No jump-box address remains in this subnet.')
            key = f't{team:03d}u{user["user_number"]}-{slot}-j{number:02d}'
            if key in planned.get('jump_boxes', {}):
                raise ValueError('This user’s jump-box setup already exists.')
            box = {'name': key, 'team': team, **spec}
            command('box-add', int(cfg['project_id']), box)
            mutate_box(SimpleNamespace(project=str(cfg['project_id']), **box, full_clone=None), True, cfg=planned, save=False)
            result.append(box)
            user['connections'] = sorted(set(user.get('connections', [])) | {'jumpbox:' + key})
    validate_project_quota(project_dir(str(cfg['project_id'])), planned)
    return result


def available_logins(auth, session, project, names, ignore=None):
    names = [valid_username(n) for n in names]
    if len({n.lower() for n in names}) != len(names):
        raise ValueError('The username format generates duplicate users.')
    with auth.connect() as db:
        for name in names:
            row = db.execute('SELECT project,name FROM guacamole_users WHERE login=? COLLATE NOCASE', (name,)).fetchone()
            if row and (int(row['project']), row['name']) != (project, ignore):
                raise ValueError(f'Username {name} already exists.')
            if db.execute('SELECT 1 FROM users WHERE name=? COLLATE NOCASE', (name,)).fetchone():
                raise ValueError(f'Username {name} belongs to a dashboard account.')
    request_broker({'operation': 'available-users', 'project': project, 'session': session, 'names': names, 'ignore': ignore})


def manage_users(auth, jobs, actor, session, project, data, cfg):
    records = ProjectUsers(auth)
    action = data.get('action')
    if action == 'delete-many':
        names = data.get('usernames')
        if not isinstance(names, list) or not 1 <= len(names) <= 100:
            raise ValueError('Select 1–100 users.')
        with auth.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for name in names:
                if not db.execute('SELECT 1 FROM guacamole_users WHERE project=? AND name=?', (project, valid_username(name))).fetchone():
                    raise ValueError('A selected user is not in this project.')
            db.executemany('DELETE FROM guacamole_users WHERE project=? AND name=?', [(project,n) for n in names])
    elif action == 'delete':
        records.delete(project, data.get('username'))
    elif action == 'update':
        existing = next((r for r in records.list(project) if r['username'] == data.get('username')), None)
        if not existing:
            raise ValueError('Project user not found.')
        login = valid_username(data.get('new_username', existing['login']))
        if login != existing['login']:
            available_logins(auth, session, project, [login], existing['username'])
            if not data.get('password'):
                raise ValueError('Set a password for the new username when renaming an account.')
        archive=None
        if login != existing['login']:
            archive=request_broker({'operation':'user-stats','project':project,'session':session,'username':existing['username']}).get('login_history',[])
        teams, connections = validate_grants(cfg, data.get('teams', existing['teams']), data.get('connections', existing['connections']))
        enabled, reset = data.get('enabled', existing['enabled']), data.get('allow_password_reset', existing['allow_password_reset'])
        if not isinstance(enabled, bool) or not isinstance(reset, bool):
            raise ValueError('Invalid account setting.')
        sealed = request_sealed_password(session, project, existing['username'], data['password']) if data.get('password') else None
        records.update(project, existing['username'], teams=teams, connections=connections, enabled=enabled,
                       sealed=sealed, login=login, allow_password_reset=reset)
        if archive is not None:
            with auth.connect() as db:
                db.execute('UPDATE guacamole_users SET history_archive=? WHERE project=? AND name=?', (json.dumps(archive[:100]),project,existing['username']))
    elif action in ('create', 'bulk-create', 'jumpbox', 'add-jumpboxes'):
        team = data.get('box',{}).get('team',0) if action=='jumpbox' else data.get('team',0)
        if isinstance(team, bool) or not isinstance(team, int) or team not in [0,*map(int,cfg.get('team_ids',[]))]:
            raise ValueError('Choose a configured team or staging.')
        prefs = preferences(auth, project)
        count = data.get('count', 1) if action == 'bulk-create' else 1
        start = data.get('start_number', next_number(auth, project, team))
        if isinstance(count,bool) or not isinstance(count,int) or not 1 <= count <= 100 or isinstance(start,bool) or not isinstance(start,int) or not 1 <= start <= 999999:
            raise ValueError('Choose 1–100 users and a positive starting user number.')
        pattern = data.get('username_template') or prefs['username_template']
        names = [expand_format(pattern,project,team,n) for n in range(start,start+count)]
        if action in ('create','jumpbox') and data.get('username'):
            names=[data['username']]
        existing = data.get('existing_user') if action in ('jumpbox','add-jumpboxes') else None
        if action=='add-jumpboxes':
            row=next((r for r in records.list(project) if r['username']==existing),None)
            if not row or row['home_team'] is None:raise ValueError('This user has no default team. Assign jump boxes explicitly.')
            team=row['home_team'];start=row['user_number'] or start
        create = not existing
        if create:
            available_logins(auth, session, project, names)
        else:
            row=next((r for r in records.list(project) if r['username']==existing),None)
            if not row:raise ValueError('Choose a user in this project.')
        teams, connections = validate_grants(cfg, data.get('teams', []), data.get('connections', []))
        users=[{'username':'u'+uuid.uuid4().hex,'login':valid_username(name),'user_number':start+i,'home_team':team,
                'teams':teams,'connections':list(connections)} for i,name in enumerate(names)]
        boxes=[]
        if action=='jumpbox':
            from crange_jobs import command
            from crange import mutate_box
            box=data.get('box');command('box-add',project,box)
            planned=copy.deepcopy(cfg)
            mutate_box(SimpleNamespace(project=str(project),**box,full_clone=None),True,cfg=planned,save=False)
            from crange import validate_project_quota,project_dir
            validate_project_quota(project_dir(str(project)),planned)
            boxes=[box];users[0]['connections'].append('jumpbox:'+box['name'])
        elif data.get('create_jumpboxes') or action=='add-jumpboxes':
            boxes=plan_boxes(cfg,users,prefs['jumpboxes'])
        if create:
            protected=[]
            for offset in range(0,len(users),20):
                minimal=[{key:item[key] for key in ('username','login','user_number','home_team')} for item in users[offset:offset+20]]
                protected.extend(request_broker({'operation':'prepare-users','project':project,'session':session,'users':minimal,
                                      'password_template':data.get('password_template'), 'password':data.get('password')})['users'])
        with auth.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if create:
                for item, sealed in zip(users,protected):
                    # The unique index is the final concurrency check for the whole batch.
                    if db.execute('SELECT 1 FROM users WHERE name=? COLLATE NOCASE',(item['login'],)).fetchone():
                        raise ValueError('Username belongs to a dashboard account.')
                    db.execute('INSERT INTO guacamole_users(project,name,login,enabled,teams,connections,password_revision,sealed_password,user_number,home_team) VALUES (?,?,?,1,?,?,?,?,?,?)',
                        (project,item['username'],item['login'],json.dumps(item['teams']),json.dumps(item['connections']),uuid.uuid4().hex,sealed['sealed_password'],item['user_number'],team))
            else:
                assigned=sorted(set(row['connections'])|set(users[0]['connections']))
                db.execute('UPDATE guacamole_users SET connections=? WHERE project=? AND name=?',(json.dumps(assigned),project,existing))
            request_id=uuid.uuid4().hex
            db.execute('INSERT INTO guacamole_enrollments(id,project,boxes) VALUES (?,?,?)',(request_id,project,json.dumps(boxes)))
        job=jobs.submit(actor,project,'guac-enroll',{'request_id':request_id})
        auth.audit(actor,'guac-users-'+action,str(project),{'count':len(users),'users':[u['login'] for u in users],'user_keys':[u['username'] for u in users] if create else [existing],'boxes':[b['name'] for b in boxes],'request_id':request_id})
        return {'job_id':job,'users':records.list(project)}
    else:
        raise ValueError('Invalid project user action.')
    auth.audit(actor,'guac-users-'+action,str(project),{'username':data.get('username'),'usernames':data.get('usernames'),'new_username':data.get('new_username'),'password_changed':bool(data.get('password')),'enabled':data.get('enabled'),'allow_password_reset':data.get('allow_password_reset')})
    job=jobs.submit(actor,project,'guac-users',{})
    return {'job_id':job,'users':records.list(project)}


def validate_grants(cfg,teams,connections):
    valid_teams={str(t) for t in [254,*cfg.get('team_ids',[])]}
    if not isinstance(teams,list) or any(isinstance(t,bool) or str(t) not in valid_teams for t in teams):
        raise ValueError('Choose configured project teams only.')
    allowed={f'{kind}:{key}' for kind,field in [('jumpbox','jump_boxes'),('vm','scenario_boxes'),('container','scenario_containers')] for key in cfg.get(field,{})}
    if not isinstance(connections,list) or any(not isinstance(k,str) or k not in allowed for k in connections):
        raise ValueError('Choose declared project connections only.')
    return sorted(set(map(str,teams))),sorted(set(connections))


def user_stats(auth,project,name,client,repo_root):
    record=next((r for r in ProjectUsers(auth).list(project) if r['username']==name),None)
    if not record:raise ValueError('Project user not found.')
    client.login();base=f'/api/session/data/{client.data_source}'
    login=quote(record['login'],safe='')
    info=client._request('GET',base+'/users/'+login) or {}
    history=client._request('GET',base+'/users/'+login+'/history?limit=50') or []
    if isinstance(history,dict):history=history.get('records',[])
    sidecar=__import__('pathlib').Path(repo_root)/'guacamole/access'/f'project-{project:03d}.applied.json'
    connections=json.loads(sidecar.read_text()).get('connections',[]) if sidecar.exists() else []
    allowed={str(x['identifier']):x['name'] for x in connections}
    active=client._request('GET',base+'/activeConnections') or {}
    sessions=[{'connection':allowed[str(x['connectionIdentifier'])],'start':x.get('startDate'),'remote_host':x.get('remoteHost')}
              for x in active.values() if x.get('username')==record['login'] and str(x.get('connectionIdentifier')) in allowed]
    with auth.connect() as db:
        saved=db.execute('SELECT history_archive FROM guacamole_users WHERE project=? AND name=?',(project,name)).fetchone()
    logins=[{'start':x.get('startDate'),'end':x.get('endDate'),'remote_host':x.get('remoteHost')} for x in history[:50]] + json.loads(saved['history_archive'])
    logins=sorted(logins,key=lambda x:x.get('start') or 0,reverse=True)[:100]
    return {'last_login':info.get('lastActive') or (logins[0]['start'] if logins else None),'login_history':logins,
            'active_connections':sessions,'failed_login_attempts':None,'attempts_note':'Guacamole exposes successful login history; failed login attempts are not available through its REST API.'}
