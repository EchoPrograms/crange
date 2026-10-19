"""Administrator-only fixed storage jobs through the authenticated root relay."""
import json
import socket


def request(action, options):
    if action not in ('review','apply'):
        raise ValueError('Invalid range storage action.')
    value = {'command':'range-storage-'+action, 'node':'proxmox5', **options}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(350); client.connect('/run/crange-ct-read/socket')
        client.sendall(json.dumps(value).encode()+b'\n'); raw=b''
        while not raw.endswith(b'\n') and len(raw)<8192:
            part=client.recv(8192-len(raw))
            if not part: break
            raw+=part
    reply=json.loads(raw)
    if set(reply) != {'value'} or not isinstance(reply['value'],str):
        raise ValueError('Range storage request was rejected; inspect its worker log.')
    result = json.loads(reply['value'])
    if action == 'apply': approve_storage(result)
    return result


def approve_storage(proof):
    """Publish the fixed native COW proof without changing other placement limits."""
    import hashlib
    import uuid
    from crange import ProxmoxAPI
    from crange_scheduling import CONFIG, atomic_json, read_settings
    from crange_network_publish import policy_write_lock
    if (proof.get('node') != 'proxmox5' or proof.get('storage') != 'crange-thin' or
            proof.get('state') != 'ready' or proof.get('snapshots_verified') is not True or
            proof.get('proof_volumes_deleted') is not True):
        raise ValueError('Root storage COW proof is incomplete.')
    api=ProxmoxAPI()
    # The hash-pinned root helper verifies the cluster storage definition.
    # The controller's narrow token can audit node storage availability but
    # intentionally cannot read administrator storage configuration.
    rows=api.request('GET','/nodes/proxmox5/storage')
    matches=[row for row in rows if row.get('storage')=='crange-thin']
    if len(matches)!=1 or not matches[0].get('active') or matches[0].get('shared',0):
        raise ValueError('Proved local thin storage is unavailable to the controller.')
    path=CONFIG/'distribution-policy.json'
    with policy_write_lock(path):
        before=hashlib.sha256(path.read_bytes()).hexdigest()
        policy,_=read_settings()
        if 'proxmox5' not in policy.get('allowed_nodes',[]):
            raise ValueError('P5 must be enrolled before storage approval.')
        if 'crange-thin' not in policy['allowed_storages']:policy['allowed_storages'].append('crange-thin')
        node=policy.setdefault('nodes',{}).setdefault('proxmox5',{})
        snapshots=node.setdefault('snapshot_storages',[])
        if 'crange-thin' not in snapshots:snapshots.append('crange-thin')
        node.setdefault('storage_proofs',{})['crange-thin']=proof
        policy['revision']='range-storage-'+uuid.uuid4().hex
        if hashlib.sha256(path.read_bytes()).hexdigest()!=before:
            raise ValueError('Placement policy changed during storage approval.')
        atomic_json(path,policy,0o600)
