"""Immutable central archived restore points, separate from native snapshots.

A vzdump archive captures one selected guest state and configuration. It does not
preserve a guest's complete native LVM/QCOW2 snapshot tree or backing dependencies.
"""
import copy
import re


class CheckpointError(ValueError):
    pass


def checkpoint_manifest(*, checkpoint_id, project_id, team_id, guest,
                        image_id, artifact, timestamp, capture_mode,
                        quiesce, owner_job, restore_verification):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', checkpoint_id):
        raise CheckpointError('Invalid immutable checkpoint ID.')
    if (isinstance(project_id, bool) or not isinstance(project_id, int) or
            project_id not in range(1, 256) or project_id in (60, 61) or
            isinstance(team_id, bool) or not isinstance(team_id, int) or team_id not in range(0, 254)):
        raise CheckpointError('Invalid checkpoint project/team ownership.')
    if guest.get('kind') not in ('vm', 'container') or not guest.get('node') or not isinstance(guest.get('vmid'), int):
        raise CheckpointError('Actual applied guest identity is required.')
    if (not artifact.get('volume_id', '').startswith('crange-library:backup/') or
            not re.fullmatch(r'[a-f0-9]{64}', artifact.get('artifact_sha256', '')) or
            not artifact.get('checksum_verified') or not artifact.get('complete_capture')):
        raise CheckpointError('Complete checksum-verified central backup artifact required.')
    if capture_mode not in ('stop', 'snapshot', 'suspend') or not isinstance(quiesce, dict):
        raise CheckpointError('Actual capture/quiesce metadata is required.')
    if not image_id or not timestamp or not owner_job:
        raise CheckpointError('Logical image, creation time and owning worker job are required.')
    if not isinstance(restore_verification, dict) or restore_verification.get('state') not in ('pending', 'passed', 'failed'):
        raise CheckpointError('Restore verification state must be explicit.')
    if restore_verification['state'] == 'passed' and not restore_verification.get('evidence'):
        raise CheckpointError('Passing restore verification requires real evidence.')
    return {'schema_version': 1, 'checkpoint_id': checkpoint_id,
            'project_id': project_id, 'team_id': team_id,
            'network_id': 254 if team_id == 0 else team_id,
            'guest': copy.deepcopy(guest), 'image_id': image_id,
            'artifact': copy.deepcopy(artifact), 'created_at': timestamp,
            'capture_mode': capture_mode, 'quiesce': copy.deepcopy(quiesce),
            'owner_job': owner_job, 'restore_verification': copy.deepcopy(restore_verification),
            'native_snapshot_tree_preserved': False, 'type': 'archived-restore-point'}


def publish_checkpoint(manifests, manifest):
    identifier = manifest['checkpoint_id']
    if identifier in manifests:
        raise CheckpointError('Checkpoint IDs are immutable; publish a new restore point.')
    result = copy.deepcopy(manifests)
    result[identifier] = copy.deepcopy(manifest)
    return result
