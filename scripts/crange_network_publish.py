"""Root-only publication of one accepted, source-bound network receipt."""
from __future__ import annotations
import argparse
import copy
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from crange_network_acceptance import CHECKS, require, validate_bundle
from crange_network_adapters import (protected, project_input_hashes, controller_source_hashes,
                                    trusted_relay_source_hash, durable_json)
from crange_scheduling import network_attestation_valid

POLICY = Path('/home/crange/.config/crange/distribution-policy.json')
PROBES = Path('/var/lib/crange-network-probes')


def sha(value):
    return hashlib.sha256(value).hexdigest()


@contextmanager
def policy_write_lock(path):
    path=Path(path)
    info=path.lstat()
    require(stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode),'Policy must be a regular file.')
    fd=os.open(path.with_name('.distribution-policy.lock'),os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        lockinfo=os.fstat(fd)
        require(stat.S_ISREG(lockinfo.st_mode),'Policy lock must be a regular file.')
        if os.geteuid()==0:os.fchown(fd,info.st_uid,info.st_gid)
        require(os.fstat(fd).st_uid==info.st_uid,'Policy lock owner differs.')
        os.fchmod(fd,0o600)
        fcntl.flock(fd,fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def atomic_policy(path, policy, expected):
    require(path.read_bytes()==expected,'Policy changed during publication.')
    info=path.lstat()
    temporary=path.with_name('.distribution-policy-'+os.urandom(12).hex()+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,stat.S_IMODE(info.st_mode))
    try:
        if os.geteuid()==0:os.fchown(fd,info.st_uid,info.st_gid)
        os.fchmod(fd,stat.S_IMODE(info.st_mode))
        with os.fdopen(fd,'w',closefd=False) as stream:
            json.dump(policy,stream,indent=2,sort_keys=True);stream.write('\n');stream.flush();os.fsync(fd)
    finally:os.close(fd)
    os.replace(temporary,path)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def merge_receipt(policy, receipt, receipt_sha256):
    """Pure exact-tuple merge; never enroll a node or infer another VNet."""
    target=receipt['scope'];node=target['node'];vnet=target['vnet']
    require(node in policy.get('allowed_nodes',[]),'Accepted node is not enrolled in policy.')
    updated=copy.deepcopy(policy)
    attestations=updated.setdefault('network_attestations',{})
    old=attestations.get(node,{})
    require(isinstance(old,dict),'Invalid existing node attestation.')
    require(not old.get('verified_networks') or network_attestation_valid(old),
            'Existing unverified VNet claims need separate review before publication.')
    entry=copy.deepcopy(old)
    evidence=entry.setdefault('scoped_receipts',{})
    existing=evidence.get(vnet)
    if existing and existing.get('receipt_sha256')==receipt_sha256 and vnet in entry.get('verified_networks',[]):
        return updated,False
    entry.update(verified=True,management_verified=True,zone='crange',mtu=1450,
                 passed_checks=sorted(CHECKS),verified_networks=sorted(set(old.get('verified_networks',[]))|{vnet}))
    evidence[vnet]={'scope':target,'receipt_sha256':receipt_sha256,
                    'source_binding_sha256':sha(json.dumps(receipt['source_binding'],sort_keys=True,separators=(',',':')).encode()),
                    'tested_at':receipt['tested_at'],'schema_version':2}
    attestations[node]=entry
    updated['revision']=str(policy.get('revision','policy'))+'+network-'+receipt_sha256[:12]
    return updated,True


def publish(source_job_id, project, *, database='/var/lib/crange-jobs/jobs.sqlite3',
            policy_path=POLICY, probe_root=PROBES, api=None):
    require(os.geteuid()==0,'Network receipt publication requires the privileged worker.')
    require(isinstance(source_job_id,str) and re.fullmatch(r'[a-f0-9]{32}',source_job_id),'Invalid source job identity.')
    with sqlite3.connect(database,timeout=10) as db:
        db.row_factory=sqlite3.Row
        row=db.execute('SELECT operation,status,project,options FROM jobs WHERE id=?',(source_job_id,)).fetchone()
    require(row is not None and row['operation']=='network-verify' and int(row['project'])==project
            and row['status'] in ('running','succeeded','failed'),'Source verification project or state differs.')
    options=json.loads(row['options'])
    bundle=protected(Path(probe_root)/('network-'+source_job_id),directory=True)
    target=validate_bundle(bundle)
    receipt=json.loads(protected(bundle/'receipt.json').read_text())
    review=json.loads(protected(bundle/'review.json').read_text())
    journal=json.loads(protected(bundle/'journal.json').read_text())
    require(receipt.get('schema_version')==2 and target==review.get('scope')==journal.get('scope')
            and journal.get('state')=='accepted_cleaned','Only accepted source-bound owned evidence may be published.')
    require(target['project_id']==project and target['node']==options.get('node') and target['vnet']==options.get('network'),
            'Queued verification and receipt scope differ.')
    require(receipt['source_binding']==review.get('source_binding'),'Receipt source binding differs from protected review.')
    receipt_hash=sha((bundle/'receipt.json').read_bytes())
    binding_hash=sha(json.dumps(receipt['source_binding'],sort_keys=True,separators=(',',':')).encode())
    previous=None
    if (bundle/'publication.json').exists():
        previous=json.loads(protected(bundle/'publication.json').read_text())
    reviewed_history=(isinstance(previous,dict) and previous.get('scope')==target
                      and previous.get('receipt_sha256')==receipt_hash
                      and previous.get('source_binding_sha256')==binding_hash
                      and previous.get('state') in ('prepared','published'))
    current_sources=(receipt['source_binding']['controller_sha256']==controller_source_hashes()
                     and receipt['source_binding']['relay_sha256']==trusted_relay_source_hash())
    require(current_sources or reviewed_history,
            'Recorded producer source requires explicit review before publication.')
    from crange import project_dir, ProxmoxAPI
    project_path=project_dir(str(project))
    require(project_input_hashes(project_path)==review['project_input_sha256'],'Project inputs changed after network verification.')
    api=api or ProxmoxAPI()
    rows=api.request('GET','/cluster/resources',{'type':'vm'})
    require(not any(int(item.get('vmid',0))==target['vmid'] for item in rows),'Owned probe is still present.')
    from crange_placement import ReservationLedger
    ledger=ReservationLedger('/var/lib/crange-distribution/reservations.sqlite3')
    try:require(ledger.get_job(target['job_id'])['state']=='released','Probe reservation is not released.')
    finally:ledger.close()
    policy_path=Path(policy_path)
    with policy_write_lock(policy_path):
        before=policy_path.read_bytes()
        policy=json.loads(before)
        updated,changed=merge_receipt(policy,receipt,receipt_hash)
        require(project_input_hashes(project_path)==review['project_input_sha256'],'Project inputs changed during publication.')
        after_bytes=(json.dumps(updated,indent=2,sort_keys=True)+'\n').encode() if changed else before
        record={'state':'prepared','scope':target,'receipt_sha256':receipt_hash,'source_binding_sha256':binding_hash,
                'policy_before_sha256':sha(before),'policy_after_sha256':sha(after_bytes),
                'publisher_sha256':sha(Path(__file__).read_bytes()),'published_at':time.time(),
                'policy_revision':updated['revision']}
        if not changed and reviewed_history:
            record=previous
        else:
            durable_json(bundle/'publication.json',record)
        if changed:atomic_policy(policy_path,updated,before)
        record['state']='published'
        durable_json(bundle/'publication.json',record)
    with sqlite3.connect(database,timeout=10) as db:
        marker='Verified network placement enabled for '+target['node']+'/'+target['vnet']+'.'
        db.execute("UPDATE jobs SET output=substr(output || ?, -50000) WHERE id=? AND instr(output,?)=0",
                   ('\n'+marker,source_job_id,marker))
    return {'published':True,'changed':changed,'project':project,'node':target['node'],
            'vnet':target['vnet'],'receipt_sha256':receipt_hash,'policy_revision':updated['revision']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source_job_id');parser.add_argument('--project',required=True,type=int)
    args=parser.parse_args()
    try:print(json.dumps(publish(args.source_job_id,args.project),sort_keys=True))
    except Exception as error:
        from crange_network_workflow import _persist_workflow_error
        _persist_workflow_error(args.source_job_id,error)
        parser.exit(1,'Accepted network receipt publication failed; inspect the protected receipt and policy.\n')


if __name__=='__main__':main()
