"""Checks the fixed Project 100 Debian stock archive linker."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/project100_link_debian12_ct.py"
SPEC = importlib.util.spec_from_file_location("project100_stock_link", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def accepted_replica(node):
    digest = "a" * 64
    return {
        "node": node,
        "state": "ready",
        "template_file_id": MODULE.ARCHIVE,
        "artifact_sha256": digest,
        "verification": {
            "artifact_sha256": digest,
            "provider_acceptance": {
                "provider_version": "0.104.0",
                "provider_job_id": "accepted-" + node,
                "content_api": {
                    "volume_id": MODULE.ARCHIVE,
                    "size": MODULE.EXPECTED_SIZE,
                    "storages": {name: {"sha256": digest, "size": MODULE.EXPECTED_SIZE}
                                 for name in MODULE.EXPECTED_STORAGES},
                },
            },
        },
    }


def good_image():
    return {
        "kind": "container",
        "source_type": "stock_archive",
        "verified": True,
        "approved_projects": ["*"],
        "artifact_sha256": "a" * 64,
        "verification": {"evidence": {"pve_appliance_metadata": {
            "filename": "debian-12-standard_12.12-1_amd64.tar.zst",
            "version": "12.12-1", "architecture": "amd64",
            "sha512sum": MODULE.EXPECTED_SHA512,
        }}},
        "replicas": [accepted_replica(node) for node in sorted(MODULE.ELIGIBLE_NODES)],
    }


class Project100ArchiveLinkTests(unittest.TestCase):
    def test_reference_requires_global_signed_and_per_node_provider_evidence(self):
        reference = MODULE.build_reference(good_image())
        self.assertEqual(reference["image_id"], "debian-12-router-v1")
        self.assertEqual(reference["verified_nodes"], ["proxmox1", "proxmox3", "proxmox5"])
        self.assertEqual(reference["archive_sha512"], MODULE.EXPECTED_SHA512)

    def test_unverified_or_wrong_project_image_is_rejected(self):
        image = good_image()
        image["verified"] = False
        with self.assertRaisesRegex(ValueError, "verified"):
            MODULE.build_reference(image)
        image = good_image()
        image["approved_projects"] = [3]
        with self.assertRaisesRegex(ValueError, "approved"):
            MODULE.build_reference(image)

    def test_missing_node_acceptance_is_rejected(self):
        image = good_image()
        image["replicas"] = [r for r in image["replicas"] if r["node"] != "proxmox5"]
        with self.assertRaisesRegex(ValueError, "P1/P3/P5"):
            MODULE.build_reference(image)

    def test_linker_preserves_existing_aliases_and_writes_fixed_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "approved-container-templates.json").write_text(
                json.dumps({"old": {"display_name": "Retained"}})
            )
            reference = MODULE.link_project_template(project, good_image())
            saved = json.loads((project / "approved-container-templates.json").read_text())
            self.assertIn("old", saved)
            self.assertEqual(saved[MODULE.ALIAS], reference)


if __name__ == "__main__":
    unittest.main()
