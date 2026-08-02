#!/usr/bin/env python3
"""Generate deterministic recovery acceptance fixtures with truthful identities."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent

from dish_tool.database import content_identity
from dish_tool.database_initialization import initialize_database

DB_PATH = HERE / "dish-tool-recovery-v12.sqlite"
SIDECAR_PATH = HERE / "live-tasks.json"
MATRIX_PATH = HERE / "fixture-matrix.json"
NOW = "2026-07-25T12:00:00+00:00"


def ident(title: str, notes: str) -> str:
    return content_identity(title, notes).digest


def add_operation(conn: sqlite3.Connection, *, op: str, task: str, expected: str,
                  status: str = "open", completed: bool = False,
                  content_done: bool = False, signoff_done: bool = False,
                  phase: str | None = None, terminal_outcome: str | None = None,
                  expected_section_gid: str = "verification") -> None:
    resolved_phase = phase or ("terminal" if completed or status in {"completed", "cancelled"} else "prepare_required")
    conn.execute(
        """INSERT INTO operations(
            operation_id, task_gid, operation_kind, status, editor_agent,
            researcher_agent, verifier_agent, run_id, independence_attestation,
            expected_identity, schema_version, content_write_completed_at,
            signoff_completed_at, movement_completed_at, created_at, completed_at,
            destination_movement_attempt_id, phase, terminal_outcome,
            expected_section_gid
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (op, task, "change", status, "gpt", "gpt", "codex", f"run-{op}", None,
         expected, "2", NOW if content_done else None, NOW if signoff_done else None,
         None, NOW, NOW if completed else None, None, resolved_phase, terminal_outcome,
         expected_section_gid),
    )
    conn.execute(
        """INSERT INTO operation_steps(
               operation_id, step_name, intended_json, completed_at
           ) VALUES(?, 'change_intent', ?, ?)""",
        (
            op,
            json.dumps(
                {
                    "level": "small",
                    "reason": "Deterministic recovery fixture change",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            NOW,
        ),
    )


def add_version(conn: sqlite3.Connection, *, version: str, task: str, op: str,
                boundary: str, title: str, notes: str, confirmed: int = 1) -> str:
    digest = ident(title, notes)
    conn.execute(
        """INSERT INTO content_versions(
            content_version_id, task_gid, operation_id, boundary, identity,
            title, notes, confirmed, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (version, task, op, boundary, digest, title, notes, confirmed, NOW),
    )
    return digest


def add_state(conn: sqlite3.Connection, *, task: str, title: str, notes: str) -> str:
    digest = ident(title, notes)
    version_id = f"head-{task}"
    conn.execute(
        """INSERT INTO content_versions(
            content_version_id, task_gid, operation_id, boundary, identity,
            title, notes, confirmed, created_at
        ) VALUES(?,?,NULL,'fixture_task_head',?,?,?,1,?)""",
        (version_id, task, digest, title, notes, NOW),
    )
    conn.execute(
        """INSERT INTO task_content_state(
            task_gid, last_confirmed_identity, last_confirmed_title,
            last_confirmed_notes, schema_version, confirmed_at,
            last_confirmed_content_version_id
        ) VALUES(?,?,?,?,?,?,?)""",
        (task, digest, title, notes, "2", NOW, version_id),
    )
    return digest


def build(output_dir: str | Path | None = None) -> None:
    root = HERE if output_dir is None else Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / DB_PATH.name
    sidecar_path = root / SIDECAR_PATH.name
    matrix_path = root / MATRIX_PATH.name
    db_path.unlink(missing_ok=True)
    conn = initialize_database(db_path)
    conn.execute("UPDATE schema_migrations SET applied_at=?", (NOW,))
    sidecars: list[dict[str, object]] = []
    scenarios: list[dict[str, object]] = []

    # Interrupted write: live state proves application.
    task, op = "task-write-applied", "op-write-applied"
    old_t, old_n = "Dish A", "baseline notes"
    new_t, new_n = "Dish A", "updated notes"
    old_id = add_state(conn, task=task, title=old_t, notes=old_n)
    add_operation(conn, op=op, task=task, expected=old_id)
    conn.execute("""INSERT INTO write_attempts(
        attempt_id, operation_id, expected_identity, intended_identity, outcome,
        started_at, purpose, intended_title, intended_notes, schema_version, context_json
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        ("wa-applied", op, old_id, ident(new_t, new_n), "started", NOW,
         "content_write", new_t, new_n, "2", json.dumps({"scenario": "applied"})))
    sidecars.append({"task_gid": task, "title": new_t, "notes": new_n,
                     "section_gid": "verification", "expected_recovery": "applied", "contradictory_request": "not-applied", "expected_row_diff": {"table": "write_attempts", "id": "wa-applied", "column": "outcome", "before": "started", "after": "confirmed"}})
    scenarios.append({"id": "write-applied", "task_gid": task,
                      "covers": ["started write", "live applied", "truthful identities"]})

    # Interrupted write: live state proves non-application.
    task, op = "task-write-not-applied", "op-write-not-applied"
    old_t, old_n = "Dish B", "baseline notes"
    new_t, new_n = "Dish B", "proposed notes"
    old_id = add_state(conn, task=task, title=old_t, notes=old_n)
    add_operation(conn, op=op, task=task, expected=old_id)
    conn.execute("""INSERT INTO write_attempts(
        attempt_id, operation_id, expected_identity, intended_identity, outcome,
        started_at, purpose, intended_title, intended_notes, schema_version, context_json
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        ("wa-not-applied", op, old_id, ident(new_t, new_n), "started", NOW,
         "content_write", new_t, new_n, "2", json.dumps({"scenario": "not_applied"})))
    sidecars.append({"task_gid": task, "title": old_t, "notes": old_n,
                     "section_gid": "verification", "expected_recovery": "not_applied", "contradictory_request": "applied", "expected_row_diff": {"table": "write_attempts", "id": "wa-not-applied", "column": "outcome", "before": "started", "after": "not_applied"}})
    scenarios.append({"id": "write-not-applied", "task_gid": task,
                      "covers": ["started write", "live not applied", "truthful identities"]})

    # Interrupted write: divergent live state remains uncertain.
    task, op = "task-write-uncertain", "op-write-uncertain"
    old_t, old_n = "Dish C", "baseline notes"
    new_t, new_n = "Dish C", "proposed notes"
    live_t, live_n = "Dish C externally edited", "third state"
    old_id = add_state(conn, task=task, title=old_t, notes=old_n)
    add_operation(conn, op=op, task=task, expected=old_id, status="uncertain")
    conn.execute("""INSERT INTO write_attempts(
        attempt_id, operation_id, expected_identity, intended_identity, outcome,
        started_at, purpose, intended_title, intended_notes, schema_version, context_json
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        ("wa-uncertain", op, old_id, ident(new_t, new_n), "uncertain", NOW,
         "content_write", new_t, new_n, "2", json.dumps({"scenario": "uncertain"})))
    sidecars.append({"task_gid": task, "title": live_t, "notes": live_n,
                     "section_gid": "verification", "expected_recovery": "uncertain"})
    scenarios.append({"id": "write-uncertain", "task_gid": task,
                      "covers": ["uncertain write", "divergent live task", "multiple identities"]})

    # Destination movement applied and not applied.
    for suffix, live_section, expected in [
        ("applied", "destination", "applied"),
        ("not-applied", "verification", "not_applied"),
    ]:
        task, op = f"task-move-{suffix}", f"op-move-{suffix}"
        title, notes = f"Dish move {suffix}", "ready notes"
        cid = add_state(conn, task=task, title=title, notes=notes)
        add_operation(conn, op=op, task=task, expected=cid)
        conn.execute("""INSERT INTO movement_attempts(
            attempt_id, operation_id, expected_section_gid, intended_section_gid,
            outcome, started_at, purpose
        ) VALUES(?,?,?,?,?,?,?)""",
            (f"ma-{suffix}", op, "verification", "destination", "started", NOW,
             "destination_submission"))
        sidecars.append({"task_gid": task, "title": title, "notes": notes,
                         "section_gid": live_section, "expected_recovery": expected})
        scenarios.append({"id": f"movement-{suffix}", "task_gid": task,
                          "covers": ["destination movement", f"live {expected}"]})

    # Confirmed and not-applied attempts, plus multiple content versions and signed binding.
    task, op = "task-signed", "op-signed"
    base_t, base_n = "Dish signed", "version one"
    signed_t, signed_n = "Dish signed", "version two signed"
    base_id = add_state(conn, task=task, title=base_t, notes=base_n)
    add_operation(conn, op=op, task=task, expected=base_id, status="open",
                  completed=False, content_done=True, signoff_done=False, phase="await_verification")
    v1 = "cv-signed-1"; v2 = "cv-signed-2"
    add_version(conn, version=v1, task=task, op=op, boundary="baseline",
                title=base_t, notes=base_n)
    signed_id = add_version(conn, version=v2, task=task, op=op, boundary="signed",
                            title=signed_t, notes=signed_n)
    conn.execute("""UPDATE task_content_state SET
        last_confirmed_identity=?, last_confirmed_title=?, last_confirmed_notes=?,
        last_confirmed_content_version_id=?
        WHERE task_gid=?""", (signed_id, signed_t, signed_n, v2, task))
    conn.execute("""INSERT INTO write_attempts(
        attempt_id, operation_id, expected_identity, intended_identity, outcome,
        started_at, finished_at, purpose, intended_title, intended_notes,
        schema_version, context_json, confirmed_content_version_id
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("wa-confirmed", op, base_id, signed_id, "confirmed", NOW, NOW,
         "verification_signoff", signed_t, signed_n, "2",
         json.dumps({"cycle_id": "cycle-signed"}), v2))
    conn.execute("""INSERT INTO verification_cycles(
        cycle_id, operation_id, task_gid, cycle_number, protocol_release,
        verifier_agent, run_id, correction_class, outcome, created_at, completed_at,
        protocol_text, reviewed_content_version_id, reviewed_identity,
        signed_content_version_id, signed_identity
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("cycle-signed", op, task, 1, "1.0.10", "codex", "run-op-signed",
         "small", "approved", NOW, NOW, "verification protocol", v2, signed_id, v2, signed_id))
    conn.execute("""UPDATE operations SET signoff_completed_at=?, status='completed', phase='terminal',
        completed_at=?, terminal_outcome='submitted' WHERE operation_id=?""", (NOW, NOW, op))
    sidecars.append({"task_gid": task, "title": signed_t, "notes": signed_n,
                     "section_gid": "destination", "expected_recovery": "none"})
    scenarios.append({"id": "signed-binding", "task_gid": task,
                      "covers": ["signed binding", "multiple content versions", "confirmed write"]})

    task, op = "task-attempt-not-applied", "op-attempt-not-applied"
    title, notes = "Dish attempt", "stable notes"
    cid = add_state(conn, task=task, title=title, notes=notes)
    add_operation(conn, op=op, task=task, expected=cid)
    conn.execute("""INSERT INTO write_attempts(
        attempt_id, operation_id, expected_identity, intended_identity, outcome,
        started_at, finished_at, purpose, intended_title, intended_notes, schema_version
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        ("wa-closed-not-applied", op, cid, ident(title, notes + " proposed"),
         "not_applied", NOW, NOW, "content_write", title, notes + " proposed", "2"))
    sidecars.append({"task_gid": task, "title": title, "notes": notes,
                     "section_gid": "verification", "expected_recovery": "none"})
    scenarios.append({"id": "closed-not-applied-attempt", "task_gid": task,
                      "covers": ["not-applied attempt"]})

    # Evidence and human-review holds, including a Verification hold.
    for route, outcome in [("evidence", "evidence-hold"), ("human_review", "verification-hold")]:
        task, op = f"task-{route}-hold", f"op-{route}-hold"
        title, notes = f"Dish {route}", "reviewed notes"
        cid = add_state(conn, task=task, title=title, notes=notes)
        add_operation(conn, op=op, task=task, expected=cid, phase="held_evidence" if route == "evidence" else "held_human")
        vid = f"cv-{route}-reviewed"
        add_version(conn, version=vid, task=task, op=op, boundary="verification_read",
                    title=title, notes=notes)
        conn.execute("""INSERT INTO verification_cycles(
            cycle_id, operation_id, task_gid, cycle_number, protocol_release,
            verifier_agent, run_id, outcome, route, resume_state, created_at,
            protocol_text, reviewed_content_version_id, reviewed_identity,
            hold_content_version_id, hold_identity, hold_section_gid
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"cycle-{route}", op, task, 1, "1.0.10", "codex", f"run-{route}",
             outcome, route, "pending-verification", NOW, "verification protocol", vid, cid,
             vid, cid, "verification"))
        sidecars.append({"task_gid": task, "title": title, "notes": notes,
                         "section_gid": "verification", "expected_recovery": "held"})
        scenarios.append({"id": f"{route}-hold", "task_gid": task,
                          "covers": [f"{route} review", outcome, "reviewed binding"]})

    # Checked-in/handoff movement ambiguity: live placement is neither side.
    task, op = "task-move-ambiguous", "op-move-ambiguous"
    title, notes = "Dish ambiguous move", "stable notes"
    cid = add_state(conn, task=task, title=title, notes=notes)
    add_operation(conn, op=op, task=task, expected=cid, status="uncertain", phase="await_verification")
    conn.execute("""INSERT INTO movement_attempts(
        attempt_id, operation_id, expected_section_gid, intended_section_gid,
        outcome, started_at, purpose
    ) VALUES(?,?,?,?,?,?,?)""", ("ma-ambiguous", op, "research", "verification", "uncertain", NOW, "verification_handoff"))
    sidecars.append({"task_gid": task, "title": title, "notes": notes,
                     "section_gid": "third-section", "expected_recovery": "uncertain",
                     "expected_row_diff": {"table": "movement_attempts", "id": "ma-ambiguous", "column": "outcome", "before": "uncertain", "after": "uncertain"}})
    scenarios.append({"id": "checked-in-movement-ambiguity", "task_gid": task,
                      "covers": ["checked-in movement ambiguity", "contradictory recovery decisions", "exact row diff"]})

    # Partially finalized write: content-version evidence exists but attempt is uncertain.
    task, op = "task-partial-write", "op-partial-write"
    old_t, old_n = "Dish partial", "old notes"
    new_t, new_n = "Dish partial", "new notes"
    old_id = add_state(conn, task=task, title=old_t, notes=old_n)
    add_operation(conn, op=op, task=task, expected=old_id, status="uncertain")
    version_id = "cv-partial-intended"
    new_id = add_version(conn, version=version_id, task=task, op=op, boundary="content_write", title=new_t, notes=new_n)
    conn.execute("""INSERT INTO write_attempts(
        attempt_id, operation_id, expected_identity, intended_identity, outcome,
        started_at, purpose, intended_title, intended_notes, schema_version,
        confirmed_content_version_id
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", ("wa-partial", op, old_id, new_id, "uncertain", NOW,
        "content_write", new_t, new_n, "2", version_id))
    sidecars.append({"task_gid": task, "title": new_t, "notes": new_n,
                     "section_gid": "verification", "expected_recovery": "applied",
                     "expected_row_diff": {"table": "write_attempts", "id": "wa-partial", "column": "outcome", "before": "uncertain", "after": "confirmed"}})
    scenarios.append({"id": "partially-finalized-write", "task_gid": task,
                      "covers": ["partially finalized attempt", "exact row diff"]})

    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    conn.close()
    if integrity != "ok":
        raise RuntimeError(f"fixture integrity failed: {integrity}")

    sidecar_path.write_text(json.dumps({"schema": 1, "tasks": sidecars}, indent=2) + "\n")
    matrix_path.write_text(json.dumps({
        "schema": 1,
        "database": db_path.name,
        "live_sidecar": sidecar_path.name,
        "identity_algorithm": "dish_tool.database.content_identity",
        "scenarios": scenarios,
    }, indent=2) + "\n")


if __name__ == "__main__":
    build()
