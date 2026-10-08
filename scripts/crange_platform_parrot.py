"""One-time administrator publication of the fixed Parrot base template.

This operation is deliberately source-specific. It keeps VM 112 unchanged,
captures a disconnected derivative through the normal identity/export/probe
workflow, then publishes only that captured derivative as an immutable image.
It is not exposed through the project-manager API.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
from pathlib import Path

# This admin command may be run by root through the PVE helper. Keep its
# configuration binding on the worker account's protected files in that case.
os.environ.setdefault('CRANGE_DISTRIBUTION_CONFIG', '/home/crange/.config/crange')
os.environ.setdefault('CRANGE_DISTRIBUTION_DATA', '/var/lib/crange-distribution')

from crange_capture import PLATFORM_BASE_SOURCES, capture_vm
from crange_distribution import (DistributionError, FileJournal, SSHVolumeHasher,
                                 catalog_transaction, publish_exported_image)
from crange_scheduling import CONFIG, DATA, atomic_json, read_settings


NODE = 'proxmox4'
SOURCE_ID = 112
JOB_ID = 'parrot-basecapture-20261007-r2'
IMAGE_ID = 'parrot-jumpbox-20261007-v1'
POLICY_PATH = DATA / 'source-policies' / (JOB_ID + '.json')


def source_only_policy(base_policy):
    """Build the minimal, nonpersistent P4 policy for this exact capture."""
    if not isinstance(base_policy, dict):
        raise DistributionError('Image publication requires a readable administrator policy.')
    policy = copy.deepcopy(base_policy)
    policy.update({
        'revision': JOB_ID,
        'enabled': True,
        'allowed_nodes': [NODE],
        'allowed_storages': ['crange-library'],
        'approved_storage': [[NODE, 'crange-library']],
        'approved_backup_storage': [[NODE, 'crange-library']],
        'isolated_bridges': ['crprobe1450'],
        'capture_isolated_bridge': 'crprobe1450',
        'capture_datastore': 'crange-library',
        'capture_backup_storage': 'crange-library',
    })
    return policy


def publish_parrot(api, *, allocated_vmid):
    """Run the fixed Parrot derivative capture and publish its real evidence."""
    if PLATFORM_BASE_SOURCES.get((NODE, SOURCE_ID), {}).get('name') != 'parrot-jump-template':
        raise DistributionError('Fixed Parrot source map does not match the requested publication.')
    base_policy, _ = read_settings()
    policy = source_only_policy(base_policy)
    if POLICY_PATH.exists():
        existing_policy = json.loads(POLICY_PATH.read_text())
        if existing_policy != policy:
            # A prior preallocation attempt used local-lvm and failed the
            # conservative thin-pool commitment gate. Migrate only that exact
            # fixed policy after confirming there was no receipt, reservation,
            # or VM allocation; retain the old protected file for audit.
            legacy = copy.deepcopy(base_policy)
            legacy.update({
                'revision': JOB_ID, 'enabled': True, 'allowed_nodes': [NODE],
                'approved_storage': [[NODE, 'local-lvm']],
                'approved_backup_storage': [[NODE, 'crange-library']],
                'isolated_bridges': ['crprobe1450'],
                'capture_isolated_bridge': 'crprobe1450',
                'capture_datastore': 'local-lvm',
                'capture_backup_storage': 'crange-library',
            })
            receipt = DATA / 'capture-journal' / (JOB_ID + '.json')
            resources = api.request('GET', '/cluster/resources', {'type': 'vm'})
            if (existing_policy != legacy or receipt.exists() or
                    any(int(row.get('vmid', -1)) == allocated_vmid for row in resources)):
                raise DistributionError('Publication policy differs from the fixed source-only migration scope.')
            from crange_placement import PlacementError, ReservationLedger
            ledger = ReservationLedger(DATA / 'reservations.sqlite3')
            try:
                try:
                    ledger.get_job(JOB_ID)
                except PlacementError:
                    pass
                else:
                    raise DistributionError('Prior publication reservation requires reconciliation before policy migration.')
            finally:
                ledger.close()
            backup_path = POLICY_PATH.with_name(POLICY_PATH.name + '.pre-nfs-20261007')
            if backup_path.exists():
                raise DistributionError('Prior source-only policy migration backup already exists.')
            shutil.copy2(POLICY_PATH, backup_path)
            atomic_json(POLICY_PATH, policy)
    else:
        atomic_json(POLICY_PATH, policy)
    if (CONFIG / 'image-catalog.json').exists():
        catalog = json.loads((CONFIG / 'image-catalog.json').read_text())
        if IMAGE_ID in catalog.get('images', {}):
            raise DistributionError('Immutable image ID already exists; do not replace a published version.')
    profile = {'agent_enabled': True, 'init_system': 'systemd'}
    journal = FileJournal(DATA / 'source-journal')
    receipt_path = DATA / 'capture-journal' / (JOB_ID + '.json')
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if (receipt.get('job_id') != JOB_ID or receipt.get('source_id') != SOURCE_ID or
                receipt.get('node') != NODE or receipt.get('template_id') != allocated_vmid or
                receipt.get('state') != 'captured'):
            raise DistributionError('Existing platform receipt is outside the fixed publication-resume scope.')
        derivative_id = int(receipt['template_id'])
        jobs = {'export_job': JOB_ID + '-export', 'probe_job': JOB_ID + '-probe'}
    else:
        derivative_id, jobs = capture_vm(
            api, project=1, node=NODE, source_id=SOURCE_ID,
            name='crange-parrot-base-20261007',
            description='Administrator-owned isolated derivative of Parrot base VM 112; publication job ' + JOB_ID,
            slug='parrot-base-20261007', profile=profile, platform_source=True,
            policy_override=policy, policy_path=POLICY_PATH, job_id=JOB_ID,
            allocated_vmid=allocated_vmid)
    export = json.loads((journal.directory / (jobs['export_job'] + '.json')).read_text())
    probe_path = journal.directory / (jobs['probe_job'] + '.json')
    probe = json.loads(probe_path.read_text())
    if probe.get('state') == 'allocated':
        prior_tasks = probe.get('provider_tasks') or []
        allowed_clone_path = f'/nodes/{NODE}/qemu/{derivative_id}/clone'
        if (probe.get('source_id') != derivative_id or probe.get('source_node') != NODE or
                probe.get('datastore_id') != 'crange-library' or len(prior_tasks) > 1 or
                (prior_tasks and (prior_tasks[0].get('path') != allowed_clone_path or
                    prior_tasks[0].get('state') not in ('running', 'confirmed-terminal') or
                    not re.fullmatch(r'UPID:[^:]+:[0-9A-F]+:[0-9A-F]+:[0-9A-F]+:[^:]+:[^:]+:[^:]*:',
                                     str(prior_tasks[0].get('upid', '')), re.IGNORECASE)))):
            raise DistributionError('Existing Parrot source-probe journal is outside the exact resume scope.')
        from crange_distribution import main as distribution_main
        distribution_main(['--catalog', str(CONFIG / 'image-catalog.json'),
            '--journal-dir', str(journal.directory), '--policy', str(POLICY_PATH),
            'source-probe-resume', '--project', '1', '--source-node', NODE,
            '--source-id', str(derivative_id), '--isolated-bridge', 'crprobe1450',
            '--job-id', jobs['probe_job'], '--identity-record',
            str(journal.directory / (JOB_ID + '-identity.json'))])
        probe = json.loads(probe_path.read_text())
    if (probe.get('state') != 'source-probed' or
            probe.get('verification', {}).get('passed') is not True or
            any(task.get('state') != 'confirmed-terminal' for task in probe.get('provider_tasks', []))):
        raise DistributionError('Parrot source-probe evidence is incomplete; publication stopped.')
    source_fingerprint = export.get('identity_capture', {}).get('source_config_sha256')
    if not re.fullmatch(r'[a-f0-9]{64}', str(source_fingerprint)):
        raise DistributionError('Parrot derivative has no captured source fingerprint.')
    catalog_file = CONFIG / 'image-catalog.json'
    catalog_transaction(catalog_file, lambda current: publish_exported_image(
        current, IMAGE_ID, export, architecture='amd64',
        source_revision='proxmox4-vm112-' + source_fingerprint,
        approved_projects=['*'], datastore_id='local-lvm',
        verification=probe['verification']))
    receipt = json.loads((DATA / 'capture-journal' / (JOB_ID + '.json')).read_text())
    receipt.update(state='published', image_id=IMAGE_ID, derivative_id=derivative_id)
    atomic_json(DATA / 'capture-journal' / (JOB_ID + '.json'), receipt)
    return {'image_id': IMAGE_ID, 'derivative_id': derivative_id,
            'export_job': jobs['export_job'], 'probe_job': jobs['probe_job'],
            'artifact_sha256': export['artifact_sha256'], 'source_fingerprint': source_fingerprint}


def main():
    """Entry point intentionally accepts no user-controlled source parameters."""
    import argparse
    from crange import ProxmoxAPI

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vmid', required=True, type=int,
                        help='Preallocated, reviewed VMID for this exact derivative.')
    args = parser.parse_args()
    result = publish_parrot(ProxmoxAPI(), allocated_vmid=args.vmid)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
