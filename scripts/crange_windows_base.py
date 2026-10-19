"""Fixed QEMU Guest Agent preparation and Sysprep for owned Windows builds.

This workflow accepts only a build ID from the template-build ledger. It never
accepts a guest ID, script, path, or credential from the dashboard.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time

from crange import ProxmoxAPI


class WindowsBaseError(ValueError):
    pass


ROOT = Path(__file__).resolve().parents[1]
ID = re.compile(r"^[0-9a-f]{32}$")
FORBIDDEN_TAGS = {"mhl-windows-sealed", "mhl-windows-sealed-locale", "crange-image"}
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
PRIVATE = Path.home() / ".config/crange/windows-base-builds"


def _journal_dir() -> Path:
    PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = PRIVATE.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise WindowsBaseError("Windows base journal directory must be worker-owned mode 0700.")
    return PRIVATE


def _journal_path(build_id: str) -> Path:
    return _journal_dir() / (build_id + ".json")


def _save(path: Path, value: dict) -> None:
    tmp = path.with_name("." + path.name + "." + secrets.token_hex(8) + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp.exists():
            tmp.unlink()


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise WindowsBaseError("Windows base journal must be worker-owned mode 0600.")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise WindowsBaseError("Windows base journal is unreadable or malformed.") from None
    if not isinstance(value, dict):
        raise WindowsBaseError("Windows base journal is malformed.")
    return value


def _lock(path: Path):
    lock_path = path.with_suffix(".json.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        os.close(fd)
        raise WindowsBaseError("Windows base journal lock must be worker-owned mode 0600.")
    return os.fdopen(fd, "r+")


def _private_answerfile(record: dict) -> dict:
    build_id, vmid = record.get("id"), record.get("vmid")
    binding = record.get("private_answerfile")
    if (not isinstance(binding, dict) or binding.get("build_id") != build_id
            or binding.get("vmid") != vmid
            or binding.get("volid") != f"local:iso/crange-win-{build_id}.iso"
            or not re.fullmatch(r"[0-9a-f]{64}", str(binding.get("sha256", "")))):
        raise WindowsBaseError(
            "This Windows build is ineligible: create a fresh owned build with its own worker-generated local answerfile. "
            "The current generic builder does not provide per-build private answerfile metadata."
        )
    secret_dir = Path.home() / ".config/crange/windows-build-secrets"
    try:
        dir_info = secret_dir.lstat()
        if (not stat.S_ISDIR(dir_info.st_mode) or dir_info.st_uid != os.geteuid()
                or stat.S_IMODE(dir_info.st_mode) != 0o700):
            raise WindowsBaseError("Windows build credential directory must be worker-owned mode 0700.")
    except OSError:
        raise WindowsBaseError("This Windows build is ineligible: its private installer credential binding is unavailable.") from None
    secret_path = secret_dir / (build_id + ".json")
    try:
        info = secret_path.lstat()
        secret = json.loads(secret_path.read_text())
    except (OSError, ValueError):
        raise WindowsBaseError("This Windows build is ineligible: its private installer credential binding is unavailable.") from None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
            or not isinstance(secret, dict) or secret.get("build_id") != build_id
            or secret.get("vmid") != vmid or secret.get("answerfile_sha256") != binding["sha256"]
            or not isinstance(secret.get("installer_admin_password"), str)
            or len(secret["installer_admin_password"]) < 20):
        raise WindowsBaseError("This Windows build is ineligible: its private installer credential binding does not match the build.")
    return binding


def _identity(record: dict, api: ProxmoxAPI) -> tuple[dict, str]:
    import crange_templates
    build_id = record.get("id")
    if not isinstance(build_id, str) or not ID.fullmatch(build_id):
        raise WindowsBaseError("Choose an installer build from the Windows Templates list.")
    if record.get("status") not in ("installing", "windows-base-prepared") or record.get("reference"):
        raise WindowsBaseError("This build is not an unpublished Windows installer build.")
    definition = record.get("definition")
    if not isinstance(definition, dict) or definition.get("os") != "windows":
        raise WindowsBaseError("Choose an owned Windows installer build.")
    node, vmid = definition.get("node"), record.get("vmid")
    if not isinstance(node, str) or not re.fullmatch(r"proxmox[1-6]", node) or type(vmid) is not int or vmid < 100:
        raise WindowsBaseError("Windows build ledger identity is malformed.")
    try:
        actual = crange_templates.owned(api, record)
        status = api.status(node, vmid)
    except Exception as exc:
        raise WindowsBaseError("Windows build is missing or live ownership could not be verified.") from exc
    tags = set(str(actual.get("tags", "")).split(";"))
    if (actual.get("name") != record.get("guest_name")
            or "crange-template-build" not in tags
            or record.get("ownership_tag") not in tags
            or tags & FORBIDDEN_TAGS
            or int(actual.get("template", 0) or 0) != 0):
        raise WindowsBaseError("Refusing a sealed, published, or identity-mismatched Windows VM; use a fresh owned build.")
    _private_answerfile(record)
    if not definition.get("windows_unattended"):
        raise WindowsBaseError("Windows base preparation requires a fresh unattended Windows installer build.")
    agent_values = set(str(actual.get("agent", "")).split(","))
    if not ({"1", "enabled=1"} & agent_values):
        raise WindowsBaseError("Enable the QEMU Guest Agent in the owned Windows build before preparation.")
    if status not in ("running", "stopped"):
        raise WindowsBaseError("Windows build is in an unsupported Proxmox state.")
    # Restrict the builder to its private installation network; never run a base
    # preparation script against a project LAN guest.
    nics = [actual.get(key, "") for key in actual if re.fullmatch(r"net[0-9]+", key)]
    if not nics or any("bridge=craccess" not in str(value) for value in nics):
        raise WindowsBaseError("Windows build must remain attached only to the approved installer network.")
    # Preparation removes the generated answerfile ISO after its setup script
    # succeeds, so this expected detachable device is excluded from identity.
    identity_config = {key: value for key, value in actual.items() if key not in ("ide0", "digest")}
    digest = hashlib.sha256(json.dumps(identity_config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"build_id": build_id, "vmid": vmid, "node": node, "guest_name": record["guest_name"],
            "status": status, "config_sha256": digest, "answerfile_sha256": record["private_answerfile"]["sha256"]}, digest


def _record_for(build_id: str) -> dict:
    import crange_templates
    try:
        record = crange_templates.load(build_id)
    except (OSError, ValueError, KeyError):
        raise WindowsBaseError("Choose an existing installer build from the Templates ledger.") from None
    if record.get("id") != build_id:
        raise WindowsBaseError("Installer build ID does not match its ledger record.")
    return record


def _require_windows_agent(api, identity):
    try:
        response = api.request('GET', f'/nodes/{identity["node"]}/qemu/{identity["vmid"]}/agent/get-osinfo')
        info = response.get('result', response) if isinstance(response, dict) else {}
        if not isinstance(info, dict) or info.get('id') != 'mswindows':
            raise ValueError('Windows guest agent is not ready.')
    except Exception:
        raise WindowsBaseError('Windows installation is not ready or its guest agent is unavailable; finish installation before preparation or sealing.') from None


def eligibility(record: dict, api: ProxmoxAPI | None = None) -> dict:
    """Return a public, secret-free eligibility summary for the Templates UI."""
    try:
        if not isinstance(record.get("definition"), dict) or record["definition"].get("os") != "windows":
            return {"eligible": False, "state": "not-windows", "reason": "Windows base actions apply only to Windows installer builds."}
        api = api or ProxmoxAPI()
        identity, _ = _identity(record, api)
        state = _load(_journal_path(record["id"]))
        phase = state.get("state", "not-started")
        if phase == "prepared":
            _require_windows_agent(api, identity)
            return {"eligible": True, "can_prepare": False, "can_seal": True, "state": phase,
                    "reason": "Preparation receipt is ready; sealing can run."}
        if phase == "sealed":
            return {"eligible": False, "can_prepare": False, "can_seal": False, "state": phase,
                    "reason": "Base sealing was recorded. Clone identity proof and publication remain separate."}
        if identity["status"] != "running":
            return {"eligible": False, "can_prepare": False, "can_seal": False, "state": phase,
                    "reason": "Start this fresh owned Windows build on the installer network before preparation."}
        _require_windows_agent(api, identity)
        return {"eligible": True, "can_prepare": True, "can_seal": False, "state": phase,
                "reason": "Fresh owned Windows build is ready for fixed preparation."}
    except WindowsBaseError as exc:
        return {"eligible": False, "can_prepare": False, "can_seal": False,
                "state": "ineligible", "reason": str(exc)}


def _ps_command(script_path: Path) -> list[str]:
    source = script_path.read_bytes()
    if len(source) > 256 * 1024:
        raise WindowsBaseError("Reviewed Windows base script exceeds the fixed size limit.")
    encoded = base64.b64encode(source.decode("utf-8").encode("utf-16le")).decode("ascii")
    return [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def _cleanup_answerfile(api: ProxmoxAPI, identity: dict, record: dict) -> None:
    binding = record.get("private_answerfile")
    expected = f"local:iso/crange-win-{identity['build_id']}.iso"
    if (not isinstance(binding, dict) or binding.get("volid") != expected
            or binding.get("build_id") != identity["build_id"]
            or binding.get("vmid") != identity["vmid"]):
        raise WindowsBaseError("Generated answerfile identity is invalid; preserve this build for administrator review.")
    config = api.vm(identity["node"], identity["vmid"])
    media = str(config.get("ide0", ""))
    parts = media.split(',') if media else ['']
    options = dict(part.split('=', 1) for part in parts[1:] if '=' in part)
    if (parts[0] not in (expected, 'none', '') or
            any('=' not in part for part in parts[1:]) or
            len(options) != len(parts) - 1 or
            set(options) - {'media', 'size'} or options.get('media', 'cdrom') != 'cdrom'):
        raise WindowsBaseError("Unexpected installer media is attached to this Windows build; refusing answerfile cleanup.")
    if parts[0] == expected:
        api.request("PUT", f"/nodes/{identity['node']}/qemu/{identity['vmid']}/config",
                    {"ide0": "none,media=cdrom"})
        if str(api.vm(identity["node"], identity["vmid"]).get("ide0", "")) not in ("none,media=cdrom", ""):
            raise WindowsBaseError("Generated answerfile ISO remains attached; preserve the prepared build.")
    try:
        from crange_windows_seed import _seed_cleanup_relay
        result = _seed_cleanup_relay({"command": "windows-seed-cleanup", "build_id": identity["build_id"]})
    except Exception:
        raise WindowsBaseError("The fixed root cleanup relay did not verify the detached answerfile ISO; retry cleanup before sealing.") from None
    if (set(result) != {"build_id", "node", "vmid", "volid", "state", "verified_absent"} or
            result.get("build_id") != identity["build_id"] or result.get("node") != identity["node"] or
            result.get("vmid") != identity["vmid"] or result.get("volid") != expected or
            result.get("state") != "cleaned" or result.get("verified_absent") is not True):
        raise WindowsBaseError("The fixed root cleanup relay did not verify the detached answerfile ISO; retry cleanup before sealing.")
    rows = api.request("GET", f"/nodes/{identity['node']}/storage/local/content", {"content": "iso"})
    if not isinstance(rows, list) or any(isinstance(row, dict) and row.get("volid") == expected for row in rows):
        raise WindowsBaseError("Generated answerfile ISO was detached but remains in node storage; retry cleanup before sealing.")


def _exec_status(api: ProxmoxAPI, identity: dict, journal: dict, path: Path,
                 *, timeout: int) -> dict:
    root = f"/nodes/{identity['node']}/qemu/{identity['vmid']}/agent"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = api.request("GET", root + "/exec-status", {"pid": journal["pid"]})
        except Exception:
            current = api.status(identity["node"], identity["vmid"])
            if current == "stopped" and journal["operation"] == "seal":
                journal["state"] = "shutdown-observed"
                journal["result"] = "QEMU Guest Agent stopped before its terminal result was available; do not retry Sysprep."
                _save(path, journal)
                raise WindowsBaseError("Windows shutdown was observed during Sysprep, but its terminal guest result is unavailable. Preserve this build for administrator review; the operation will not be resent.") from None
            time.sleep(1)
            continue
        if isinstance(result, dict) and result.get("exited"):
            raw = result.get("out-data", "")
            journal["raw_terminal_output"] = raw
            journal["exitcode"] = result.get("exitcode")
            if result.get("exitcode") != 0:
                journal["state"] = "failed"
                journal["result"] = "Fixed Windows base operation failed; protected guest output is withheld."
                _save(path, journal)
                raise WindowsBaseError(journal["result"])
            if journal["operation"] == "prepare":
                journal["state"] = "prepared"
                journal["result"] = "Fixed Windows base preparation completed. This receipt is not clone identity proof."
                _save(path, journal)
                return journal
            # Sysprep must complete by shutting the source down. Do not treat
            # command completion alone as proof of a sealed template.
            stop_deadline = time.monotonic() + 300
            while time.monotonic() < stop_deadline:
                if api.status(identity["node"], identity["vmid"]) == "stopped":
                    journal["state"] = "sealed"
                    journal["result"] = "Sysprep returned successfully and the owned build stopped. Clone identity proof and publication remain separate."
                    _save(path, journal)
                    return journal
                time.sleep(2)
            journal["state"] = "seal-running"
            journal["result"] = "Sysprep returned, but the owned VM has not stopped; preserve the handle and do not retry."
            _save(path, journal)
            raise WindowsBaseError(journal["result"])
        time.sleep(1)
    journal["state"] = "prepare-running" if journal["operation"] == "prepare" else "seal-running"
    journal["result"] = "Windows base operation is still running; resume this build ID to poll the same QEMU Guest Agent handle."
    _save(path, journal)
    raise WindowsBaseError(journal["result"])


def _run(build_id: str, operation: str, *, api: ProxmoxAPI | None = None, timeout: int = 1200) -> dict:
    if not isinstance(build_id, str) or not ID.fullmatch(build_id):
        raise WindowsBaseError("Choose an installer build from the Templates list.")
    if operation not in ("prepare", "seal"):
        raise WindowsBaseError("Choose a fixed Windows base action.")
    api = api or ProxmoxAPI()
    path = _journal_path(build_id)
    with _lock(path) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = _record_for(build_id)
        identity, _ = _identity(record, api)
        journal = _load(path)
        journal_identity = {key: identity[key] for key in ("build_id", "vmid", "node", "guest_name", "config_sha256", "answerfile_sha256")}
        if journal and any(journal.get(key) != value for key, value in journal_identity.items()):
            raise WindowsBaseError("Windows base journal identity changed; preserving the existing operation handle.")
        state = journal.get("state")
        if operation == "prepare" and state == "prepared":
            _cleanup_answerfile(api, identity, record)
            return journal
        if operation == "seal" and state == "sealed":
            return journal
        if operation == "seal" and state != "prepared" and state != "seal-running":
            raise WindowsBaseError("Prepare this eligible Windows build before sealing it.")
        if operation == "seal" and state == "prepared":
            _cleanup_answerfile(api, identity, record)
        if state in ("shutdown-observed", "failed"):
            raise WindowsBaseError(journal.get("result", "This Windows build is frozen for administrator review."))
        if state in ("prepare-running", "seal-running"):
            if journal.get("operation") != operation:
                raise WindowsBaseError("Another fixed Windows base action is still running; resume its existing handle first.")
            result = _exec_status(api, identity, journal, path, timeout=timeout)
            if operation == "prepare" and result.get("state") == "prepared":
                _cleanup_answerfile(api, identity, record)
            return result
        if identity["status"] != "running":
            raise WindowsBaseError("Start this fresh owned Windows build before running the fixed base action.")
        script = ROOT / "ansible/windows" / ("prepare-generalized-base.ps1" if operation == "prepare" else "generalize-base.ps1")
        if not script.is_file():
            raise WindowsBaseError("The reviewed Windows base script is unavailable.")
        _require_windows_agent(api, identity)
        script_sha = hashlib.sha256(script.read_bytes()).hexdigest()
        command = _ps_command(script)
        root = f"/nodes/{identity['node']}/qemu/{identity['vmid']}/agent"
        response = api.request("POST", root + "/exec", {"command": command})
        pid = response.get("pid") if isinstance(response, dict) else None
        if type(pid) is not int or pid <= 0:
            raise WindowsBaseError("QEMU Guest Agent did not return a valid operation handle.")
        journal = {**journal_identity, "operation": operation,
                   "state": "prepare-running" if operation == "prepare" else "seal-running",
                   "script_sha256": script_sha, "pid": pid, "started_at": time.time(),
                   "result": "Fixed script submitted; resumption polls this same handle and never resends it."}
        _save(path, journal)
        result = _exec_status(api, identity, journal, path, timeout=timeout)
        if operation == "prepare" and result.get("state") == "prepared":
            _cleanup_answerfile(api, identity, record)
        return result


def prepare(build_id: str, *, api: ProxmoxAPI | None = None) -> dict:
    return _run(build_id, "prepare", api=api)


def seal(build_id: str, *, api: ProxmoxAPI | None = None) -> dict:
    return _run(build_id, "seal", api=api)
