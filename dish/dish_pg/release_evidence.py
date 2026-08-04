"""Typed, digest-bound evidence contracts for Stage A release authority."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from . import stage6_models as rel


REQUIRED_EVIDENCE = (
    ("authority_coverage", "current_to_target"),
    ("command_semantic_delta", "retained_commands"),
    ("characterization", "frozen_current_behavior"),
    ("production_change_ledger", "source_commit_closure"),
    ("fault_injection", "crash_boundaries"),
    ("contention", "same_task_and_independent_tasks"),
    ("backup_restore", "restore_rehearsal"),
    ("create_correlation", "lost_response_safety"),
    ("protocol_coherence", "service_openapi_routing"),
)
REQUIRED_REHEARSALS = ("full", "activation", "restore", "fault_injection")

REQUIRED_REHEARSAL_CHECKPOINTS = {
    "full": (
        "source_capture",
        "import_validation",
        "semantic_parity",
        "projection_reconciliation",
    ),
    "activation": (
        "writer_fence",
        "authority_activation",
        "rollback_burn",
        "first_admission",
    ),
    "restore": (
        "backup_verified",
        "restore_completed",
        "generation_rotated",
        "stale_request_rejected",
    ),
    "fault_injection": (
        "precommit_crash",
        "postcommit_replay",
        "projection_retry",
        "restart_recovery",
    ),
}

EVIDENCE_ARTIFACT_KINDS = {
    ("authority_coverage", "current_to_target"): "authority-coverage-report",
    ("command_semantic_delta", "retained_commands"): "command-semantic-delta-report",
    ("characterization", "frozen_current_behavior"): "characterization-manifest",
    ("production_change_ledger", "source_commit_closure"): "production-change-ledger",
    ("fault_injection", "crash_boundaries"): "fault-injection-report",
    ("contention", "same_task_and_independent_tasks"): "contention-report",
    ("backup_restore", "restore_rehearsal"): "backup-restore-report",
    ("create_correlation", "lost_response_safety"): "create-correlation-report",
    ("protocol_coherence", "service_openapi_routing"): "protocol-coherence-report",
}

REHEARSAL_CHECKPOINT_EVIDENCE_KINDS = {
    kind: {checkpoint: f"{kind}-{checkpoint}-evidence" for checkpoint in checkpoints}
    for kind, checkpoints in REQUIRED_REHEARSAL_CHECKPOINTS.items()
}

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class ReleaseAuthorityError(ValueError):
    """A release or cutover transition failed closed."""


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _require_sha256(value: object, field: str) -> str:
    if not _is_sha256(value):
        raise ReleaseAuthorityError(
            f"{field} must be exact lowercase hexadecimal SHA-256"
        )
    return str(value)


def _require_nonblank(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseAuthorityError(f"{field} must be a nonblank string")
    return value


def _validate_evidence_payload(
    *, category: str, evidence_key: str, outcome: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    contract = (category, evidence_key)
    expected_kind = EVIDENCE_ARTIFACT_KINDS.get(contract)
    if expected_kind is None:
        raise ReleaseAuthorityError("unknown release evidence contract")
    if outcome not in {"pass", "fail"}:
        raise ReleaseAuthorityError("release evidence outcome must be pass or fail")
    body = dict(payload)
    if body.get("artifact_kind") != expected_kind:
        raise ReleaseAuthorityError(
            "release evidence artifact kind does not match contract"
        )
    _require_nonblank(body.get("artifact_identity"), "artifact_identity")
    _require_nonblank(body.get("artifact_path"), "artifact_path")
    _require_sha256(body.get("artifact_sha256"), "artifact_sha256")
    _require_sha256(body.get("source_manifest_sha256"), "source_manifest_sha256")
    if body.get("gate_name") != f"{category}:{evidence_key}":
        raise ReleaseAuthorityError("release evidence gate name does not match contract")
    if body.get("gate_result") != outcome:
        raise ReleaseAuthorityError(
            "release evidence gate result does not match outcome"
        )
    return body


def _validate_checkpoint_payload(
    *, rehearsal: rel.RehearsalRun, checkpoint_kind: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    expected = REHEARSAL_CHECKPOINT_EVIDENCE_KINDS.get(
        rehearsal.rehearsal_kind, {}
    ).get(checkpoint_kind)
    if expected is None:
        raise ReleaseAuthorityError("checkpoint is not valid for rehearsal class")
    body = dict(payload)
    if body.get("rehearsal_kind") != rehearsal.rehearsal_kind:
        raise ReleaseAuthorityError("checkpoint rehearsal kind does not match run")
    if body.get("checkpoint_kind") != checkpoint_kind:
        raise ReleaseAuthorityError("checkpoint kind does not match payload")
    if body.get("evidence_kind") != expected:
        raise ReleaseAuthorityError(
            "checkpoint evidence kind does not match contract"
        )
    _require_nonblank(body.get("artifact_identity"), "artifact_identity")
    _require_nonblank(body.get("artifact_path"), "artifact_path")
    _require_sha256(body.get("artifact_sha256"), "artifact_sha256")
    source_manifest = _require_sha256(
        body.get("source_manifest_sha256"), "source_manifest_sha256"
    )
    if source_manifest != rehearsal.source_manifest_sha256:
        raise ReleaseAuthorityError(
            "checkpoint source manifest does not match rehearsal"
        )
    if body.get("gate_result") not in {"pass", "fail"}:
        raise ReleaseAuthorityError("checkpoint gate result must be pass or fail")
    return body


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()
