"""Generate and attach one private unattended-install seed per Windows build."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
from datetime import datetime, timezone
import re
import secrets
import shutil
import socket
import stat
import subprocess
import string
import tempfile
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

from crange_windows_answerfile import UNATTEND

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates/windows/server2025-autounattend.xml.in"
SETUP = ROOT / "templates/windows/crange-win-setup.ps1"
VOLUME = re.compile(r"^(?:local|crange-library|media-mirror1|media-mirror4):iso/[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\.iso$", re.I)


class WindowsSeedError(ValueError):
    pass


class SeedUploadFailure(WindowsSeedError):
    def __init__(self, submitted: bool, message: str):
        super().__init__(message)
        self.submitted = submitted


class WindowsSeedUncertain(WindowsSeedError):
    pass


SeedUploadUncertain = WindowsSeedUncertain


NODE_MANAGEMENT_IPS = {f"proxmox{i}": f"10.0.0.{100 + i}" for i in range(1, 7)}
SEED_CLEANUP_RELAY = "/run/crange-ct-read/socket"


def _seed_cleanup_relay(request, *, timeout=900):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(SEED_CLEANUP_RELAY)
        client.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n") and len(data) <= 8192:
            chunk = client.recv(8193 - len(data))
            if not chunk:
                break
            data += chunk
    if len(data) > 8192 or not data.endswith(b"\n"):
        raise WindowsSeedUncertain("Fixed private seed cleanup relay response is incomplete; preserve this build.")
    try:
        result = json.loads(data)
    except ValueError:
        raise WindowsSeedUncertain("Fixed private seed cleanup relay response is invalid; preserve this build.") from None
    if not isinstance(result, dict) or set(result) != {"value"} or not isinstance(result["value"], dict):
        raise WindowsSeedError("Fixed private seed cleanup relay denied this build; preserve its private journal.")
    return result["value"]


def seed_cleanup_relay_ready() -> bool:
    try:
        result = _seed_cleanup_relay({"command": "windows-seed-cleanup-ready"}, timeout=5)
    except (OSError, WindowsSeedError, WindowsSeedUncertain, socket.timeout):
        return False
    return (set(result) == {"schema_version", "cleanup_supported", "relay_sha256"} and
            result.get("schema_version") == 1 and result.get("cleanup_supported") is True and
            re.fullmatch(r"[0-9a-f]{64}", str(result.get("relay_sha256", ""))) is not None)


def selected_node_upload_endpoint(api, node: str) -> str:
    """Resolve only the selected approved node's fixed management endpoint."""
    if node not in NODE_MANAGEMENT_IPS:
        raise WindowsSeedError("Choose an approved Proxmox node.")
    rows = api.request("GET", "/cluster/status")
    if not isinstance(rows, list):
        raise WindowsSeedError("Selected Proxmox node management identity is unavailable.")
    matches = [row for row in rows if isinstance(row, dict) and row.get("type") == "node" and
               row.get("name") == node and str(row.get("online")) == "1"]
    if len(matches) != 1 or matches[0].get("ip") != NODE_MANAGEMENT_IPS[node]:
        raise WindowsSeedError("Selected Proxmox node management identity is not verified.")
    try:
        address = ipaddress.ip_address(matches[0]["ip"])
    except (KeyError, ValueError, TypeError):
        raise WindowsSeedError("Selected Proxmox node management identity is invalid.") from None
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_private:
        raise WindowsSeedError("Selected Proxmox node management identity is invalid.")
    return f"https://{address}:8006/api2/json"


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not path.is_dir() or info.st_uid != os.geteuid() or (info.st_mode & 0o777) != 0o700:
        raise WindowsSeedError("Windows installer secret directory must be worker-owned mode 0700.")


def _atomic_secret(path: Path, value: dict, *, replace=True) -> None:
    temp = path.with_name("." + path.name + "." + secrets.token_hex(8) + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temp, path)
            os.chmod(path, 0o600)
        else:
            # link() is an atomic no-replace install for the initial build binding.
            os.link(temp, path, follow_symlinks=False)
            temp.unlink()
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if temp.exists():
            temp.unlink()


def _read_secret(path: Path) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise WindowsSeedError("Private Windows installer journal ownership or mode is invalid.") from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                (info.st_mode & 0o777) != 0o600 or info.st_nlink != 1):
            raise WindowsSeedError("Private Windows installer journal ownership or mode is invalid.")
        with os.fdopen(fd, "r") as stream:
            fd = -1
            value = json.load(stream)
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(value, dict):
        raise WindowsSeedError("Private Windows installer journal is invalid.")
    return value


def _journal_path(build_id: str) -> Path:
    return Path.home() / ".config/crange/windows-build-secrets" / (build_id + ".json")


def _artifact_path(build_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", str(build_id)):
        raise WindowsSeedError("Windows installer identity is invalid.")
    return _journal_path(build_id).with_suffix(".iso")


def _persist_artifact(source: Path, build_id: str) -> Path:
    target = _artifact_path(build_id)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_stream:
            fd = -1
            while chunk := input_stream.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if fd >= 0:
            os.close(fd)
        target.unlink(missing_ok=True)
        raise
    return target


def _remove_artifact(build_id: str, journal: dict) -> None:
    path = _artifact_path(build_id)
    if not path.exists():
        return
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
            (info.st_mode & 0o777) != 0o600 or info.st_nlink != 1 or
            info.st_size != int(journal.get("iso_size", -1))):
        raise WindowsSeedError("Private answerfile artifact identity is invalid; preserve it for administrator review.")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != journal.get("iso_sha256"):
        raise WindowsSeedError("Private answerfile artifact checksum changed; preserve it for administrator review.")
    path.unlink()
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_journal(path: Path, journal: dict, *, initial=False) -> None:
    if not initial:
        current = _read_secret(path)
        for key in ("build_id", "vmid", "node", "volid", "artifact_name", "iso_sha256", "iso_size"):
            if current.get(key) != journal.get(key):
                raise WindowsSeedError("Private installer journal identity changed; preserve it for review.")
    _atomic_secret(path, journal, replace=not initial)


def public_status(build_id: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{32}", str(build_id)):
        raise WindowsSeedError("Windows installer identity is invalid.")
    path = _journal_path(build_id)
    if not path.exists():
        return None
    _private_dir(path.parent)
    journal = _read_secret(path)
    if journal.get("build_id") != build_id:
        raise WindowsSeedError("Private installer journal belongs to another build.")
    state = journal.get("state")
    reasons = {
        "uploading": "Upload outcome is uncertain. Reconcile the exact private ISO before retrying.",
        "upload_uncertain": "Upload outcome is uncertain. Reconcile the exact private ISO before retrying.",
        "uploaded": "Private answerfile ISO is attached to this build.",
        "cleaned": "Private answerfile ISO was verified absent.",
    }
    return {"state": state, "volid": journal.get("volid"),
            "node": journal.get("node"), "vmid": journal.get("vmid"),
            "iso_size": journal.get("iso_size"), "created_at": journal.get("created_at"),
            "reason": reasons.get(state, "Private seed journal needs administrator review."),
            "reconcile_required": state in ("uploading", "upload_uncertain", "uploaded", "cleaned")}


def reconcile_upload(build_id: str, record: dict, api) -> dict:
    """Clean one exact ambiguous seed only after provider absence is confirmed."""
    from datetime import datetime, timezone

    path = _journal_path(build_id)
    _private_dir(path.parent)
    journal = _read_secret(path)
    node = record.get("definition", {}).get("node")
    expected_volid = f"local:iso/crange-win-{build_id}.iso"
    if (journal.get("build_id") != build_id or journal.get("vmid") != record.get("vmid") or
            journal.get("node") != node or journal.get("volid") != expected_volid or
            journal.get("artifact_name") != (build_id + ".iso") or
            journal.get("state") not in ("uploading", "upload_uncertain", "uploaded", "cleaned")):
        raise WindowsSeedError("Private seed journal does not match this owned build; preserve it for review.")
    if journal.get("state") == "cleaned":
        return {"build_id": build_id, "vmid": record["vmid"], "node": node,
                "volid": expected_volid, "state": "cleaned"}
    try:
        started = datetime.fromisoformat(journal["created_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        raise WindowsSeedError("Private seed journal timestamp is invalid; preserve it for review.") from None
    age = (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()
    if age < 120:
        raise WindowsSeedUncertain("Upload may still be processing. Wait two minutes, then reconcile this build again.")
    resources = api.request("GET", "/cluster/resources", {"type": "vm"})
    matches = [row for row in resources if int(row.get("vmid", -1)) == int(record["vmid"])]
    if matches:
        if len(matches) != 1 or matches[0].get("node") != node or matches[0].get("type") != "qemu":
            raise WindowsSeedError("Build VMID is occupied by another resource; preserve the seed journal for review.")
        try:
            owned(api, record)
        except Exception:
            raise WindowsSeedError("A VMID collision or identity change prevents seed cleanup; preserve the build.") from None
        raise WindowsSeedError("The owned installer VM still exists; retain its private seed for the fixed Windows workflow.")
    tasks = api.request("GET", f"/nodes/{node}/tasks", {"since": int(started.timestamp()) - 5, "limit": 500})
    pending = [task for task in tasks if int(task.get("starttime", 0)) >= int(started.timestamp()) - 5 and
               "upload" in str(task.get("type", "")).lower() and task.get("status") == "running"]
    if pending:
        raise WindowsSeedUncertain("A Proxmox upload task is still running; preserve the private seed and retry reconciliation later.")
    rows = api.request("GET", f"/nodes/{node}/storage/local/content", {"content": "iso"})
    exact = [row for row in rows if isinstance(row, dict) and row.get("volid") == expected_volid]
    if exact:
        if len(exact) != 1 or int(exact[0].get("size", -1)) != int(journal.get("iso_size", -2)):
            raise WindowsSeedError("The exact seed volume has an unexpected size; preserve it for administrator review.")
    elif not isinstance(rows, list):
        raise WindowsSeedUncertain("Proxmox did not provide a storage inventory; keep the seed journal and retry reconciliation.")
    relay_result = _seed_cleanup_relay({"command": "windows-seed-cleanup", "build_id": build_id})
    if (set(relay_result) != {"build_id", "node", "vmid", "volid", "state", "verified_absent"} or
            relay_result.get("build_id") != build_id or relay_result.get("node") != node or
            relay_result.get("vmid") != record["vmid"] or relay_result.get("volid") != expected_volid or
            relay_result.get("state") != "cleaned" or relay_result.get("verified_absent") is not True):
        raise WindowsSeedUncertain("Root cleanup has not verified the exact private ISO absent; preserve this build and retry reconciliation.")
    rows = api.request("GET", f"/nodes/{node}/storage/local/content", {"content": "iso"})
    if not isinstance(rows, list) or any(isinstance(row, dict) and row.get("volid") == expected_volid for row in rows):
        raise WindowsSeedUncertain("The private ISO is not verified absent through Proxmox; preserve its journal and retry reconciliation.")
    journal["state"] = "cleaned"
    journal["cleanup_verified_at"] = datetime.now(timezone.utc).isoformat()
    _write_journal(path, journal)
    return {"build_id": build_id, "vmid": record["vmid"], "node": node,
            "volid": expected_volid, "state": "cleaned"}


def finalize_cleanup(build_id: str) -> None:
    """Forget a cleaned seed only after the owning workflow releases its lease."""
    path = _journal_path(build_id)
    journal = _read_secret(path)
    if (journal.get("build_id") != build_id or journal.get("artifact_name") != (build_id + ".iso") or
            journal.get("state") != "cleaned"):
        raise WindowsSeedError("Private seed cleanup is not verified; preserve its journal.")
    _remove_artifact(build_id, journal)
    path.unlink()
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _upload_iso(api, node: str, filename: str, iso: Path, expected_sha: str) -> str:
    from urllib.parse import urlsplit

    volid = "local:iso/" + filename
    rows = api.request("GET", f"/nodes/{node}/storage/local/content", {"content": "iso"})
    if any(row.get("volid") == volid for row in rows if isinstance(row, dict)):
        raise WindowsSeedError("A private answerfile ISO already exists for this build; preserve the build for administrator review.")
    boundary = "crangewin" + uuid.uuid4().hex
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"content\"\r\n\r\niso\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"filename\"; filename=\"{filename}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    target = urlsplit(selected_node_upload_endpoint(api, node))
    connection = (http.client.HTTPSConnection(target.hostname, target.port or 443,
                  context=api.ssl_context, timeout=180) if target.scheme == "https" else
                  http.client.HTTPConnection(target.hostname, target.port or 80, timeout=180))
    digest = hashlib.sha256()
    submitted = False
    try:
        submitted = True
        connection.putrequest("POST", target.path + f"/nodes/{node}/storage/local/upload")
        connection.putheader("Authorization", "PVEAPIToken=" + api.token)
        connection.putheader("Content-Type", "multipart/form-data; boundary=" + boundary)
        connection.putheader("Content-Length", str(head.__len__() + iso.stat().st_size + tail.__len__()))
        connection.endheaders()
        connection.send(head)
        with iso.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                connection.send(chunk)
                digest.update(chunk)
        connection.send(tail)
        response = connection.getresponse()
        body = response.read(65536)
        if response.status != 200:
            raise SeedUploadFailure(True, f"Private answerfile upload returned HTTP {response.status}.")
        try:
            result = json.loads(body).get("data")
        except (ValueError, AttributeError):
            raise SeedUploadFailure(True, "Private answerfile upload returned an invalid response.") from None
        if isinstance(result, str) and result.startswith("UPID:"):
            api.task(node, result, timeout=600)
    except SeedUploadFailure:
        raise
    except Exception as exc:
        if submitted:
            raise SeedUploadFailure(True, "Private answerfile upload response was interrupted.") from None
        raise WindowsSeedError("Private answerfile upload could not be started.") from None
    finally:
        connection.close()
    if digest.hexdigest() != expected_sha:
        raise SeedUploadFailure(True, "Private answerfile upload checksum did not match the generated seed.")
    try:
        verified = api.request("GET", f"/nodes/{node}/storage/local/content", {"content": "iso"})
    except Exception:
        raise SeedUploadFailure(True, "Private answerfile ISO storage verification was interrupted.") from None
    if not any(row.get("volid") == volid and int(row.get("size", 0)) == iso.stat().st_size
               for row in verified if isinstance(row, dict)):
        raise SeedUploadFailure(True, "Private answerfile ISO was not verified in the selected node storage.")
    return volid


def create(build_id: str, vmid: int, node: str, driver_iso: str, api) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", str(build_id)) or type(vmid) is not int or vmid < 100:
        raise WindowsSeedError("Windows installer identity is invalid.")
    if node not in NODE_MANAGEMENT_IPS or not isinstance(driver_iso, str) or not VOLUME.fullmatch(driver_iso):
        raise WindowsSeedError("Choose an approved Windows driver ISO and node.")
    if not TEMPLATE.is_file() or not SETUP.is_file():
        raise WindowsSeedError("Reviewed Windows unattended seed files are unavailable.")
    xorriso = shutil.which("xorriso")
    if not xorriso:
        raise WindowsSeedError("Unattended Windows build requires xorriso on the controller; ask the platform administrator to install the declared xorriso dependency.")
    secret_dir = Path.home() / ".config/crange/windows-build-secrets"
    _private_dir(secret_dir)
    secret_path = secret_dir / (build_id + ".json")
    if secret_path.exists():
        raise WindowsSeedError("Private installer credentials already exist for this build ID; preserve it for administrator review.")
    if not seed_cleanup_relay_ready():
        raise WindowsSeedError("Fixed root cleanup relay is unavailable; unattended Windows creation is disabled until safe seed cleanup is deployed.")
    alphabet = string.ascii_letters + string.digits + "!@#$%^*-_"
    password_chars = [secrets.choice(string.ascii_uppercase), secrets.choice(string.ascii_lowercase),
                      secrets.choice(string.digits), secrets.choice("!@#$%^*-_")]
    password_chars.extend(secrets.choice(alphabet) for _ in range(36))
    secrets.SystemRandom().shuffle(password_chars)
    password = "".join(password_chars)
    raw = TEMPLATE.read_text(encoding="utf-8")
    marker = "@@MHL_TEMP_ADMIN_PASSWORD@@"
    if raw.count(marker) != 1:
        raise WindowsSeedError("Reviewed Windows unattended template has an invalid credential marker.")
    answer = raw.replace(marker, password)
    try:
        root = ET.fromstring(answer)
    except ET.ParseError:
        raise WindowsSeedError("Reviewed Windows unattended template is malformed.") from None
    if root.tag != "{" + UNATTEND + "}unattend":
        raise WindowsSeedError("Reviewed Windows unattended template namespace is invalid.")
    xml_bytes = answer.encode("utf-8")
    digest = hashlib.sha256(xml_bytes).hexdigest()
    secret = {"build_id": build_id, "vmid": vmid, "node": node,
              "volid": "local:iso/crange-win-" + build_id + ".iso",
              "artifact_name": build_id + ".iso",
              "answerfile_sha256": digest, "installer_admin_password": password}
    with tempfile.TemporaryDirectory(prefix="crange-win-seed-") as temporary:
        work = Path(temporary)
        os.chmod(work, 0o700)
        (work / "Autounattend.xml").write_bytes(xml_bytes)
        (work / "crange-win-setup.ps1").write_bytes(SETUP.read_bytes())
        output = work / ("crange-win-" + build_id + ".iso")
        command = [xorriso, "-as", "mkisofs", "-o", str(output), "-V", "CRANGE_UNATTEND",
                   "-J", "-r", "Autounattend.xml", "crange-win-setup.ps1"]
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=120, cwd=work)
        except (OSError, subprocess.SubprocessError):
            raise WindowsSeedError("xorriso could not build the private Windows answerfile ISO.") from None
        if not output.is_file() or not 1 <= output.stat().st_size <= 64 * 1024 * 1024:
            raise WindowsSeedError("Generated Windows answerfile ISO has an invalid size.")
        artifact = _persist_artifact(output, build_id)
        iso_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
        secret.update(state="uploading", created_at=datetime.now(timezone.utc).isoformat(),
                      iso_sha256=iso_sha, iso_size=artifact.stat().st_size)
        try:
            _write_journal(secret_path, secret, initial=True)
        except Exception:
            _remove_artifact(build_id, secret)
            raise
        try:
            volid = _upload_iso(api, node, output.name, artifact, iso_sha)
        except SeedUploadFailure as exc:
            if not exc.submitted:
                secret_path.unlink(missing_ok=True)
                _remove_artifact(build_id, secret)
                raise WindowsSeedError(str(exc)) from None
            secret["state"] = "upload_uncertain"
            _write_journal(secret_path, secret)
            raise SeedUploadUncertain("Private answerfile upload outcome is uncertain; preserve this build and use the fixed reconcile action.") from None
        secret["state"] = "uploaded"
        _write_journal(secret_path, secret)
        _remove_artifact(build_id, secret)
    return {"build_id": build_id, "vmid": vmid, "volid": volid, "sha256": digest,
            "iso_sha256": iso_sha, "iso_size": secret["iso_size"], "driver_iso": driver_iso}
