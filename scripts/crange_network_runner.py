"""Source-only fixed probe lifecycle. No production backend or HTTP operation.

Backend adapters must enforce API task ownership, reservations, deadlines and
exact saved-plan application. Until those adapters exist, no queue action may
expose this runner. Cancellation must reconcile its durable journal instead of
killing the worker and silently abandoning a guest or router rule.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Protocol

from crange_network_acceptance import CHECKS, probe_from_plan, scope, validate_receipt
from crange_scheduling import atomic_json


def safe_failure_cause(error):
    """Return a bounded diagnostic label; never persist exception text."""
    cause = getattr(error, 'safe_cause', None)
    if isinstance(cause, str) and re.fullmatch(r'[a-z0-9_]{1,80}', cause):
        return cause
    if isinstance(error, TimeoutError):
        return 'timeout_detail_redacted'
    if isinstance(error, ConnectionError):
        return 'connection_detail_redacted'
    if isinstance(error, PermissionError):
        return 'permission_detail_redacted'
    return f'{type(error).__name__.lower()}_detail_redacted'


def plan_digest(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class ProbeBackend(Protocol):
    """Privileged fixed adapters only; methods never take command strings.

    reviewed_plan validates saved-plan hash against administrator approval, not
    a client boolean. destroy_plan must inspect live owner tags and journal
    before planning. vmid_absent queries the cluster, not just Terraform state.
    Failure/timeout leaves reservation held until independent reconciliation.
    """
    def vmid_absent(self, target): ...
    def reserve(self, target): ...
    def create_plan(self, target): ...
    def reviewed_plan(self, target, operation, digest): ...
    def apply_saved_plan(self, target, operation, digest, timeout): ...
    def install_management_rule(self, target, rule): ...
    def run_check(self, target, check, timeout): ...
    def remove_management_rule(self, target, rule): ...
    def temporary_rules_absent(self, target): ...
    def destroy_plan(self, target): ...
    def probe_state_empty(self, target): ...
    def input_unchanged(self, target): ...
    def cleanup_artifact(self, target): ...
    def release_reservation(self, target): ...


class NetworkProbeRunner:
    def __init__(self, backend: ProbeBackend, journal: Path, *, clock=time.monotonic):
        self.backend = backend
        self.journal = Path(journal)
        self.clock = clock

    def run(self, target, *, source_revision, tested_at):
        target = scope(**target)
        if self.journal.exists():
            raise ValueError('Existing probe journal requires owned reconciliation; never retry allocation blindly.')
        if self.backend.vmid_absent(target) is not True:
            raise ValueError('Probe VMID already exists.')
        rule = {'owner': target['job_id'], 'sources': ['10.60.0.20', '10.60.0.10'],
                'destination': target['address'].split('/')[0] + '/32',
                'protocol': 'tcp', 'port': 22}
        record = {'schema_version': 1, 'scope': target, 'rule': rule, 'state': 'reserving'}
        def save(state, **fields):
            record.update(fields, state=state)
            atomic_json(self.journal, record)
            parent_fd = os.open(self.journal.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        # Exclusive claim prevents concurrent retries racing the exists check.
        self.journal.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        parent_fd = os.open(self.journal.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        # Journal before every possible mutation; uncertain reservations are kept.
        save('reserving')
        try:
            self.backend.reserve(target)
        except Exception as error:
            save('reservation_uncertain', failure=type(error).__name__,
                 cause=safe_failure_cause(error))
            raise
        save('reserved')
        created = False
        rule_attempted = False
        checks = {}
        failure = None
        cleanup_errors = []
        create = None
        delete = None
        try:
            create = self.backend.create_plan(target)
            probe_from_plan(create, 'create', target)
            digest = plan_digest(create)
            if self.backend.reviewed_plan(target, 'create', digest) is not True:
                raise ValueError('Exact create plan lacks administrator review.')
            save('create_pending', create_plan_sha256=digest)
            created = True  # apply may partially succeed or time out
            self.backend.apply_saved_plan(target, 'create', digest, timeout=600)
            save('created')
            rule_attempted = True  # rule mutation may partially succeed
            save('management_rule_pending')
            self.backend.install_management_rule(target, rule)
            save('testing')
            for check in sorted(CHECKS):
                started = self.clock()
                result = self.backend.run_check(target, check, timeout=60)
                elapsed = self.clock() - started
                if not isinstance(result, dict) or result.get('passed') is not True or not 0 < elapsed <= 60:
                    raise ValueError('Fixed network check failed or exceeded deadline: ' + check)
                checks[check] = dict(result, duration_seconds=elapsed)
                save('testing', completed_checks=sorted(checks))
        except Exception as error:
            # Do not persist exception text: provider errors can contain secrets.
            failure = type(error).__name__
            cause = safe_failure_cause(error)
        finally:
            if rule_attempted:
                save('rule_cleanup_pending')
                try:
                    self.backend.remove_management_rule(target, rule)
                except Exception:
                    cleanup_errors.append('management_rule')
            if created:
                save('destroy_pending')
                try:
                    delete = self.backend.destroy_plan(target)
                    probe_from_plan(delete, 'delete', target)
                    digest = plan_digest(delete)
                    if self.backend.reviewed_plan(target, 'delete', digest) is not True:
                        raise ValueError('Exact destroy plan lacks administrator review.')
                    save('destroy_pending', delete_plan_sha256=digest)
                    self.backend.apply_saved_plan(target, 'delete', digest, timeout=600)
                except Exception:
                    cleanup_errors.append('probe')
        try:
            cleanup = {
                'vmid_absent_before': True,
                'vmid_absent_after': self.backend.vmid_absent(target) is True,
                'probe_state_empty': self.backend.probe_state_empty(target) is True,
                'temporary_rules_absent': self.backend.temporary_rules_absent(target) is True,
                'input_unchanged': self.backend.input_unchanged(target) is True,
                'artifact_sha256': self.backend.cleanup_artifact(target),
            }
            if not all(cleanup[key] for key in cleanup if key != 'artifact_sha256'):
                cleanup_errors.append('absence_verification')
        except Exception:
            cleanup_errors.append('absence_verification')
            cleanup = {}
        if cleanup_errors:
            save('cleanup_uncertain', failure=failure, cause=cause if failure else None,
                 cleanup_errors=sorted(set(cleanup_errors)))
            raise ValueError('Cleanup uncertain; retain reservation and reconcile owned journal.')
        # Failed tests can release only after absence has been independently checked.
        save('cleaned_release_pending', cleanup=cleanup)
        self.backend.release_reservation(target)
        if failure:
            save('failed_cleaned', failure=failure, cause=cause)
            raise ValueError('Network acceptance failed; owned probe and temporary rules removed.')
        receipt = {'schema_version': 1, 'scope': target, 'zone': 'crange', 'mtu': 1450,
                   'source_revision': source_revision, 'tested_at': tested_at,
                   'checks': checks, 'cleanup': cleanup}
        try:
            validate_receipt(receipt, create, delete)
        except Exception:
            save('failed_cleaned', failure='InvalidEvidence')
            raise ValueError('Completed run has invalid evidence; no receipt issued.')
        save('accepted_cleaned', receipt=receipt)
        return receipt
