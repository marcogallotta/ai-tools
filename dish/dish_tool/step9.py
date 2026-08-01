"""Step 9 movement-only submit and live-evidence recovery."""
from __future__ import annotations

import dataclasses
import hashlib
import re
import json
import sqlite3
from typing import Any

from .constants import COOKING_PROJECT_GID
from .database import (
    atomic_persistence,
    complete_operation_step,
    content_identity,
    create_verification_cycle,
    declare_operation_step,
    finalize_confirmed_movement_attempt,
    pending_operation_steps,
    record_actor_fact,
    record_audit,
    transition_operation,
)
from .errors import DishRuleError
from .small_correction_lineage import assert_small_correction_write_lineage
from .lifecycle import assert_transition, require_status
from .models import SectionRegistry, resolve_destination, utc_now
from .task_document import (
    DESTINATION_RE,
    DocumentParseError,
    PlanningBrief,
    document_parse_error_payloads,
    finding_payload,
    parse_canonical_planning_notes,
    parse_task_document,
    validate_planning_brief,
    validate_task_document,
)
from .task_store import move_exact, read_complete_task, write_exact_content
from .recovery import begin_movement_attempt
from .governed_diff import canonical_diff


def _operation(conn: sqlite3.Connection, operation_id: str):
    row = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if row is None:
        raise DishRuleError("NOT_FOUND", f"operation not found: {operation_id}", rule="operation_not_found")
    if row["status"] != "open":
        raise DishRuleError("WRONG_STATE", "operation is not open", rule="operation_not_open")
    if row["signoff_completed_at"] is None:
        raise DishRuleError("WRONG_STATE", "operation has no confirmed signoff", rule="signoff_not_completed")
    return row



def _approved_signoff(conn: sqlite3.Connection, operation_id: str):
    row = conn.execute(
        """SELECT cycle.*, version.identity AS version_identity,
                  version.confirmed AS version_confirmed,
                  version.task_gid AS version_task_gid
             FROM verification_cycles AS cycle
             JOIN content_versions AS version
               ON version.content_version_id=cycle.signed_content_version_id
            WHERE cycle.operation_id=? AND cycle.outcome='approved'
              AND cycle.completed_at IS NOT NULL
            ORDER BY cycle.completed_at DESC, cycle.rowid DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if (
        row is None
        or not row["signed_identity"]
        or not row["signed_content_version_id"]
        or row["version_confirmed"] != 1
        or row["version_identity"] != row["signed_identity"]
        or row["version_task_gid"] != row["task_gid"]
    ):
        raise DishRuleError(
            "CONFLICT",
            "confirmed signed content version is missing or inconsistent",
            rule="signed_content_binding_invalid",
        )
    return row


def submission_identity_evidence(
    conn: sqlite3.Connection, operation_id: str
) -> dict[str, Any]:
    """Return the immutable approval plus any completed Marco destination-repair chain."""
    approved = _approved_signoff(conn, operation_id)
    approved_identity = approved["signed_identity"]
    effective_identity = approved_identity
    latest_repair = None
    rows = conn.execute(
        """SELECT attempt.*, version.identity AS version_identity,
                  version.confirmed AS version_confirmed
             FROM write_attempts AS attempt
             JOIN content_versions AS version
               ON version.content_version_id=attempt.confirmed_content_version_id
            WHERE attempt.operation_id=?
              AND attempt.purpose='destination_repair'
              AND attempt.outcome='confirmed'
            ORDER BY attempt.started_at, attempt.rowid""",
        (operation_id,),
    ).fetchall()
    for row in rows:
        context = json.loads(row["context_json"] or "{}")
        step_name = str(context.get("repair_step") or "")
        completed_step = (
            None
            if not step_name
            else conn.execute(
                "SELECT completed_at FROM operation_steps WHERE operation_id=? AND step_name=?",
                (operation_id, step_name),
            ).fetchone()
        )
        if completed_step is None or completed_step["completed_at"] is None:
            continue
        if (
            context.get("authorization_kind") != "marco_destination_repair"
            or context.get("approved_identity") != approved_identity
            or context.get("source_identity") != effective_identity
            or row["expected_identity"] != effective_identity
            or row["intended_identity"] != row["version_identity"]
            or row["version_confirmed"] != 1
        ):
            raise DishRuleError(
                "CONFLICT",
                "destination repair evidence is inconsistent",
                rule="destination_repair_evidence_invalid",
            )
        effective_identity = row["intended_identity"]
        latest_repair = {
            "repair_step": step_name,
            "source_identity": context.get("source_identity"),
            "repaired_identity": effective_identity,
            "before_destination": context.get("before_destination"),
            "after_destination": context.get("after_destination"),
            "reason": context.get("reason"),
            "actor_run_id": context.get("actor_run_id"),
            "write_attempt_id": row["attempt_id"],
            "content_version_id": row["confirmed_content_version_id"],
        }
    return {
        "approved_identity": approved_identity,
        "approved_cycle_id": approved["cycle_id"],
        "effective_identity": effective_identity,
        "destination_repair": latest_repair,
    }


def latest_destination_failure(conn: sqlite3.Connection, operation_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT details FROM audit_events
             WHERE operation_id=? AND event_type='operation.destination_movement_failed'
             ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        details = json.loads(row["details"])
    except (TypeError, json.JSONDecodeError):
        return None
    return details if isinstance(details, dict) else None

def _signed_identity(conn: sqlite3.Connection, operation_id: str) -> str:
    return str(_approved_signoff(conn, operation_id)["signed_identity"])


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


def _movement_failure_details(
    *,
    op,
    destination_gid: str | None,
    retry_safe: bool,
    reason: str,
) -> dict[str, Any]:
    return {
        "task_gid": op["task_gid"],
        "failed_destination_gid": destination_gid,
        "operation_state": "ready_move_failed",
        "content_approved": True,
        "movement_retry_safe": retry_safe,
        "required_authorization": (
            None if retry_safe else "Marco admin destination repair"
        ),
        "legal_next_action": (
            "submit" if retry_safe else "dish-admin repair-destination"
        ),
        "retryable": bool(retry_safe),
        "failure_reason": reason,
    }


def _record_movement_failure(
    conn: sqlite3.Connection,
    *,
    op,
    destination_gid: str | None,
    retry_safe: bool,
    reason: str,
) -> dict[str, Any]:
    details = _movement_failure_details(
        op=op,
        destination_gid=destination_gid,
        retry_safe=retry_safe,
        reason=reason,
    )
    digest = hashlib.sha256(
        json.dumps(details, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    step_name = f"destination_failure:{digest}"
    intended = {**details, "classification_step": step_name}
    declare_operation_step(conn, op["operation_id"], step_name, intended)
    with atomic_persistence(conn, "destination_failure_classification"):
        if op["phase"] != "ready_move_failed":
            transition_operation(
                conn, op["operation_id"], phase="ready_move_failed"
            )
        prior = conn.execute(
            """SELECT 1 FROM audit_events
                 WHERE operation_id=?
                   AND event_type='operation.destination_movement_failed'
                   AND json_extract(details, '$.classification_step')=? LIMIT 1""",
            (op["operation_id"], step_name),
        ).fetchone()
        if prior is None:
            record_audit(
                conn, submission_id=None, task_gid=op["task_gid"],
                operation_id=op["operation_id"],
                event_type="operation.destination_movement_failed",
                actor_agent=None, details=intended,
                result_code="BACKEND_REJECTED" if retry_safe else "VALIDATION_FAILED",
                result_ok=False, governed_kind="decision",
                before_state={"phase": op["phase"]},
                after_state={"phase": "ready_move_failed", "retry_safe": retry_safe},
            )
        complete_operation_step(conn, op["operation_id"], step_name)
    return intended


def completed_submit_live(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    schema=None,
) -> dict[str, Any]:
    """Prove and return an already completed submission without mutating again."""
    op = conn.execute(
        "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    if op is None:
        raise DishRuleError(
            "NOT_FOUND", f"operation not found: {operation_id}", rule="operation_not_found"
        )
    if op["status"] != "completed" or op["terminal_outcome"] != "destination_handled":
        raise DishRuleError(
            "WRONG_STATE",
            "operation is not an already completed submission",
            rule="operation_not_open",
            details={"actual": op["status"]},
        )
    identity_evidence = submission_identity_evidence(conn, operation_id)
    signed_identity = _signed_identity(conn, operation_id)
    effective_identity = (
        identity_evidence["effective_identity"]
        if identity_evidence["destination_repair"] is not None
        else signed_identity
    )
    live = read_complete_task(
        backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID
    )
    if live.identity != effective_identity:
        raise DishRuleError(
            "CONFLICT",
            "completed submission content no longer matches its signed identity",
            rule="post_signoff_content_drift",
            details={
                "signed_identity": signed_identity,
                "effective_identity": effective_identity,
                "actual_identity": live.identity,
            },
        )
    try:
        document = parse_task_document(f"{live.title}\n{live.notes}")
    except DocumentParseError as exc:
        raise DishRuleError(
            "VALIDATION_FAILED", "signed task is no longer canonical", rule=exc.rule
        ) from exc
    check = validate_task_document(
        document, expected_schema_version=op["schema_version"], schema=schema
    )
    if not check.ok or document.state.values["Verified by"] == "None":
        raise DishRuleError(
            "VALIDATION_FAILED",
            "live task is not the completed signed ready task",
            rule="signed_ready_required",
        )
    require_status(document.state, {"ready"}, action="submit")

    audit = conn.execute(
        """SELECT details FROM audit_events
             WHERE operation_id=? AND event_type='operation.submitted'
               AND result_ok=1
             ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if audit is None:
        raise DishRuleError(
            "CONFLICT",
            "completed submission lacks durable terminal evidence",
            rule="completed_submission_evidence_missing",
        )
    details = json.loads(audit["details"])
    recorded_section = str(details.get("section_gid") or "")
    if not recorded_section or live.section_gid != recorded_section:
        raise DishRuleError(
            "CONFLICT",
            "completed submission placement no longer matches terminal evidence",
            rule="post_movement_placement_drift",
            details={
                "recorded_section_gid": recorded_section or None,
                "actual_section_gid": live.section_gid,
            },
        )
    if details.get("moved") or details.get("handoff") == "already_at_destination":
        movement = conn.execute(
            """SELECT * FROM movement_attempts
                 WHERE operation_id=? AND purpose='destination_submission'
                   AND outcome='confirmed'
                 ORDER BY finished_at DESC, rowid DESC LIMIT 1""",
            (operation_id,),
        ).fetchone()
        if movement is None or movement["intended_section_gid"] != live.section_gid:
            raise DishRuleError(
                "CONFLICT",
                "completed submission movement evidence is inconsistent",
                rule="completed_submission_movement_evidence_invalid",
            )

    destination_value = document.planning_brief.values["Destination section"]
    destination = None
    match = DESTINATION_RE.match(destination_value)
    if match is not None:
        destination = {"name": match.group("name"), "gid": match.group("gid")}
    return {
        "operation_id": operation_id,
        "signed_identity": signed_identity,
        "effective_identity": effective_identity,
        "destination_repair": identity_evidence["destination_repair"],
        "handoff": details.get("handoff"),
        "moved": bool(details.get("moved")),
        "destination": destination,
        "destination_diagnostic": details.get("destination_diagnostic"),
        "task": {
            "gid": live.gid,
            "title": live.title,
            "notes": live.notes,
            "section_gid": live.section_gid,
        },
        "completed_submission_reused": True,
    }


def _finalize_submission_terminal(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    intended: dict[str, Any],
    recovered: bool,
) -> None:
    op = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if op is None:
        raise DishRuleError(
            "NOT_FOUND", "operation not found", rule="operation_not_found"
        )
    if intended.get("movement_attempt_id"):
        movement = conn.execute(
            """SELECT * FROM movement_attempts
                 WHERE attempt_id=? AND operation_id=?""",
            (intended["movement_attempt_id"], operation_id),
        ).fetchone()
        if (
            movement is None
            or movement["outcome"] != "confirmed"
            or movement["confirmed_section_gid"] != intended.get("section_gid")
        ):
            raise DishRuleError(
                "CONFLICT", "submission movement evidence is incomplete",
                rule="workflow_movement_incomplete",
            )
    with atomic_persistence(conn, "submission_terminal"):
        declare_operation_step(conn, operation_id, "submission_terminal", intended)
        if op["status"] != "completed":
            transition_operation(
                conn, operation_id, phase="terminal", status="completed",
                terminal_outcome="destination_handled",
            )
        else:
            # Acquire the local writer lock before any fault-injection pause in
            # the proof audit, so readers cannot interleave inside the suffix.
            conn.execute(
                "UPDATE operations SET phase=phase WHERE operation_id=?",
                (operation_id,),
            )
        prior = conn.execute(
            """SELECT 1 FROM audit_events
                 WHERE operation_id=? AND event_type='operation.submitted' LIMIT 1""",
            (operation_id,),
        ).fetchone()
        if prior is None:
            record_audit(
                conn, submission_id=None, task_gid=op["task_gid"],
                operation_id=operation_id, event_type="operation.submitted",
                actor_agent=None,
                details={
                    "handoff": intended.get("handoff"),
                    "moved": bool(intended.get("moved")),
                    "section_gid": intended.get("section_gid"),
                    "destination_diagnostic": intended.get("destination_diagnostic"),
                    "movement_attempt_id": intended.get("movement_attempt_id"),
                    "recovered": recovered,
                },
                result_code="OK", result_ok=True, governed_kind="lock",
                before_state={"operation_id": operation_id, "status": "open"},
                after_state={"operation_id": operation_id, "status": "completed"},
                actor_source="recovery" if recovered else "submission-command",
            )
        complete_operation_step(conn, operation_id, "submission_terminal")
        intent_step = conn.execute(
            "SELECT completed_at FROM operation_steps WHERE operation_id=? AND step_name='submission_terminal_intent'",
            (operation_id,),
        ).fetchone()
        if intent_step is not None and intent_step["completed_at"] is None:
            complete_operation_step(conn, operation_id, "submission_terminal_intent")


def _resume_submission_terminal(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
) -> dict[str, Any] | None:
    step = conn.execute(
        """SELECT * FROM operation_steps
             WHERE operation_id=?
               AND step_name IN ('submission_terminal_intent','submission_terminal')
               AND completed_at IS NULL
             ORDER BY CASE step_name WHEN 'submission_terminal_intent' THEN 0 ELSE 1 END
             LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if step is None:
        return None
    intended = json.loads(step["intended_json"])
    op = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    live = read_complete_task(
        backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID
    )
    if (
        live.identity != intended.get("effective_identity")
        or live.section_gid != intended.get("section_gid")
    ):
        raise DishRuleError(
            "CONFLICT", "live task does not satisfy submission terminal intent",
            rule="workflow_step_evidence_mismatch",
        )
    _finalize_submission_terminal(
        conn, operation_id=operation_id, intended=intended, recovered=True
    )
    destination = intended.get("destination")
    return {
        "operation_id": operation_id,
        "signed_identity": intended.get("signed_identity"),
        "effective_identity": intended.get("effective_identity"),
        "destination_repair": intended.get("destination_repair"),
        "handoff": intended.get("handoff"),
        "moved": bool(intended.get("moved")),
        "destination": destination,
        "destination_diagnostic": intended.get("destination_diagnostic"),
        "task": dataclasses.asdict(live),
        "submission_recovered": True,
    }


def submit_live(conn: sqlite3.Connection, backend: Any, *, operation_id: str, schema=None) -> dict[str, Any]:
    op = _operation(conn, operation_id)
    resumed = _resume_submission_terminal(
        conn, backend, operation_id=operation_id
    )
    if resumed is not None:
        return resumed
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    identity_evidence = submission_identity_evidence(conn, operation_id)
    signed_identity = _signed_identity(conn, operation_id)
    effective_identity = (
        identity_evidence["effective_identity"]
        if identity_evidence["destination_repair"] is not None
        else signed_identity
    )
    if live.identity != effective_identity:
        raise DishRuleError(
            "CONFLICT", "task content changed after signoff; a new Verification cycle is required",
            rule="post_signoff_content_drift",
            details={
                "signed_identity": signed_identity,
                "effective_identity": effective_identity,
                "actual_identity": live.identity,
            },
        )
    try:
        document = parse_task_document(f"{live.title}\n{live.notes}")
    except DocumentParseError as exc:
        raise DishRuleError("VALIDATION_FAILED", "signed task is no longer canonical", rule=exc.rule) from exc
    check = validate_task_document(document, expected_schema_version=op["schema_version"], schema=schema)
    if not check.ok or document.state.values["Verified by"] == "None":
        raise DishRuleError("VALIDATION_FAILED", "live task is not a valid signed ready task", rule="signed_ready_required")
    require_status(document.state, {"ready"}, action="submit")
    pending_material_changes = [
        line
        for line in document.material_changes
        if line.endswith(" — pending-verification")
    ]
    if pending_material_changes:
        raise DishRuleError(
            "VALIDATION_FAILED",
            "a Material changes entry still claims verification is pending",
            rule="material_change_verification_pending",
            retryable=False,
            details={
                "pending_material_changes": pending_material_changes,
                "required_state": "verified",
            },
        )
    assert_transition(action="submit", before="ready", after="ready")

    registry = SectionRegistry.from_sections(backend.list_sections(COOKING_PROJECT_GID))
    destination, diagnostic = _destination(document, registry)
    current = live.section_gid
    handoff = diagnostic
    moved = False
    if destination is None:
        destination_value = document.planning_brief.values["Destination section"]
        destination_match = DESTINATION_RE.match(destination_value)
        destination_gid = None if destination_match is None else destination_match.group("gid")
        details = _record_movement_failure(
            conn,
            op=op,
            destination_gid=destination_gid,
            retry_safe=False,
            reason=str(diagnostic or "destination_invalid"),
        )
        raise DishRuleError(
            "VALIDATION_FAILED",
            "approved content is ready but its destination cannot be resolved",
            rule="destination_movement_unresolvable",
            retryable=False,
            details=details,
        )
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
        try:
            live = move_exact(
                conn, backend, operation_id=operation_id, task_gid=op["task_gid"],
                project_gid=COOKING_PROJECT_GID, expected_identity=effective_identity,
                expected_section_gid=current, intended_section_gid=destination.gid, purpose="destination_submission",
            )
        except DishRuleError as exc:
            attempt = _latest_movement_attempt(conn, operation_id)
            if attempt is not None and attempt["outcome"] == "not_applied":
                details = _record_movement_failure(
                    conn,
                    op=op,
                    destination_gid=destination.gid,
                    retry_safe=True,
                    reason=exc.rule or "destination_movement_rejected",
                )
                raise DishRuleError(
                    exc.code,
                    str(exc),
                    rule=exc.rule or "destination_movement_rejected",
                    retryable=exc.retryable,
                    details={**exc.details, **details},
                    errors=exc.errors,
                ) from exc
            raise
        moved = True
        handoff = "moved_to_destination"
    elif current == registry.research_queue_gid:
        handoff = "research_queue_preserved"
    else:
        handoff = "manual_placement_preserved"

    # Persist a recoverable terminal intent after exact movement confirmation.
    # The operation remains open until the proof audit, terminal state, and step
    # completion commit together.
    movement_attempt_id = op["destination_movement_attempt_id"]
    refreshed_op = conn.execute(
        "SELECT destination_movement_attempt_id FROM operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    if refreshed_op is not None and refreshed_op["destination_movement_attempt_id"]:
        movement_attempt_id = refreshed_op["destination_movement_attempt_id"]
    terminal_intent = {
        "phase": "terminal", "status": "completed",
        "terminal_outcome": "destination_handled",
        "signed_identity": signed_identity,
        "effective_identity": effective_identity,
        "destination_repair": identity_evidence["destination_repair"],
        "handoff": handoff, "moved": moved,
        "section_gid": live.section_gid,
        "destination": None if destination is None else {"name": destination.name, "gid": destination.gid},
        "destination_diagnostic": diagnostic,
        "movement_attempt_id": movement_attempt_id,
    }
    declare_operation_step(
        conn, operation_id, "submission_terminal_intent", terminal_intent
    )
    _finalize_submission_terminal(
        conn, operation_id=operation_id, intended=terminal_intent, recovered=False
    )
    return {
        "operation_id": operation_id,
        "signed_identity": signed_identity,
        "effective_identity": effective_identity,
        "destination_repair": identity_evidence["destination_repair"],
        "handoff": handoff,
        "moved": moved,
        "destination": None if destination is None else {"name": destination.name, "gid": destination.gid},
        "destination_diagnostic": diagnostic,
        "task": {"gid": live.gid, "title": live.title, "notes": live.notes, "section_gid": live.section_gid},
    }


def _complete_destination_repair_step(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    step_name: str,
    context: dict[str, Any],
    repaired_identity: str,
    recovered: bool = False,
) -> None:
    with atomic_persistence(conn, "destination_repair_finalize"):
        op = conn.execute(
            "SELECT task_gid, phase FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if op is None:
            raise DishRuleError(
                "NOT_FOUND", "operation not found", rule="operation_not_found"
            )
        complete_operation_step(conn, operation_id, step_name)
        if op["phase"] != "await_submission":
            transition_operation(conn, operation_id, phase="await_submission")
        prior = conn.execute(
            """SELECT 1 FROM audit_events
                 WHERE operation_id=? AND event_type='operation.destination_repaired'
                   AND json_extract(details, '$.repair_step')=? LIMIT 1""",
            (operation_id, step_name),
        ).fetchone()
        if prior is None:
            record_audit(
                conn,
                submission_id=None,
                task_gid=op["task_gid"],
                operation_id=operation_id,
                event_type="operation.destination_repaired",
                actor_agent=None,
                details={
                    **context,
                    "repair_step": step_name,
                    "repaired_identity": repaired_identity,
                    "recovered": recovered,
                },
                result_code="OK",
                result_ok=True,
                governed_kind="decision",
                before_state={
                    "identity": context.get("source_identity"),
                    "destination": context.get("before_destination"),
                },
                after_state={
                    "identity": repaired_identity,
                    "destination": context.get("after_destination"),
                },
                actor_run_id=context.get("actor_run_id"),
                actor_source="recovery" if recovered else "marco-admin",
            )


def _resume_destination_repair(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    destination_section_gid: str,
    reason: str,
    actor_run_id: str | None,
) -> dict[str, Any] | None:
    attempt = conn.execute(
        """SELECT * FROM write_attempts
             WHERE operation_id=? AND purpose='destination_repair'
               AND outcome='confirmed'
             ORDER BY started_at DESC, rowid DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if attempt is None:
        return None
    context = json.loads(attempt["context_json"] or "{}")
    step_name = str(context.get("repair_step") or "")
    if not step_name:
        return None
    step = conn.execute(
        "SELECT * FROM operation_steps WHERE operation_id=? AND step_name=?",
        (operation_id, step_name),
    ).fetchone()
    prior = conn.execute(
        """SELECT 1 FROM audit_events
             WHERE operation_id=? AND event_type='operation.destination_repaired'
               AND json_extract(details, '$.repair_step')=? LIMIT 1""",
        (operation_id, step_name),
    ).fetchone()
    if step is None or (step["completed_at"] is not None and prior is not None):
        return None
    if str(destination_section_gid or "").strip() != str(context.get("after_destination") or "").rsplit(" — ", 1)[-1]:
        raise DishRuleError(
            "CONFLICT", "destination repair replay differs from durable intent",
            rule="destination_repair_replay_mismatch",
        )
    if str(reason or "").strip() != str(context.get("reason") or "").strip():
        raise DishRuleError(
            "CONFLICT", "destination repair reason differs from durable intent",
            rule="destination_repair_replay_mismatch",
        )
    expected_run = str(context.get("actor_run_id") or "").strip() or None
    supplied_run = str(actor_run_id or "").strip() or None
    if expected_run != supplied_run:
        raise DishRuleError(
            "AGENT_MISMATCH", "destination repair run differs from durable intent",
            rule="destination_repair_replay_mismatch",
        )
    op = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    live = read_complete_task(
        backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID
    )
    if live.identity != attempt["intended_identity"]:
        raise DishRuleError(
            "CONFLICT", "live content does not satisfy destination repair intent",
            rule="workflow_step_evidence_mismatch",
        )
    _complete_destination_repair_step(
        conn, operation_id=operation_id, step_name=step_name, context=context,
        repaired_identity=live.identity, recovered=True,
    )
    after_name, after_gid = str(context["after_destination"]).rsplit(" — ", 1)
    return {
        "operation_id": operation_id, "task_gid": op["task_gid"],
        "content_approved": True,
        "approval_cycle_id": context.get("approved_cycle_id"),
        "approved_identity": context.get("approved_identity"),
        "source_identity": context.get("source_identity"),
        "repaired_identity": live.identity,
        "before_destination": context.get("before_destination"),
        "after_destination": {"name": after_name, "gid": after_gid},
        "reason": context.get("reason"),
        "movement_retry_safe": True, "legal_next_action": "submit",
        "task": dataclasses.asdict(live), "repair_recovered": True,
    }


def repair_destination_live(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    destination_section_gid: str,
    reason: str,
    actor_run_id: str | None = None,
    schema=None,
) -> dict[str, Any]:
    resumed = _resume_destination_repair(
        conn, backend, operation_id=operation_id,
        destination_section_gid=destination_section_gid, reason=reason,
        actor_run_id=actor_run_id,
    )
    if resumed is not None:
        return resumed
    op = _operation(conn, operation_id)
    if op["phase"] != "ready_move_failed":
        raise DishRuleError(
            "WRONG_STATE",
            "destination repair is legal only after an unrecoverable final movement failure",
            rule="destination_repair_not_required",
            details={"actual_phase": op["phase"]},
        )
    failure = latest_destination_failure(conn, operation_id)
    if failure is None or bool(failure.get("movement_retry_safe")):
        raise DishRuleError(
            "WRONG_STATE",
            "the failed movement is retryable without changing the approved destination",
            rule="destination_repair_not_required",
            details={"legal_next_action": "submit"},
        )
    clean_gid = str(destination_section_gid or "").strip()
    clean_reason = str(reason or "").strip()
    if not clean_gid:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "replacement destination section GID is required",
            rule="destination_section_gid_required",
            details={"field": "destination_section_gid"},
        )
    if not clean_reason:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "destination repair reason is required",
            rule="destination_repair_reason_required",
            details={"field": "reason"},
        )

    identity_evidence = submission_identity_evidence(conn, operation_id)
    live = read_complete_task(
        backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID
    )
    if live.identity != identity_evidence["effective_identity"]:
        raise DishRuleError(
            "CONFLICT",
            "live task does not match the approved destination-repair baseline",
            rule="post_signoff_content_drift",
            details={
                "required_identity": identity_evidence["effective_identity"],
                "actual_identity": live.identity,
            },
        )
    try:
        document = parse_task_document(f"{live.title}\n{live.notes}")
    except DocumentParseError as exc:
        raise DishRuleError(
            "VALIDATION_FAILED", "signed task is no longer canonical", rule=exc.rule
        ) from exc
    check = validate_task_document(
        document, expected_schema_version=op["schema_version"], schema=schema
    )
    if not check.ok or document.state.values["Status"] != "ready":
        raise DishRuleError(
            "VALIDATION_FAILED",
            "destination repair requires the approved ready task",
            rule="signed_ready_required",
        )

    registry = SectionRegistry.from_sections(backend.list_sections(COOKING_PROJECT_GID))
    replacement = registry.by_gid.get(clean_gid)
    if replacement is None:
        raise DishRuleError(
            "VALIDATION_FAILED",
            "replacement destination does not exist in Cooking",
            rule="destination_unresolved",
            details={"gid": clean_gid},
        )
    replacement = resolve_destination(replacement.name, replacement.gid, registry)
    before_destination = document.planning_brief.values["Destination section"]
    after_destination = f"{replacement.name} — {replacement.gid}"
    if before_destination == after_destination:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "replacement destination is unchanged",
            rule="destination_repair_unchanged",
            details={"destination": after_destination},
        )
    failed_gid = str(failure.get("failed_destination_gid") or "").strip() or None
    current_match = DESTINATION_RE.match(before_destination)
    current_gid = None if current_match is None else current_match.group("gid")
    if failed_gid is not None and current_gid != failed_gid:
        raise DishRuleError(
            "CONFLICT",
            "live destination no longer matches the recorded movement failure",
            rule="destination_repair_failure_drift",
            details={"failed_destination_gid": failed_gid, "live_destination_gid": current_gid},
        )

    values = dict(document.planning_brief.values)
    values["Destination section"] = after_destination
    repaired = dataclasses.replace(
        document, planning_brief=PlanningBrief(values)
    )
    diff = canonical_diff(document, repaired)
    if set(diff) != {"planning.Destination section"}:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "destination repair attempted to change unrelated canonical content",
            rule="destination_repair_scope_invalid",
            details={"changed_paths": sorted(diff)},
        )
    rendered = repaired.render().splitlines()
    title = rendered[0]
    notes = "\n".join(rendered[1:]) + "\n"
    intended_identity = content_identity(title, notes).digest
    step_name = f"destination_repair:{intended_identity}"
    context = {
        "authorization_kind": "marco_destination_repair",
        "approved_identity": identity_evidence["approved_identity"],
        "approved_cycle_id": identity_evidence["approved_cycle_id"],
        "source_identity": identity_evidence["effective_identity"],
        "before_destination": before_destination,
        "after_destination": after_destination,
        "failed_destination_gid": failed_gid,
        "reason": clean_reason,
        "actor_run_id": str(actor_run_id or "").strip() or None,
        "repair_step": step_name,
    }
    declare_operation_step(
        conn,
        operation_id,
        step_name,
        {"title": title, "notes": notes, **context},
    )
    try:
        confirmed = write_exact_content(
            conn,
            backend,
            operation_id=operation_id,
            task_gid=op["task_gid"],
            project_gid=COOKING_PROJECT_GID,
            expected_identity=live.identity,
            expected_section_gid=live.section_gid,
            title=title,
            notes=notes,
            schema_version=op["schema_version"],
            purpose="destination_repair",
            context=context,
        )
    except DishRuleError:
        attempt = conn.execute(
            """SELECT * FROM write_attempts
                 WHERE operation_id=? AND purpose='destination_repair'
                   AND intended_identity=?
                 ORDER BY started_at DESC, rowid DESC LIMIT 1""",
            (operation_id, intended_identity),
        ).fetchone()
        if attempt is not None and attempt["outcome"] == "not_applied":
            complete_operation_step(conn, operation_id, step_name)
        raise
    _complete_destination_repair_step(
        conn,
        operation_id=operation_id,
        step_name=step_name,
        context=context,
        repaired_identity=confirmed.identity,
    )
    return {
        "operation_id": operation_id,
        "task_gid": op["task_gid"],
        "content_approved": True,
        "approval_cycle_id": identity_evidence["approved_cycle_id"],
        "approved_identity": identity_evidence["approved_identity"],
        "source_identity": identity_evidence["effective_identity"],
        "repaired_identity": confirmed.identity,
        "before_destination": before_destination,
        "after_destination": {"name": replacement.name, "gid": replacement.gid},
        "reason": clean_reason,
        "movement_retry_safe": True,
        "legal_next_action": "submit",
        "task": {
            "gid": confirmed.gid,
            "title": confirmed.title,
            "notes": confirmed.notes,
            "section_gid": confirmed.section_gid,
        },
    }


def _assert_recoverable_planning_content(notes: str) -> None:
    try:
        brief = parse_canonical_planning_notes(notes)
    except DocumentParseError as exc:
        raise DishRuleError(
            "CONFLICT",
            "Planning recovery will not accept non-canonical live content",
            rule="planning_recovery_validation_failed",
            errors=document_parse_error_payloads(exc),
        ) from exc
    findings = validate_planning_brief(brief).findings
    if findings:
        raise DishRuleError(
            "CONFLICT",
            "Planning recovery will not accept invalid live content",
            rule="planning_recovery_validation_failed",
            errors=[finding_payload(finding) for finding in findings],
        )



def _latest_recovery_attempt(
    conn: sqlite3.Connection, *, table: str, operation_id: str
):
    if table not in {"write_attempts", "movement_attempts"}:
        raise ValueError("unsupported recovery attempt table")
    return conn.execute(
        f"SELECT * FROM {table} WHERE operation_id=? "
        "ORDER BY started_at DESC, rowid DESC LIMIT 1",
        (operation_id,),
    ).fetchone()


def _recover_content_attempt(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    op,
    live,
    requested_outcome: str,
    actions: list[dict[str, Any]],
) -> tuple[str, Any]:
    from .database import (
        finalize_confirmed_write_attempt,
        finalize_not_applied_write_attempt,
    )

    attempt = _latest_recovery_attempt(
        conn, table="write_attempts", operation_id=operation_id
    )
    state = "no_incomplete_content_write"
    incomplete = attempt is not None and (
        attempt["outcome"] in {"started", "uncertain"}
        or (
            attempt["outcome"] == "confirmed"
            and not attempt["confirmed_content_version_id"]
        )
    )
    if not incomplete:
        return (
            "confirmed_signoff" if op["signoff_completed_at"] else state,
            attempt,
        )
    intended_exact = (
        attempt["intended_title"] is not None
        and attempt["intended_notes"] is not None
        and live.title == attempt["intended_title"]
        and live.notes == attempt["intended_notes"]
    )
    if live.identity == attempt["intended_identity"] and intended_exact:
        state = "confirmed_content_write"
        if requested_outcome == "inspect":
            return state, attempt
        if requested_outcome != "applied":
            raise DishRuleError(
                "CONFLICT",
                "requested outcome contradicts live write evidence",
                rule="recovery_outcome_mismatch",
            )
        if op["operation_kind"] == "planning":
            _assert_recoverable_planning_content(live.notes)
        version = finalize_confirmed_write_attempt(
            conn,
            attempt_id=attempt["attempt_id"],
            task_gid=op["task_gid"],
            title=live.title,
            notes=live.notes,
            schema_version=attempt["schema_version"] or op["schema_version"],
        )
        actions.append(
            {
                "kind": "content_write",
                "outcome": "confirmed",
                "content_version_id": version["content_version_id"],
            }
        )
        return "reconciled_confirmed_content_write", attempt
    if live.identity == attempt["expected_identity"]:
        state = "confirmed_content_write_not_applied"
        if requested_outcome == "inspect":
            return state, attempt
        if requested_outcome != "not-applied":
            raise DishRuleError(
                "CONFLICT",
                "requested outcome contradicts live write evidence",
                rule="recovery_outcome_mismatch",
            )
        finalize_not_applied_write_attempt(conn, attempt_id=attempt["attempt_id"])
        actions.append({"kind": "content_write", "outcome": "not_applied"})
        if attempt["purpose"] == "destination_repair":
            context = json.loads(attempt["context_json"] or "{}")
            repair_step = str(context.get("repair_step") or "")
            if repair_step:
                pending = conn.execute(
                    "SELECT completed_at FROM operation_steps "
                    "WHERE operation_id=? AND step_name=?",
                    (operation_id, repair_step),
                ).fetchone()
                if pending is not None and pending["completed_at"] is None:
                    complete_operation_step(conn, operation_id, repair_step)
                    actions.append(
                        {
                            "kind": "workflow_step",
                            "step": repair_step,
                            "outcome": "not_applied",
                        }
                    )
        return "reconciled_not_applied_content_write", attempt
    if requested_outcome != "inspect":
        raise DishRuleError(
            "CONFLICT",
            "live task does not prove whether the write applied",
            rule="recovery_evidence_ambiguous",
            retryable=False,
        )
    return "unresolved_content_write", attempt


def _recover_movement_attempt(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    op,
    live,
    requested_outcome: str,
    actions: list[dict[str, Any]],
) -> tuple[str, Any]:
    from .database import (
        finalize_confirmed_movement_attempt,
        finalize_not_applied_movement_attempt,
    )

    attempt = _latest_recovery_attempt(
        conn, table="movement_attempts", operation_id=operation_id
    )
    state = "no_incomplete_movement"
    incomplete = attempt is not None and (
        attempt["outcome"] in {"started", "uncertain"}
        or (
            attempt["outcome"] == "confirmed"
            and attempt["purpose"] == "destination_submission"
            and op["destination_movement_attempt_id"] != attempt["attempt_id"]
        )
    )
    if not incomplete:
        if op["signoff_completed_at"] and op["movement_completed_at"] is None:
            state = "confirmed_signoff_incomplete_movement"
        return state, attempt
    if live.section_gid == attempt["intended_section_gid"]:
        state = "confirmed_movement"
        if requested_outcome == "inspect":
            return state, attempt
        if requested_outcome != "applied":
            raise DishRuleError(
                "CONFLICT",
                "requested outcome contradicts live movement evidence",
                rule="recovery_outcome_mismatch",
            )
        if (
            op["operation_kind"] == "planning"
            and attempt["purpose"] == "planning_handoff"
        ):
            _assert_recoverable_planning_content(live.notes)
        finalized = finalize_confirmed_movement_attempt(
            conn,
            attempt_id=attempt["attempt_id"],
            live_section_gid=live.section_gid,
        )
        actions.append(
            {
                "kind": "movement",
                "outcome": "confirmed",
                "purpose": finalized["purpose"],
            }
        )
        return "reconciled_confirmed_movement", attempt
    if live.section_gid == attempt["expected_section_gid"]:
        state = "confirmed_movement_not_applied"
        if requested_outcome == "inspect":
            return state, attempt
        if requested_outcome != "not-applied":
            raise DishRuleError(
                "CONFLICT",
                "requested outcome contradicts live movement evidence",
                rule="recovery_outcome_mismatch",
            )
        finalize_not_applied_movement_attempt(
            conn, attempt_id=attempt["attempt_id"]
        )
        actions.append(
            {
                "kind": "movement",
                "outcome": "not_applied",
                "purpose": attempt["purpose"],
            }
        )
        return "reconciled_not_applied_movement", attempt
    if requested_outcome != "inspect":
        raise DishRuleError(
            "CONFLICT",
            "live placement does not prove whether movement applied",
            rule="recovery_evidence_ambiguous",
            retryable=False,
        )
    return "unresolved_movement", attempt
def _recover_workflow_step_group_1(
    conn: sqlite3.Connection,
    *,
    backend: Any,
    operation_id: str,
    op,
    live,
    step,
    intended: dict[str, Any],
    actions: list[dict[str, Any]],
) -> tuple[bool, Any]:
    if step['step_name'].startswith('destination_repair:'):
        if live.title != intended.get('title') or live.notes != intended.get('notes'):
            raise DishRuleError('CONFLICT', 'live content does not satisfy destination-repair intent', rule='workflow_step_evidence_mismatch')
        _complete_destination_repair_step(conn, operation_id=operation_id, step_name=step['step_name'], context=intended, repaired_identity=live.identity, recovered=True)
        actions.append({'kind': 'workflow_step', 'step': step['step_name'], 'outcome': 'confirmed'})
        return True, live
    if step['step_name'] == 'candidate_write':
        if live.title == intended.get('title') and live.notes == intended.get('notes'):
            complete_operation_step(conn, operation_id, 'candidate_write')
            actions.append({'kind': 'workflow_step', 'step': 'candidate_write', 'outcome': 'confirmed'})
        else:
            raise DishRuleError('CONFLICT', 'live content does not satisfy candidate-write intent', rule='workflow_step_evidence_mismatch')
        return True, live
    if step['step_name'] == 'handoff_validation':
        if live.title != intended.get('title') or live.notes != intended.get('notes'):
            raise DishRuleError('CONFLICT', 'live content does not satisfy handoff-validation intent', rule='workflow_step_evidence_mismatch')
        exact = parse_task_document(f'{live.title}\n{live.notes}')
        validation = validate_task_document(exact, expected_schema_version=intended['schema_version'], schema=intended.get('schema'))
        if not validation.ok:
            raise DishRuleError('CONFLICT', 'confirmed candidate still fails deterministic handoff validation', rule='handoff_validation_failed')
        complete_operation_step(conn, operation_id, 'handoff_validation')
        actions.append({'kind': 'workflow_step', 'step': 'handoff_validation', 'outcome': 'confirmed'})
        return True, live
    if step['step_name'] == 'verification_cycle':
        existing = conn.execute('SELECT cycle_id FROM verification_cycles WHERE operation_id=? AND completed_at IS NULL ORDER BY cycle_number DESC LIMIT 1', (operation_id,)).fetchone()
        if existing is None:
            number = conn.execute('SELECT COALESCE(MAX(cycle_number),0)+1 FROM verification_cycles WHERE task_gid=?', (op['task_gid'],)).fetchone()[0]
            existing = create_verification_cycle(conn, operation_id=operation_id, task_gid=op['task_gid'], cycle_number=number, protocol_release=intended['protocol_release'], protocol_text=intended.get('protocol_text'))
        complete_operation_step(conn, operation_id, 'verification_cycle')
        actions.append({'kind': 'workflow_step', 'step': 'verification_cycle', 'outcome': 'confirmed'})
        return True, live
    return False, live


def _recover_workflow_step_group_2(
    conn: sqlite3.Connection,
    *,
    backend: Any,
    operation_id: str,
    op,
    live,
    step,
    intended: dict[str, Any],
    actions: list[dict[str, Any]],
) -> tuple[bool, Any]:
    if step['step_name'] in {'planning_write', 'migration_write', 'small_corrected_write', 'hold_write', 'large_write', 'reopen_write', 'hold_resolution_write', 'signoff_write'} or step['step_name'].startswith('route_write:'):
        if step['step_name'] == 'planning_write':
            _assert_recoverable_planning_content(live.notes)
        if live.title == intended.get('title') and live.notes == intended.get('notes'):
            complete_operation_step(conn, operation_id, step['step_name'])
            actions.append({'kind': 'workflow_step', 'step': step['step_name'], 'outcome': 'confirmed'})
        else:
            raise DishRuleError('CONFLICT', 'live content does not satisfy workflow write intent', rule='workflow_step_evidence_mismatch')
        return True, live
    if step['step_name'] in {'verification_handoff', 'planning_handoff'}:
        target = intended['section_gid']
        purpose = 'verification_handoff' if step['step_name'] == 'verification_handoff' else 'planning_handoff'
        if step['step_name'] == 'planning_handoff':
            _assert_recoverable_planning_content(live.notes)
        if live.section_gid != target:
            live = move_exact(conn, backend, operation_id=operation_id, task_gid=op['task_gid'], project_gid=COOKING_PROJECT_GID, expected_identity=live.identity, expected_section_gid=live.section_gid, intended_section_gid=target, purpose=purpose)
        complete_operation_step(conn, operation_id, step['step_name'])
        if step['step_name'] == 'verification_handoff':
            transition_operation(conn, operation_id, phase='await_verification')
        actions.append({'kind': 'workflow_step', 'step': step['step_name'], 'outcome': 'confirmed'})
        return True, live
    if step['step_name'] == 'small_review_binding':
        cycle = conn.execute('SELECT * FROM verification_cycles WHERE cycle_id=?', (intended['cycle_id'],)).fetchone()
        if cycle is None:
            raise DishRuleError('CONFLICT', 'Small-correction cycle is missing', rule='workflow_cycle_missing')
        reviewed_identity = intended.get('reviewed_identity') or cycle['reviewed_identity']
        corrected_identity = intended.get('corrected_identity') or intended.get('identity')
        if cycle['reviewed_identity'] != reviewed_identity:
            raise DishRuleError('CONFLICT', 'Small-correction review binding no longer matches its inspected candidate', rule='workflow_step_evidence_mismatch')
        if live.identity != corrected_identity:
            raise DishRuleError('CONFLICT', 'live correction does not match review-binding intent', rule='workflow_step_evidence_mismatch')
        assert_small_correction_write_lineage(conn, cycle=cycle, corrected_identity=corrected_identity)
        complete_operation_step(conn, operation_id, 'small_review_binding')
        actions.append({'kind': 'workflow_step', 'step': 'small_review_binding', 'outcome': 'confirmed'})
        return True, live
    if step['step_name'] == 'reopen_reset':
        if live.identity != intended['candidate_identity']:
            raise DishRuleError('CONFLICT', 'live reopen candidate does not match reset intent', rule='workflow_step_evidence_mismatch')
        import uuid
        conn.execute('INSERT OR IGNORE INTO two_pass_resets(\n                           reset_id, operation_id, source_cycle_id, candidate_identity,\n                           canonical_path, category, before_json, after_json, created_at\n                       ) VALUES(?,?,?,?,?,?,?,?,?)', (str(uuid.uuid4()), operation_id, intended['source_cycle_id'], intended['candidate_identity'], intended['canonical_path'], intended['category'], json.dumps(intended['before']), json.dumps(intended['after']), utc_now()))
        complete_operation_step(conn, operation_id, 'reopen_reset')
        actions.append({'kind': 'workflow_step', 'step': 'reopen_reset', 'outcome': 'confirmed'})
        return True, live
    return False, live


def _recover_workflow_step_group_3(
    conn: sqlite3.Connection,
    *,
    backend: Any,
    operation_id: str,
    op,
    live,
    step,
    intended: dict[str, Any],
    actions: list[dict[str, Any]],
) -> tuple[bool, Any]:
    if step['step_name'] == 'small_signoff':
        cycle = conn.execute('SELECT * FROM verification_cycles WHERE cycle_id=?', (intended['cycle_id'],)).fetchone()
        if cycle is not None and cycle['outcome'] == 'approved':
            complete_operation_step(conn, operation_id, 'small_signoff')
        else:
            from .step7 import approve_live
            if cycle is None:
                raise DishRuleError('CONFLICT', 'Small-correction cycle is missing', rule='workflow_cycle_missing')
            reviewed_identity = intended.get('reviewed_identity') or cycle['reviewed_identity']
            corrected_identity = intended.get('corrected_identity') or live.identity
            result = approve_live(conn, backend, operation_id=operation_id, agent=intended['agent'], model=intended.get('model'), reviewed_identity=reviewed_identity, approval_candidate_identity=corrected_identity, semantic_review_complete=True, provenance_complete=True, correction_class='small', run_id=intended.get('run_id'))
            live = read_complete_task(backend, task_gid=op['task_gid'], project_gid=COOKING_PROJECT_GID)
            complete_operation_step(conn, operation_id, 'small_signoff')
        actions.append({'kind': 'workflow_step', 'step': 'small_signoff', 'outcome': 'confirmed'})
        return True, live
    if step['step_name'].startswith('route_cycle_finalize:'):
        cycle = conn.execute('SELECT * FROM verification_cycles WHERE cycle_id=?', (intended['cycle_id'],)).fetchone()
        if cycle is None:
            raise DishRuleError('CONFLICT', 'route cycle is missing', rule='workflow_cycle_missing')
        if cycle['completed_at'] is None:
            hold_version_id = None
            hold_identity = intended.get('hold_identity')
            hold_section_gid = intended.get('hold_section_gid')
            if hold_identity:
                hold_version = conn.execute('SELECT content_version_id FROM content_versions\n                                 WHERE operation_id=? AND task_gid=? AND identity=? AND confirmed=1\n                                 ORDER BY created_at DESC, rowid DESC LIMIT 1', (operation_id, op['task_gid'], hold_identity)).fetchone()
                if hold_version is None:
                    raise DishRuleError('CONFLICT', 'hold write lacks confirmed content evidence', rule='workflow_step_evidence_mismatch')
                hold_version_id = hold_version['content_version_id']
            conn.execute('UPDATE verification_cycles\n                              SET correction_class=?, outcome=?, route=?, resume_state=?, completed_at=?,\n                                  hold_content_version_id=?, hold_identity=?, hold_section_gid=?\n                            WHERE cycle_id=?', (intended.get('correction_class'), intended['outcome'], intended.get('route'), intended.get('resume_state'), utc_now(), hold_version_id, hold_identity, hold_section_gid, intended['cycle_id']))
        complete_operation_step(conn, operation_id, step['step_name'])
        actions.append({'kind': 'workflow_step', 'step': 'route_cycle_finalize', 'outcome': 'confirmed'})
        return True, live
    if step['step_name'] in {'reopen_actor', 'hold_resolution_actor'} or step['step_name'].startswith('route_actor:'):
        if live.identity != intended['candidate_identity']:
            raise DishRuleError('CONFLICT', 'live candidate does not match actor-lineage intent', rule='workflow_step_evidence_mismatch')
        record_actor_fact(conn, operation_id=operation_id, task_gid=op['task_gid'], role=intended['role'], agent=intended['agent'], run_id=intended.get('run_id'), independence_attestation=intended.get('independence_attestation'), candidate_identity=intended['candidate_identity'], source_cycle_id=intended.get('source_cycle_id'))
        complete_operation_step(conn, operation_id, step['step_name'])
        actions.append({'kind': 'workflow_step', 'step': step['step_name'], 'outcome': 'confirmed'})
        return True, live
    if step['step_name'] in {'reopen_cycle', 'hold_resolution_cycle'} or step['step_name'].startswith('route_new_cycle:'):
        existing = conn.execute('SELECT cycle_id FROM verification_cycles WHERE operation_id=? AND completed_at IS NULL AND protocol_release=? ORDER BY cycle_number DESC LIMIT 1', (operation_id, intended['protocol_release'])).fetchone()
        if existing is None:
            number = conn.execute('SELECT COALESCE(MAX(cycle_number),0)+1 FROM verification_cycles WHERE task_gid=?', (op['task_gid'],)).fetchone()[0]
            existing = create_verification_cycle(conn, operation_id=operation_id, task_gid=op['task_gid'], cycle_number=number, protocol_release=intended['protocol_release'], protocol_text=intended.get('protocol_text'))
        complete_operation_step(conn, operation_id, step['step_name'])
        actions.append({'kind': 'workflow_step', 'step': step['step_name'], 'outcome': 'confirmed', 'cycle_id': existing['cycle_id']})
        return True, live
    return False, live


def _recover_workflow_step_group_4(
    conn: sqlite3.Connection,
    *,
    backend: Any,
    operation_id: str,
    op,
    live,
    step,
    intended: dict[str, Any],
    actions: list[dict[str, Any]],
) -> tuple[bool, Any]:
    if step['step_name'] == 'research_preconstruction_hold_resolution':
        refreshed_op = conn.execute('SELECT * FROM operations WHERE operation_id=?', (operation_id,)).fetchone()
        expected_phase = 'held_evidence' if intended.get('resolution_kind') == 'evidence' else 'held_human'
        if live.identity != refreshed_op['expected_identity']:
            raise DishRuleError('CONFLICT', 'live task changed while the pre-construction hold resolution was interrupted', rule='workflow_step_evidence_mismatch')
        if refreshed_op['phase'] == expected_phase:
            transition_operation(conn, operation_id, phase='prepare_required', status='open')
        elif refreshed_op['phase'] != 'prepare_required':
            raise DishRuleError('CONFLICT', 'operation phase does not match the pre-construction hold resolution intent', rule='workflow_step_evidence_mismatch', details={'expected_phases': [expected_phase, 'prepare_required'], 'actual_phase': refreshed_op['phase']})
        prior = conn.execute("SELECT 1 FROM audit_events WHERE operation_id=? AND event_type='research.preconstruction_resolved' LIMIT 1", (operation_id,)).fetchone()
        if prior is None:
            record_audit(conn, submission_id=None, task_gid=op['task_gid'], operation_id=operation_id, event_type='research.preconstruction_resolved', actor_agent=None, details=dict(intended), result_code='OK', result_ok=True, governed_kind='decision', before_state={'phase': expected_phase, 'candidate_content_existed': False}, after_state={'phase': 'prepare_required', 'resume_status': 'pending-research'}, actor_source='recovery')
        complete_operation_step(conn, operation_id, 'research_preconstruction_hold_resolution')
        actions.append({'kind': 'workflow_step', 'step': 'research_preconstruction_hold_resolution', 'outcome': 'confirmed'})
        return True, live
    if step['step_name'] == 'hold_resolution_decision':
        prior = conn.execute("SELECT 1 FROM audit_events WHERE operation_id=? AND event_type='hold.resolved' LIMIT 1", (operation_id,)).fetchone()
        if prior is None:
            record_audit(conn, submission_id=None, task_gid=op['task_gid'], operation_id=operation_id, event_type='hold.resolved', actor_agent=None, details=dict(intended), result_code='OK', result_ok=True, governed_kind='decision', actor_source='recovery')
        complete_operation_step(conn, operation_id, 'hold_resolution_decision')
        actions.append({'kind': 'workflow_step', 'step': 'hold_resolution_decision', 'outcome': 'confirmed'})
        return True, live
    if step['step_name'] == 'signoff_finalize':
        refreshed_op = conn.execute('SELECT * FROM operations WHERE operation_id=?', (operation_id,)).fetchone()
        cycle = conn.execute('SELECT * FROM verification_cycles WHERE cycle_id=?', (intended['cycle_id'],)).fetchone()
        if refreshed_op['signoff_completed_at'] is None or cycle is None or cycle['outcome'] != 'approved':
            raise DishRuleError('CONFLICT', 'signoff evidence is incomplete', rule='workflow_signoff_incomplete')
        transition_operation(conn, operation_id, phase='await_submission')
        complete_operation_step(conn, operation_id, 'signoff_finalize')
        actions.append({'kind': 'workflow_step', 'step': 'signoff_finalize', 'outcome': 'confirmed'})
        return True, live
    if step['step_name'] in {'reopen_phase', 'hold_resolution_phase', 'submission_terminal_intent', 'submission_terminal', 'planning_terminal', 'migration_terminal', 'verification_phase', 'non_material_terminal'} or step['step_name'].startswith('route_phase:'):
        if step['step_name'] == 'planning_terminal':
            _assert_recoverable_planning_content(live.notes)
        if step['step_name'] in {'submission_terminal_intent', 'submission_terminal'}:
            if live.identity != intended.get('effective_identity') or live.section_gid != intended.get('section_gid'):
                raise DishRuleError('CONFLICT', 'live task does not satisfy submission terminal intent', rule='workflow_step_evidence_mismatch')
            _finalize_submission_terminal(conn, operation_id=operation_id, intended=intended, recovered=True)
        else:
            transition_operation(conn, operation_id, phase=intended.get('phase', 'terminal'), status=intended.get('status'), terminal_outcome=intended.get('terminal_outcome'), inherited_signoff_cycle_id=intended.get('inherited_signoff_cycle_id'))
            complete_operation_step(conn, operation_id, step['step_name'])
        actions.append({'kind': 'workflow_step', 'step': step['step_name'], 'outcome': 'confirmed'})
        return True, live
    return False, live



def _recover_pending_workflow_steps(
    conn: sqlite3.Connection,
    *,
    backend: Any,
    operation_id: str,
    op,
    live,
    requested_outcome: str,
    actions: list[dict[str, Any]],
):
    if requested_outcome != "applied":
        return live
    for step in pending_operation_steps(conn, operation_id):
        intended = json.loads(step["intended_json"])
        for handler in (
            _recover_workflow_step_group_1,
            _recover_workflow_step_group_2,
            _recover_workflow_step_group_3,
            _recover_workflow_step_group_4,
        ):
            handled, live = handler(
                conn,
                backend=backend,
                operation_id=operation_id,
                op=op,
                live=live,
                step=step,
                intended=intended,
                actions=actions,
            )
            if handled:
                break
    return live


def _finish_operation_recovery(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    op,
    live,
    requested_outcome: str,
    reason: str,
    actions: list[dict[str, Any]],
    content_state: str,
    movement_state: str,
    write_attempt,
    movement_attempt,
) -> dict[str, Any]:
    refreshed = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    record_audit(
        conn,
        submission_id=None,
        task_gid=op["task_gid"],
        operation_id=operation_id,
        event_type="operation.recovery",
        actor_agent=None,
        details={
            "requested_outcome": requested_outcome,
            "reason": reason,
            "actions": actions,
            "content_state": content_state,
            "movement_state": movement_state,
        },
        result_code="OK",
        result_ok=True,
    )
    from .operation_execution import resolve_recovered_unclaimed_local_executions

    resolved = resolve_recovered_unclaimed_local_executions(
        conn, operation_id=operation_id
    )
    return {
        "operation_id": operation_id,
        "live_identity": live.identity,
        "live_section_gid": live.section_gid,
        "content_recovery_state": content_state,
        "movement_recovery_state": movement_state,
        "actions": actions,
        "operation_status": refreshed["status"],
        "resolved_local_execution_ids": resolved,
        "write_attempt": (
            None
            if write_attempt is None
            else {key: write_attempt[key] for key in write_attempt.keys()}
        ),
        "movement_attempt": (
            None
            if movement_attempt is None
            else {key: movement_attempt[key] for key in movement_attempt.keys()}
        ),
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
    op = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if op is None:
        raise DishRuleError(
            "NOT_FOUND",
            f"operation not found: {operation_id}",
            rule="operation_not_found",
        )
    live = read_complete_task(
        backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID
    )
    actions: list[dict[str, Any]] = []
    content_state, write_attempt = _recover_content_attempt(
        conn,
        operation_id=operation_id,
        op=op,
        live=live,
        requested_outcome=requested_outcome,
        actions=actions,
    )
    movement_state, movement_attempt = _recover_movement_attempt(
        conn,
        operation_id=operation_id,
        op=op,
        live=live,
        requested_outcome=requested_outcome,
        actions=actions,
    )
    live = _recover_pending_workflow_steps(
        conn,
        backend=backend,
        operation_id=operation_id,
        op=op,
        live=live,
        requested_outcome=requested_outcome,
        actions=actions,
    )
    return _finish_operation_recovery(
        conn,
        operation_id=operation_id,
        op=op,
        live=live,
        requested_outcome=requested_outcome,
        reason=reason,
        actions=actions,
        content_state=content_state,
        movement_state=movement_state,
        write_attempt=write_attempt,
        movement_attempt=movement_attempt,
    )

