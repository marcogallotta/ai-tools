"""Step 9 movement-only submit and live-evidence recovery."""
from __future__ import annotations

import re
import json
import sqlite3
from typing import Any

from .constants import COOKING_PROJECT_GID
from .database import finalize_confirmed_movement_attempt, record_audit, transition_operation
from .errors import DishRuleError
from .lifecycle import assert_transition, require_status
from .models import SectionRegistry, resolve_destination, utc_now
from .task_document import DESTINATION_RE, DocumentParseError, parse_task_document, validate_task_document
from .task_store import move_exact, read_complete_task
from .recovery import begin_movement_attempt


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
        """SELECT signed_identity, signed_content_version_id
             FROM verification_cycles
            WHERE operation_id = ? AND outcome = 'approved' AND completed_at IS NOT NULL
            ORDER BY cycle_number DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if row is None or not row["signed_identity"] or not row["signed_content_version_id"]:
        raise DishRuleError("CONFLICT", "confirmed signed content version is missing", rule="signed_identity_missing")
    version = conn.execute(
        "SELECT identity FROM content_versions WHERE content_version_id = ? AND confirmed = 1",
        (row["signed_content_version_id"],),
    ).fetchone()
    if version is None or version["identity"] != row["signed_identity"]:
        raise DishRuleError("CONFLICT", "signed content binding is inconsistent", rule="signed_content_binding_invalid")
    return row["signed_identity"]


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


def submit_live(conn: sqlite3.Connection, backend: Any, *, operation_id: str, schema=None) -> dict[str, Any]:
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
    check = validate_task_document(document, expected_schema_version=op["schema_version"], schema=schema)
    if not check.ok or document.state.values["Verified by"] == "None":
        raise DishRuleError("VALIDATION_FAILED", "live task is not a valid signed ready task", rule="signed_ready_required")
    require_status(document.state, {"ready"}, action="submit")
    assert_transition(action="submit", before="ready", after="ready")

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
            attempt_id = begin_movement_attempt(
                conn, operation_id=operation_id, expected_section_gid=current,
                intended_section_gid=current, purpose="destination_submission",
            )
            finalize_confirmed_movement_attempt(
                conn, attempt_id=attempt_id, live_section_gid=current,
            )
    elif current == registry.verification_queue_gid:
        last = _latest_movement_attempt(conn, operation_id)
        if last is not None and last["intended_section_gid"] == destination.gid and last["outcome"] == "confirmed":
            # A confirmed move cannot be repeated. The live reread above is authoritative;
            # reaching this branch means placement subsequently drifted.
            raise DishRuleError("CONFLICT", "confirmed destination movement no longer matches live placement", rule="post_movement_placement_drift")
        live = move_exact(
            conn, backend, operation_id=operation_id, task_gid=op["task_gid"],
            project_gid=COOKING_PROJECT_GID, expected_identity=signed_identity,
            expected_section_gid=current, intended_section_gid=destination.gid, purpose="destination_submission",
        )
        moved = True
        handoff = "moved_to_destination"
    elif current == registry.research_queue_gid:
        handoff = "research_queue_preserved"
    else:
        handoff = "manual_placement_preserved"

    transition_operation(conn, operation_id, phase="terminal", status="completed", terminal_outcome="destination_handled")
    record_audit(
        conn, submission_id=None, task_gid=op["task_gid"], operation_id=operation_id,
        event_type="operation.submitted", actor_agent=None,
        details={"handoff": handoff, "moved": moved, "section_gid": live.section_gid, "destination_diagnostic": diagnostic},
        result_code="OK", result_ok=True, governed_kind="lock",
        before_state={"operation_id": operation_id, "status": "open"},
        after_state={"operation_id": operation_id, "status": "completed"},
        actor_source="submission-command",
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


def recover_operation(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    requested_outcome: str = "inspect",
    reason: str = "live evidence reconciliation",
) -> dict[str, Any]:
    """Reconcile interrupted mutation intents from exact live task evidence."""
    from .database import (
        finalize_confirmed_movement_attempt, finalize_confirmed_write_attempt,
        finalize_not_applied_movement_attempt, finalize_not_applied_write_attempt,
    )
    op = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if op is None:
        raise DishRuleError("NOT_FOUND", f"operation not found: {operation_id}", rule="operation_not_found")
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    actions: list[dict[str, Any]] = []

    write_attempt = conn.execute(
        """SELECT * FROM write_attempts WHERE operation_id = ?
             ORDER BY started_at DESC, rowid DESC LIMIT 1""", (operation_id,)
    ).fetchone()
    content_state = "no_incomplete_content_write"
    if write_attempt is not None and (
        write_attempt["outcome"] in {"started", "uncertain"}
        or (write_attempt["outcome"] == "confirmed" and not write_attempt["confirmed_content_version_id"])
    ):
        intended_exact = (write_attempt["intended_title"] is not None and write_attempt["intended_notes"] is not None
                          and live.title == write_attempt["intended_title"] and live.notes == write_attempt["intended_notes"])
        if live.identity == write_attempt["intended_identity"] and intended_exact:
            evidence = "applied"
            content_state = "confirmed_content_write"
            if requested_outcome != "inspect":
                if requested_outcome != "applied":
                    raise DishRuleError("CONFLICT", "requested outcome contradicts live write evidence", rule="recovery_outcome_mismatch")
                version = finalize_confirmed_write_attempt(
                    conn, attempt_id=write_attempt["attempt_id"], task_gid=op["task_gid"],
                    title=live.title, notes=live.notes, schema_version=write_attempt["schema_version"] or op["schema_version"],
                )
                actions.append({"kind": "content_write", "outcome": "confirmed", "content_version_id": version["content_version_id"]})
                content_state = "reconciled_confirmed_content_write"
        elif live.identity == write_attempt["expected_identity"]:
            evidence = "not-applied"
            content_state = "confirmed_content_write_not_applied"
            if requested_outcome != "inspect":
                if requested_outcome != "not-applied":
                    raise DishRuleError("CONFLICT", "requested outcome contradicts live write evidence", rule="recovery_outcome_mismatch")
                finalize_not_applied_write_attempt(conn, attempt_id=write_attempt["attempt_id"])
                actions.append({"kind": "content_write", "outcome": "not_applied"})
                content_state = "reconciled_not_applied_content_write"
        else:
            evidence = "unresolved"
            content_state = "unresolved_content_write"
            if requested_outcome != "inspect":
                raise DishRuleError("CONFLICT", "live task does not prove whether the write applied", rule="recovery_evidence_ambiguous", retryable=False)
    elif op["signoff_completed_at"]:
        content_state = "confirmed_signoff"

    movement_attempt = conn.execute(
        """SELECT * FROM movement_attempts WHERE operation_id = ?
             ORDER BY started_at DESC, rowid DESC LIMIT 1""", (operation_id,)
    ).fetchone()
    movement_state = "no_incomplete_movement"
    if movement_attempt is not None and (
        movement_attempt["outcome"] in {"started", "uncertain"}
        or (movement_attempt["outcome"] == "confirmed"
            and movement_attempt["purpose"] == "destination_submission"
            and op["destination_movement_attempt_id"] != movement_attempt["attempt_id"])
    ):
        if live.section_gid == movement_attempt["intended_section_gid"]:
            movement_state = "confirmed_movement"
            if requested_outcome != "inspect":
                if requested_outcome != "applied":
                    raise DishRuleError("CONFLICT", "requested outcome contradicts live movement evidence", rule="recovery_outcome_mismatch")
                finalized = finalize_confirmed_movement_attempt(conn, attempt_id=movement_attempt["attempt_id"], live_section_gid=live.section_gid)
                actions.append({"kind": "movement", "outcome": "confirmed", "purpose": finalized["purpose"]})
                movement_state = "reconciled_confirmed_movement"
        elif live.section_gid == movement_attempt["expected_section_gid"]:
            movement_state = "confirmed_movement_not_applied"
            if requested_outcome != "inspect":
                if requested_outcome != "not-applied":
                    raise DishRuleError("CONFLICT", "requested outcome contradicts live movement evidence", rule="recovery_outcome_mismatch")
                finalize_not_applied_movement_attempt(conn, attempt_id=movement_attempt["attempt_id"])
                actions.append({"kind": "movement", "outcome": "not_applied", "purpose": movement_attempt["purpose"]})
                movement_state = "reconciled_not_applied_movement"
        else:
            movement_state = "unresolved_movement"
            if requested_outcome != "inspect":
                raise DishRuleError("CONFLICT", "live placement does not prove whether movement applied", rule="recovery_evidence_ambiguous", retryable=False)
    elif op["signoff_completed_at"] and op["movement_completed_at"] is None:
        movement_state = "confirmed_signoff_incomplete_movement"

    # Resume only the missing suffix of a declared high-level workflow.
    from .database import complete_operation_step, create_verification_cycle, pending_operation_steps, transition_operation
    pending_steps = pending_operation_steps(conn, operation_id)
    if requested_outcome == "applied":
        for step in pending_steps:
            intended = json.loads(step["intended_json"])
            if step["step_name"] == "candidate_write":
                if live.title == intended.get("title") and live.notes == intended.get("notes"):
                    complete_operation_step(conn, operation_id, "candidate_write")
                    actions.append({"kind": "workflow_step", "step": "candidate_write", "outcome": "confirmed"})
                else:
                    raise DishRuleError("CONFLICT", "live content does not satisfy candidate-write intent", rule="workflow_step_evidence_mismatch")
            elif step["step_name"] == "verification_cycle":
                existing = conn.execute("SELECT cycle_id FROM verification_cycles WHERE operation_id=? AND completed_at IS NULL ORDER BY cycle_number DESC LIMIT 1", (operation_id,)).fetchone()
                if existing is None:
                    number = conn.execute("SELECT COALESCE(MAX(cycle_number),0)+1 FROM verification_cycles WHERE task_gid=?", (op["task_gid"],)).fetchone()[0]
                    existing = create_verification_cycle(conn, operation_id=operation_id, task_gid=op["task_gid"], cycle_number=number, protocol_release=intended["protocol_release"], protocol_text=intended.get("protocol_text"))
                complete_operation_step(conn, operation_id, "verification_cycle")
                actions.append({"kind": "workflow_step", "step": "verification_cycle", "outcome": "confirmed"})
            elif step["step_name"] in {"planning_write", "migration_write", "small_corrected_write", "hold_write", "large_write", "reopen_write"}:
                if live.title == intended.get("title") and live.notes == intended.get("notes"):
                    complete_operation_step(conn, operation_id, step["step_name"])
                    actions.append({"kind": "workflow_step", "step": step["step_name"], "outcome": "confirmed"})
                else:
                    raise DishRuleError("CONFLICT", "live content does not satisfy workflow write intent", rule="workflow_step_evidence_mismatch")
            elif step["step_name"] in {"verification_handoff", "planning_handoff"}:
                target = intended["section_gid"]
                purpose = "verification_handoff" if step["step_name"] == "verification_handoff" else "planning_handoff"
                if live.section_gid != target:
                    live = move_exact(conn, backend, operation_id=operation_id, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID, expected_identity=live.identity, expected_section_gid=live.section_gid, intended_section_gid=target, purpose=purpose)
                complete_operation_step(conn, operation_id, step["step_name"])
                if step["step_name"] == "verification_handoff":
                    transition_operation(conn, operation_id, phase="await_verification")
                actions.append({"kind": "workflow_step", "step": step["step_name"], "outcome": "confirmed"})
            elif step["step_name"] == "small_review_binding":
                from .step7 import bind_cycle_review
                if live.identity != intended["identity"]:
                    raise DishRuleError("CONFLICT", "live correction does not match review-binding intent", rule="workflow_step_evidence_mismatch")
                bind_cycle_review(conn, cycle_id=intended["cycle_id"], operation_id=operation_id, task_gid=op["task_gid"], identity=live.identity)
                complete_operation_step(conn, operation_id, "small_review_binding")
                actions.append({"kind": "workflow_step", "step": "small_review_binding", "outcome": "confirmed"})
            elif step["step_name"] == "small_signoff":
                cycle = conn.execute("SELECT * FROM verification_cycles WHERE cycle_id=?", (intended["cycle_id"],)).fetchone()
                if cycle is not None and cycle["outcome"] == "approved":
                    complete_operation_step(conn, operation_id, "small_signoff")
                else:
                    from .step7 import approve_live
                    result = approve_live(conn, backend, operation_id=operation_id, agent=intended["agent"], reviewed_identity=live.identity, semantic_review_complete=True, provenance_complete=True, correction_class="small", run_id=intended.get("run_id"), independence_attestation=intended.get("independence_attestation"))
                    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
                    complete_operation_step(conn, operation_id, "small_signoff")
                actions.append({"kind": "workflow_step", "step": "small_signoff", "outcome": "confirmed"})
            elif step["step_name"] in {"planning_terminal", "migration_terminal"}:
                transition_operation(conn, operation_id, phase=intended.get("phase", "terminal"), status=intended.get("status", "completed"), terminal_outcome=intended.get("terminal_outcome"))
                complete_operation_step(conn, operation_id, step["step_name"])
                actions.append({"kind": "workflow_step", "step": step["step_name"], "outcome": "confirmed"})

    refreshed = conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    record_audit(conn, submission_id=None, task_gid=op["task_gid"], operation_id=operation_id,
                 event_type="operation.recovery", actor_agent=None,
                 details={"requested_outcome": requested_outcome, "reason": reason, "actions": actions,
                          "content_state": content_state, "movement_state": movement_state},
                 result_code="OK", result_ok=True)
    return {
        "operation_id": operation_id, "live_identity": live.identity, "live_section_gid": live.section_gid,
        "content_recovery_state": content_state, "movement_recovery_state": movement_state,
        "actions": actions, "operation_status": refreshed["status"],
        "write_attempt": None if write_attempt is None else {k: write_attempt[k] for k in write_attempt.keys()},
        "movement_attempt": None if movement_attempt is None else {k: movement_attempt[k] for k in movement_attempt.keys()},
    }
