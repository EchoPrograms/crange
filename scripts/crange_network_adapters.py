"""Protected concrete saved-plan and owned QGA diagnostic adapters.

Not a complete ProbeBackend: no allocation, UFW mutation or acceptance publisher.
The administrator CLI inspects only; it never applies a plan or runs a guest.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import pwd
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import time
import urllib.parse
import urllib.request
import urllib.error

from crange_network_acceptance import PROBE_ADDRESS, probe_from_plan, require, scope
from crange_network_runner import plan_digest, safe_failure_cause
from crange_scheduling import atomic_json

ROOT = Path('/var/lib/crange-network-probes')
CREDENTIALS = Path('/home/crange/.config/crange/proxmox.env')


RELAY_SOURCE_HASH = Path('/etc/crange/network-relay-source.sha256')


def controller_source_hashes():
    from crange_network_acceptance import CONTROLLER_SOURCE_FILES
    directory=Path(__file__).resolve().parent
    hashes={}
    for name in sorted(CONTROLLER_SOURCE_FILES):
        path=directory/name
        require(path.is_file() and not path.is_symlink(),'Controller source must be a regular file.')
        hashes[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def trusted_relay_source_hash():
    path=protected(RELAY_SOURCE_HASH)
    require(not path.stat().st_mode & 0o077,'Relay source trust file must be root-private.')
    value=path.read_text().strip()
    require(re.fullmatch(r'[a-f0-9]{64}',value),'Invalid trusted relay source SHA-256.')
    return value


def project_input_hashes(path):
    """Bind identical desired-state files and pinned provider in every workflow."""
    files = {item.name: item for item in Path(path).iterdir()
             if item.name.endswith(('.tf', '.tf.json', '.tfvars', '.tfvars.json'))
             or item.name == '.terraform.lock.hcl'}
    require(bool(files), 'Project desired-state inputs are missing.')
    return {name: hashlib.sha256(item.read_bytes()).hexdigest()
            for name, item in sorted(files.items())}


def protected(path, *, directory=False):
    path = Path(path)
    info = path.lstat()
    require(info.st_uid == 0 and not info.st_mode & 0o022 and not stat.S_ISLNK(info.st_mode),
            'Network probe inputs must be root-owned and not writable by group/others.')
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
            'Network probe input has the wrong file type.')
    return path


def worker_file(path, *, private=False):
    """Existing controller-worker inputs may belong to root or crange service."""
    path=Path(path)
    info=path.lstat()
    require(stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode), 'Worker input must be a regular file.')
    trusted={0,pwd.getpwnam('crange').pw_uid}
    require(info.st_uid in trusted and not info.st_mode & (0o077 if private else 0o022),
            'Worker input ownership/permissions are unsafe.')
    return path


def safe_terraform_apply_cause(output, returncode):
    """Classify provider failures without journaling raw CLI diagnostics."""
    text = str(output).lower()
    if 'permission denied' in text and any(term in text for term in
            ('fork/exec', 'failed to instantiate provider', 'failed to load plugin')):
        return 'terraform_local_provider_exec_permission'
    if re.search(r'\b403\b', text) or 'not authorized' in text:
        return 'proxmox_http_403_authorization'
    if re.search(r'\b401\b', text) or 'unauthorized' in text:
        return 'proxmox_http_401_authentication'
    if 'parameter verification failed' in text or re.search(r'\b400\b', text):
        return 'proxmox_http_400_validation'
    if re.search(r'\b409\b', text) or 'already exists' in text:
        return 'proxmox_http_409_conflict'
    if re.search(r'\b5\d\d\b', text):
        return 'proxmox_http_5xx'
    if 'timeout' in text or 'timed out' in text or 'deadline exceeded' in text:
        return 'proxmox_request_timeout'
    if any(term in text for term in ('connection reset', 'connection refused', 'no route to host', 'connection failed')):
        return 'proxmox_transport_error'
    code = returncode if type(returncode) is int and 0 <= returncode <= 255 else 255
    return f'terraform_apply_exit_{code}_detail_redacted'


class SavedTerraformProbe:
    def __init__(self, job_id, *, run=subprocess.run):
        require(isinstance(job_id,str) and __import__('re').fullmatch(r'network-[a-f0-9]{32}',job_id), 'Invalid probe job.')
        protected(ROOT.parent, directory=True)
        protected(ROOT, directory=True)
        self.root = protected(ROOT/job_id, directory=True)
        self.run = run
        self.manifest = json.loads(protected(self.root/'review.json').read_text())
        self.target = scope(**self.manifest['scope'])
        require(self.target['job_id'] == job_id, 'Manifest owner mismatch.')
        self._check_inputs()

    def _check_inputs(self):
        candidates = {path.name for path in self.root.iterdir() if path.name.endswith(('.tf', '.tf.json', '.tfvars', '.tfvars.json'))}
        require(candidates == {'main.tf'}, 'Unreviewed extra Terraform configuration is forbidden.')
        files = self.manifest.get('input_sha256', {})
        # Only a fixed minimal standalone Terraform configuration is supported.
        require(set(files) == {'main.tf', '.terraform.lock.hcl'}, 'Review both fixed Terraform inputs.')
        for filename, digest in files.items():
            require(hashlib.sha256(protected(self.root/filename).read_bytes()).hexdigest() == digest,
                    'Reviewed Terraform inputs changed.')
        lock = (self.root/'.terraform.lock.hcl').read_text()
        require('registry.terraform.io/bpg/proxmox' in lock and '"0.104.0"' in lock, 'Provider version must remain pinned.')

    def inspect(self, operation, *, partial_source=None):
        require(operation in ('create','delete'), 'Unknown plan operation.')
        self._check_inputs()
        path = protected(self.root/(operation+'.tfplan'))
        approval = self.manifest.get('plans', {}).get(operation, {})
        binary_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        require(binary_digest == approval.get('binary_sha256'), 'Saved plan has no matching administrator review.')
        result = self.run(['terraform','show','-json',str(path)], cwd=self.root,
                          capture_output=True,text=True,timeout=30,check=False)
        require(result.returncode == 0, 'Cannot inspect saved probe plan.')
        plan = json.loads(result.stdout)
        if operation == 'delete' and partial_source is not None:
            partial_delete_plan(plan, self.target, self.inspect('create'), partial_source)
        else:
            probe_from_plan(plan, operation, self.target)
        require(plan_digest(plan) == approval.get('json_sha256'), 'Decoded plan differs from reviewed digest.')
        return plan

    def _env(self):
        values = {}
        credential_file = worker_file(CREDENTIALS,private=True)
        for line in credential_file.read_text().splitlines():
            line = line.removeprefix('export ').strip()
            key, sep, raw = line.partition('=')
            if sep and key in ('PROXMOX_VE_ENDPOINT','PROXMOX_VE_API_TOKEN'):
                parsed = shlex.split(raw, comments=True)
                require(len(parsed) == 1, 'Invalid protected API credential format.')
                values[key] = parsed[0]
        require(set(values) == {'PROXMOX_VE_ENDPOINT','PROXMOX_VE_API_TOKEN'}, 'Protected API configuration incomplete.')
        from crange_proxmox_compat import reviewed_identity
        identity=reviewed_identity()
        if identity['enabled']:values['PROXMOX_VE_ENDPOINT']=identity['endpoint']
        return {'PATH':'/usr/local/bin:/usr/bin:/bin', 'HOME':'/root',
                'PROXMOX_VE_INSECURE':'true', **values}

    def apply_saved_plan(self, target, operation, digest, timeout, *, partial_source=None):
        require(target == self.target and 0 < timeout <= 600, 'Invalid saved-plan application scope.')
        plan = self.inspect(operation, partial_source=partial_source)
        require(plan_digest(plan) == digest, 'Caller plan digest mismatch.')
        result = self.run(['terraform','apply','-no-color','-input=false',str(self.root/(operation+'.tfplan'))],
                          cwd=self.root,env=self._env(),capture_output=True,text=True,timeout=timeout,check=False)
        # Keep provider diagnostics private before reducing them to a safe cause.
        # These files are administrator evidence, never journal/UI content.
        for stream, output in (('stdout', result.stdout), ('stderr', result.stderr)):
            path = self.root / (operation + '-apply.' + stream)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                require(info.st_uid == 0 and stat.S_ISREG(info.st_mode),
                        'Provider diagnostic file must be root-owned and regular.')
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, 'w', closefd=False) as evidence:
                    evidence.write(output or '')
                    evidence.flush()
                    os.fsync(fd)
            finally:
                os.close(fd)
        if result.returncode != 0:
            error = ValueError('Reviewed probe plan apply failed; reconcile owned resources.')
            error.safe_cause = safe_terraform_apply_cause(result.stdout + '\n' + result.stderr, result.returncode)
            raise error
        return {'operation':operation,'plan_sha256':digest}

    def state_addresses(self):
        if not (self.root/'terraform.tfstate').exists():
            require(not (self.root/'terraform.tfstate.backup').exists(), 'Missing current state with backup needs reconciliation.')
            return []
        result=self.run(['terraform','state','list'],cwd=self.root,env=self._env(),
                        capture_output=True,text=True,timeout=30,check=False)
        require(result.returncode == 0, 'Cannot inspect standalone probe state.')
        addresses=result.stdout.splitlines()
        require(addresses in ([],[PROBE_ADDRESS]), 'Standalone state owns an unrelated resource.')
        return addresses

    def generate_delete_plan(self, *, partial_source=None):
        """Machine review: exactly one owned delete, then persist both digests."""
        self._check_inputs()
        require(self.state_addresses() == [PROBE_ADDRESS], 'Probe is not the sole state resource.')
        path=self.root/'delete.tfplan'
        require(not path.is_symlink(), 'Saved destroy plan cannot be a symlink.')
        result=self.run(['terraform','plan','-destroy','-no-color','-input=false','-out='+str(path)],
                        cwd=self.root,env=self._env(),capture_output=True,text=True,timeout=600,check=False)
        require(result.returncode == 0, 'Owned destroy plan generation failed.')
        # Terraform inherits the worker umask and may otherwise create 0660.
        info=path.lstat()
        require(stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) and info.st_uid == self.root.stat().st_uid,
                'Generated destroy plan must be an owned regular file.')
        path.chmod(0o600)
        exported=self.run(['terraform','show','-json',str(protected(path))],cwd=self.root,
                          capture_output=True,text=True,timeout=30,check=False)
        require(exported.returncode == 0, 'Owned destroy plan decoding failed.')
        plan=json.loads(exported.stdout)
        if partial_source is None:
            probe_from_plan(plan,'delete',self.target)
        else:
            partial_delete_plan(plan,self.target,self.inspect('create'),partial_source)
        self.manifest.setdefault('plans',{})['delete']={
            'binary_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'json_sha256':plan_digest(plan),'review':'fixed-owned-probe-delete'}
        durable_json(self.root/'review.json',self.manifest)
        durable_json(self.root/'delete-plan.json',plan)
        return plan


class BoundedPVE:
    """Use a per-request deadline below the fixed diagnostic total deadline."""
    def __init__(self, api, *, open_url=urllib.request.urlopen):
        self.api=api
        self.open_url=open_url

    def request(self, method, path, fields, *, deadline):
        remaining=deadline-time.monotonic()
        require(remaining > 0, 'Probe diagnostic deadline expired.')
        encoded=urllib.parse.urlencode(fields or {},doseq=True)
        url=self.api.endpoint+path
        data=None
        if method=='GET':
            if encoded: url+='?'+encoded
        else: data=encoded.encode()
        request=urllib.request.Request(url,data=data,method=method,
            headers={'Authorization':'PVEAPIToken='+self.api.token,'Content-Type':'application/x-www-form-urlencoded'})
        for attempt in range(2 if method == 'GET' else 1):
            remaining = deadline - time.monotonic()
            require(remaining > 0, 'Probe diagnostic deadline expired.')
            try:
                with self.open_url(request,context=self.api.ssl_context,timeout=min(10,remaining)) as response:
                    body=json.load(response)
                break
            except urllib.error.HTTPError as failure:
                if method == 'GET' and failure.code == 595 and attempt == 0:
                    continue
                cause=f'guest_api_http_{failure.code}' if 400 <= failure.code <= 599 else 'guest_api_http_error'
            except (TimeoutError, socket.timeout):
                if method == 'GET' and attempt == 0:
                    continue
                cause='guest_api_timeout'
            except (urllib.error.URLError, ConnectionError):
                if method == 'GET' and attempt == 0:
                    continue
                cause='guest_api_transport'
            except (ValueError, UnicodeError):
                cause='guest_api_invalid_json'
            except Exception:
                cause='guest_api_unknown_failure'
            error=ValueError('Bounded probe API request failed: '+cause)
            error.safe_cause=cause
            raise error from None
        require(isinstance(body,dict) and 'data' in body, 'Probe API response missing data.')
        return body['data']


class OwnedQGADiagnostics:
    """Only fixed readonly diagnostics against an exact tagged disposable VM.

    These diagnostics are NOT the six acceptance checks. In particular, gateway
    MTU/address reads do not attest cross-node VXLAN or prove isolation.
    """
    def __init__(self, transport, *, sleep=time.sleep):
        self.transport=transport
        self.sleep=sleep

    def run(self, target, diagnostic, *, timeout=30):
        target=scope(**target)
        require(type(timeout) is int and 1 <= timeout <= 60, 'Diagnostic deadline must be bounded.')
        gateway=target['address'].split('/')[0].rsplit('.',1)[0]+'.1'
        commands={
            'address':['/bin/busybox','ip','-4','address','show','dev','eth0'],
            'routes':['/bin/busybox','ip','-4','route','show'],
            'mtu':['/bin/busybox','cat','/sys/class/net/eth0/mtu'],
            'dns':['/bin/busybox','timeout','15','/bin/busybox','nslookup','example.com','10.60.2.1'],
            'egress':['/bin/busybox','timeout','15','/bin/busybox','wget','-q','-O','/dev/null','https://example.com/'],
        }
        require(diagnostic in commands, 'Unknown fixed diagnostic.')
        deadline=time.monotonic()+timeout
        base=f'/nodes/{target["node"]}/qemu/{target["vmid"]}'
        read=lambda method,path,fields=None:self.transport.request(method,path,fields,deadline=deadline)
        config=read('GET',base+'/config')
        tags=set(config.get('tags','').split(';'))
        require({'crange-network-probe',target['job_id']} <= tags, 'Guest ownership tags do not match.')
        require(config.get('template',0) in (0,'0',False) and config.get('lock') is None, 'Guest is a template or locked.')
        nics=[key for key in config if __import__('re').fullmatch(r'net\d+',key)]
        require(nics == ['net0'], 'Probe must have exactly one NIC.')
        net=dict(item.split('=',1) for item in config['net0'].split(',') if '=' in item)
        require(net.get('bridge') == target['vnet'] and net.get('link_down','0') == '0', 'Probe VNet/NIC mismatch.')
        ipv4=dict(item.split('=',1) for item in config.get('ipconfig0','').split(',') if '=' in item)
        require(ipv4.get('ip') == target['address'] and ipv4.get('gw') == gateway, 'Probe initialization address mismatch.')
        status=read('GET',base+'/status/current')
        require(status.get('status') == 'running', 'Owned probe must already be running.')
        result=read('POST',base+'/agent/exec',{'command':commands[diagnostic]})
        require(isinstance(result,dict) and type(result.get('pid')) is int and result['pid'] > 0, 'Invalid diagnostic PID.')
        pid=result['pid']
        while time.monotonic() < deadline:
            result=read('GET',base+'/agent/exec-status',{'pid':pid})
            if result.get('exited'):
                output=str(result.get('out-data',''))
                require(len(output.encode()) <= 65536, 'Diagnostic output exceeded limit.')
                return {'passed':result.get('exitcode') == 0,'output':output}
            self.sleep(min(0.25,max(0,deadline-time.monotonic())))
        raise ValueError('Owned diagnostic exceeded deadline.')


class FixedRouterUFW:
    """Two fixed tagged SSH rules on an administrator-selected Debian team router.

    Router addresses/key must come from protected desired-state inventory. This
    adapter is not exposed to HTTP and never edits OPNsense. Unknown/mismatched
    rule lines are rejected, rather than deleting by a mutable numeric index.
    """
    KEY = Path('/home/crange/.ssh/id_ed25519_range')

    def __init__(self, target, *, project_router, team_router, parent_team_router=None,
                 forwarding_parent=False, run=subprocess.run):
        self.target=scope(**target)
        project=ipaddress.ip_address(project_router)
        team=ipaddress.ip_address(team_router)
        require(project in ipaddress.ip_network('10.60.2.0/24') and 2 <= int(str(project).split('.')[-1]) <= 254,
                'Project router must use declared shared transit.')
        transit = ipaddress.ip_network(f'10.{target["project_id"]}.0.0/24')
        if forwarding_parent:
            require(target['vnet'][4] == 'c' and team in transit
                    and parent_team_router == str(team)
                    and 2 <= int(str(team).split('.')[-1]) <= 254,
                    'Custom probe forwarding parent must be its verified team router.')
        elif parent_team_router is None:
            require(target['vnet'][4] == 't' and team in transit
                    and 2 <= int(str(team).split('.')[-1]) <= 254,
                    'Team router must use project transit.')
        else:
            parent = ipaddress.ip_address(parent_team_router)
            owner = int(str(team).split('.')[2])
            require(target['vnet'][4] == 'c' and parent in transit
                    and 2 <= int(str(parent).split('.')[-1]) <= 254
                    and 1 <= owner <= 254 and owner != int(target['vnet'][-3:])
                    and team in ipaddress.ip_network(f'10.{target["project_id"]}.{owner}.0/24')
                    and 2 <= int(str(team).split('.')[-1]) <= 100,
                    'Custom router must use its declared parent LAN.')
        worker_file(self.KEY,private=True)
        worker_file('/home/crange/.ssh/known_hosts')
        proxy=shlex.join(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
                         '-o','ConnectTimeout=5','-o','UserKnownHostsFile=/home/crange/.ssh/known_hosts',
                         '-i',str(self.KEY),'-W','%h:%p','root@'+str(project)])
        # The parent router forwards this connection. Preserve the project
        # router as its SSH source, matching the managed firewall allowlist.
        # Opening another SSH tunnel on the parent would change that source.
        self.prefix=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
                     '-o','ConnectTimeout=5','-o','UserKnownHostsFile=/home/crange/.ssh/known_hosts',
                     '-i',str(self.KEY),'-o','ProxyCommand='+proxy,'root@'+str(team)]
        self.run=run

    def _rule(self, target, rule):
        expected={'owner':self.target['job_id'],'sources':['10.60.0.20','10.60.0.10'],
                  'destination':self.target['address'].split('/')[0]+'/32','protocol':'tcp','port':22}
        require(target == self.target and rule == expected, 'Management rule scope mismatch.')
        return expected

    def _execute(self, arguments):
        return self._fixed_ssh(['/usr/sbin/ufw',*arguments])

    def observe(self, observation, *, timeout=5):
        commands={'firewall_counters':['/usr/sbin/iptables-save','-c'],
                  'dhcp_config':['/bin/cat','/etc/dnsmasq.d/crange-dhcp.conf'],
                  'upstream_route':['/sbin/ip','-4','route','get','1.1.1.1']}
        require(observation in commands, 'Unknown fixed router observation.')
        return self._fixed_ssh(commands[observation],timeout=timeout)

    def _fixed_ssh(self, command, *, timeout=20):
        require(type(timeout) in (int,float) and 0 < timeout <= 20, 'Router observation deadline must be bounded.')
        result=self.run(self.prefix+[shlex.join(command)],
                        capture_output=True,text=True,timeout=timeout,check=False)
        require(result.returncode == 0, 'Fixed router rule operation failed.')
        require(len(result.stdout.encode()) <= 65536, 'Router rule output exceeded bound.')
        return result.stdout

    def _status(self):
        output=self._execute(['status','numbered'])
        require(output.startswith('Status: active'), 'Router UFW must already be active.')
        return output

    def install_management_rule(self, target, rule):
        rule=self._rule(target,rule)
        require(rule['owner'] not in self._status(), 'Rule owner already exists; reconcile before retry.')
        for index,source in enumerate(rule['sources']):
            self._execute(['route','allow','from',source,'to',rule['destination'],'port','22','proto','tcp',
                           'comment',rule['owner']+'-'+str(index)])

    def remove_management_rule(self, target, rule):
        rule=self._rule(target,rule)
        for index,source in enumerate(rule['sources']):
            owner=rule['owner']+'-'+str(index)
            lines=[line for line in self._status().splitlines() if owner in line]
            if not lines: continue
            # UFW pads status columns, including multiple spaces between ALLOW
            # and FWD. Match the entire fixed rule with flexible whitespace;
            # do not weaken ownership or delete by the mutable row number.
            destination=rule['destination'].split('/')[0]
            pattern=(r'\s*\[\s*\d+\]\s+'+re.escape(destination)+r'(?:/32)?\s+'
                     r'22/tcp\s+ALLOW\s+FWD\s+'+re.escape(source)+r'(?:/32)?\s+'
                     r'#\s+'+re.escape(owner)+r'\s*')
            require(len(lines) == 1 and re.fullmatch(pattern,lines[0]) is not None,
                    'Owned router rule differs; require manual reconciliation.')
            self._execute(['--force','route','delete','allow','from',source,'to',rule['destination'],
                           'port','22','proto','tcp','comment',owner])
        require(rule['owner'] not in self._status(), 'Temporary router rules still present.')

    def temporary_rules_absent(self, target):
        require(target == self.target, 'Router cleanup scope mismatch.')
        return self.target['job_id'] not in self._status()


class RoutedRouterUFW:
    """Own the probe's two-source allowlist on both routed firewalls."""
    def __init__(self, parent, child):
        self.parent, self.child = parent, child

    def install_management_rule(self, target, rule):
        self.parent.install_management_rule(target, rule)
        self.child.install_management_rule(target, rule)

    def remove_management_rule(self, target, rule):
        failures = []
        for router in (self.child, self.parent):
            try:
                router.remove_management_rule(target, rule)
            except Exception as error:
                failures.append(error)
        require(not failures, 'Owned custom probe rules require reconciliation on both routers.')

    def temporary_rules_absent(self, target):
        observed = [router.temporary_rules_absent(target) for router in (self.parent, self.child)]
        return all(observed)

    def observe(self, observation, *, timeout=5):
        return self.child.observe(observation, timeout=timeout)


def router_firewall(target, routers):
    child = FixedRouterUFW(target, project_router=routers['project_router'],
                           team_router=routers['team_router'],
                           parent_team_router=routers.get('parent_team_router'))
    if not routers.get('parent_team_router'):
        return child
    parent = FixedRouterUFW(target, project_router=routers['project_router'],
                            team_router=routers['parent_team_router'],
                            parent_team_router=routers['parent_team_router'], forwarding_parent=True)
    return RoutedRouterUFW(parent, child)


def durable_json(path, value):
    atomic_json(path,value)
    descriptor=os.open(Path(path).parent,os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def _config_digest(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def cluster_probe(api, target):
    rows=api.request('GET','/cluster/resources',{'type':'vm'})
    matches=[row for row in rows if int(row.get('vmid',0)) == target['vmid']]
    require(len(matches) <= 1, 'Cluster VMID identity is ambiguous.')
    if not matches:return None
    row=matches[0]
    require(row.get('node') == target['node'] and row.get('type') == 'qemu', 'Probe node/type mismatch.')
    config=api.request('GET',f'/nodes/{target["node"]}/qemu/{target["vmid"]}/config')
    tags=set(config.get('tags','').split(';'))
    require({'crange-network-probe',target['job_id']} <= tags, 'Live probe ownership mismatch.')
    require(config.get('template',0) in (0,'0',False) and config.get('lock') is None,
            'Live probe is a template or has an outstanding provider lock.')
    nics=[key for key in config if __import__('re').fullmatch(r'net\d+',key)]
    require(nics == ['net0'], 'Live probe NIC count mismatch.')
    net=dict(item.split('=',1) for item in config['net0'].split(',') if '=' in item)
    require(net.get('bridge') == target['vnet'], 'Live probe VNet mismatch.')
    address=dict(item.split('=',1) for item in config.get('ipconfig0','').split(',') if '=' in item)
    gateway=target['address'].split('/')[0].rsplit('.',1)[0]+'.1'
    require(address.get('ip') == target['address'] and address.get('gw') == gateway,
            'Live probe address mismatch.')
    return config


def partial_delete_plan(plan, target, create_plan, source_config):
    """Validate one destroy of a clone left disconnected before configuration."""
    require(isinstance(plan, dict) and plan.get('errored', False) is False,
            'Partial-clone delete plan has errors.')
    changes = plan.get('resource_changes', [])
    mutations = [r for r in changes if r.get('change', {}).get('actions') != ['no-op']]
    require(len(mutations) == 1, 'Partial-clone delete must mutate exactly one VM.')
    row = mutations[0]
    require(row.get('address') == PROBE_ADDRESS and row.get('mode') == 'managed'
            and row.get('type') == 'proxmox_virtual_environment_vm'
            and row.get('change', {}).get('actions') == ['delete'],
            'Partial-clone plan must contain only the exact VM deletion.')
    expected = probe_from_plan(create_plan, 'create', target)
    before = row.get('change', {}).get('before')
    require(isinstance(before, dict) and before.get('node_name') == target['node']
            and before.get('vm_id') == target['vmid'] and before.get('id') == str(target['vmid'])
            and before.get('pool_id') == 'crange-probes' and before.get('name') == expected.get('name')
            and before.get('template', False) is False,
            'Partial-clone deletion identity differs from approved scope.')
    source_tags = set(source_config.get('tags', '').split(';'))
    require(source_tags and set(before.get('tags', [])) == source_tags,
            'Partial-clone deletion does not match approved source tags.')
    clones = before.get('clone', [])
    approved_clones = expected.get('clone', [])
    source = approved_clones[0].get('vm_id') if len(approved_clones) == 1 else None
    require(len(clones) == 1 and clones[0].get('vm_id') == source
            and clones[0].get('node_name') == target['node'] and clones[0].get('full') is True,
            'Partial-clone deletion source differs from approved plan.')
    disks = before.get('disk', [])
    expected_disks = expected.get('disk', [])
    require(len(disks) == len(expected_disks) == 1 and disks[0].get('datastore_id') == 'local-lvm'
            and disks[0].get('size') == expected_disks[0].get('size')
            and disks[0].get('path_in_datastore') == f'vm-{target["vmid"]}-disk-0',
            'Partial-clone deletion disk is outside the exact target VM.')
    source_net = dict(item.split('=', 1) for item in source_config.get('net0', '').split(',') if '=' in item)
    devices = before.get('network_device', [])
    require(len(devices) == 1 and devices[0].get('bridge') == source_net.get('bridge')
            and devices[0].get('disconnected') is True,
            'Partial-clone deletion network differs from disconnected source baseline.')
    source_ip = dict(item.split('=', 1) for item in source_config.get('ipconfig0', '').split(',') if '=' in item)
    init = before.get('initialization', [])
    require(len(init) == 1 and len(init[0].get('ip_config', [])) == 1,
            'Partial-clone deletion lacks source initialization identity.')
    ipv4 = init[0]['ip_config'][0].get('ipv4', [])
    require(len(ipv4) == 1 and ipv4[0].get('address') == source_ip.get('ip')
            and ipv4[0].get('gateway') == source_ip.get('gw'),
            'Partial-clone deletion initialization differs from source baseline.')
    return before


def partial_create_probe(backend, api, target, journal):
    """Prove ownership of a clone left behind before provider config completed.

    This path is cleanup-only. It requires the protected 403 journal, the
    reviewed create plan, the sole Terraform state resource, and a stopped VM
    matching that plan which still has the approved template's pre-config
    network settings. It never treats the guest as accepted or runnable.
    """
    require(journal.get('scope') == target
            and journal.get('state') in ('cleanup_uncertain','reconcile_destroy_pending')
            and journal.get('cause') == 'proxmox_http_403_authorization',
            'Partial create journal is not eligible for owned cleanup.')
    plan = backend.inspect('create')
    expected = probe_from_plan(plan, 'create', target)
    require(journal.get('create_plan_sha256') == plan_digest(plan),
            'Partial create journal does not bind the reviewed create plan.')
    require(backend.state_addresses() == [PROBE_ADDRESS],
            'Partial create must be the sole Terraform-owned resource.')
    state_path = protected(backend.root/'terraform.tfstate')
    state = json.loads(state_path.read_text())
    resources = [r for r in state.get('resources', []) if r.get('mode') == 'managed']
    require(len(resources) == 1 and resources[0].get('type') == 'proxmox_virtual_environment_vm'
            and resources[0].get('name') == 'network_acceptance_probe',
            'Partial create Terraform identity is ambiguous.')
    instances = resources[0].get('instances', [])
    require(len(instances) == 1, 'Partial create Terraform instance is ambiguous.')
    attrs = instances[0].get('attributes', {})
    require(attrs.get('id') == str(target['vmid']),
            'Partial create Terraform VM ID differs from target.')
    for key in ('vm_id', 'node_name', 'name', 'pool_id', 'tags'):
        require(attrs.get(key) == expected.get(key),
                'Partial create Terraform identity differs from reviewed plan.')
    state_clones = attrs.get('clone', [])
    plan_clones = expected.get('clone', [])
    require(len(state_clones) == len(plan_clones) == 1
            and state_clones[0].get('vm_id') == plan_clones[0].get('vm_id')
            and state_clones[0].get('node_name') == target['node']
            and state_clones[0].get('full') is True,
            'Partial create clone source differs from reviewed plan.')
    rows = api.request('GET', '/cluster/resources', {'type': 'vm'})
    matches = [row for row in rows if int(row.get('vmid', 0)) == target['vmid']]
    require(len(matches) == 1, 'Partial create cluster identity is ambiguous.')
    row = matches[0]
    require(row.get('node') == target['node'] and row.get('type') == 'qemu'
            and row.get('name') == expected.get('name') and row.get('pool') == 'crange-probes'
            and row.get('status') == 'stopped', 'Partial create live identity differs.')
    config = api.request('GET', f'/nodes/{target["node"]}/qemu/{target["vmid"]}/config')
    source = backend.manifest['source']
    source_config = api.request('GET', f'/nodes/{target["node"]}/qemu/{source["template_id"]}/config')
    require(_config_digest(source_config) == source['config_sha256']
            and source_config.get('template') in (1, '1', True),
            'Partial create source template changed.')
    require(config.get('name') == expected.get('name') and config.get('lock') is None
            and config.get('template', 0) in (0, '0', False)
            and config.get('tags') == source_config.get('tags'),
            'Partial create live config is not an untouched source clone.')
    require(config.get('cores') == expected['cpu'][0]['cores']
            and int(config.get('memory', 0)) == expected['memory'][0]['dedicated'],
            'Partial create resource limits differ.')
    source_net = dict(item.split('=', 1) for item in source_config.get('net0', '').split(',') if '=' in item)
    live_net = dict(item.split('=', 1) for item in config.get('net0', '').split(',') if '=' in item)
    require(source_net.get('bridge') and live_net.get('bridge') == source_net['bridge']
            and live_net.get('link_down') == '1' and config.get('ipconfig0') == source_config.get('ipconfig0'),
            'Partial create network config is not the untouched disconnected source clone.')
    return {'live': config, 'source': source_config}


def reconcile_owned_probe(backend, api, router):
    """Crash-safe cleanup only. No retry/create/test/policy change or ledger release.

    The fixed controller's own exact-plan inspection is the destroy review.
    A remaining ledger reservation is intentionally retained for its separate
    provider-task/absence reconciliation path.
    """
    journal_path=protected(backend.root/'journal.json')
    with (backend.root/'reconcile.lock').open('a') as lock:
        os.chmod(lock.name,0o600)
        fcntl.flock(lock,fcntl.LOCK_EX)
        journal=json.loads(journal_path.read_text())
        require(journal.get('scope') == backend.target, 'Journal and manifest ownership differ.')
        require(journal.get('state') not in ('accepted_cleaned','failed_cleaned'),
                'Terminal journal does not require cleanup.')
        target=backend.target
        rule={'owner':target['job_id'],'sources':['10.60.0.20','10.60.0.10'],
              'destination':target['address'].split('/')[0]+'/32','protocol':'tcp','port':22}
        require(journal.get('rule') == rule, 'Journal management rule identity mismatch.')
        def save(state,**fields):
            journal.update(state=state,**fields)
            durable_json(journal_path,journal)
        # Ownership must be established before any cleanup mutation.
        partial = False
        source_config = None
        try:
            existing=cluster_probe(api,target)
        except ValueError:
            # A 403 can leave only the initial, disconnected template clone.
            # Admit that exact state for cleanup only after the stricter proof.
            proof=partial_create_probe(backend,api,target,journal)
            existing=proof['live']
            source_config=proof['source']
            partial=True
        addresses=backend.state_addresses()
        save('reconcile_rule_cleanup_pending')
        errors=[]
        try:router.remove_management_rule(target,rule)
        except Exception:errors.append('management_rules')
        if existing is not None:
            save('reconcile_destroy_pending')
            phase='destroy_plan_generation'
            try:
                plan=(backend.generate_delete_plan(partial_source=source_config) if partial
                      else backend.generate_delete_plan())
                digest=plan_digest(plan)
                save('reconcile_destroy_pending',delete_plan_sha256=digest)
                # Recheck actual ownership immediately before applying saved plan.
                phase='ownership_recheck'
                if partial:
                    proof=partial_create_probe(backend,api,target,journal)
                    require(proof['live'] == existing and proof['source'] == source_config,
                            'Partial clone changed during cleanup; reconcile state separately.')
                else:
                    require(cluster_probe(api,target) is not None,'Probe disappeared during cleanup; reconcile state separately.')
                phase='saved_plan_application'
                if partial:
                    backend.apply_saved_plan(target,'delete',digest,600,partial_source=source_config)
                else:
                    backend.apply_saved_plan(target,'delete',digest,600)
            except Exception as error:
                errors.append('probe')
                cause=safe_failure_cause(error)
                if cause == f'{type(error).__name__.lower()}_detail_redacted':
                    cause=f'reconcile_{phase}_validation'
                save('reconcile_destroy_pending',destroy_failure_cause=cause)
        elif addresses:
            # Do not blindly drop state after a timed-out create/delete task.
            errors.append('absent_guest_retained_state')
        try:
            require(cluster_probe(api,target) is None,'Probe still exists.')
            require(backend.state_addresses() == [],'Probe state remains.')
            require(router.temporary_rules_absent(target) is True,'Temporary rules remain.')
        except Exception:errors.append('absence_verification')
        if errors:
            save('cleanup_uncertain',cleanup_errors=sorted(set(errors)))
            raise ValueError('Owned cleanup uncertain; reservation retained.')
        save('cleanup_verified_reservation_pending',cleanup_errors=[])
        return {'scope':target,'guest_absent':True,'state_empty':True,'rules_absent':True,
                'reservation_released':False,'policy_changed':False}


class FixedManagementChecks:
    """Actual controller CT114/source .20 and Guac VM110/source .10 origins.

    Origin node bindings come only from a protected administrator manifest.
    Controller execution must occur inside the controller: the TCP socket binds
    10.60.0.20 explicitly. Guac execution uses the fixed root relay, which runs
    only a bounded TCP/22 banner probe from VM110 and grants no QGA API rights.
    """
    def __init__(self, transport, origins, *, guac_probe=None,
                 connect_socket=socket.socket):
        require(set(origins) == {'controller','guacamole'}, 'Bind both exact management origins.')
        expected={'controller':(114,'10.60.0.20','lxc'),'guacamole':(110,'10.60.0.10','qemu')}
        for name,(vmid,address,kind) in expected.items():
            origin=origins[name]
            require(set(origin) == {'node','vmid','address','kind'} and origin['vmid']==vmid
                    and origin['address']==address and origin['kind']==kind
                    and origin['node'] in {f'proxmox{i}' for i in range(1,7)}, 'Management origin binding mismatch.')
        self.origins=origins
        self.transport=transport
        self.connect_socket=connect_socket
        self.guac_probe=guac_probe

    def run(self, target, check, *, timeout=30):
        target=scope(**target)
        require(check in ('controller','guacamole') and type(timeout) is int and 1 <= timeout <= 60,
                'Only bounded fixed management checks are available.')
        deadline=time.monotonic()+timeout
        read=lambda method,path,fields=None:self.transport.request(method,path,fields,deadline=deadline)
        class DeadlineAPI:
            def request(self,method,path,fields=None):return read(method,path,fields)
        require(cluster_probe(DeadlineAPI(),target) is not None,'Owned target is absent.')
        origin=self.origins[check]
        rows=read('GET','/cluster/resources',{'type':'vm'})
        matches=[row for row in rows if int(row.get('vmid',0))==origin['vmid']]
        require(len(matches)==1 and matches[0].get('node')==origin['node']
                and matches[0].get('type')==origin['kind'], 'Management origin node/type differs.')
        base=f'/nodes/{origin["node"]}/{origin["kind"]}/{origin["vmid"]}'
        require(matches[0].get('status')=='running','Management origin must be running.')
        address=target['address'].split('/')[0]
        if check=='controller':
            config=read('GET',base+'/config')
            net=dict(item.split('=',1) for item in config.get('net0','').split(',') if '=' in item)
            require(net.get('ip') == '10.60.0.20/24','Controller address binding differs.')
            with self.connect_socket(socket.AF_INET,socket.SOCK_STREAM) as client:
                client.settimeout(min(5,max(0.001,deadline-time.monotonic())))
                client.bind(('10.60.0.20',0))
                client.connect((address,22))
                banner=client.recv(256).decode('ascii','replace')
            require(banner.startswith('SSH-2.0-'),'Controller did not receive an SSH protocol banner.')
            return {'passed':True,'origin':origin,'output':banner.strip()}
        require(callable(self.guac_probe),'Fixed Guac-origin relay is unavailable.')
        result=self.guac_probe(target,origin,timeout=max(1,int(deadline-time.monotonic())))
        require(isinstance(result,dict) and result.get('passed') is True and
                result.get('origin')==origin and isinstance(result.get('output'),str) and
                result['output'].startswith('SSH-2.0-'),
                'Guac-origin TCP handshake was not established through the fixed relay.')
        return result


def main():
    parser=argparse.ArgumentParser(description='Root-only saved-plan inspection or owned probe cleanup; reconcile mutates infrastructure.')
    parser.add_argument('job_id')
    parser.add_argument('operation',choices=('create','delete','reconcile'))
    args=parser.parse_args()
    try:
        require(os.geteuid() == 0, 'Controller administrator privileges required.')
        backend=SavedTerraformProbe(args.job_id)
        if args.operation == 'reconcile':
            from crange import ProxmoxAPI
            routers=backend.manifest['routers']
            router=router_firewall(backend.target,routers)
            print(json.dumps(reconcile_owned_probe(backend,ProxmoxAPI(),router),sort_keys=True))
            return
        plan=backend.inspect(args.operation)
    except (ValueError,KeyError,TypeError,OSError,subprocess.SubprocessError):
        parser.exit(1,'Protected probe operation rejected; inspect owned journal before retry.\n')
    print(json.dumps({'scope':backend.target,'operation':args.operation,'plan_sha256':plan_digest(plan),'applied':False}))


if __name__=='__main__': main()
