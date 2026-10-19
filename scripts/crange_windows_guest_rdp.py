"""Worker-private credentials for the separate Windows Guacamole RDP account."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import string
import tempfile


SCHEMA_VERSION = 1
BOX_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
IMAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _identity(project_id, box_key, guest_id, image_id):
    if type(project_id) is not int or not 1 <= project_id <= 255:
        raise ValueError("Invalid Windows RDP credential project.")
    if not isinstance(box_key, str) or not BOX_KEY.fullmatch(box_key):
        raise ValueError("Invalid Windows RDP credential box key.")
    if type(guest_id) is not int or not 100 <= guest_id <= 999999999:
        raise ValueError("Invalid Windows RDP credential guest ID.")
    if not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id):
        raise ValueError("Invalid Windows RDP approved image identity.")
    return project_id, box_key, guest_id, image_id


def reference(project_id, box_key, guest_id, image_id):
    project_id, box_key, guest_id, image_id = _identity(project_id, box_key, guest_id, image_id)
    return {"type": "crange-windows-rdp-v1", "project_id": project_id,
            "box_key": box_key, "guest_id": guest_id, "image_id": image_id}


def _password():
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    chars = [secrets.choice(string.ascii_lowercase), secrets.choice(string.ascii_uppercase),
             secrets.choice(string.digits), secrets.choice("!@#$%^&*()-_=+")]
    chars.extend(secrets.choice(alphabet) for _ in range(28))
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def store_path():
    configured = os.environ.get("CRANGE_WINDOWS_GUEST_RDP_FILE")
    return Path(configured) if configured else Path.home() / ".config/crange/windows-guest-rdp.json"


def _check(path, expected_mode, directory=False):
    info = path.lstat()
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not kind_ok or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != expected_mode:
        what = "directory" if directory else "file"
        raise ValueError(f"Windows RDP credential {what} must be worker-owned mode {expected_mode:04o}.")


def _open_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        os.close(fd)
        raise ValueError("Windows RDP credential lock must be worker-owned mode 0600.")
    return os.fdopen(fd, "r+")


def _load(path):
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "records": {}}
    _check(path, 0o600)
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise ValueError("Windows RDP credential store is unreadable or malformed.") from None
    if (not isinstance(value, dict) or set(value) != {"schema_version", "records"}
            or value["schema_version"] != SCHEMA_VERSION or not isinstance(value["records"], dict)):
        raise ValueError("Windows RDP credential store has an unsupported format.")
    return value


def _write(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".windows-rdp.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _records():
    path = store_path()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("Windows RDP credential directory cannot be a symbolic link.")
    _check(path.parent, 0o700, directory=True)
    if path.is_symlink():
        raise ValueError("Windows RDP credential store cannot be a symbolic link.")
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _open_lock(lock_path) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return path, _load(path)


def store(project_id, box_key, guest_id, image_id, password, *, show_credentials=False):
    identity = _identity(project_id, box_key, guest_id, image_id)
    if not isinstance(password, str) or len(password) < 16 or any(ord(c) < 32 for c in password):
        raise ValueError("Windows RDP passwords must contain at least 16 printable characters.")
    if not isinstance(show_credentials, bool):
        raise ValueError("Windows RDP credential disclosure policy must be boolean.")
    path, data = _records()
    key = ":".join(str(value) for value in identity)
    next_record = {"project_id": identity[0], "box_key": identity[1], "guest_id": identity[2],
                   "image_id": identity[3], "username": "user", "password": password,
                   "show_credentials": show_credentials}
    with _open_lock(path.with_suffix(path.suffix + ".lock")) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _load(path)
        old = data["records"].get(key)
        if old is not None and old != next_record:
            raise ValueError("Windows RDP credential is already bound to another image or secret.")
        data["records"][key] = next_record
        _write(path, data)
    return reference(*identity)


def resolve(ref, *, project_id, box_key, guest_id, image_id):
    expected_ref = reference(project_id, box_key, guest_id, image_id)
    if ref != expected_ref or not isinstance(ref, dict) or set(ref) != set(expected_ref):
        raise ValueError("Windows RDP credential reference does not match the applied guest.")
    path, data = _records()
    record = data["records"].get(":".join(str(value) for value in _identity(
        project_id, box_key, guest_id, image_id)))
    expected = {"project_id": project_id, "box_key": box_key, "guest_id": guest_id, "image_id": image_id}
    if not isinstance(record, dict) or any(record.get(key) != value for key, value in expected.items()):
        raise ValueError("Windows RDP credential ownership does not match the applied guest and image.")
    username, password, disclose = record.get("username"), record.get("password"), record.get("show_credentials")
    if (username != "user" or not isinstance(password, str) or len(password) < 16
            or any(ord(c) < 32 for c in password) or not isinstance(disclose, bool)):
        raise ValueError("Windows RDP credential record is malformed.")
    return {"username": username, "password": password, "show_credentials": disclose}
