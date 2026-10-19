# Project 100 sealed Windows publication contract

VM 8912 is a never-booted stopped source. Its source identity is null; no
historic MachineGuid or SID is asserted or inferred. Publication uses the
root-owned offline preparation bundle and the two already completed independent
clone proofs (8913 and 8914).

The fixed `capture-sealed-windows-p100` worker action makes one fixed
`sealed-windows-source-proof` request through the root-owned
`/run/crange-ct-read/socket` relay. The relay authenticates the controller
container's peer UID and exposes no path or command parameters. On P4 it reads
only these fixed paths:

- `/var/lib/crange-template-builds/windows-locale-candidate8912/inspection.json`
- `/var/lib/crange-template-builds/windows-locale-candidate8912/preparation.json`
- `/var/lib/crange-template-builds/windows-locale-probe8913.json`
- `/var/lib/crange-template-builds/windows-locale-probe8914.json`
- `/var/lib/crange-template-builds/windows-locale-probe8914-qga-result.json`

It requires root ownership, regular files, and modes `0644` for the inspection
record and `0600` for all other records. Under a root-owned snapshot lock, it
reads the proof files and live PVE config/status/task data twice and rejects any
change between reads. It checks the exact frozen source
configuration hash, owned volumes, GeneralizationState 4 / CleanupState 2,
reseal-to-OOBE state, unchanged SYSTEM/SOFTWARE/SAM hashes, prepared answer-file
hash, no manual clone interventions, and the second probe's persisted QGA
response. It then rechecks live PVE state: source 8912 is stopped and still has
the frozen configuration; both independent clone tasks succeeded; both clones
remain running in their isolated proof configuration. The validated result is
written to the controller's protected `source-journal` and is required again by
export, which freshly rechecks the source freeze. The protected P4 preparation
record may omit the redundant top-level config digest; the worker recomputes
the fixed identity hash from the embedded frozen config and compares it with
the live source.

The sealed Windows catalog record stores two clone identities as proof. It does
not store or infer a source identity. Publication derives its verification
record from those protected clone records and approves the image only for
Project 100 under the fixed image ID `mhl-windows-server-2025-base-p100-v1`.
The resulting profile is `mhl-server2025-desktop-experience`, displayed as
“Windows Server 2025 Desktop Experience.” Other Windows Server and Client
identity records remain supported by the catalog schema.

The fixed reviewed sequence is:

1. Run `scripts/crange_distribution.py ... capture-sealed-windows-p100` with the
   normal protected catalog, journal, and policy paths.
2. Run `template-sealed-windows-p100`. It revalidates the proof and frozen
   source, records a fixed conversion intent and provider UPID, and resumes only
   that task handle. An uncertain submission is never automatically repeated.
3. Export it through the existing `export` action with the fixed source journal
   `p100-sealed-windows-8912-export-r2` journal and the worker-private policy
   at `CRANGE_DISTRIBUTION_DATA/source-policies/p100-sealed-windows-8912.json`.
   The policy revision is `p100-sealed-windows-8912-20261008`; preserve its
   existing Proxmox 1 and 2 library pairs and require the reviewed
   `proxmox4:crange-library` pair. Resume only the recorded UPID
   `UPID:proxmox4:001CD7DD:1C4D9C31:6AC823C6:vzdump:8912:crange-iac@pve!terraform:`.
4. Publish the completed export through the existing `publish` action with
   `--project 100`; omit `--verification`, which is derived from the protected
   proof. The source Git revision was not captured in the protected source
   journal, so record `p100-sealed-windows-source-config-r1-20261008` as the
   explicit capture/configuration revision. The immutable source configuration
   digest remains separately bound in the sealed provenance record. Keep the
   image ID and destination datastore explicit in the reviewed command.

This document describes the guarded action. It does not authorize template
conversion, export, or catalog publication.
