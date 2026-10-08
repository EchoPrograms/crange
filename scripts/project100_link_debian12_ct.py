#!/usr/bin/env python3
"""Link the already verified Debian 12 stock archive into Project 100's CT allowlist.

This fixed administrator action only writes one project-local template alias.
It never downloads media, changes the global catalog, or creates a guest.
Run on the controller as ``crange`` after checking the central catalog receipt.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ID = 100
IMAGE_ID = "debian-12-router-v1"
ALIAS = "mhl-debian12-stock"
ARCHIVE = "crange-library:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst"
EXPECTED_SHA512 = "50c85eaaece677a3ebe01cc909b83872e9da2a22c29ae652838afce71e83222fdf40f6accecd7d52b180e912fc1f85ecdf7b3fc4d3027da4d865e509a9e76597"
EXPECTED_SIZE = 123731847
ELIGIBLE_NODES = {"proxmox1", "proxmox3", "proxmox5"}
EXPECTED_STORAGES = {"crange-library", "media-mirror1", "media-mirror4"}


def build_reference(image: dict) -> dict:
    if (image.get("kind") != "container" or image.get("source_type") != "stock_archive"
            or image.get("verified") is not True
            or not ("*" in image.get("approved_projects", []) or PROJECT_ID in image.get("approved_projects", []))):
        raise ValueError("The Debian archive must be verified and approved for Project 100.")

    digest = image.get("artifact_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("The Debian archive has no verified SHA256.")

    signed_metadata = (image.get("verification", {}).get("evidence", {})
                       .get("pve_appliance_metadata") or {})
    if not (signed_metadata.get("filename") == ARCHIVE.split("/")[-1]
            and signed_metadata.get("version") == "12.12-1"
            and signed_metadata.get("architecture") == "amd64"
            and signed_metadata.get("sha512sum") == EXPECTED_SHA512):
        signed_metadata = {}
    accepted_nodes = set()
    for replica in image.get("replicas", []):
        if (replica.get("state") != "ready" or replica.get("node") not in ELIGIBLE_NODES
                or replica.get("artifact_sha256") != digest):
            continue
        verification = replica.get("verification") or {}
        evidence = verification.get("evidence") or {}
        acceptance = verification.get("provider_acceptance") or {}
        content = acceptance.get("content_api") or {}
        storages = content.get("storages") or {}
        copies_match = all(
            storage in storages
            and storages[storage].get("sha256") == digest
            and storages[storage].get("size") == EXPECTED_SIZE
            for storage in EXPECTED_STORAGES
        )
        if (verification.get("artifact_sha256") == digest
                and content.get("volume_id") == ARCHIVE
                and content.get("size") == EXPECTED_SIZE
                and copies_match
                and acceptance.get("provider_version") == "0.104.0"
                and acceptance.get("provider_job_id")):
            accepted_nodes.add(replica["node"])

    if not signed_metadata:
        raise ValueError("The replica receipt lacks the signed PVE Debian 12.12-1 aplinfo checksum.")
    if not ELIGIBLE_NODES <= accepted_nodes:
        raise ValueError("P1/P3/P5 need independent provider and storage-copy acceptance receipts.")

    return {
        "display_name": "Debian 12.12 stock container archive",
        "image_id": IMAGE_ID,
        "template_file_id": ARCHIVE,
        "archive_sha256": digest,
        "archive_size": EXPECTED_SIZE,
        "archive_sha512": EXPECTED_SHA512,
        "verified_nodes": sorted(accepted_nodes),
    }


def link_project_template(path: Path, image: dict) -> dict:
    reference = build_reference(image)
    target = path / "approved-container-templates.json"
    current = json.loads(target.read_text())
    prior = current.get(ALIAS)
    if prior and prior != reference:
        raise ValueError(f"Project alias {ALIAS!r} already names a different image receipt.")
    current[ALIAS] = reference
    fd, temporary = tempfile.mkstemp(prefix=".mhl-container-templates.", dir=path)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(current, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return reference


def main() -> int:
    from crange import project_dir
    from crange_scheduling import read_settings

    path = project_dir(str(PROJECT_ID))
    _policy, catalog = read_settings()
    image = catalog.get("images", {}).get(IMAGE_ID)
    if not image:
        raise ValueError("The fixed Debian 12 logical image is absent from the catalog.")
    reference = link_project_template(path, image)
    print(json.dumps({"project_id": PROJECT_ID, "alias": ALIAS, "image_id": IMAGE_ID,
                      "verified_nodes": reference["verified_nodes"], "archive_size": EXPECTED_SIZE}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
