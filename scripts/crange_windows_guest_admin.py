"""Worker-private, per-guest Windows administrator credentials.

The returned reference contains only project and desired box identity. The
credential file is controller-local and is never part of project state or Git.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile


SCHEMA_VERSION = 1
BOX_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
IMAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
USERNAME = re.compile(r"^[A-Za-z0-9_.@\\-]{1,256}$")


def _project(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 255:
        raise ValueError("Invalid Windows credential project.")
    return value


def _box_key(value):
    if not isinstance(value, str) or not BOX_KEY.fullmatch(value):
        raise ValueError("Invalid Windows credential box key.")
    return value


def _guest_id(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 999999999:
        raise ValueError("Invalid Windows guest ID.")
    return value


def _image_id(value):
    if not isinstance(value, str) or not IMAGE_ID.fullmatch(value):
        raise ValueError("Invalid approved Windows image identity.")
    return value


def _username(value):
    if not isinstance(value, str) or not USERNAME.fullmatch(value):
        raise ValueError("Invalid Windows administrator account name.")
    return value


def _password(value):
    if not isinstance(value, str) or not 16 <= len(value) <= 1024 or any(ord(c) < 32 for c in value):
        raise ValueError("Windows administrator passwords must contain 16–1024 printable characters.")
    return value


def _reference_key(project_id, box_key):
    return f"{_project(project_id)}:{_box_key(box_key)}"


def reference(project_id, box_key):
    """Return the non-secret binding carried by trusted worker metadata."""
    return {"project_id": _project(project_id), "box_key": _box_key(box_key)}


def store_path():
    configured = os.environ.get("CRANGE_WINDOWS_GUEST_ADMIN_FILE")
    return Path(configured) if configured else Path.home() / ".config/crange/windows-guest-admin.json"


def _check_owner_mode(path, expected_mode, description):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) and description == "file":
        raise ValueError("Windows credential store must be a regular file.")
    if description == "directory" and not stat.S_ISDIR(info.st_mode):
        raise ValueError("Windows credential directory is not a directory.")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != expected_mode:
        raise ValueError(f"Windows credential {description} must be owned by the worker with mode {expected_mode:04o}.")


def _directory(path):
    parent = path.parent
    if not parent.exists():
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(parent, 0o700)
    _check_owner_mode(parent, 0o700, "directory")
    return parent


def _open_lock(path, *, exclusive):
    flags = os.O_CREAT | (os.O_RDWR if exclusive else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        os.close(fd)
        raise ValueError("Windows credential lock must be owned by the worker with mode 0600.")
    return os.fdopen(fd, "r+" if exclusive else "r")


def _read(path):
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "records": {}}
    _check_owner_mode(path, 0o600, "file")
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Windows credential store is unreadable or malformed.") from exc
    if (not isinstance(data, dict) or set(data) != {"schema_version", "records"}
            or data["schema_version"] != SCHEMA_VERSION or not isinstance(data["records"], dict)):
        raise ValueError("Windows credential store has an unsupported format.")
    return data


def _write_atomic(path, data):
    payload = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=".windows-guest-admin.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def store(project_id, box_key, guest_id, image_id, username, password):
    """Store or rotate one credential without changing other guest records."""
    project_id = _project(project_id)
    box_key = _box_key(box_key)
    guest_id = _guest_id(guest_id)
    image_id = _image_id(image_id)
    username = _username(username)
    password = _password(password)
    path = store_path()
    _directory(path)
    if path.is_symlink():
        raise ValueError("Windows credential store cannot be a symbolic link.")
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _open_lock(lock_path, exclusive=True) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read(path)
        key = _reference_key(project_id, box_key)
        data["records"][key] = {
            "project_id": project_id,
            "box_key": box_key,
            "guest_id": guest_id,
            "image_id": image_id,
            "username": username,
            "password": password,
        }
        _write_atomic(path, data)
    return reference(project_id, box_key)


def resolve(ref, *, project_id, box_key, guest_id, image_id):
    """Resolve only when caller's live identity exactly matches the record."""
    project_id = _project(project_id)
    box_key = _box_key(box_key)
    guest_id = _guest_id(guest_id)
    image_id = _image_id(image_id)
    if not isinstance(ref, dict) or set(ref) != {"project_id", "box_key"}:
        raise ValueError("Invalid Windows credential reference.")
    try:
        ref_identity = reference(ref.get("project_id"), ref.get("box_key"))
    except ValueError as exc:
        raise ValueError("Invalid Windows credential reference.") from exc
    if ref_identity != reference(project_id, box_key):
        raise ValueError("Windows credential reference does not match this project box.")
    path = store_path()
    parent = _directory(path)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _open_lock(lock_path, exclusive=False) as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        data = _read(path)
    record = data["records"].get(_reference_key(project_id, box_key))
    expected = {"project_id": project_id, "box_key": box_key,
                "guest_id": guest_id, "image_id": image_id}
    if not isinstance(record, dict) or any(record.get(key) != value for key, value in expected.items()):
        raise ValueError("Windows credential ownership does not match the deployed guest and image.")
    return {"username": _username(record.get("username")),
            "password": _password(record.get("password"))}
