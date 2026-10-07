"""Compact provider labels backed by full durable cache job identities."""
import re


def cache_tag(vmid):
    if type(vmid) is not int or not 100 <= vmid <= 999999999:
        raise ValueError('Invalid cache VMID.')
    return f'crange-cache-{vmid}'


def cache_description(job_id, image_id):
    return f'CRANGE distribution {job_id} image {image_id}'


def cache_owned(config, vmid, job_id):
    """Accept exact legacy jobs or compact labels with full hidden provenance.

    A compact VMID tag alone is insufficient: a recycled ID must not inherit
    ownership from the catalog. The description binds it to its original job.
    """
    if not isinstance(job_id, str) or not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', job_id):
        return False
    tags = set(str(config.get('tags', '')).split(';'))
    if 'crange-image' not in tags:
        return False
    if f'crange-job-{job_id}' in tags:
        return True
    try:
        marker = cache_tag(vmid)
    except ValueError:
        return False
    descriptions = str(config.get('description', '')).splitlines()
    return marker in tags and any(
        line.startswith(f'CRANGE distribution {job_id} image ') or
        line == f'CRANGE verification {job_id}' for line in descriptions)


def cache_tags(source, vmid):
    # Cloning must never retain another cache generation's ownership labels.
    tags = {tag for tag in str(source.get('tags', '')).split(';')
            if tag and not tag.startswith(('crange-job-', 'crange-cache-'))}
    return ';'.join(sorted(tags | {'crange-image', cache_tag(vmid)}))
