#!/usr/bin/env python3
"""Regenerate guest SSH host keys when a captured container becomes a new instance."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile


def ensure_identity(root=Path('/'), runner=subprocess.run):
    def read(relative):
        try:
            return (root / relative).read_text().strip()
        except FileNotFoundError:
            return ''
    identity = {'machine_id': read('etc/machine-id'), 'hostname': socket.gethostname(),
                'mac': read('sys/class/net/eth0/address')}
    if not identity['machine_id'] and not identity['mac']:
        raise RuntimeError('Guest identity is unavailable; refusing to rotate host keys.')
    marker = root / 'var/lib/crange/ssh-identity.json'
    try:
        previous = json.loads(marker.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        previous = None
    rotated = previous != identity
    directory = root / 'etc/ssh'
    keys = list(directory.glob('ssh_host_*_key'))
    before = {key.name for key in keys}
    if rotated:
        for key in keys:
            key.unlink(missing_ok=True)
            Path(str(key) + '.pub').unlink(missing_ok=True)
    runner(['/usr/bin/ssh-keygen', '-A'], check=True, capture_output=True, text=True)
    generated = bool({key.name for key in directory.glob('ssh_host_*_key')} - before)
    marker.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.ssh-identity.', dir=marker.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(identity, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {'rotated': rotated, 'generated': generated}


if __name__ == '__main__':
    try:
        if os.geteuid() != 0:
            raise RuntimeError('Run the guest identity helper as root.')
        print(json.dumps(ensure_identity()))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SystemExit(f'crange: Guest SSH identity setup failed: {exc}')
