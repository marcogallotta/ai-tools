"""Independent literal contract for checked-in recovery fixture artifacts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


EXPECTED_LIVE_TASKS = {
    "task-write-applied": ("verification", "applied", "not-applied", ("write_attempts", "wa-applied", "outcome", "started", "confirmed")),
    "task-write-not-applied": ("verification", "not_applied", "applied", ("write_attempts", "wa-not-applied", "outcome", "started", "not_applied")),
    "task-write-uncertain": ("verification", "uncertain", None, None),
    "task-move-applied": ("destination", "applied", None, None),
    "task-move-not-applied": ("verification", "not_applied", None, None),
    "task-signed": ("destination", "none", None, None),
    "task-attempt-not-applied": ("verification", "none", None, None),
    "task-evidence-hold": ("verification", "held", None, None),
    "task-human_review-hold": ("verification", "held", None, None),
    "task-move-ambiguous": ("third-section", "uncertain", None, ("movement_attempts", "ma-ambiguous", "outcome", "uncertain", "uncertain")),
    "task-partial-write": ("verification", "applied", None, ("write_attempts", "wa-partial", "outcome", "uncertain", "confirmed")),
}

EXPECTED_SCENARIOS = {
    "write-applied": ("task-write-applied", ("started write", "live applied", "truthful identities")),
    "write-not-applied": ("task-write-not-applied", ("started write", "live not applied", "truthful identities")),
    "write-uncertain": ("task-write-uncertain", ("uncertain write", "divergent live task", "multiple identities")),
    "movement-applied": ("task-move-applied", ("destination movement", "live applied")),
    "movement-not-applied": ("task-move-not-applied", ("destination movement", "live not_applied")),
    "signed-binding": ("task-signed", ("signed binding", "multiple content versions", "confirmed write")),
    "closed-not-applied-attempt": ("task-attempt-not-applied", ("not-applied attempt",)),
    "evidence-hold": ("task-evidence-hold", ("evidence review", "evidence-hold", "reviewed binding")),
    "human_review-hold": ("task-human_review-hold", ("human_review review", "verification-hold", "reviewed binding")),
    "checked-in-movement-ambiguity": ("task-move-ambiguous", ("checked-in movement ambiguity", "contradictory recovery decisions", "exact row diff")),
    "partially-finalized-write": ("task-partial-write", ("partially finalized attempt", "exact row diff")),
}

EXPECTED_OPERATIONS = {
    "task-write-applied": ("open", "prepare_required"),
    "task-write-not-applied": ("open", "prepare_required"),
    "task-write-uncertain": ("uncertain", "prepare_required"),
    "task-move-applied": ("open", "prepare_required"),
    "task-move-not-applied": ("open", "prepare_required"),
    "task-signed": ("completed", "terminal"),
    "task-attempt-not-applied": ("open", "prepare_required"),
    "task-evidence-hold": ("open", "held_evidence"),
    "task-human_review-hold": ("open", "held_human"),
    "task-move-ambiguous": ("uncertain", "await_verification"),
    "task-partial-write": ("uncertain", "prepare_required"),
}

EXPECTED_WRITE_ATTEMPTS = {
    "wa-applied": ("op-write-applied", "started", "content_write"),
    "wa-not-applied": ("op-write-not-applied", "started", "content_write"),
    "wa-uncertain": ("op-write-uncertain", "uncertain", "content_write"),
    "wa-confirmed": ("op-signed", "confirmed", "verification_signoff"),
    "wa-closed-not-applied": ("op-attempt-not-applied", "not_applied", "content_write"),
    "wa-partial": ("op-partial-write", "uncertain", "content_write"),
}

EXPECTED_MOVEMENT_ATTEMPTS = {
    "ma-applied": ("op-move-applied", "started", "destination_submission"),
    "ma-not-applied": ("op-move-not-applied", "started", "destination_submission"),
    "ma-ambiguous": ("op-move-ambiguous", "uncertain", "verification_handoff"),
}

EXPECTED_WRITE_VERSION_EVIDENCE = {
    "wa-applied": ("fixture", "test.modified_at", 1),
    "wa-not-applied": ("fixture", "test.modified_at", 1),
    "wa-uncertain": ("fixture", "test.modified_at", 1),
}

EXPECTED_MOVEMENT_VERSION_EVIDENCE = {
    "ma-applied": ("fixture", "test.modified_at", 1),
    "ma-not-applied": ("fixture", "test.modified_at", 1),
}

EXPECTED_CYCLES = {
    "cycle-signed": ("op-signed", "approved", True),
    "cycle-evidence": ("op-evidence-hold", "evidence-hold", False),
    "cycle-human_review": ("op-human_review-hold", "verification-hold", False),
}


def assert_sidecars_match_contract(live_path: Path, matrix_path: Path) -> None:
    live = json.loads(live_path.read_text(encoding="utf-8"))
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    assert live["schema"] == 1
    projected_live = {}
    for item in live["tasks"]:
        row_diff = item.get("expected_row_diff")
        projected_live[item["task_gid"]] = (
            item["section_gid"],
            item["expected_recovery"],
            item.get("contradictory_request"),
            None if row_diff is None else (
                row_diff["table"], row_diff["id"], row_diff["column"],
                row_diff["before"], row_diff["after"],
            ),
        )
    assert projected_live == EXPECTED_LIVE_TASKS

    assert matrix["schema"] == 1
    assert matrix["database"] == "dish-tool-recovery-v12.sqlite"
    assert matrix["live_sidecar"] == "live-tasks.json"
    assert matrix["identity_algorithm"] == "dish_tool.database.content_identity"
    projected_matrix = {
        item["id"]: (item["task_gid"], tuple(item["covers"]))
        for item in matrix["scenarios"]
    }
    assert projected_matrix == EXPECTED_SCENARIOS


def assert_database_matches_contract(database_path: Path) -> None:
    conn = sqlite3.connect(database_path)
    try:
        operations = {
            task_gid: (status, phase)
            for task_gid, status, phase in conn.execute(
                "SELECT task_gid,status,phase FROM operations"
            )
        }
        assert operations == EXPECTED_OPERATIONS

        write_attempts = {
            attempt_id: (operation_id, outcome, purpose)
            for attempt_id, operation_id, outcome, purpose in conn.execute(
                "SELECT attempt_id,operation_id,outcome,purpose FROM write_attempts"
            )
        }
        assert write_attempts == EXPECTED_WRITE_ATTEMPTS

        write_version_evidence = {
            attempt_id: (expected_modified_at, version_source, version_reliable)
            for attempt_id, expected_modified_at, version_source, version_reliable
            in conn.execute(
                "SELECT attempt_id,expected_modified_at,version_source,version_reliable "
                "FROM write_attempts WHERE expected_modified_at IS NOT NULL"
            )
        }
        assert write_version_evidence == EXPECTED_WRITE_VERSION_EVIDENCE

        movement_attempts = {
            attempt_id: (operation_id, outcome, purpose)
            for attempt_id, operation_id, outcome, purpose in conn.execute(
                "SELECT attempt_id,operation_id,outcome,purpose FROM movement_attempts"
            )
        }
        assert movement_attempts == EXPECTED_MOVEMENT_ATTEMPTS

        movement_version_evidence = {
            attempt_id: (expected_modified_at, version_source, version_reliable)
            for attempt_id, expected_modified_at, version_source, version_reliable
            in conn.execute(
                "SELECT attempt_id,expected_modified_at,version_source,version_reliable "
                "FROM movement_attempts WHERE expected_modified_at IS NOT NULL"
            )
        }
        assert movement_version_evidence == EXPECTED_MOVEMENT_VERSION_EVIDENCE

        cycles = {
            cycle_id: (operation_id, outcome, completed_at is not None)
            for cycle_id, operation_id, outcome, completed_at in conn.execute(
                "SELECT cycle_id,operation_id,outcome,completed_at FROM verification_cycles"
            )
        }
        assert cycles == EXPECTED_CYCLES
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
