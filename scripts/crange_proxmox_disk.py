"""Read Proxmox disk properties in shorthand or named-volume API form."""
import math
import re


def managed_volume(value):
    tokens = str(value).split(',')
    volumes = [token for token in tokens if token and '=' not in token]
    volumes += [token.split('=', 1)[1] for token in tokens if token.startswith(('volume=', 'file='))]
    if len(volumes) != 1:
        raise ValueError('Disk volume is missing or ambiguous.')
    volume = volumes[0]
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+:[A-Za-z0-9_/.-]+', volume)
            or '..' in volume.split(':', 1)[1].split('/')):
        raise ValueError('Disk is not a managed Proxmox storage volume.')
    return volume


def size_bytes(value):
    sizes = [part.removeprefix('size=') for part in str(value).split(',') if part.startswith('size=')]
    if len(sizes) != 1:
        raise ValueError('Disk size is missing or ambiguous.')
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)([KMGT]?)(?:i?B)?', sizes[0], re.I)
    if not match:
        raise ValueError('Disk size is invalid.')
    number = float(match[1]) * {'':1, 'K':2**10, 'M':2**20, 'G':2**30, 'T':2**40}[match[2].upper()]
    if not math.isfinite(number) or number <= 0:
        raise ValueError('Disk size must be positive and finite.')
    return number
