"""Identify allocated guests in Terraform output maps after targeted plans.

Unapplied declarations may have computed addresses but a null guest ID. They
must not become Ansible targets, firewall endpoints, or Guacamole connections.
"""
import copy


def has_guest_id(value):
    return (isinstance(value, dict) and type(value.get('id')) is int
            and 100 <= value['id'] <= 999999999)


def complete_metadata(outputs, desired, applied):
    """Fill missing computed metadata only for the exact saved guest generation.

    Targeted Terraform plans can omit placement input overrides for unchanged
    guests. Explicit conflicts remain visible to the callers' identity checks.
    """
    if not all(isinstance(value, dict) for value in (outputs, desired, applied)):
        raise ValueError('Applied guest metadata must be mappings.')
    result = copy.deepcopy(outputs)
    fields = ('image_id', 'node_name', 'template_id', 'template_node', 'datastore_id',
              'clone_mode', 'template_file_id')
    for field, kind in (('scenario_boxes', 'vm'), ('scenario_containers', 'container')):
        for key, guest in (result.get(field, {}).get('value') or {}).items():
            box = desired.get(field, {}).get(key, {})
            record = applied.get(f'{kind}:{key}', {})
            if (not isinstance(record, dict) or not has_guest_id(guest) or type(record.get('guest_id')) is not int
                    or record['guest_id'] != guest['id'] or not box.get('template')
                    or record.get('declared_template') != box['template']
                    or (box.get('image') and box['image'] != record.get('image_id'))):
                continue
            placement = guest.get('placement') or {}
            if not isinstance(placement, dict):
                continue
            for name in fields:
                if placement.get(name) in (None, '') and record.get(name) not in (None, ''):
                    placement[name] = record[name]
            guest['placement'] = placement
    return result
