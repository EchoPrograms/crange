"""Keep reusable OS bases separate from immutable scenario release images."""
import re


def is_release_template(alias, reference=None):
    reference = reference if isinstance(reference, dict) else {}
    return bool(reference.get('release_id') or reference.get('purpose') == 'release'
                or any(re.match(r'^p[0-9]{1,3}-image-', str(value))
                       for value in (alias, reference.get('image_id', ''))))


def creation_templates(approved):
    return {key: value for key, value in approved.items()
            if not is_release_template(key, value) and not (value.get('superseded_by') in approved
                and approved[value['superseded_by']].get('image_id'))}


def template_choices(approved):
    """One selectable alias per identical base image and hardware profile."""
    import json
    result, seen = {}, set()
    for alias, reference in creation_templates(approved).items():
        identity = reference.get('image_id') or (
            (reference.get('template_node'), reference['template_id'])
            if reference.get('template_id') is not None else alias)
        marker = (str(identity), json.dumps(reference.get('profile', {}), sort_keys=True))
        if marker not in seen:
            result[alias] = reference
            seen.add(marker)
    return result
