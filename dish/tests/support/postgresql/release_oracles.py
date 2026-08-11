"""Small, literal release-contract oracles independent of production inventories."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

EXPECTED_RELEASE_EVIDENCE = (
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

EXPECTED_EVIDENCE_ARTIFACT_KINDS = {
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

EXPECTED_REHEARSALS = ("full", "activation", "restore", "fault_injection")

EXPECTED_REHEARSAL_CHECKPOINTS = {
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
    "cutover": (),
}

EXPECTED_REHEARSAL_CHECKPOINT_EVIDENCE_KINDS = {
    "full": {
        "source_capture": "full-source_capture-evidence",
        "import_validation": "full-import_validation-evidence",
        "semantic_parity": "full-semantic_parity-evidence",
        "projection_reconciliation": "full-projection_reconciliation-evidence",
    },
    "activation": {
        "writer_fence": "activation-writer_fence-evidence",
        "authority_activation": "activation-authority_activation-evidence",
        "rollback_burn": "activation-rollback_burn-evidence",
        "first_admission": "activation-first_admission-evidence",
    },
    "restore": {
        "backup_verified": "restore-backup_verified-evidence",
        "restore_completed": "restore-restore_completed-evidence",
        "generation_rotated": "restore-generation_rotated-evidence",
        "stale_request_rejected": "restore-stale_request_rejected-evidence",
    },
    "fault_injection": {
        "precommit_crash": "fault_injection-precommit_crash-evidence",
        "postcommit_replay": "fault_injection-postcommit_replay-evidence",
        "projection_retry": "fault_injection-projection_retry-evidence",
        "restart_recovery": "fault_injection-restart_recovery-evidence",
    },
}

CANONICAL_VECTOR_VALUE = {
    "z": [3, 2, 1],
    "a": {"unicode": "café", "flag": True, "none": None},
}
CANONICAL_VECTOR_BYTES = (
    b'{"a":{"flag":true,"none":null,"unicode":"caf\xc3\xa9"},"z":[3,2,1]}'
)
CANONICAL_VECTOR_SHA256 = "f2775020d241a5758698f508e39b3df7fe70c0a22d2fac3bd937bdf21c010a30"


def independent_canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def independent_sha256_json(value: Any) -> str:
    return hashlib.sha256(independent_canonical_json(value)).hexdigest()


def assert_exact_inventory(
    actual: Iterable[object], expected: Iterable[object], *, label: str
) -> None:
    actual_tuple = tuple(actual)
    expected_tuple = tuple(expected)
    actual_set = set(actual_tuple)
    expected_set = set(expected_tuple)
    missing = expected_set - actual_set
    unexpected = actual_set - expected_set
    duplicates = len(actual_tuple) != len(actual_set)
    if missing or unexpected or duplicates or len(actual_tuple) != len(expected_tuple):
        raise AssertionError(
            f"{label} inventory mismatch: missing={sorted(missing)!r} "
            f"unexpected={sorted(unexpected)!r} duplicates={duplicates} "
            f"actual_count={len(actual_tuple)} expected_count={len(expected_tuple)}"
        )


def assert_exact_mapping(
    actual: Mapping[object, object], expected: Mapping[object, object], *, label: str
) -> None:
    assert_exact_inventory(actual, expected, label=f"{label} keys")
    wrong = {
        key: (expected[key], actual[key])
        for key in expected
        if actual[key] != expected[key]
    }
    if wrong:
        raise AssertionError(f"{label} values mismatch: {wrong!r}")
