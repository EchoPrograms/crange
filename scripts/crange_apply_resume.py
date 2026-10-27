"""Finish access setup from a completed apply receipt, without another apply."""
import hashlib
import hmac
import json
from datetime import datetime, timezone


def resume(c, args, path, manifest, record):
    if record.get('project') != int(args.project) or record.get('plan_id') != args.plan_id:
        raise c.CrangeError('Saved plan belongs to another project.')
    if not record.get('applied_at'):
        raise c.CrangeError('Infrastructure has no completed apply receipt.')
    if any(record.get(key) for key in ('bootstrap', 'network_foundation', 'team_foundation', 'release_teams')):
        raise c.CrangeError('Use this deployment type’s configuration workflow to finish setup.')
    changes = record.get('changes', [])
    if any(row.get('actions') not in (['create'], ['update'], ['read'], ['no-op']) for row in changes):
        raise c.CrangeError('Configuration recovery requires a guest creation/update receipt without deletions.')
    config = path / 'crange.auto.tfvars.json'
    if not config.exists():
        config = path / 'terraform.tfvars'
    actual = hashlib.sha256(config.read_bytes()).hexdigest()
    if not hmac.compare_digest(actual, record.get('inputs_sha256', '')):
        raise c.CrangeError('Project inputs changed after deployment; review the current changes first.')
    if not hmac.compare_digest(c.automation_inputs_digest(path), record.get('automation_inputs_sha256', '')):
        raise c.CrangeError('Template approvals or Terraform source changed after deployment; review first.')
    detail = {'resource_changes': [
        {'address': row['address'], 'change': {'actions': row['actions']}}
        for row in changes]}
    print('Infrastructure already applied; finishing configuration for the reviewed guests.', flush=True)
    cfg = c.json_config(path)
    from crange_apply_scope import guest_key
    keys = {guest_key(change.get('address', '')) for change in changes
            if change.get('actions') != ['no-op']}
    disk_keys = ['vm:' + key for key, box in cfg.get('scenario_boxes', {}).items()
                 if box.get('disk_gb') is not None and 'vm:' + key in keys]
    if disk_keys:
        from crange_vm_disk_grow import initialize_selected
        initialize_selected(path, cfg, resource_keys=disk_keys,
                            team='254' if getattr(args, 'staging_only', False) else None)
    if getattr(args, 'staging_only', False):
        from crange_change_state import record_applied_definitions
        record_applied_definitions(path, cfg, resource_keys=None, scope='254')
    c._configure_applied_access(args, detail)
    record['configuration_completed_at'] = datetime.now(timezone.utc).isoformat()
    from crange_scheduling import atomic_json
    atomic_json(manifest, record, 0o600)
    summary = path / '.crange-plans' / (args.plan_id + '.json')
    if summary.exists():
        value = json.loads(summary.read_text())
        value['configuration_completed_at'] = record['configuration_completed_at']
        atomic_json(summary, value, 0o640)
    print('Reviewed guest access configuration completed; no Terraform apply was repeated.', flush=True)
