"""Offline administrator evidence gate; never executes commands or edits policy.

A receipt is scoped to one project, node, VNet and owned disposable VM. It is
not an automatic-placement grant. Logs are retained locally, never printed.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
from pathlib import Path

CHECKS = frozenset({'vxlan_mtu', 'controller', 'guacamole', 'dns',
                    'internet_egress', 'team_isolation'})
PROBE_ADDRESS = 'proxmox_virtual_environment_vm.network_acceptance_probe'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def scope(project_id, node, vnet, address, vmid, job_id):
    require(type(project_id) is int and 1 <= project_id <= 255 and project_id not in (60, 61),
            'Invalid range project.')
    require(isinstance(node, str) and re.fullmatch(r'proxmox[1-5]', node), 'Invalid enrolled node.')
    require(isinstance(vnet, str) and re.fullmatch(fr'p{project_id:03d}[tc](?:00[1-9]|0[1-9][0-9]|1[0-9]{{2}}|2[0-4][0-9]|25[0-4])', vnet),
            'Choose an exact project team/custom LAN VNet; transit acceptance is separate.')
    lan_id = int(vnet[-3:])
    interface = ipaddress.ip_interface(address)
    require(interface.version == 4 and interface.network == ipaddress.ip_network(f'10.{project_id}.{lan_id}.0/24')
            and 2 <= int(str(interface.ip).split('.')[-1]) <= 100,
            'Probe needs an unused declared static LAN address .2-.100 with /24.')
    require(type(vmid) is int and 100 <= vmid <= 999999999, 'Invalid probe VMID.')
    require(isinstance(job_id, str) and re.fullmatch(r'network-[a-f0-9]{32}', job_id), 'Invalid ownership job ID.')
    return {'project_id': project_id, 'node': node, 'vnet': vnet,
            'address': str(interface), 'vmid': vmid, 'job_id': job_id}


def probe_from_plan(plan, operation, expected):
    require(isinstance(plan, dict) and plan.get('errored', False) is False, 'Plan has errors.')
    changes = plan.get('resource_changes', [])
    require(isinstance(changes, list), 'Invalid plan changes.')
    mutations = [r for r in changes if r.get('change', {}).get('actions') != ['no-op']]
    require(len(mutations) == 1, 'Plan must mutate exactly one owned probe.')
    row = mutations[0]
    require(row.get('address') == PROBE_ADDRESS and row.get('mode') == 'managed'
            and row.get('type') == 'proxmox_virtual_environment_vm'
            and row.get('change', {}).get('actions') == [operation],
            'Plan must contain only the reviewed probe create or delete.')
    value = row['change'].get('after' if operation == 'create' else 'before')
    require(isinstance(value, dict), 'Probe identity must be known.')
    require(value.get('node_name') == expected['node'] and value.get('vm_id') == expected['vmid'],
            'Probe node/VMID mismatch.')
    require(expected['job_id'] in value.get('tags', []) and 'crange-network-probe' in value.get('tags', []),
            'Probe ownership tags missing.')
    require(value.get('template', False) is False, 'A probe cannot be a template.')
    devices = value.get('network_device', [])
    require(len(devices) == 1 and devices[0].get('bridge') == expected['vnet']
            and devices[0].get('enabled', True) is True, 'Probe must have one NIC on the exact VNet.')
    initialization = value.get('initialization', [])
    require(len(initialization) == 1 and len(initialization[0].get('ip_config', [])) == 1,
            'Probe needs exactly one declared IPv4 configuration.')
    ipv4 = initialization[0]['ip_config'][0].get('ipv4', [])
    gateway = str(ipaddress.ip_interface(expected['address']).network.network_address + 1)
    require(len(ipv4) == 1 and ipv4[0].get('address') == expected['address']
            and ipv4[0].get('gateway') == gateway, 'Probe static address/gateway mismatch.')
    return value


def validate_receipt(report, create_plan, delete_plan):
    require(isinstance(report, dict) and report.get('schema_version') == 1, 'Unsupported evidence schema.')
    expected = scope(**report['scope'])
    probe_from_plan(create_plan, 'create', expected)
    probe_from_plan(delete_plan, 'delete', expected)
    require(report.get('zone') == 'crange' and report.get('mtu') == 1450, 'Wrong zone/MTU.')
    require(isinstance(report.get('source_revision'), str) and re.fullmatch(r'[a-f0-9]{40}', report['source_revision']),
            'Record the exact reviewed source commit.')
    require(isinstance(report.get('tested_at'), str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', report['tested_at']),
            'Record a UTC evidence timestamp.')
    checks = report.get('checks', {})
    require(set(checks) == CHECKS, 'All six fixed checks are mandatory.')
    for name, result in checks.items():
        require(isinstance(result, dict) and result.get('passed') is True, f'{name} failed or missing.')
        duration = result.get('duration_seconds')
        require(type(duration) in (int, float) and 0 < duration <= 60, f'{name} must be bounded to 60 seconds.')
        require(isinstance(result.get('artifact_sha256'), str) and re.fullmatch(r'[a-f0-9]{64}', result['artifact_sha256']),
                f'{name} needs retained command-level evidence.')
    cleanup = report.get('cleanup', {})
    for key in ('vmid_absent_before', 'vmid_absent_after', 'probe_state_empty', 'temporary_rules_absent', 'input_unchanged'):
        require(cleanup.get(key) is True, f'Cleanup evidence missing: {key}.')
    require(isinstance(cleanup.get('artifact_sha256'), str) and re.fullmatch(r'[a-f0-9]{64}', cleanup['artifact_sha256']),
            'Cleanup requires retained evidence.')
    return expected


def validate_bundle(directory):
    root = Path(directory).resolve()
    report = json.loads((root / 'receipt.json').read_text())
    plans = [json.loads((root / name).read_text()) for name in ('create-plan.json', 'delete-plan.json')]
    expected = validate_receipt(report, *plans)
    artifacts = dict(report['checks'], cleanup=report['cleanup'])
    for name, result in artifacts.items():
        artifact = root / (name + '.log')
        require(artifact.is_file() and not artifact.is_symlink() and artifact.stat().st_size <= 4 * 1024 * 1024,
                f'{name} evidence must be a regular retained file of at most 4 MiB.')
        require(hashlib.sha256(artifact.read_bytes()).hexdigest() == result['artifact_sha256'],
                f'{name} evidence checksum mismatch.')
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path, help='Reviewed local evidence directory')
    args = parser.parse_args()
    try:
        expected = validate_bundle(args.bundle)
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(1, f'Acceptance rejected: {error}\n')
    print(json.dumps({'validated_scope': expected, 'policy_changed': False}, sort_keys=True))


if __name__ == '__main__':
    main()
