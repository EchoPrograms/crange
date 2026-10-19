"""Immutable protected Guacamole guest credentials, referenced without secrets."""
import hashlib
import base64
import json
import os
from pathlib import Path
import re


def reference(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('Invalid guest credential reference.')
    return value


def validate(value):
    if not isinstance(value, dict) or set(value) != {'username', 'password', 'show_credentials'}:
        raise ValueError('Invalid guest connection credentials.')
    for key, minimum, maximum in (('username', 1, 256), ('password', 1, 1024)):
        text = value[key]
        if not isinstance(text, str) or not minimum <= len(text) <= maximum or any(ord(c) < 32 for c in text):
            raise ValueError(f'Invalid guest {key}. Use {minimum}–{maximum} characters without control characters.')
    if not isinstance(value['show_credentials'], bool):
        raise ValueError('Invalid credential disclosure setting.')
    return value


def binding(ref):
    return 'guest-' + reference(ref)


def directory():
    return Path(os.environ.get('CRANGE_GUEST_CREDENTIALS_DIR', Path.home() / '.config/crange/guest-credentials'))


def secret():
    return Path(os.environ.get('CRANGE_GUAC_JSON_KEY', Path.home() / '.config/crange/guacamole-json.secret')).read_text().strip()


def seal(value, key, project, ref):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    value = validate(value)
    nonce = os.urandom(12)
    derived = hashlib.sha256(bytes.fromhex(key) + b'crange-guest-credentials').digest()
    associated = f'{int(project)}:{reference(ref)}'.encode()
    return base64.urlsafe_b64encode(nonce + AESGCM(derived).encrypt(nonce, json.dumps(value).encode(), associated)).decode()


def unseal(value, key, project, ref):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        raw = base64.urlsafe_b64decode(value)
        derived = hashlib.sha256(bytes.fromhex(key) + b'crange-guest-credentials').digest()
        associated = f'{int(project)}:{reference(ref)}'.encode()
        return validate(json.loads(AESGCM(derived).decrypt(raw[:12], raw[12:], associated)))
    except Exception as exc:
        raise ValueError('Cannot read protected guest credentials.') from exc


def install(project, ref, sealed):
    """Authenticate the encrypted record before storing it outside the repository."""
    ref = reference(ref)
    try:
        unseal(sealed, secret(), project, ref)
    except (OSError, ValueError) as exc:
        raise ValueError('Cannot validate protected guest credentials.') from exc
    directory().mkdir(parents=True, exist_ok=True, mode=0o700)
    directory().chmod(0o700)
    parent = directory() / f'project-{int(project):03d}'
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent.chmod(0o700)
    target = parent / f'{ref}.json'
    payload = json.dumps({'project_id': int(project), 'reference': ref, 'sealed_credentials': sealed}) + '\n'
    # References are immutable: a release may still use the earlier record.
    if target.exists():
        if target.read_text() != payload:
            raise ValueError('Guest credential reference already exists.')
        return
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def resolve(record):
    ref = record.get('credential_ref')
    if not ref:
        return None
    if isinstance(ref, dict) and ref.get('type') == 'crange-windows-rdp-v1':
        from crange_windows_guest_rdp import resolve as resolve_windows_rdp
        value = resolve_windows_rdp(
            ref, project_id=record.get('project_id'), box_key=record.get('crange_box_key'),
            guest_id=record.get('crange_guest_id'), image_id=record.get('crange_image_id'))
        image_id=record.get('crange_image_id','')
        if image_id.startswith(f"p{record.get('project_id'):03d}-image-"):
            from crange_scheduling import read_settings
            image=read_settings()[1].get('images',{}).get(image_id,{})
            identity=image.get('source_identity',{})
            if (identity.get('identity_mode')=='windows-scenario-snapshot-v1' and
                    identity.get('snapshot_project_id')==record.get('project_id') and
                    image.get('verified') is True and identity.get('domain_role',0)>=4):
                from crange_windows_snapshot import validate_captured_identity
                validate_captured_identity(identity)
                value={**value,'username':value['username']+'@'+identity['domain_name']}
        return value
    if isinstance(ref, dict):
        raise ValueError('Unsupported typed guest credential reference.')
    project = record.get('project_id')
    if isinstance(project, bool) or not isinstance(project, int) or not 1 <= project <= 255:
        raise ValueError('Invalid guest credential project.')
    try:
        data = json.loads((directory() / f'project-{project:03d}' / f'{reference(ref)}.json').read_text())
        if data['project_id'] != project or data['reference'] != ref:
            raise ValueError('Credential ownership mismatch.')
        return unseal(data['sealed_credentials'], secret(), project, ref)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError('Protected guest credentials are unavailable; ask the project manager to restore them.') from exc
