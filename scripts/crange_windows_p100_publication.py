"""Fixed, admin-only resume and publication flow for the sealed P100 image."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from crange_distribution import DistributionError, FileJournal

IMAGE_ID = 'mhl-windows-server-2025-base-p100-v1'
SOURCE_SHA256 = '5e9fdb9337ec631b74100dc1c1f47ffdfb02f8873b32ee6119b49bc7cef27e9d'
EXPORT_JOB = 'p100-sealed-windows-8912-export-r2'
EXPORT_UPID = 'UPID:proxmox4:001CD7DD:1C4D9C31:6AC823C6:vzdump:8912:crange-iac@pve!terraform:'
POLICY_REVISION = 'p100-sealed-windows-8912-20261008'
SOURCE_REVISION = 'p100-sealed-windows-source-config-r1-20261008'
DISPLAY_NAME = 'Windows Server 2025 Desktop Experience'


def paths():
    from crange_scheduling import CONFIG, DATA
    config, data = Path(CONFIG), Path(DATA)
    return config, data, data / 'source-policies' / 'p100-sealed-windows-8912.json'


def _private_json(path: Path):
    st = path.lstat()
    if (path.is_symlink() or not stat.S_ISREG(st.st_mode) or
            st.st_uid not in (0, os.geteuid()) or stat.S_IMODE(st.st_mode) != 0o600):
        raise DistributionError('Protected P100 publication record has unsafe file permissions.')
    return json.loads(path.read_text())


def _read_state():
    config, data, policy_path = paths()
    journal_dir = data / 'source-journal'
    capture_path = journal_dir / 'p100-sealed-windows-8912.json'
    conversion_path = journal_dir / 'p100-sealed-windows-8912-template-conversion.json'
    export_path = journal_dir / (EXPORT_JOB + '.json')
    state = {'available': False, 'label': DISPLAY_NAME,
             'alias': 'mhl-server2025-desktop-experience',
             'reason': 'The protected sealed-source capture has not been recorded.'}
    try:
        capture = _private_json(capture_path)
        from crange_catalog import validate_sealed_windows_capture
        validate_sealed_windows_capture(capture)
        if capture.get('project') != 100 or capture.get('source_config_sha256') != SOURCE_SHA256:
            raise DistributionError('The protected source record does not match the approved P100 image.')
        conversion = _private_json(conversion_path)
        capture_sha = hashlib.sha256(capture_path.read_bytes()).hexdigest()
        if (conversion.get('state') != 'template-ready' or
                conversion.get('source_capture_sha256') != capture_sha or
                conversion.get('source_config_sha256') != SOURCE_SHA256):
            raise DistributionError('The existing template conversion has not reached its verified ready state.')
        export = _private_json(export_path)
        if (export.get('job_id') != EXPORT_JOB or export.get('node') != 'proxmox4' or
                export.get('kind') != 'vm' or export.get('source_id') != 8912 or
                export.get('identity_capture') != capture):
            raise DistributionError('The recorded export is outside the fixed P100 source scope.')
        upid = export.get('upid')
        if upid != EXPORT_UPID:
            raise DistributionError('The export journal does not contain the exact already verified P100 backup task.')
        if export.get('job_id') != EXPORT_JOB:
            raise DistributionError('The export journal is not the exact reviewed P100 resume record.')
        policy = _private_json(policy_path)
        approved_pairs = policy.get('approved_backup_storage')
        permitted_pairs = [['proxmox1', 'crange-library'],
                           ['proxmox2', 'crange-library'],
                           ['proxmox4', 'crange-library']]
        if (policy.get('revision') != POLICY_REVISION or
                not isinstance(approved_pairs, list) or
                ['proxmox4', 'crange-library'] not in approved_pairs or
                any(pair not in permitted_pairs for pair in approved_pairs)):
            raise DistributionError('The protected P100 export policy does not match its reviewed revision and approved storage scope.')
        catalog_path = config / 'image-catalog.json'
        catalog = json.loads(catalog_path.read_text()) if catalog_path.is_file() else {'images': {}}
        from crange_catalog import validate_catalog
        validate_catalog(catalog)
        existing = catalog.get('images', {}).get(IMAGE_ID)
        if existing:
            expected_capture = capture
            if (existing.get('artifact_sha256') != export.get('artifact_sha256') or
                    existing.get('identity_capture') != expected_capture or
                    existing.get('approved_projects') != [100] or
                    existing.get('windows_profile') != 'mhl-server2025-desktop-experience' or
                    existing.get('display_name') != DISPLAY_NAME):
                raise DistributionError('The immutable P100 image ID already exists with different publication evidence; stop for review.')
            state.update(available=False, published=True,
                         image_id=IMAGE_ID,
                         artifact_sha256=existing['artifact_sha256'],
                         reason='Published and approved for Project 100. SHA-256: ' + existing['artifact_sha256'])
            return state
        if export.get('state') == 'exported':
            if (not export.get('complete_capture') or not export.get('verified') or
                    not re.fullmatch(r'[a-f0-9]{64}', str(export.get('artifact_sha256', '')))):
                raise DistributionError('The recorded export is missing its verified artifact checksum.')
            detail = 'The recorded archive and SHA-256 will be rechecked before catalog publication.'
        elif export.get('state') == 'exporting':
            detail = 'A prior vzdump task is recorded. The publish action will resume that exact task and will not submit another backup.'
        else:
            raise DistributionError('The recorded export is not in a resumable or verified state.')
        state.update(available=True, reason=detail, source='Frozen P100 source 8912',
                     conversion='Template conversion verified', export=detail,
                     project=100, image_id=IMAGE_ID)
    except (OSError, ValueError, TypeError, KeyError, DistributionError) as exc:
        state['reason'] = str(exc) or 'Protected publication records could not be verified.'
    return state


def status():
    """Return only sanitized readiness information for the admin template page."""
    return _read_state()


def publish():
    """Resume the already journaled export and publish its verified artifact."""
    state = _read_state()
    if not state['available']:
        raise DistributionError(state['reason'])
    config, data, policy_path = paths()
    journal = FileJournal(data / 'source-journal')
    catalog = config / 'image-catalog.json'
    export_path = journal.directory / (EXPORT_JOB + '.json')
    from crange_distribution import main as distribution_main
    common = ['--catalog', str(catalog), '--journal-dir', str(journal.directory),
              '--policy', str(policy_path)]
    distribution_main(common + ['export', '--node', 'proxmox4', '--kind', 'vm',
        '--source-id', '8912', '--storage', 'crange-library', '--job-id', EXPORT_JOB,
        '--identity-record', str(journal.directory / 'p100-sealed-windows-8912.json')])
    # The publication command independently derives Windows verification from
    # the protected clone proofs and revalidates artifact provenance/checksum.
    distribution_main(common + ['publish', '--export-record', str(export_path),
        '--image-id', IMAGE_ID, '--architecture', 'amd64',
        '--source-revision', SOURCE_REVISION,
        '--project', '100', '--datastore-id', 'crange-library'])
    print(f'Published {DISPLAY_NAME} ({IMAGE_ID}) for Project 100.')
