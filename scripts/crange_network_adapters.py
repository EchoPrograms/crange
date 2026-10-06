"""Protected concrete saved-plan and owned QGA diagnostic adapters.

Not a complete ProbeBackend: no allocation, UFW mutation or acceptance publisher.
The administrator CLI inspects only; it never applies a plan or runs a guest.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import time
import urllib.parse
import urllib.request

from crange_network_acceptance import probe_from_plan, require, scope
from crange_network_runner import plan_digest

ROOT = Path('/var/lib/crange-network-probes')
CREDENTIALS = Path('/home/crange/.config/crange/proxmox.env')


def protected(path, *, directory=False):
    path = Path(path)
    info = path.lstat()
    require(info.st_uid == 0 and not info.st_mode & 0o022 and not stat.S_ISLNK(info.st_mode),
            'Network probe inputs must be root-owned and not writable by group/others.')
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
            'Network probe input has the wrong file type.')
    return path


class SavedTerraformProbe:
    def __init__(self, job_id, *, run=subprocess.run):
        require(isinstance(job_id,str) and __import__('re').fullmatch(r'network-[a-f0-9]{32}',job_id), 'Invalid probe job.')
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

    def inspect(self, operation):
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
        probe_from_plan(plan, operation, self.target)
        require(plan_digest(plan) == approval.get('json_sha256'), 'Decoded plan differs from reviewed digest.')
        return plan

    def apply_saved_plan(self, target, operation, digest, timeout):
        require(target == self.target and 0 < timeout <= 600, 'Invalid saved-plan application scope.')
        plan = self.inspect(operation)
        require(plan_digest(plan) == digest, 'Caller plan digest mismatch.')
        # Never source shell text; parse only the two credential variables.
        values = {}
        for line in protected(CREDENTIALS).read_text().splitlines():
            line = line.removeprefix('export ').strip()
            key, sep, raw = line.partition('=')
            if sep and key in ('PROXMOX_VE_ENDPOINT','PROXMOX_VE_API_TOKEN'):
                parsed = shlex.split(raw, comments=True)
                require(len(parsed) == 1, 'Invalid protected API credential format.')
                values[key] = parsed[0]
        require(set(values) == {'PROXMOX_VE_ENDPOINT','PROXMOX_VE_API_TOKEN'}, 'Protected API configuration incomplete.')
        # Fixed environment avoids inherited TF_CLI_ARGS / alternate TF_DATA_DIR.
        env = {'PATH':'/usr/local/bin:/usr/bin:/bin', 'HOME':'/root',
               'PROXMOX_VE_INSECURE':'true', **values}
        result = self.run(['terraform','apply','-no-color','-input=false',str(self.root/(operation+'.tfplan'))],
                          cwd=self.root,env=env,capture_output=True,text=True,timeout=timeout,check=False)
        require(result.returncode == 0, 'Reviewed probe plan apply failed; reconcile owned resources.')
        return {'operation':operation,'plan_sha256':digest}


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
        try:
            with self.open_url(request,context=self.api.ssl_context,timeout=min(10,remaining)) as response:
                body=json.load(response)
        except Exception:
            raise ValueError('Bounded probe API request failed.') from None
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
            'dns':['/bin/busybox','timeout','15','/bin/busybox','nslookup','example.com',gateway],
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
    KEY = Path('/etc/crange/network-probe-ssh-key')

    def __init__(self, target, *, project_router, team_router, run=subprocess.run):
        self.target=scope(**target)
        project=ipaddress.ip_address(project_router)
        team=ipaddress.ip_address(team_router)
        require(project in ipaddress.ip_network('10.60.2.0/24') and 2 <= int(str(project).split('.')[-1]) <= 254,
                'Project router must use declared shared transit.')
        require(team in ipaddress.ip_network(f'10.{target["project_id"]}.0.0/24')
                and 2 <= int(str(team).split('.')[-1]) <= 254, 'Team router must use project transit.')
        protected(self.KEY)
        self.prefix=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
                     '-o','ConnectTimeout=5','-i',str(self.KEY),'-J','root@'+str(project),'root@'+str(team)]
        self.run=run

    def _rule(self, target, rule):
        expected={'owner':self.target['job_id'],'sources':['10.60.0.20','10.60.0.10'],
                  'destination':self.target['address'].split('/')[0]+'/32','protocol':'tcp','port':22}
        require(target == self.target and rule == expected, 'Management rule scope mismatch.')
        return expected

    def _execute(self, arguments):
        result=self.run(self.prefix+[shlex.join(['/usr/sbin/ufw',*arguments])],
                        capture_output=True,text=True,timeout=20,check=False)
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
            require(len(lines) == 1 and source in lines[0].split()
                    and rule['destination'].split('/')[0] in lines[0].split() and '22/tcp' in lines[0].split()
                    and lines[0].split()[-1] == owner
                    and 'ALLOW FWD' in lines[0], 'Owned router rule differs; require manual reconciliation.')
            self._execute(['--force','delete','route','allow','from',source,'to',rule['destination'],
                           'port','22','proto','tcp','comment',owner])
        require(rule['owner'] not in self._status(), 'Temporary router rules still present.')

    def temporary_rules_absent(self, target):
        require(target == self.target, 'Router cleanup scope mismatch.')
        return self.target['job_id'] not in self._status()


def main():
    parser=argparse.ArgumentParser(description='Administrator dry-run inspection of a protected saved probe plan.')
    parser.add_argument('job_id')
    parser.add_argument('operation',choices=('create','delete'))
    args=parser.parse_args()
    try:
        require(os.geteuid() == 0, 'Controller administrator privileges required.')
        backend=SavedTerraformProbe(args.job_id)
        plan=backend.inspect(args.operation)
    except (ValueError,KeyError,TypeError,OSError,subprocess.SubprocessError):
        parser.exit(1,'Protected probe inspection rejected. No operation applied.\n')
    print(json.dumps({'scope':backend.target,'operation':args.operation,'plan_sha256':plan_digest(plan),'applied':False}))


if __name__=='__main__': main()
