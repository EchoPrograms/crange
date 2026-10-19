#!/usr/bin/python3
"""Admin deployment: backup CRANGE sources/captures; clone isolated probes."""
import json,subprocess

def main():
    roles=json.loads(subprocess.check_output(['pveum','role','list','--output-format','json'],text=True))
    for role,privilege in [('CrangeCaptureBackup','VM.Backup'),('CrangeProbeClone','VM.Clone')]:
        matches=[r for r in roles if r.get('roleid')==role]
        if matches:
            if set(matches[0].get('privs','').split(','))!={privilege}:
                raise RuntimeError(f'Existing {role} differs from its fixed privilege set')
        else:subprocess.run(['pveum','role','add',role,'--privs',privilege],check=True)
        pools = ['crange-probes', 'crange-iac'] if privilege == 'VM.Backup' else ['crange-probes']
        for pool in pools:
            subprocess.run(['pveum','acl','modify','/pool/'+pool,'--roles',role,
                            '--users','crange-iac@pve','--tokens','crange-iac@pve!terraform','--propagate','1'],check=True)
    role='CrangeCaptureStorage';privileges={'Datastore.Audit','Datastore.AllocateSpace'}
    matches=[r for r in roles if r.get('roleid')==role]
    if matches and set(matches[0].get('privs','').split(','))!=privileges:
        raise RuntimeError('Existing capture storage role differs from its fixed privileges')
    if not matches:
        subprocess.run(['pveum','role','add',role,'--privs',','.join(sorted(privileges))],check=True)
    subprocess.run(['pveum','acl','modify','/storage/ssd-lvm','--roles',role,
                    '--users','crange-iac@pve','--tokens','crange-iac@pve!terraform','--propagate','1'],check=True)
    role='CrangeGuestConsole';privilege='VM.Console'
    matches=[r for r in roles if r.get('roleid')==role]
    if matches and set(matches[0].get('privs','').split(','))!={privilege}:
        raise RuntimeError('Existing console role differs from its fixed privilege')
    if not matches:subprocess.run(['pveum','role','add',role,'--privs',privilege],check=True)
    subprocess.run(['pveum','acl','modify','/pool/crange-iac','--roles',role,
                    '--users','crange-iac@pve','--tokens','crange-iac@pve!terraform','--propagate','1'],check=True)
    print('crange: backup granted on CRANGE source/probe pools; clone granted only on the probe pool')

if __name__=='__main__':
    try:main()
    except Exception as exc:raise SystemExit(f'crange: capture permission setup failed: {exc}') from None
