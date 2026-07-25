"""Step 9 movement-only submit and live-evidence recovery."""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from .constants import COOKING_PROJECT_GID
from .database import mark_operation_completion, record_audit
from .errors import DishRuleError
from .models import SectionRegistry, resolve_destination, utc_now
from .task_document import DESTINATION_RE, DocumentParseError, parse_task_document, validate_task_document
from .task_store import move_exact, read_complete_task


def _operation(conn: sqlite3.Connection, operation_id: str):
    row = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if row is None:
        raise DishRuleError("NOT_FOUND", f"operation not found: {operation_id}", rule="operation_not_found")
    if row["status"] != "open":
        raise DishRuleError("WRONG_STATE", "operation is not open", rule="operation_not_open")
    if row["signoff_completed_at"] is None:
        raise DishRuleError("WRONG_STATE", "operation has no confirmed signoff", rule="signoff_not_completed")
    return row


def _signed_identity(conn: sqlite3.Connection, operation_id: str) -> str:
    row = conn.execute(
        "SELECT identity FROM content_versions WHERE operation_id = ? AND confirmed = 1 ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (operation_id,),
    ).fetchone()
    if row is None:
        raise DishRuleError("CONFLICT", "confirmed signed content identity is missing", rule="signed_identity_missing")
    return row["identity"]


def _destination(document, registry: SectionRegistry):
    value = document.planning_brief.values["Destination section"]
    if value == "[destination missing]":
        return None, "destination_missing"
    if value == "[destination invalid]":
        return None, "destination_invalid"
    match = DESTINATION_RE.match(value)
    if match is None:
        return None, "destination_invalid"
    try:
        return resolve_destination(match.group("name"), match.group("gid"), registry), None
    except DishRuleError:
        return None, "destination_invalid"


def _latest_movement_attempt(conn: sqlite3.Connection, operation_id: str):
    return conn.execute(
        "SELECT * FROM movement_attempts WHERE operation_id = ? ORDER BY started_at DESC, rowid DESC LIMIT 1",
        (operation_id,),
    ).fetchone()


def submit_live(conn: sqlite3.Connection, backend: Any, *, operation_id: str) -> dict[str, Any]:
    op = _operation(conn, operation_id)
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    signed_identity = _signed_identity(conn, operation_id)
    if live.identity != signed_identity:
        raise DishRuleError(
            "CONFLICT", "task content changed after signoff; a new Verification cycle is required",
            rule="post_signoff_content_drift",
            details={"signed_identity": signed_identity, "actual_identity": live.identity},
        )
    try:
        document = parse_task_document(f"{live.title}\n{live.notes}")
    except DocumentParseError as exc:
        raise DishRuleError("VALIDATION_FAILED", "signed task is no longer canonical", rule=exc.rule) from exc
    check = validate_task_document(document, expected_schema_version=op["schema_version"])
    if not check.ok or document.state.values["Status"] != "ready" or document.state.values["Verified by"] == "None":
        raise DishRuleError("VALIDATION_FAILED", "live task is not a valid signed ready task", rule="signed_ready_required")

    registry = SectionRegistry.from_sections(backend.list_sections(COOKING_PROJECT_GID))
    destination, diagnostic = _destination(document, registry)
    current = live.section_gid
    handoff = diagnostic
    moved = False

    if destination is None:
        handoff = diagnostic
    elif current == destination.gid:
        handoff = "already_at_destination"
        if op["movement_completed_at"] is None:
            mark_operation_completion(conn, operation_id, "movement")
    elif current == registry.verification_queue_gid:
        last = _latest_movement_attempt(conn, operation_id)
        if last is not None and last["intended_section_gid"] == destination.gid and last["outcome"] == "confirmed":
            # A confirmed move cannot be repeated. The live reread above is authoritative;
            # reaching this branch means placement subsequently drifted.
            raise DishRuleError("CONFLICT", "confirmed destination movement no longer matches live placement", rule="post_movement_placement_drift")
        live = move_exact(
            conn, backend, operation_id=operation_id, task_gid=op["task_gid"],
            project_gid=COOKING_PROJECT_GID, expected_identity=signed_identity,
            expected_section_gid=current, intended_section_gid=destination.gid,
        )
        moved = True
        handoff = "moved_to_destination"
    elif current == registry.research_queue_gid:
        handoff = "research_queue_preserved"
    else:
        handoff = "manual_placement_preserved"

    conn.execute(
        "UPDATE operations SET status = 'completed', completed_at = ? WHERE operation_id = ? AND status = 'open'",
        (utc_now(), operation_id),
    )
    record_audit(
        conn, submission_id=None, task_gid=op["task_gid"], operation_id=operation_id,
        event_type="operation.submitted", actor_agent=None,
        details={"handoff": handoff, "moved": moved, "section_gid": live.section_gid, "destination_diagnostic": diagnostic},
        result_code="OK", result_ok=True,
    )
    return {
        "operation_id": operation_id,
        "signed_identity": signed_identity,
        "handoff": handoff,
        "moved": moved,
        "destination": None if destination is None else {"name": destination.name, "gid": destination.gid},
        "destination_diagnostic": diagnostic,
        "task": {"gid": live.gid, "title": live.title, "notes": live.notes, "section_gid": live.section_gid},
    }


def recover_operation(conn: sqlite3.Connection, backend: Any, *, operation_id: str) -> dict[str, Any]:
    op = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if op is None:
        raise DishRuleError("NOT_FOUND", f"operation not found: {operation_id}", rule="operation_not_found")
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    signed = _signed_identity(conn, operation_id) if op["signoff_completed_at"] else None
    write_attempt = conn.execute(
        "SELECT * FROM write_attempts WHERE operation_id = ? ORDER BY started_at DESC, rowid DESC LIMIT 1", (operation_id,)
    ).fetchone()
    movement_attempt = _latest_movement_attempt(conn, operation_id)
    if signed is not None and live.identity == signed:
        content_state = "confirmed_signoff"
    elif write_attempt is not None and write_attempt["outcome"] == "uncertain":
        content_state = "uncertain_content_write"
    elif write_attempt is not None and write_attempt["outcome"] == "confirmed":
        content_state = "confirmed_content_write_incomplete_recording"
    else:
        content_state = "no_incomplete_content_write"
    if movement_attempt is not None and movement_attempt["outcome"] == "uncertain":
        movement_state = "uncertain_movement"
    elif signed is not None and op["movement_completed_at"] is None:
        movement_state = "confirmed_signoff_incomplete_movement"
    else:
        movement_state = "no_incomplete_movement"
    return {
        "operation_id": operation_id,
        "live_identity": live.identity,
        "live_section_gid": live.section_gid,
        "content_recovery_state": content_state,
        "movement_recovery_state": movement_state,
        "write_attempt": None if write_attempt is None else {k: write_attempt[k] for k in write_attempt.keys()},
        "movement_attempt": None if movement_attempt is None else {k: movement_attempt[k] for k in movement_attempt.keys()},
    }
