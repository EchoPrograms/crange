"""Unprivileged peer client for one fixed Guac-origin TCP/22 probe relay."""
from __future__ import annotations

import argparse
import json
import socket
import sys

SOCKET = '/run/crange-ct-read/socket'
MAX_RESPONSE = 65536


def request(target, *, socket_factory=socket.socket):
    from crange_network_acceptance import scope
    guac_node = target.get('guac_node')
    expected_relay = target.get('relay_sha256')
    if not isinstance(expected_relay,str) or not __import__('re').fullmatch(r'[a-f0-9]{64}',expected_relay):
        raise ValueError('Bind the reviewed relay source SHA-256.')
    target = scope(**{key: target[key] for key in
                      ('project_id', 'node', 'vnet', 'address', 'vmid', 'job_id')})
    if not isinstance(guac_node, str) or not __import__('re').fullmatch(r'proxmox[1-6]', guac_node):
        raise ValueError('Invalid fixed Guac origin node.')
    value = {'command': 'network-probe-guac-origin', 'node': guac_node, 'probe_node': target['node'],
             'project_id': target['project_id'], 'vnet': target['vnet'],
             'job_id': target['job_id'].removeprefix('network-'),
             'address': target['address'].split('/')[0],
             'vmid': target['vmid']}
    encoded = json.dumps(value, separators=(',', ':')).encode() + b'\n'
    if len(encoded) > 4096:
        raise ValueError('Fixed Guac relay request exceeded its bound.')
    with socket_factory(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(30)
        client.connect(SOCKET)
        client.sendall(encoded)
        chunks = bytearray()
        while len(chunks) <= MAX_RESPONSE:
            chunk = client.recv(min(4097, MAX_RESPONSE + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if chunks.endswith(b'\n'):
                break
    if len(chunks) > MAX_RESPONSE or not chunks.endswith(b'\n'):
        raise ValueError('Fixed Guac relay returned an invalid response.')
    result = json.loads(chunks)
    if not isinstance(result, dict) or set(result) != {'value'}:
        raise ValueError('Fixed Guac relay request failed.')
    value = result['value']
    if (not isinstance(value, dict) or value.get('passed') is not True or
            value.get('origin') != {'node': guac_node, 'vmid': 110,
                                    'address': '10.60.0.10', 'kind': 'qemu'} or
            value.get('source_address') != '10.60.0.10' or
            value.get('target_address') != target['address'].split('/')[0] or
            value.get('target_port') != 22 or
            value.get('relay_sha256') != expected_relay or
            not isinstance(value.get('output'), str) or
            not value['output'].startswith('SSH-2.0-')):
        raise ValueError('Fixed Guac-origin TCP handshake was not established.')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=int, required=True)
    parser.add_argument('--node', required=True)
    parser.add_argument('--network', required=True)
    parser.add_argument('--address', required=True)
    parser.add_argument('--vmid', type=int, required=True)
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--guac-node', required=True)
    parser.add_argument('--relay-source-sha256',required=True)
    args = parser.parse_args()
    target = {'project_id': args.project, 'node': args.node, 'vnet': args.network,
              'address': args.address, 'vmid': args.vmid, 'job_id': args.job_id,
              'guac_node': args.guac_node,'relay_sha256':args.relay_source_sha256}
    print(json.dumps(request(target), sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Fixed Guac-origin relay rejected the request.')
