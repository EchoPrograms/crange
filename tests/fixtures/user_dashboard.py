#!/usr/bin/env python3
"""Serve the real dashboard against synthetic, in-memory users only.

Run from any directory: python3 tests/fixtures/user_dashboard.py [port]
Binds localhost. No controller imports, credentials, outbound requests, or jobs.
Unknown endpoints and infrastructure job submissions fail closed.
"""
import copy
import json
import re
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

GUI = Path(__file__).resolve().parents[2] / 'gui'
PROJECTS = [
    dict(project_id=100, display_name='Meridian Harbor Logistics', team_ids=['1', '2', '3'],
         has_state=True, jump_boxes={}, scenario_boxes={
             'linux-01': dict(name='Linux workstation 1', team_id=1, ip_last_octet=11,
                              is_jumpbox=True, node_name='proxmox2', cores=4, memory_mb=8192,
                              template='approved-base', guac_services=['ssh']),
             'staging-app': dict(name='Staging application', team_id=254, ip_last_octet=30, node_name='proxmox2', cores=2, memory_mb=2048, template='approved-base', health_checks=[dict(type='tcp',port=80,timeout=5)]),
             'windows-01': dict(name='Windows workstation 1', team_id=2, ip_last_octet=12,
                                is_jumpbox=True, node_name='proxmox3', cores=4, memory_mb=8192,
                                template='windows', guac_services=['rdp'])}, scenario_containers={'dns':dict(name='DNS server', team_id=3, ip_last_octet=53, node_name='proxmox3', cores=1, memory_mb=512,template='debian-13',health_checks=[])},
         custom_networks={}, releases=[], release_rollouts={'1':{'desired':{'version':'0.1.5','release_id':'release-fixture-new'},'applied':{'version':'0.1.2','release_id':'release-fixture-old'},'health':{'status':'not-checked'}}}, staged_plans=[],
         vm_templates=['approved-base', 'windows'], container_templates=[],
         quota=dict(cores=256, memory_mb=524288), quota_usage=dict(cores=8, memory_mb=16384),
         user_defaults=dict(username_template='mhl-t{team-number}-u{user-number}',
                            password_template_set=True, jumpboxes=[dict(cores=4, memory=8192)])),
    dict(project_id=102, display_name='Small practice lab', team_ids=['1'], has_state=True,
         jump_boxes={}, scenario_boxes={}, scenario_containers={}, custom_networks={},
         quota=dict(cores=32, memory_mb=65536), user_defaults=dict(password_template_set=True))]
USERS = [dict(username=name, login=name, home_team=team, user_number=number,
              enabled=enabled, teams=[], connections=connections, allow_password_reset=False)
         for name, team, number, enabled, connections in [
             ('mhl-t1-u1', 1, 1, True, ['vm:linux-01']),
             ('mhl-t1-u2', 1, 2, False, []),
             ('mhl-t2-u1', 2, 1, True, ['vm:windows-01']),
             ('staging-reviewer-with-a-long-account-name', 254, 1, True, [])]]
ACCOUNTS = [dict(username='fixture-admin', admin=True, projects=[], roles={}, teams={}, enabled=True),
            dict(username='coordinator', admin=False, projects=[100, 102],
                 roles={'100': 'manager', '102': 'participant'}, teams={}, enabled=True),
            dict(username='participant', admin=False, projects=[100], roles={'100': 'participant'},
                 teams={'100': [1]}, enabled=False)]
SNAPSHOTS = {'vm:linux-01':[dict(name='before-exercise',description='Baseline workstation',created=1792700000)], 'vm:windows-01':[], 'vm:staging-app':[], 'container:dns':[]}
STATE = dict(snapshots=copy.deepcopy(SNAPSHOTS), job_reads={}, read_fail_next=0, projects=copy.deepcopy(PROJECTS), users={100: copy.deepcopy(USERS), 102: []},
             accounts=copy.deepcopy(ACCOUNTS), writes=[], jobs={}, error=False, fail_resource=None, power={'vm:linux-01':'running','vm:windows-01':'stopped','vm:staging-app':'running','container:dns':'running'})


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(GUI), **kwargs)

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; connect-src 'self'; frame-src 'none'; form-action 'self'; style-src 'self' 'unsafe-inline'")
        super().end_headers()

    def reply(self, value, code=200):
        payload = json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/__fixture__/state':
            return self.reply(dict(writes=STATE['writes'], accounts=STATE['accounts'], users=STATE['users']))
        if not path.startswith('/api/'):
            return super().do_GET()
        route = path[5:]
        if route == 'session':
            return self.reply(dict(username='fixture-admin', admin=True, projects=['100', '102'], managed_projects=['100', '102'], csrf='local-fixture'))
        if route == 'projects':
            records=copy.deepcopy(STATE['projects'])
            for project in records:
                for field in ('scenario_boxes','scenario_containers'):
                    for box in project.get(field,{}).values():
                        box['health_revision']=hashlib.sha256(json.dumps(box.get('health_checks',[]),sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
            return self.reply(dict(projects=records))
        if route == 'users':
            return self.reply(dict(users=STATE['accounts']))
        if route == 'audit':
            return self.reply(dict(events=[]))
        if route == 'connector':
            return self.reply({})
        if route == 'guac-users':
            if STATE['error']:
                return self.reply(dict(error='Fixture connection interrupted'), 503)
            from urllib.parse import parse_qs
            project = int(parse_qs(urlsplit(self.path).query)['project'][0])
            return self.reply(dict(users=STATE['users'][project]))
        if route == 'guac-user-stats':
            return self.reply(dict(last_login='2026-10-23T01:28:41Z',login_history=[dict(start='2026-10-22T20:00:00Z',end='2026-10-22T21:00:00Z',remote_host='10.60.0.20') for _ in range(27)],active_connections=[]))
        if route == 'project-status':
            return self.reply(dict(boxes={key:dict(state=value,node='proxmox2',guest_id=500+index,observed_at=time.time()) for index,(key,value) in enumerate(STATE['power'].items())}))
        if route == 'jobs':
            return self.reply(dict(jobs=list(STATE['jobs'].values()), total=0, page_count=1))
        if route.startswith('jobs/') and route[5:] in STATE['jobs']:
            if STATE['read_fail_next']:
                STATE['read_fail_next'] -= 1
                return self.reply(dict(error='Synthetic observation interruption'),503)
            job=STATE['jobs'][route[5:]]
            remaining=STATE['job_reads'].get(job['id'],0)
            if remaining:
                STATE['job_reads'][job['id']]=remaining-1
                job['status']='running'
            elif job['status'] not in ('failed','interrupted'):
                job['status']='succeeded'
            return self.reply(dict(job=job))
        return self.reply(dict(error='No fixture for ' + route), 501)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
        path = urlsplit(self.path).path
        if path == '/__fixture__/control':
            STATE['error'] = bool(data.get('error'))
            STATE['fail_resource'] = data.get('fail_resource')
            if 'read_fail_next' in data: STATE['read_fail_next']=int(data['read_fail_next'])
            if data.get('stale_health'):STATE['projects'][0]['scenario_boxes']['staging-app']['health_checks']=[dict(type='tcp',port=81,timeout=5)]
            if data.get('reset'):
                STATE.update(projects=copy.deepcopy(PROJECTS),snapshots=copy.deepcopy(SNAPSHOTS),job_reads={},read_fail_next=0,users={100:copy.deepcopy(USERS),102:[]},accounts=copy.deepcopy(ACCOUNTS),writes=[],jobs={},power={'vm:linux-01':'running','vm:windows-01':'stopped','vm:staging-app':'running','container:dns':'running'})
            return self.reply({})
        # Never retain passwords, even synthetic ones, in the inspection endpoint.
        STATE['writes'].append(dict(path=path, data={k: v for k, v in data.items() if 'password' not in k}))
        if path == '/api/users':
            account = next((a for a in STATE['accounts'] if a['username'] == data.get('username')), None)
            action = data.get('action')
            if action == 'create':
                if account:
                    return self.reply(dict(error='Username already exists'), 409)
                STATE['accounts'].append(dict(username=data['username'], admin=False, projects=data['projects'],
                                               roles={str(p): data['role'] for p in data['projects']}, enabled=True, teams={}))
            elif account and action == 'set-enabled':
                account['enabled'] = data['enabled']
            elif account and action == 'set-role':
                account['roles'][str(data['project'])] = data['role']
            elif account and action == 'delete':
                STATE['accounts'].remove(account)
            elif account and action is None:
                account['projects'] = data['projects']
                account['teams'] = data['teams']
            else:
                return self.reply(dict(error='Unsupported fixture account action'), 400)
            return self.reply(dict(users=STATE['accounts']))
        if path == '/api/guac-users':
            records = STATE['users'][data['project']]
            action = data['action']
            record = next((r for r in records if r['username'] == data.get('username')), None)
            if action == 'set-enabled-many':
                names = data['usernames']
                if any(not any(r['username'] == name for r in records) for name in names):
                    return self.reply(dict(error='Unknown fixture account'), 400)
                for r in records:
                    if r['username'] in names:
                        r['enabled'] = data['enabled']
            elif action == 'update' and record:
                for field in ('enabled', 'allow_password_reset', 'teams', 'connections'):
                    if field in data:
                        record[field] = data[field]
                record['login'] = data.get('new_username', record['login'])
            elif action in ('create', 'bulk-create'):
                name = data.get('username', 'fixture-bulk-user')
                records.append(dict(username=name, login=name, home_team=data['team'] or 254,
                                    user_number=data['start_number'], enabled=True, teams=data.get('teams', []),
                                    connections=data.get('connections', []), allow_password_reset=False))
            elif action in ('delete', 'delete-many'):
                names = data.get('usernames', [data.get('username')])
                records[:] = [r for r in records if r['username'] not in names]
            else:
                return self.reply(dict(error='Unsupported fixture project-user action'), 400)
            job_id = f'{len(STATE["jobs"])+1:032x}'
            STATE['jobs'][job_id] = dict(id=job_id, operation='guac-users', project=data['project'],
                                         actor='fixture-admin', status='succeeded', created=time.time(),
                                         started=time.time(), finished=time.time(), output='Fixture account update complete.')
            return self.reply(dict(users=records, job_id=job_id), 202)
        if path == '/api/jobs' and data.get('operation') in ('guac-box-control','guac-snapshot','machine-health-update'):
            options = data['options']
            key = options.get('resource_key') or options.get('kind','')+':'+options.get('name','')
            if key not in STATE['power']:
                return self.reply(dict(error='Unknown fixture machine'), 400)
            if STATE['fail_resource'] == key:
                return self.reply(dict(error='Synthetic queue rejection'), 503)
            if data['operation'] == 'guac-box-control':
                if options.get('action') not in ('start','shutdown','restart','stop'):
                    return self.reply(dict(error='Unknown fixture power action'), 400)
                STATE['power'][key] = 'running' if options['action'] in ('start','restart') else 'stopped'
            output='Synthetic machine action complete.';health_error=None
            if data['operation']=='machine-health-update':
                project=next(p for p in STATE['projects'] if p['project_id']==data['project'])
                box=project[{'vm':'scenario_boxes','container':'scenario_containers'}[options['kind']]][options['name']]
                revision=hashlib.sha256(json.dumps(box.get('health_checks',[]),sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
                if revision!=options['expected_revision']:health_error='Health checks changed since this editor opened. Refresh the machine details and try again.'
                for check in options['checks']:
                    if check.get('type') not in ('ping','tcp','http') or check.get('type')!='ping' and not 1<=check.get('port',0)<=65535 or not 1<=check.get('timeout',5)<=60:
                        return self.reply(dict(error='Invalid health check'),400)
                if not health_error:
                    box['health_checks']=options['checks']
                    output='Updated health checks. New revision: '+hashlib.sha256(json.dumps(options['checks'],sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
            if data['operation']=='guac-snapshot':
                action=options['action'];name=options.get('snapshot','');rows=STATE['snapshots'][key]
                match=next((row for row in rows if row['name']==name),None)
                if action!='list' and not re.fullmatch('[a-z][a-z0-9-]{0,39}',name):
                    return self.reply(dict(error='Invalid snapshot name'),400)
                if action=='create':
                    if match: return self.reply(dict(error='Snapshot already exists'),409)
                    rows.append(dict(name=name,created=time.time(),description='CRANGE user snapshot'))
                elif action=='delete':
                    if not match: return self.reply(dict(error='Snapshot does not exist'),404)
                    rows.remove(match)
                elif action=='restore':
                    if not match: return self.reply(dict(error='Snapshot does not exist'),404)
                    if STATE['power'][key]!='stopped':return self.reply(dict(error='Stop this machine before restoring a snapshot.'),400)
                elif action!='list':return self.reply(dict(error='Invalid snapshot action'),400)
                output=json.dumps({'box':key,'snapshots':rows}) if action=='list' else 'Synthetic snapshot '+action+' complete.'
            job_id = f'{len(STATE["jobs"])+1:032x}'
            if data['operation']=='guac-snapshot':STATE['job_reads'][job_id]=1
            STATE['jobs'][job_id] = dict(id=job_id, operation=data['operation'], resource_key=key, project=data['project'],
                                         actor='fixture-admin', status='failed' if health_error else 'succeeded', created=time.time(),
                                         started=time.time(), finished=time.time(), output=health_error or output)
            return self.reply(dict(job_id=job_id), 202)
        return self.reply(dict(error='Infrastructure operations are disabled in this fixture'), 403)


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8768
    print(f'Local dashboard fixture: http://127.0.0.1:{port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
