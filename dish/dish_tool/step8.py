"""Step 8 protocol-native Verification correction and hold routes."""
from __future__ import annotations

import dataclasses
import difflib
import json
import re
import sqlite3
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from .constants import COOKING_PROJECT_GID, MECHANICAL_PROPOSAL_AGENT, REJECTION_ROUTES
from .database import (
    complete_operation_step,
    confirm_task_content,
    consume_reserved_marco_authorizations,
    content_identity,
    create_verification_cycle,
    declare_operation_step,
    record_actor_fact,
    record_audit,
    release_marco_authorization_reservations,
    transition_operation,
)
from .transactions import savepoint_transaction
from .errors import DishRuleError
from .hold_resolution import resolve_preconstruction_hold_to_successor
from .small_correction_lineage import assert_small_correction_write_lineage
from .models import (
    material_change_line,
    material_editor_line,
    utc_now,
    validate_rejection_reason,
)
from .lifecycle import assert_transition, hold, pending_verification, require_status, resumed
from .task_document import DocumentParseError, TaskState, document_parse_error_payloads, parse_task_document, validate_task_document, finding_payload
from .task_store import read_complete_task, write_exact_content
from .releases import current_verification_protocol_release
from .governed_diff import (
    agent_attested_decision_appends,
    canonical_diff,
    GOVERNED_FIELDS,
    governed_changes,
    governed_changes_requiring_authorization,
    require_governed_authorization,
    preserve_material_change_history,
    require_small_scope,
)
from .step7 import approve_live, assert_verifier_authority
from .human_actions import exact_action, relay_text
from .workflow_policy import hold_resolution_outcome
from .semantic_proposals import (
    claim_semantic_proposal, get_semantic_proposal, mark_semantic_proposal_applied,
    proposal_payload, queue_semantic_proposal, release_semantic_proposal_claim,
    semantic_proposal_baseline_content, semantic_proposal_drift_details,
    validate_semantic_proposal_integrity,
)

ROUTES = frozenset(REJECTION_ROUTES)
RESET_CATEGORIES = {"evidence", "premise", "method", "scope"}


def _preconstruction_research_hold(
    conn: sqlite3.Connection,
    *,
    op,
    agent: str,
    route: str,
    reason: str,
    resume_status: str | None,
    run_id: str | None,
    request_id: str | None,
    file_path: str | None,
    model: str | None,
    quantified_blocker: dict[str, Any] | None = None,
    human_review_basis: str | None = None,
    repairs_considered: str | None = None,
    human_review_options: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if route not in {"evidence", "human-review"}:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "Research may be held before construction only for evidence or human review",
            rule="preconstruction_hold_route_invalid",
        )
    if resume_status != "pending-research":
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "pre-construction Research holds must resume to pending-research",
            rule="preconstruction_resume_status_invalid",
            details={"expected": "pending-research", "actual": resume_status},
        )
    if file_path:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "pre-construction Research holds cannot include candidate content",
            rule="hold_candidate_unexpected",
        )
    if model:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "pre-construction Research holds do not accept a model field",
            rule="hold_model_unexpected",
        )
    if agent != op["researcher_agent"]:
        raise DishRuleError(
            "AGENT_MISMATCH",
            "Research hold agent does not match the recorded researcher",
            rule="operation_actor_mismatch",
            details={"expected": op["researcher_agent"], "actual": agent},
        )
    if str(op["run_id"] or "").strip() != str(run_id or "").strip():
        raise DishRuleError(
            "AGENT_MISMATCH",
            "Research hold run does not match the originating Research run",
            rule="service_run_id_conflict",
        )
    resolver = "Marco/admin supply-evidence" if route == "evidence" else "Marco/admin record-human-decision"
    intended = {
        "description": "Research blocked before construction",
        "route": route,
        "reason": reason,
        "task_gid": op["task_gid"],
        "originating_agent": agent,
        "originating_run_id": run_id,
        "request_id": request_id,
        "timestamp": utc_now(),
        "resolver": resolver,
        "resume_status": "pending-research",
        "candidate_content_existed": False,
        "quantified_blocker": quantified_blocker,
        "human_review_basis": str(human_review_basis or "").strip() or None,
        "repairs_considered": str(repairs_considered or "").strip() or None,
        "human_review_options": human_review_options or [],
    }
    target_phase = "held_evidence" if route == "evidence" else "held_human"
    with savepoint_transaction(conn, "research_preconstruction_hold"):
        declare_operation_step(
            conn, op["operation_id"], "research_preconstruction_hold", intended
        )
        transition_operation(conn, op["operation_id"], phase=target_phase)
        complete_operation_step(
            conn, op["operation_id"], "research_preconstruction_hold"
        )
        record_audit(
            conn,
            submission_id=None,
            task_gid=op["task_gid"],
            operation_id=op["operation_id"],
            event_type="research.preconstruction_blocked",
            actor_agent=agent,
            actor_run_id=run_id,
            details=intended,
            result_code="OK",
            result_ok=True,
            governed_kind="decision",
            before_state={
                "phase": "prepare_required",
                "candidate_content_existed": False,
            },
            after_state={
                "phase": target_phase,
                "resume_status": "pending-research",
            },
            actor_source="research-command",
        )
    return {"operation_id": op["operation_id"], **intended, "phase": target_phase}


def _rows(conn: sqlite3.Connection, operation_id: str):
    op = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if op is None:
        raise DishRuleError("NOT_FOUND", f"operation not found: {operation_id}", rule="operation_not_found")
    if op["status"] != "open":
        raise DishRuleError("WRONG_STATE", "operation is not open", rule="operation_not_open")
    cycle = conn.execute("SELECT * FROM verification_cycles WHERE operation_id = ? AND completed_at IS NULL ORDER BY cycle_number DESC LIMIT 1", (operation_id,)).fetchone()
    if cycle is None:
        raise DishRuleError("WRONG_STATE", "operation has no pending Verification cycle", rule="verification_cycle_missing")
    return op, cycle


def _candidate(path: str):
    try:
        return parse_task_document(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise DishRuleError("INVALID_ARGUMENT", "corrected candidate could not be read", rule="candidate_file_unreadable") from exc
    except DocumentParseError as exc:
        raise DishRuleError("VALIDATION_FAILED", "corrected candidate is not canonical", errors=document_parse_error_payloads(exc)) from exc


def _render(document):
    lines = document.render().splitlines()
    return lines[0], "\n".join(lines[1:]) + "\n"


def _confirmed_version(conn: sqlite3.Connection, *, operation_id: str, task_gid: str, identity: str):
    row = conn.execute(
        """SELECT * FROM content_versions
             WHERE operation_id=? AND task_gid=? AND identity=? AND confirmed=1
             ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (operation_id, task_gid, identity),
    ).fetchone()
    if row is None:
        raise DishRuleError(
            "CONFLICT", "confirmed content evidence is missing",
            rule="content_version_missing", details={"identity": identity},
        )
    return row



def _held_document(conn: sqlite3.Connection, *, cycle, live):
    if not cycle["hold_content_version_id"] or not cycle["hold_identity"] or not cycle["hold_section_gid"]:
        raise DishRuleError(
            "WRONG_STATE",
            "held operation requires migration reconciliation before it can resume",
            rule="hold_baseline_reconciliation_required",
            details={"cycle_id": cycle["cycle_id"]},
        )
    version = conn.execute(
        "SELECT * FROM content_versions WHERE content_version_id=?",
        (cycle["hold_content_version_id"],),
    ).fetchone()
    if (
        version is None
        or version["confirmed"] != 1
        or version["operation_id"] != cycle["operation_id"]
        or version["task_gid"] != cycle["task_gid"]
        or version["identity"] != cycle["hold_identity"]
    ):
        raise DishRuleError(
            "CONFLICT", "persisted hold baseline is inconsistent",
            rule="hold_baseline_invalid", details={"cycle_id": cycle["cycle_id"]},
        )
    if live.identity != cycle["hold_identity"]:
        raise DishRuleError(
            "CONFLICT", "live task content changed while the hold was open",
            rule="hold_content_drift",
            details={"expected_identity": cycle["hold_identity"], "actual_identity": live.identity},
        )
    if live.section_gid != cycle["hold_section_gid"]:
        raise DishRuleError(
            "CONFLICT", "live task placement changed while the hold was open",
            rule="hold_placement_drift",
            details={"expected_section_gid": cycle["hold_section_gid"], "actual_section_gid": live.section_gid},
        )
    return parse_task_document(f"{version['title']}\n{version['notes']}")


def _write_document(
    conn, backend, op, live, document, *, schema=None, authorization_ids=(),
    write_context: dict[str, object] | None = None,
):
    check = validate_task_document(document, expected_schema_version=op["schema_version"], schema=schema)
    if not check.ok:
        raise DishRuleError("VALIDATION_FAILED", "candidate failed deterministic validation", errors=[finding_payload(f) for f in check.findings])
    title, notes = _render(document)
    try:
        context = dict(write_context or {})
        if authorization_ids:
            context["authorization_ids"] = list(authorization_ids)
        return write_exact_content(
            conn, backend, operation_id=op["operation_id"], task_gid=op["task_gid"],
            project_gid=COOKING_PROJECT_GID, expected_identity=live.identity,
            expected_section_gid=live.section_gid, title=title, notes=notes,
            schema_version=op["schema_version"],
            context=context or None,
        )
    except DishRuleError as exc:
        if authorization_ids and exc.code != "BACKEND_UNCERTAIN":
            release_marco_authorization_reservations(
                conn, operation_id=op["operation_id"], authorization_ids=authorization_ids
            )
        raise


def approve_small(conn: sqlite3.Connection, backend: Any, *, operation_id: str, agent: str, model: str | None = None, file_path: str, reviewed_identity: str, semantic_review_complete: bool, provenance_complete: bool, run_id: str | None = None, schema=None):
    op, cycle = _rows(conn, operation_id)
    inherited_attestation = assert_verifier_authority(
        cycle, agent=agent, run_id=run_id
    )
    if not semantic_review_complete or not provenance_complete:
        raise DishRuleError("VALIDATION_FAILED", "semantic self-review and provenance completion are required", rule="verification_inputs_incomplete")
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    persisted_reviewed = cycle["reviewed_identity"]
    if not persisted_reviewed or not cycle["reviewed_content_version_id"]:
        raise DishRuleError("WRONG_STATE", "Verification cycle has no persisted reviewed content", rule="reviewed_content_missing")
    if reviewed_identity != persisted_reviewed:
        raise DishRuleError("CONFLICT", "caller review identity does not match the persisted review", rule="reviewed_identity_mismatch", retryable=True)
    if live.identity != persisted_reviewed:
        raise DishRuleError("CONFLICT", "live candidate changed after verifier review", rule="stale_verifier_review")
    corrected = _candidate(file_path)
    reviewed_document = parse_task_document(f"{live.title}\n{live.notes}")
    corrected_state = dict(corrected.state.values)
    corrected_state["Researched by"] = reviewed_document.state.values["Researched by"]
    corrected = dataclasses.replace(corrected, state=TaskState(corrected_state))
    corrected = preserve_material_change_history(reviewed_document, corrected)
    require_small_scope(reviewed_document, corrected)
    state = dict(corrected.state.values)
    state.update({"Status": "pending-verification", "Status detail": "None", "Resume status": "None", "Verified by": "None", "Verification protocol release": cycle["protocol_release"], "Self-verified": material_editor_line(agent, model, utc_now()[:10])})
    changes = tuple(corrected.material_changes) + (material_change_line(
        agent,
        model,
        utc_now()[:10],
        change="applied a small Verification correction",
        reason="exact candidate replaced and self-reviewed",
        materiality="Small",
    ),)
    corrected = dataclasses.replace(corrected, state=TaskState(state), material_changes=changes)
    precheck = validate_task_document(corrected, expected_schema_version=op["schema_version"], schema=schema)
    if not precheck.ok:
        raise DishRuleError("VALIDATION_FAILED", "candidate failed deterministic validation", errors=[finding_payload(f) for f in precheck.findings])
    authorization_ids = require_governed_authorization(
        conn, reviewed_document, corrected, task_gid=op["task_gid"], operation_id=operation_id
    )
    intended_title, intended_notes = _render(corrected)
    intended_identity = content_identity(intended_title, intended_notes).digest
    declare_operation_step(conn, operation_id, "small_corrected_write", {"title": intended_title, "notes": intended_notes, "identity": intended_identity})
    declare_operation_step(
        conn,
        operation_id,
        "small_review_binding",
        {
            "cycle_id": cycle["cycle_id"],
            "reviewed_identity": persisted_reviewed,
            "corrected_identity": intended_identity,
        },
    )
    declare_operation_step(
        conn,
        operation_id,
        "small_signoff",
        {
            "cycle_id": cycle["cycle_id"],
            "agent": agent,
            "model": model,
            "run_id": run_id,
            "independence_attestation": inherited_attestation,
            "reviewed_identity": persisted_reviewed,
            "corrected_identity": intended_identity,
        },
    )
    confirmed = _write_document(conn, backend, op, live, corrected, schema=schema, authorization_ids=authorization_ids)
    complete_operation_step(conn, operation_id, "small_corrected_write")
    if confirmed.identity != intended_identity:
        raise DishRuleError(
            "BACKEND_UNCERTAIN",
            "confirmed Small correction identity does not match its durable intent",
            rule="small_correction_identity_mismatch",
        )
    assert_small_correction_write_lineage(
        conn, cycle=cycle, corrected_identity=confirmed.identity
    )
    complete_operation_step(conn, operation_id, "small_review_binding")
    result = approve_live(
        conn,
        backend,
        operation_id=operation_id,
        agent=agent,
        model=model,
        reviewed_identity=persisted_reviewed,
        approval_candidate_identity=confirmed.identity,
        semantic_review_complete=True,
        provenance_complete=True,
        correction_class="small",
        run_id=run_id,
        schema=schema,
    )
    complete_operation_step(conn, operation_id, "small_signoff")
    return result


def _validate_rejection_route_arguments(
    *,
    route: str,
    model: str | None,
    file_path: str | None,
    resume_status: str | None,
    independence_attestation: str | None,
) -> None:
    permitted = {
        "large": [
            "submission_id", "agent", "reason", "route", "model", "file_path",
        ],
        "evidence": [
            "submission_id", "agent", "reason", "route", "resume_status",
        ],
        "human-review": [
            "submission_id", "agent", "reason", "route", "resume_status",
        ],
    }[route]
    errors: list[dict[str, Any]] = []

    def add(rule: str, field: str, message: str) -> None:
        errors.append({
            "rule": rule,
            "field": field,
            "message": message,
            "route": route,
            "permitted_arguments": permitted,
        })

    if str(independence_attestation or "").strip():
        add(
            "hold_independence_attestation_unexpected",
            "independence_attestation",
            "every rejection route inherits the verifier run already bound by Verification start",
        )

    if route == "large":
        if resume_status is not None:
            add(
                "large_resume_status_unexpected", "resume_status",
                "Large correction sets pending-verification automatically",
            )
        if not str(model or "").strip():
            add("large_model_required", "model", "Large correction requires model")
        if not str(file_path or "").strip():
            add(
                "large_candidate_required", "file_path",
                "Large correction requires a complete corrected candidate",
            )
    else:
        if str(file_path or "").strip():
            add(
                "hold_candidate_unexpected", "file_path",
                "hold routes do not accept candidate content",
            )
        if str(model or "").strip():
            add("hold_model_unexpected", "model", "hold routes do not accept model")
        if resume_status not in {"pending-verification", "pending-research"}:
            add(
                "resume_status_required", "resume_status",
                "hold routes require pending-research or pending-verification",
            )

    if errors:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "arguments are incompatible with the selected rejection route",
            rule="rejection_route_arguments_invalid",
            retryable=True,
            details={"route": route, "permitted_arguments": permitted},
            errors=errors,
        )



def _resume_pending_rejection_finalize(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    agent: str,
    route: str,
    reason: str,
    run_id: str | None,
    independence_attestation: str | None,
) -> dict[str, Any] | None:
    step = conn.execute(
        """SELECT * FROM operation_steps
             WHERE operation_id=? AND step_name LIKE 'route_cycle_finalize:%'
               AND completed_at IS NULL
             ORDER BY rowid DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if step is None:
        return None
    intended = json.loads(step["intended_json"])
    if intended.get("decision_route") != route:
        raise DishRuleError(
            "CONFLICT", "rejection replay route differs from durable intent",
            rule="rejection_replay_mismatch",
        )
    if intended.get("decision_reason") != reason:
        raise DishRuleError(
            "CONFLICT", "rejection replay reason differs from durable intent",
            rule="rejection_replay_mismatch",
        )
    cycle_id = intended.get("cycle_id")
    cycle = conn.execute(
        "SELECT * FROM verification_cycles WHERE cycle_id=? AND operation_id=?",
        (cycle_id, operation_id),
    ).fetchone()
    op = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if cycle is None or op is None or op["status"] != "open":
        return None
    authority_attestation = cycle["independence_attestation"]
    assert_verifier_authority(
        cycle, agent=agent, run_id=run_id,
        independence_attestation=authority_attestation,
    )
    decision_identity = intended.get("decision_identity")
    attempt = conn.execute(
        """SELECT * FROM write_attempts
             WHERE operation_id=? AND outcome='confirmed'
               AND intended_identity=?
             ORDER BY started_at DESC, rowid DESC LIMIT 1""",
        (operation_id, decision_identity),
    ).fetchone()
    if attempt is None or not attempt["confirmed_content_version_id"]:
        return None
    version = conn.execute(
        """SELECT * FROM content_versions
             WHERE content_version_id=? AND operation_id=? AND confirmed=1""",
        (attempt["confirmed_content_version_id"], operation_id),
    ).fetchone()
    if version is None or version["identity"] != decision_identity:
        raise DishRuleError(
            "CONFLICT", "rejection write evidence differs from durable intent",
            rule="rejection_replay_mismatch",
        )
    live = read_complete_task(
        backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID
    )
    if live.identity != decision_identity:
        raise DishRuleError(
            "CONFLICT", "live rejection outcome differs from durable intent",
            rule="rejection_replay_mismatch",
        )
    suffix = str(cycle_id)
    route_write_step = f"route_write:{suffix}"
    route_actor_step = f"route_actor:{suffix}"
    route_cycle_step = f"route_cycle_finalize:{suffix}"
    route_new_cycle_step = f"route_new_cycle:{suffix}"
    route_phase_step = f"route_phase:{suffix}"
    verification_hold = bool(intended.get("verification_hold"))
    outcome = str(intended.get("outcome") or ("verification-hold" if verification_hold else "rejected"))
    persisted_route = intended.get("route")
    resume_state = intended.get("resume_state")
    target_phase_row = conn.execute(
        "SELECT intended_json FROM operation_steps WHERE operation_id=? AND step_name=?",
        (operation_id, route_phase_step),
    ).fetchone()
    if target_phase_row is None:
        raise DishRuleError(
            "CONFLICT", "rejection phase intent is missing",
            rule="workflow_step_evidence_mismatch",
        )
    target_phase = json.loads(target_phase_row["intended_json"]).get("phase")
    new_cycle = None
    with savepoint_transaction(conn, "verification_rejected_replay_finalize"):
        complete_operation_step(conn, operation_id, route_write_step)
        if route == "large":
            actor_row = conn.execute(
                "SELECT intended_json FROM operation_steps WHERE operation_id=? AND step_name=?",
                (operation_id, route_actor_step),
            ).fetchone()
            if actor_row is None:
                raise DishRuleError(
                    "CONFLICT", "rejection actor intent is missing",
                    rule="workflow_step_evidence_mismatch",
                )
            actor = json.loads(actor_row["intended_json"])
            record_actor_fact(
                conn, operation_id=operation_id, task_gid=op["task_gid"],
                role=actor["role"], agent=actor["agent"], run_id=actor["run_id"],
                independence_attestation=actor.get("independence_attestation"),
                candidate_identity=actor.get("candidate_identity"),
                source_cycle_id=actor.get("source_cycle_id"),
            )
            complete_operation_step(conn, operation_id, route_actor_step)
            conn.execute(
                "UPDATE operations SET editor_agent=?, verifier_agent=NULL, run_id=?, independence_attestation=? WHERE operation_id=?",
                (actor["agent"], actor["run_id"], actor.get("independence_attestation"), operation_id),
            )
        if target_phase and op["phase"] != target_phase:
            transition_operation(conn, operation_id, phase=target_phase)
        hold_fields = (None, None, None)
        if verification_hold or route in {"evidence", "human-review"}:
            hold_fields = (version["content_version_id"], decision_identity, live.section_gid)
        conn.execute(
            """UPDATE verification_cycles
                  SET correction_class=?, outcome=?, route=?, resume_state=?, completed_at=?,
                      hold_content_version_id=?, hold_identity=?, hold_section_gid=?
                WHERE cycle_id=? AND completed_at IS NULL""",
            (
                intended.get("correction_class"), outcome, persisted_route,
                resume_state, utc_now(), hold_fields[0], hold_fields[1],
                hold_fields[2], cycle_id,
            ),
        )
        complete_operation_step(conn, operation_id, route_cycle_step)
        if route == "large" and not verification_hold:
            next_step = conn.execute(
                "SELECT intended_json FROM operation_steps WHERE operation_id=? AND step_name=?",
                (operation_id, route_new_cycle_step),
            ).fetchone()
            if next_step is None:
                raise DishRuleError(
                    "CONFLICT", "next Verification cycle intent is missing",
                    rule="workflow_step_evidence_mismatch",
                )
            next_intended = json.loads(next_step["intended_json"])
            next_number = conn.execute(
                "SELECT COALESCE(MAX(cycle_number), 0) + 1 FROM verification_cycles WHERE task_gid=?",
                (op["task_gid"],),
            ).fetchone()[0]
            new_cycle = create_verification_cycle(
                conn, operation_id=operation_id, task_gid=op["task_gid"],
                cycle_number=next_number,
                protocol_release=next_intended["protocol_release"],
                protocol_text=next_intended["protocol_text"], route=None,
            )
            complete_operation_step(conn, operation_id, route_new_cycle_step)
        complete_operation_step(conn, operation_id, route_phase_step)
        prior = conn.execute(
            """SELECT 1 FROM audit_events
                 WHERE operation_id=? AND event_type='verification.rejected'
                   AND json_extract(details, '$.cycle_id')=? LIMIT 1""",
            (operation_id, cycle_id),
        ).fetchone()
        if prior is None:
            record_audit(
                conn, submission_id=None, task_gid=op["task_gid"],
                operation_id=operation_id, event_type="verification.rejected",
                actor_agent=agent,
                details={
                    "cycle_id": cycle_id, "route": route, "reason": reason,
                    "verification_hold": verification_hold, "identity": decision_identity,
                    "recovered": True,
                },
                result_code="OK", result_ok=True, governed_kind="decision",
                before_state={
                    "outcome": None,
                    "reviewed_identity": cycle["reviewed_identity"],
                    "status": "pending-verification",
                },
                after_state={
                    "outcome": outcome, "route": route,
                    "resume_state": resume_state,
                    "status": intended.get("decision_status"),
                },
                actor_run_id=run_id, actor_attestation=authority_attestation,
                actor_source="exact-replay",
            )
    return {
        "operation_id": operation_id, "route": route,
        "verification_hold": verification_hold,
        "new_cycle_id": None if new_cycle is None else new_cycle["cycle_id"],
        "task": dataclasses.asdict(live),
        "rejection_recovered": True,
    }


def _resume_rejected_cycle(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    agent: str,
    route: str,
    reason: str,
    run_id: str | None,
    independence_attestation: str | None,
) -> dict[str, Any] | None:
    pending = _resume_pending_rejection_finalize(
        conn, backend, operation_id=operation_id, agent=agent, route=route,
        reason=reason, run_id=run_id,
        independence_attestation=independence_attestation,
    )
    if pending is not None:
        return pending
    op = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if op is None or op["status"] != "open":
        return None
    cycle = conn.execute(
        """SELECT * FROM verification_cycles
             WHERE operation_id=? AND completed_at IS NOT NULL
               AND outcome IN ('rejected','verification-hold')
             ORDER BY completed_at DESC, rowid DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if cycle is None:
        return None
    persisted_route = (
        "large" if cycle["correction_class"] == "large"
        else "human-review" if cycle["route"] == "human_review"
        else cycle["route"]
    )
    if persisted_route != route:
        return None
    suffix = cycle["cycle_id"]
    required_steps = [f"route_cycle_finalize:{suffix}", f"route_phase:{suffix}"]
    if route == "large" and cycle["outcome"] != "verification-hold":
        required_steps.append(f"route_new_cycle:{suffix}")
    rows = conn.execute(
        f"SELECT step_name, completed_at FROM operation_steps WHERE operation_id=? AND step_name IN ({','.join('?' for _ in required_steps)})",
        (operation_id, *required_steps),
    ).fetchall()
    completed = {row["step_name"] for row in rows if row["completed_at"] is not None}
    if set(required_steps) != completed:
        return None
    prior = conn.execute(
        """SELECT 1 FROM audit_events
             WHERE operation_id=? AND event_type='verification.rejected'
               AND json_extract(details, '$.cycle_id')=? LIMIT 1""",
        (operation_id, cycle["cycle_id"]),
    ).fetchone()
    unresolved = conn.execute(
        """SELECT 1 FROM operation_executions
             WHERE operation_id=? AND command='reject'
               AND status='uncertain' AND resolved_at IS NULL LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if prior is not None and unresolved is None:
        return None
    authority_attestation = cycle["independence_attestation"]
    assert_verifier_authority(
        cycle, agent=agent, run_id=run_id,
        independence_attestation=authority_attestation,
    )
    live = read_complete_task(
        backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID
    )
    if cycle["hold_identity"] and live.identity != cycle["hold_identity"]:
        raise DishRuleError(
            "CONFLICT", "live rejection outcome differs from durable cycle evidence",
            rule="rejection_replay_mismatch",
        )
    document = parse_task_document(f"{live.title}\n{live.notes}")
    if prior is None:
        with savepoint_transaction(conn, "rejection_replay_audit"):
            record_audit(
                conn, submission_id=None, task_gid=op["task_gid"],
                operation_id=operation_id, event_type="verification.rejected",
                actor_agent=agent,
                details={
                    "cycle_id": cycle["cycle_id"],
                    "route": route, "reason": reason,
                    "verification_hold": cycle["outcome"] == "verification-hold",
                    "identity": live.identity, "recovered": True,
                },
                result_code="OK", result_ok=True, governed_kind="decision",
                before_state={"outcome": None, "reviewed_identity": cycle["reviewed_identity"], "status": "pending-verification"},
                after_state={
                    "outcome": cycle["outcome"], "route": route,
                    "resume_state": document.state.values["Resume status"],
                    "status": document.state.values["Status"],
                },
                actor_run_id=run_id, actor_attestation=authority_attestation,
                actor_source="exact-replay",
            )
    new_cycle = conn.execute(
        """SELECT cycle_id FROM verification_cycles
             WHERE operation_id=? AND completed_at IS NULL
             ORDER BY cycle_number DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    return {
        "operation_id": operation_id, "route": route,
        "verification_hold": cycle["outcome"] == "verification-hold",
        "new_cycle_id": None if new_cycle is None else new_cycle["cycle_id"],
        "task": dataclasses.asdict(live),
        "rejection_recovered": prior is None,
    }



def _validated_human_review_options(options) -> list[dict[str, Any]]:
    if not isinstance(options, list) or not (1 <= len(options) <= 6):
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "Human Review requires between one and six concrete choices",
            rule="human_review_options_required",
        )
    normalized: list[dict[str, Any]] = []
    allowed_fields = {
        "Dish candidate", "Purpose", "Role", "Priors", "Locks", "Exemptions",
        "Research emphasis", "Destination section", "Researched by",
    }
    for index, raw in enumerate(options):
        if not isinstance(raw, dict):
            raise DishRuleError(
                "INVALID_ARGUMENT", "Human Review choices must be objects",
                rule="human_review_option_invalid", details={"index": index},
            )
        label = str(raw.get("label") or "").strip()
        decision = str(raw.get("decision") or "").strip()
        if not label or not decision:
            raise DishRuleError(
                "INVALID_ARGUMENT", "Each Human Review choice needs a label and concrete decision",
                rule="human_review_option_invalid", details={"index": index},
            )
        authorization = raw.get("authorization")
        normalized_authorization = None
        if authorization is not None:
            if not isinstance(authorization, dict):
                raise DishRuleError(
                    "INVALID_ARGUMENT", "Human Review option authorization must be an object",
                    rule="human_review_option_authorization_invalid", details={"index": index},
                )
            field = str(authorization.get("field") or "").strip()
            before = authorization.get("before")
            after = authorization.get("after")
            if field not in allowed_fields or not isinstance(before, str) or not isinstance(after, str):
                raise DishRuleError(
                    "INVALID_ARGUMENT", "Human Review option authorization is incomplete or unsupported",
                    rule="human_review_option_authorization_invalid", details={"index": index},
                )
            if before == after:
                raise DishRuleError(
                    "INVALID_ARGUMENT", "Human Review option authorization must change the governed field",
                    rule="human_review_option_authorization_noop", details={"index": index, "field": field},
                )
            normalized_authorization = {"field": field, "before": before, "after": after}
        normalized.append(
            {
                "option_id": chr(ord("A") + index),
                "label": label,
                "decision": decision,
                "recommended": index == 0,
                "authorization": normalized_authorization,
            }
        )
    return normalized


def _human_review_preflight(*, route: str, reason: str, confirmed=False, basis=None, repairs_considered=None, quantified_blocker=None, options=None) -> list[dict[str, Any]]:
    if route != "human-review":
        return []
    clean_basis = str(basis or "").strip()
    clean_repairs = str(repairs_considered or "").strip()
    normalized_options = _validated_human_review_options(options) if options is not None else []
    if confirmed and clean_basis and clean_repairs and normalized_options:
        return normalized_options
    raise DishRuleError(
        "CONFIRMATION_REQUIRED",
        "Human Review needs a deliberate decision preflight before Dish parks the task",
        rule="human_review_preflight_required",
        details={
            "unresolved_issue": reason,
            "quantified_blocker": quantified_blocker,
            "decision_standard": (
                "Use a reasonable defensible estimate, with its assumptions stated, when an exact yield, portion, or "
                "similar value is unknowable. Do not invent a midpoint when no single estimate is defensible. Uncertainty "
                "alone is not a blocker: ask Marco only when it could materially change a safety, nutrition, settled-intent, "
                "or executability conclusion. A structured numeric threshold blocker must state one defensible estimate, "
                "the limit, and the material excess or shortfall."
            ),
            "exact_resolution_route": (
                "If the verifier can already construct one exact governed candidate that resolves the concern, do not "
                "ask Marco an open-ended Human Review question. Submit that candidate as a Large correction so Dish "
                "queues the exact semantic proposal for Marco to approve or reject."
            ),
            "human_review_is_allowed": (
                "Human Review is appropriate when a genuine unresolved human choice remains after reasonable "
                "estimation and within-authority repair."
            ),
            "questions": [
                "What is the plain-language issue Marco needs to decide?",
                "What route do you recommend, and why?",
                "What other plausible routes should Marco be able to choose?",
                "Can you construct the exact governed fix now and send it through the semantic-proposal review flow?",
                "Can any option carry one exact governed-field authorization rather than another round of clarification?",
            ],
            "retry": {
                "fresh_request_id": True,
                "human_review_confirmed": True,
                "human_review_basis": "<one concise question Marco is actually deciding>",
                "repairs_considered": "<what you tried or considered before asking Marco>",
                "human_review_options": [
                    {
                        "label": "<recommended route>",
                        "decision": "<exact decision to record if Marco chooses A>",
                    },
                    {
                        "label": "<another plausible route>",
                        "decision": "<exact decision to record if Marco chooses B>",
                    },
                ],
            },
        },
    )


def _validate_human_review_option_authorizations(document, options: list[dict[str, Any]]) -> None:
    for option in options:
        authorization = option.get("authorization")
        if not isinstance(authorization, dict):
            continue
        field = authorization["field"]
        actual = str(document.planning_brief.values.get(field, ""))
        if actual != authorization["before"]:
            raise DishRuleError(
                "CONFLICT",
                "A proposed Human Review option no longer matches the reviewed governed field",
                rule="human_review_option_authorization_stale",
                details={
                    "option_id": option["option_id"],
                    "field": field,
                    "expected_before": authorization["before"],
                    "actual_before": actual,
                },
            )


def _governed_change_needs_intent_confirmation(change) -> bool:
    if not isinstance(change.before, str) or not isinstance(change.after, str):
        return False
    before = change.before.strip()
    after = change.after.strip()
    if before == after:
        return False

    def deaccent(value: str) -> str:
        decomposed = unicodedata.normalize("NFKD", value)
        return " ".join(
            "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold().split()
        )

    # Diacritic/case/spacing-only differences and tiny edits inside a longer
    # governed text are exactly where an accidental cleanup can masquerade as
    # a human-authority change. Dish does not declare them non-semantic; it
    # asks the agent to explicitly say the change was intended.
    if deaccent(before) == deaccent(after):
        return True
    if max(len(before), len(after)) >= 40:
        return difflib.SequenceMatcher(None, before, after).ratio() >= 0.97
    return False


def _confirm_intended_governed_changes(before_document, document, declared_fields) -> tuple[dict[str, Any], ...]:
    changes, declared = _validated_declared_governed_fields(
        before_document, document, declared_fields
    )
    if not changes:
        return ()
    actual_fields = tuple(change.field for change in changes)
    suspicious = tuple(
        change for change in changes
        if _governed_change_needs_intent_confirmation(change)
    )
    missing = tuple(change for change in suspicious if change.field not in declared)
    if missing:
        payload = tuple(
            {"field": change.field, "before": change.before, "after": change.after}
            for change in missing
        )
        raise DishRuleError(
            "CONFIRMATION_REQUIRED",
            "candidate contains small governed-text edits that may be incidental cleanup",
            rule="governed_change_intent_confirmation_required",
            details={
                "governed_changes_needing_confirmation": list(payload),
                "all_governed_fields_changed": list(actual_fields),
                "declared_fields": list(declared),
                "instruction": (
                    "Restore incidental governed-field edits exactly to the live baseline and retry. "
                    "If a listed small edit is genuinely intended, retry with governed_change_fields naming that field. "
                    "Dish is asking for intent, not deciding that the edit is semantically trivial."
                ),
                "fresh_request_id": True,
            },
        )
    return tuple(
        {"field": change.field, "before": change.before, "after": change.after}
        for change in changes
    )


def _validated_declared_governed_fields(before_document, document, declared_fields):
    changes = governed_changes(before_document, document)
    actual_fields = tuple(change.field for change in changes)
    declared = tuple(
        dict.fromkeys(
            str(field).strip()
            for field in (declared_fields or ())
            if str(field).strip()
        )
    )
    unknown = sorted(set(declared) - set(GOVERNED_FIELDS))
    if unknown:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "governed_change_fields contains unsupported field names",
            rule="governed_change_field_invalid",
            details={"unsupported": unknown, "allowed": list(GOVERNED_FIELDS)},
        )
    non_actual = sorted(set(declared) - set(actual_fields))
    if non_actual:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "governed_change_fields names fields that are unchanged in the candidate",
            rule="governed_change_field_not_changed",
            details={"unchanged": non_actual, "actual_fields": list(actual_fields)},
        )
    return changes, declared


def _require_agent_attested_decision_intent(
    before_document, document, declared_fields
) -> tuple[str, ...]:
    appended = agent_attested_decision_appends(before_document, document)
    if not appended:
        return ()
    _changes, declared = _validated_declared_governed_fields(
        before_document, document, declared_fields
    )
    if "Decisions" not in declared:
        raise DishRuleError(
            "CONFIRMATION_REQUIRED",
            "an attributed Marco Decision append requires explicit agent attestation",
            rule="decision_attestation_required",
            details={
                "appended_decisions": list(appended),
                "required_governed_change_field": "Decisions",
                "instruction": (
                    "Retry the same exact candidate with governed_change_fields including Decisions "
                    "only if Marco actually stated these choices in the conversation. This records "
                    "agent-attested provenance; it is not formal authorization for another governed field."
                ),
                "fresh_request_id": True,
            },
        )
    return appended

def _validated_quantified_blocker(*, metric=None, actual=None, limit=None, delta=None, unit=None, basis=None):
    values = {"metric": metric, "actual": actual, "limit": limit, "delta": delta, "unit": unit, "basis": basis}
    present = {key: value for key, value in values.items() if value is not None and str(value).strip() != ""}
    if not present:
        return None
    if set(present) != set(values):
        raise DishRuleError("INVALID_ARGUMENT", "quantified blocker fields must be supplied together", rule="quantified_blocker_incomplete", details={"missing": sorted(set(values) - set(present))})
    try:
        actual_n, limit_n, delta_n = float(actual), float(limit), float(delta)
    except (TypeError, ValueError) as exc:
        raise DishRuleError("INVALID_ARGUMENT", "actual, limit, and delta must be numeric", rule="quantified_blocker_numeric") from exc
    expected_delta = actual_n - limit_n
    if abs(expected_delta - delta_n) > 1e-9:
        raise DishRuleError("INVALID_ARGUMENT", "delta must equal actual minus limit", rule="quantified_blocker_delta_mismatch", details={"expected_delta": expected_delta, "actual_delta": delta_n})
    return {"metric": str(metric).strip(), "actual": actual_n, "limit": limit_n, "delta": delta_n, "unit": str(unit).strip(), "basis": str(basis).strip()}


def reject_route(conn: sqlite3.Connection, backend: Any, *, operation_id: str, agent: str, model: str | None = None, route: str, reason: str, file_path: str | None = None, resume_status: str | None = None, run_id: str | None = None, independence_attestation: str | None = None, request_id: str | None = None, schema=None, honest_root=None, blocker_metric=None, blocker_actual=None, blocker_limit=None, blocker_delta=None, blocker_unit=None, blocker_basis=None, human_review_confirmed: bool = False, human_review_basis: str | None = None, repairs_considered: str | None = None, human_review_options=None, governed_change_fields=None):

    route = str(route or "").strip()
    reason = validate_rejection_reason(reason)
    quantified_blocker = _validated_quantified_blocker(metric=blocker_metric, actual=blocker_actual, limit=blocker_limit, delta=blocker_delta, unit=blocker_unit, basis=blocker_basis)
    if route not in ROUTES:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "rejection route is not supported",
            rule="invalid_rejection_route",
            details={"allowed": list(REJECTION_ROUTES), "actual": route},
        )
    _validate_rejection_route_arguments(
        route=route,
        model=model,
        file_path=file_path,
        resume_status=resume_status,
        independence_attestation=independence_attestation,
    )
    normalized_human_review_options = _human_review_preflight(
        route=route, reason=reason, confirmed=human_review_confirmed,
        basis=human_review_basis, repairs_considered=repairs_considered,
        quantified_blocker=quantified_blocker, options=human_review_options,
    )
    resumed = _resume_rejected_cycle(
        conn, backend, operation_id=operation_id, agent=agent, route=route,
        reason=reason, run_id=run_id,
        independence_attestation=independence_attestation,
    )
    if resumed is not None:
        return resumed
    op = conn.execute(
        "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    if op is None:
        raise DishRuleError(
            "NOT_FOUND", f"operation not found: {operation_id}", rule="operation_not_found"
        )
    if (
        op["status"] == "open"
        and op["phase"] == "prepare_required"
        and op["operation_kind"] == "initial"
        and op["content_write_completed_at"] is None
    ):
        if route == "human-review" and any(
            isinstance(option.get("authorization"), dict)
            for option in normalized_human_review_options
        ):
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "pre-construction Human Review choices cannot carry governed-field authorization",
                rule="preconstruction_human_review_authorization_unavailable",
                details={
                    "reason": (
                        "No reviewed candidate exists yet, so Dish cannot bind an exact governed before/after authorization."
                    )
                },
            )
        return _preconstruction_research_hold(
            conn,
            op=op,
            agent=agent,
            route=route,
            reason=reason,
            resume_status=resume_status,
            run_id=run_id,
            request_id=request_id,
            file_path=file_path,
            model=model,
            quantified_blocker=quantified_blocker,
            human_review_basis=human_review_basis,
            repairs_considered=repairs_considered,
            human_review_options=normalized_human_review_options,
        )
    op, cycle = _rows(conn, operation_id)
    authority_attestation = cycle["independence_attestation"]
    assert_verifier_authority(
        cycle,
        agent=agent,
        run_id=run_id,
        independence_attestation=authority_attestation,
    )
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    persisted_reviewed = cycle["reviewed_identity"]
    if not persisted_reviewed or not cycle["reviewed_content_version_id"]:
        raise DishRuleError("WRONG_STATE", "Verification cycle has no persisted reviewed content", rule="reviewed_content_missing")
    if live.identity != persisted_reviewed:
        raise DishRuleError("CONFLICT", "live candidate changed after verifier review", rule="stale_verifier_review")
    document = parse_task_document(f"{live.title}\n{live.notes}")
    if route == "human-review":
        _validate_human_review_option_authorizations(
            document, normalized_human_review_options
        )
    require_status(document.state, {"pending-verification"}, action="Verification outcome")
    state = dict(document.state.values)
    changes = tuple(document.material_changes)

    if route == "large":
        corrected = _candidate(file_path)
        corrected_state = dict(corrected.state.values)
        corrected_state["Researched by"] = document.state.values["Researched by"]
        corrected = dataclasses.replace(corrected, state=TaskState(corrected_state))
        corrected = preserve_material_change_history(document, corrected)
        if honest_root is None:
            raise DishRuleError("INTERNAL_ERROR", "current Honest checkout is required for a new Verification cycle", rule="honest_root_required")
        snapshot = current_verification_protocol_release(honest_root)
        assert_transition(action="large_correction", before="pending-verification", after="pending-verification")
        state = dict(pending_verification(corrected.state.values, protocol_release=snapshot.identity).values)
        state["Self-verified"] = material_editor_line(agent, model, utc_now()[:10])
        changes = tuple(corrected.material_changes) + (material_change_line(
            agent,
            model,
            utc_now()[:10],
            change="applied a large Verification correction",
            reason=reason,
            materiality="Large",
        ),)
        document = dataclasses.replace(corrected, state=TaskState(state), material_changes=changes)
    elif route == "evidence":
        assert_transition(action="request_evidence", before="pending-verification", after="pending-evidence")
        document = dataclasses.replace(document, state=hold(state, target="pending-evidence", detail=reason, resume_status=resume_status))
    else:
        assert_transition(action="request_human_review", before="pending-verification", after="pending-human-review")
        document = dataclasses.replace(document, state=hold(state, target="pending-human-review", detail=reason, resume_status=resume_status))

    completed = conn.execute("SELECT COUNT(*) FROM verification_cycles WHERE operation_id = ? AND completed_at IS NOT NULL AND outcome != 'approved'", (operation_id,)).fetchone()[0]
    verification_hold = completed + 1 >= 3 and route == "large"
    if verification_hold:
        assert_transition(action="verification_hold", before="pending-verification", after="pending-human-review")
        document = dataclasses.replace(document, state=hold(document.state.values, target="pending-human-review", detail=f"Three consecutive Verification rounds ended without a signable task: {reason}", resume_status="pending-verification"))

    precheck = validate_task_document(document, expected_schema_version=op["schema_version"], schema=schema)
    if not precheck.ok:
        raise DishRuleError("VALIDATION_FAILED", "candidate failed deterministic validation", errors=[finding_payload(f) for f in precheck.findings])
    before_document = parse_task_document(f"{live.title}\n{live.notes}")
    agent_attested_decisions = _require_agent_attested_decision_intent(
        before_document, document, governed_change_fields
    )
    intended_title, intended_notes = _render(document)
    intended_identity = content_identity(intended_title, intended_notes).digest
    try:
        authorization_ids = require_governed_authorization(
            conn, before_document, document,
            task_gid=op["task_gid"], operation_id=operation_id,
            proposal_reason=reason,
            agent_attested_decisions=agent_attested_decisions,
        )
    except DishRuleError as exc:
        if exc.rule != "governed_change_unauthorized" or route != "large":
            raise
        changes_for_proposal = _confirm_intended_governed_changes(
            before_document, document, governed_change_fields
        )
        linked_for_proposal = tuple(
            {"path": path, "before": old, "after": new}
            for path, (old, new) in canonical_diff(before_document, document).items()
        )
        proposal = queue_semantic_proposal(
            conn,
            task_gid=op["task_gid"],
            operation_id=operation_id,
            cycle_id=cycle["cycle_id"],
            baseline_identity=live.identity,
            candidate_identity=intended_identity,
            candidate_title=intended_title,
            candidate_notes=intended_notes,
            proposal_reason=reason,
            explanation={
                "problem": (
                    "The corrected candidate changes governed facts that Dish will not let an "
                    "agent install without Marco's approval: "
                    + ", ".join(change["field"] for change in changes_for_proposal)
                    + "."
                ),
                "cause": reason,
                "why_not_ordinary_correction": (
                    "This is a Large correction to protected task intent, not a routine wording "
                    "or execution fix. The agent may propose the exact consequence but may not "
                    "choose or broaden the governed facts on Marco's behalf."
                ),
                "recommended_resolution": (
                    "Review the complete linked candidate, including every contradiction caused "
                    "by the same interpretation, then approve or reject the bundle as one unit."
                ),
                "scope": "This task, this exact baseline, and this exact candidate only.",
                "command_effect": "Approval authorizes the complete bundle; it does not sign the dish.",
                "after_success": (
                    "Dish mechanically revalidates and applies the exact approved bundle. If that "
                    "application cannot complete safely, the durable approval remains available for retry/recovery."
                ),
            },
            linked_changes=linked_for_proposal,
            changes=changes_for_proposal,
            protocol_release=snapshot.identity,
            protocol_text=snapshot.text,
            proposer_agent=agent,
            proposer_run_id=str(cycle["run_id"] or run_id or ""),
            agent_attested_decisions=agent_attested_decisions,
        )
        review_action = exact_action(
            kind="inspect-semantic-proposal",
            command="review-inspect",
            positional=(proposal["proposal_id"],),
            summary="Review the queued semantic proposal.",
            effect="Show Marco the rationale and every linked governed change before approval.",
            after_success={"instruction": "Approve, reject, or defer the proposal from the review queue."},
        )
        raise DishRuleError(
            "VALIDATION_FAILED",
            "candidate requires Marco approval; the complete semantic proposal was queued",
            rule="semantic_proposal_queued",
            retryable=False,
            errors=exc.errors,
            details={
                **exc.details,
                "proposal_id": proposal["proposal_id"],
                "proposal_status": proposal["status"],
                "proposal_queued": True,
                "batch_may_continue": True,
                "required_admin_action": "review-inspect",
                **review_action.payload(),
                "directive": relay_text(
                    review_action,
                    instruction=(
                        "This task is safely parked in the review queue. The agent may continue "
                        "reviewing unrelated tasks instead of waiting for Marco."
                    ),
                ),
            },
        ) from exc
    outcome = "verification-hold" if verification_hold else "rejected"
    target_phase = "held_human" if (verification_hold or route == "human-review") else ("held_evidence" if route == "evidence" else "await_verification")
    route_suffix = cycle["cycle_id"]
    route_write_step = f"route_write:{route_suffix}"
    route_actor_step = f"route_actor:{route_suffix}"
    route_cycle_step = f"route_cycle_finalize:{route_suffix}"
    route_new_cycle_step = f"route_new_cycle:{route_suffix}"
    route_phase_step = f"route_phase:{route_suffix}"
    declare_operation_step(conn, operation_id, route_write_step, {"title": intended_title, "notes": intended_notes, "route": route})
    if route == "large":
        declare_operation_step(conn, operation_id, route_actor_step, {
            "role": "material_editor",
            "agent": agent,
            "run_id": cycle["run_id"],
            "independence_attestation": cycle["independence_attestation"],
            "candidate_identity": intended_identity,
            "source_cycle_id": cycle["cycle_id"],
        })
    declare_operation_step(conn, operation_id, route_cycle_step, {
        "cycle_id": cycle["cycle_id"], "correction_class": "large" if route == "large" else None,
        "outcome": outcome, "route": {"evidence": "evidence", "human-review": "human_review"}.get(route),
        "resume_state": document.state.values["Resume status"],
        "hold_identity": intended_identity if (verification_hold or route in {"evidence", "human-review"}) else None,
        "hold_section_gid": live.section_gid if (verification_hold or route in {"evidence", "human-review"}) else None,
        "decision_route": route,
        "decision_reason": reason,
        "decision_identity": intended_identity,
        "decision_status": document.state.values["Status"],
        "verification_hold": verification_hold,
        "quantified_blocker": quantified_blocker,
        "human_review_basis": str(human_review_basis or "").strip() or None,
        "repairs_considered": str(repairs_considered or "").strip() or None,
        "human_review_options": normalized_human_review_options,
        "actor_agent": agent,
        "actor_run_id": run_id,
        "actor_attestation": authority_attestation,
    })
    if route == "large" and not verification_hold:
        declare_operation_step(conn, operation_id, route_new_cycle_step, {"protocol_release": snapshot.identity, "protocol_text": snapshot.text})
    declare_operation_step(conn, operation_id, route_phase_step, {"phase": target_phase})
    confirmed = _write_document(conn, backend, op, live, document, schema=schema, authorization_ids=authorization_ids)
    with savepoint_transaction(conn, "verification_rejected_finalize"):
        complete_operation_step(conn, operation_id, route_write_step)
        if route == "large":
            record_actor_fact(
                conn, operation_id=operation_id, task_gid=op["task_gid"],
                role="material_editor", agent=agent, run_id=cycle["run_id"],
                independence_attestation=cycle["independence_attestation"],
                candidate_identity=confirmed.identity, source_cycle_id=cycle["cycle_id"],
            )
            complete_operation_step(conn, operation_id, route_actor_step)
            conn.execute(
                "UPDATE operations SET editor_agent=?, verifier_agent=NULL, run_id=?, independence_attestation=? WHERE operation_id=?",
                (agent, cycle["run_id"], cycle["independence_attestation"], operation_id),
            )
        if verification_hold or route == "human-review":
            transition_operation(conn, operation_id, phase="held_human")
        elif route == "evidence":
            transition_operation(conn, operation_id, phase="held_evidence")
        hold_fields = (None, None, None)
        if verification_hold or route in {"evidence", "human-review"}:
            hold_version = _confirmed_version(
                conn, operation_id=operation_id, task_gid=op["task_gid"], identity=confirmed.identity
            )
            hold_fields = (hold_version["content_version_id"], confirmed.identity, confirmed.section_gid)
        conn.execute(
            """UPDATE verification_cycles
                  SET correction_class=?, outcome=?, route=?, resume_state=?, completed_at=?,
                      hold_content_version_id=?, hold_identity=?, hold_section_gid=?
                WHERE cycle_id=?""",
            (
                "large" if route == "large" else None,
                outcome,
                {"evidence": "evidence", "human-review": "human_review"}.get(route),
                document.state.values["Resume status"], utc_now(),
                hold_fields[0], hold_fields[1], hold_fields[2], cycle["cycle_id"],
            ),
        )
        complete_operation_step(conn, operation_id, route_cycle_step)
        if route == "large" and not verification_hold:
            next_number = conn.execute("SELECT COALESCE(MAX(cycle_number), 0) + 1 FROM verification_cycles WHERE task_gid = ?", (op["task_gid"],)).fetchone()[0]
            new_cycle = create_verification_cycle(conn, operation_id=operation_id, task_gid=op["task_gid"], cycle_number=next_number, protocol_release=snapshot.identity, protocol_text=snapshot.text, route=None)
            complete_operation_step(conn, operation_id, route_new_cycle_step)
            transition_operation(conn, operation_id, phase="await_verification")
        else:
            new_cycle = None
        complete_operation_step(conn, operation_id, route_phase_step)
        if agent_attested_decisions:
            record_audit(
                conn,
                submission_id=None,
                task_gid=op["task_gid"],
                operation_id=operation_id,
                event_type="decision.agent_attested",
                actor_agent=agent,
                actor_run_id=run_id,
                actor_attestation=authority_attestation,
                actor_source="agent-attested-conversation",
                details={
                    "cycle_id": cycle["cycle_id"],
                    "appended_decisions": list(agent_attested_decisions),
                    "formal_marco_authorization": False,
                },
                result_code="OK",
                result_ok=True,
                governed_kind="decision",
                before_state={"Decisions": list(before_document.decisions)},
                after_state={"Decisions": list(document.decisions)},
            )
        record_audit(conn, submission_id=None, task_gid=op["task_gid"], operation_id=operation_id, event_type="verification.rejected", actor_agent=agent, details={"cycle_id": cycle["cycle_id"], "route": route, "reason": reason, "quantified_blocker": quantified_blocker, "human_review_basis": str(human_review_basis or "").strip() or None, "repairs_considered": str(repairs_considered or "").strip() or None, "human_review_options": normalized_human_review_options, "verification_hold": verification_hold, "identity": confirmed.identity}, result_code="OK", result_ok=True, governed_kind="decision", before_state={"outcome": None, "reviewed_identity": cycle["reviewed_identity"], "status": "pending-verification"}, after_state={"outcome": outcome, "route": route, "resume_state": document.state.values["Resume status"], "status": document.state.values["Status"]}, actor_run_id=run_id, actor_attestation=authority_attestation)
    return {
        "operation_id": operation_id,
        "route": route,
        "verification_hold": verification_hold,
        "new_cycle_id": None if new_cycle is None else new_cycle["cycle_id"],
        "task": dataclasses.asdict(confirmed),
        "parked_task_gid": op["task_gid"],
        "batch_may_continue": True,
        "batch_continuation_reason": (
            "This task is durably parked for Marco review."
            if verification_hold or route in {"human-review", "evidence"}
            else "This correction round is complete and a later fresh verifier is required."
        ),
    }



def _confirmed_semantic_proposal_application(
    conn: sqlite3.Connection,
    *,
    proposal: sqlite3.Row,
    applying_agent: str,
    applying_owner_id: str,
    applying_run_id: str,
    expected_authorization_changes,
) -> dict[str, Any]:
    """Prove one already-confirmed write is this exact claimed proposal application.

    This is intentionally stricter than "the candidate is live".  Recovery is allowed
    only when immutable write-attempt intent binds the proposal/candidate and exact
    applying actor/run, the durable approval names the same authorization IDs, those
    exact grants match the proposal's governed changes and are already consumed by the
    confirmed candidate, and the write attempt is bound to an exact confirmed content
    version.
    """
    if proposal["status"] != "claimed":
        raise DishRuleError(
            "CONFLICT",
            "confirmed proposal-application recovery requires an existing claim",
            rule="semantic_proposal_application_recovery_claim_missing",
        )
    if (
        proposal["claimed_agent"] != applying_agent
        or proposal["claimed_owner_id"] != applying_owner_id
        or proposal["claimed_run_id"] != applying_run_id
    ):
        raise DishRuleError(
            "CONFLICT",
            "proposal application recovery actor does not match the durable claim",
            rule="semantic_proposal_application_recovery_actor_mismatch",
            details={
                "claimed_agent": proposal["claimed_agent"],
                "claimed_owner_id": proposal["claimed_owner_id"],
                "claimed_run_id": proposal["claimed_run_id"],
            },
        )

    approval_rows = conn.execute(
        """SELECT event_id,details FROM audit_events
             WHERE operation_id=? AND event_type='semantic_proposal.approved'
               AND json_extract(details, '$.proposal_id')=?
             ORDER BY created_at,event_id""",
        (proposal["operation_id"], proposal["proposal_id"]),
    ).fetchall()
    if len(approval_rows) != 1:
        raise DishRuleError(
            "CONFLICT",
            "proposal application recovery requires one exact durable approval fact",
            rule="semantic_proposal_application_recovery_approval_invalid",
            details={"approval_fact_count": len(approval_rows)},
        )
    approval_details = json.loads(approval_rows[0]["details"])
    approval_authorization_ids = tuple(approval_details.get("authorization_ids") or ())
    if len(approval_authorization_ids) != len(set(approval_authorization_ids)):
        raise DishRuleError(
            "CONFLICT",
            "proposal approval contains a duplicate authorization binding",
            rule="semantic_proposal_application_recovery_authorization_invalid",
        )

    matching_attempts = []
    for attempt in conn.execute(
        """SELECT attempt.*, version.task_gid AS confirmed_task_gid,
                         version.operation_id AS confirmed_operation_id,
                         version.identity AS confirmed_identity,
                         version.title AS confirmed_title,
                         version.notes AS confirmed_notes
              FROM write_attempts AS attempt
              LEFT JOIN content_versions AS version
                ON version.content_version_id=attempt.confirmed_content_version_id
             WHERE attempt.operation_id=? AND attempt.outcome='confirmed'
             ORDER BY attempt.started_at,attempt.attempt_id""",
        (proposal["operation_id"],),
    ).fetchall():
        context = json.loads(attempt["context_json"] or "{}")
        binding = context.get("semantic_proposal_application")
        if not isinstance(binding, dict) or binding.get("proposal_id") != proposal["proposal_id"]:
            continue
        matching_attempts.append((attempt, context, binding))
    if len(matching_attempts) != 1:
        raise DishRuleError(
            "CONFLICT",
            "proposal application recovery lacks one exact confirmed write binding",
            rule="semantic_proposal_application_recovery_write_invalid",
            details={"matching_confirmed_write_count": len(matching_attempts)},
        )

    attempt, context, binding = matching_attempts[0]
    bound_authorization_ids = tuple(context.get("authorization_ids") or ())
    expected_binding = {
        "proposal_id": proposal["proposal_id"],
        "candidate_identity": proposal["candidate_identity"],
        "application_actor": applying_agent,
        "application_owner_id": applying_owner_id,
        "application_run_id": applying_run_id,
    }
    if any(binding.get(key) != value for key, value in expected_binding.items()):
        raise DishRuleError(
            "CONFLICT",
            "confirmed write is bound to a different semantic proposal application",
            rule="semantic_proposal_application_recovery_binding_mismatch",
        )
    if bound_authorization_ids != approval_authorization_ids:
        raise DishRuleError(
            "CONFLICT",
            "confirmed write authorization set differs from the durable proposal approval",
            rule="semantic_proposal_application_recovery_authorization_mismatch",
        )
    if (
        attempt["purpose"] != "content_write"
        or attempt["expected_identity"] != proposal["baseline_identity"]
        or attempt["intended_identity"] != proposal["candidate_identity"]
        or attempt["intended_title"] != proposal["candidate_title"]
        or attempt["intended_notes"] != proposal["candidate_notes"]
        or attempt["confirmed_task_gid"] != proposal["task_gid"]
        or attempt["confirmed_operation_id"] != proposal["operation_id"]
        or attempt["confirmed_identity"] != proposal["candidate_identity"]
        or attempt["confirmed_title"] != proposal["candidate_title"]
        or attempt["confirmed_notes"] != proposal["candidate_notes"]
    ):
        raise DishRuleError(
            "CONFLICT",
            "confirmed write evidence does not match the immutable approved candidate",
            rule="semantic_proposal_application_recovery_write_mismatch",
        )

    expected_by_field = {
        change.field: (
            json.dumps(change.before, sort_keys=True, separators=(",", ":")),
            json.dumps(change.after, sort_keys=True, separators=(",", ":")),
        )
        for change in expected_authorization_changes
    }
    if len(expected_by_field) != len(expected_authorization_changes):
        raise DishRuleError(
            "CONFLICT",
            "proposal recovery cannot disambiguate repeated governed authorization fields",
            rule="semantic_proposal_application_recovery_authorization_invalid",
        )
    if len(bound_authorization_ids) != len(expected_by_field):
        raise DishRuleError(
            "CONFLICT",
            "confirmed write authorization count differs from the approved governed changes",
            rule="semantic_proposal_application_recovery_authorization_mismatch",
        )
    for authorization_id in bound_authorization_ids:
        authorization = conn.execute(
            "SELECT * FROM marco_authorizations WHERE authorization_id=?",
            (authorization_id,),
        ).fetchone()
        if authorization is None:
            raise DishRuleError(
                "CONFLICT",
                "proposal recovery authorization evidence is missing",
                rule="semantic_proposal_application_recovery_authorization_invalid",
            )
        expected = expected_by_field.get(authorization["field_name"])
        if (
            expected is None
            or authorization["task_gid"] != proposal["task_gid"]
            or authorization["operation_id"] != proposal["operation_id"]
            or authorization["before_json"] != expected[0]
            or authorization["after_json"] != expected[1]
            or authorization["reserved_by_operation_id"] != proposal["operation_id"]
            or authorization["consumed_at"] is None
            or authorization["consumed_identity"] != proposal["candidate_identity"]
        ):
            raise DishRuleError(
                "CONFLICT",
                "proposal recovery authorization does not prove this exact confirmed application",
                rule="semantic_proposal_application_recovery_authorization_mismatch",
                details={"authorization_id": authorization_id},
            )
        grants = conn.execute(
            """SELECT event_id FROM audit_events
                 WHERE operation_id=? AND event_type='marco.authorization'
                   AND json_extract(details, '$.authorization_id')=?
                   AND json_extract(details, '$.proposal_id')=?""",
            (proposal["operation_id"], authorization_id, proposal["proposal_id"]),
        ).fetchall()
        if len(grants) != 1:
            raise DishRuleError(
                "CONFLICT",
                "proposal recovery authorization grant is not uniquely bound to this proposal",
                rule="semantic_proposal_application_recovery_authorization_invalid",
                details={"authorization_id": authorization_id, "grant_count": len(grants)},
            )

    return {
        "attempt_id": attempt["attempt_id"],
        "confirmed_content_version_id": attempt["confirmed_content_version_id"],
        "authorization_ids": bound_authorization_ids,
    }


def apply_semantic_proposal(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    proposal_id: str,
    agent: str,
    model: str,
    owner_id: str,
    run_id: str,
    request_id: str | None,
    schema=None,
) -> dict[str, Any]:
    """Claim and install one exact Marco-approved Large-correction bundle."""
    clean_id = str(proposal_id or "").strip()
    if not clean_id:
        raise DishRuleError(
            "INVALID_ARGUMENT", "proposal ID is required",
            rule="semantic_proposal_id_required",
        )
    initial_proposal = get_semantic_proposal(conn, clean_id)
    was_claimed = initial_proposal["status"] == "claimed"
    proposal = claim_semantic_proposal(
        conn, proposal_id=clean_id, agent=agent, owner_id=owner_id, run_id=run_id,
        request_id=request_id,
    )
    write_confirmed = False
    candidate_already_live = False
    recovered_confirmed_write = False
    authorization_ids: tuple[str, ...] = ()
    try:
        op, cycle = _rows(conn, str(proposal["operation_id"]))
        if str(cycle["cycle_id"]) != str(proposal["cycle_id"]):
            raise DishRuleError(
                "CONFLICT", "the proposal's Verification cycle is no longer current",
                rule="semantic_proposal_cycle_stale",
                details={
                    "proposal_cycle_id": proposal["cycle_id"],
                    "current_cycle_id": cycle["cycle_id"],
                },
            )
        live = read_complete_task(
            backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID
        )
        live_matches_baseline = live.identity == proposal["baseline_identity"]
        candidate_already_live = live.identity == proposal["candidate_identity"]
        if not live_matches_baseline and not candidate_already_live:
            raise DishRuleError(
                "CONFLICT", "the live task content changed after the proposal was created",
                rule="semantic_proposal_stale",
                details={
                    **semantic_proposal_drift_details(
                        conn, proposal, live_title=live.title, live_notes=live.notes
                    ),
                    "live_matches_candidate": False,
                    "required_action": (
                        "Inspect the exact title/notes diff before deciding whether fresh review is "
                        "required. Due date, section, assignee, completion, and comments are not "
                        "part of semantic proposal content identity."
                    ),
                },
            )
        if live_matches_baseline:
            baseline_title, baseline_notes = live.title, live.notes
        else:
            # The exact approved candidate can already be live after a previously confirmed
            # write whose proposal finalization failed, or after legacy behavior exposed the
            # candidate early.  Once Marco has approved this exact candidate, reconcile that
            # state instead of demanding a second write/review cycle.
            baseline_title, baseline_notes = semantic_proposal_baseline_content(conn, proposal)
        before_document, document = validate_semantic_proposal_integrity(
            conn,
            proposal,
            baseline_title=baseline_title,
            baseline_notes=baseline_notes,
        )
        agent_attested_decisions = tuple(
            json.loads(proposal["agent_attested_decisions_json"])
        )
        expected_authorization_changes = governed_changes_requiring_authorization(
            before_document, document,
            agent_attested_decisions=agent_attested_decisions,
        )
        if candidate_already_live and was_claimed:
            recovery = _confirmed_semantic_proposal_application(
                conn,
                proposal=proposal,
                applying_agent=agent,
                applying_owner_id=owner_id,
                applying_run_id=run_id,
                expected_authorization_changes=expected_authorization_changes,
            )
            authorization_ids = tuple(recovery["authorization_ids"])
            recovered_confirmed_write = True
            write_confirmed = True
        else:
            authorization_ids = require_governed_authorization(
                conn, before_document, document,
                task_gid=op["task_gid"], operation_id=op["operation_id"],
                proposal_reason=proposal["proposal_reason"],
                agent_attested_decisions=agent_attested_decisions,
            )
        step_suffix = clean_id
        write_step = f"semantic_proposal_write:{step_suffix}"
        actor_step = f"semantic_proposal_actor:{step_suffix}"
        cycle_step = f"semantic_proposal_cycle:{step_suffix}"
        new_cycle_step = f"semantic_proposal_new_cycle:{step_suffix}"
        proposal_step = f"semantic_proposal_applied:{step_suffix}"
        declare_operation_step(conn, op["operation_id"], write_step, {
            "proposal_id": clean_id,
            "baseline_identity": proposal["baseline_identity"],
            "candidate_identity": proposal["candidate_identity"],
        })
        declare_operation_step(conn, op["operation_id"], actor_step, {
            "role": "material_editor", "agent": proposal["proposer_agent"],
            "run_id": proposal["proposer_run_id"],
            "candidate_identity": proposal["candidate_identity"],
            "source_cycle_id": cycle["cycle_id"],
        })
        declare_operation_step(conn, op["operation_id"], cycle_step, {
            "cycle_id": cycle["cycle_id"], "outcome": "rejected",
            "correction_class": "large", "proposal_id": clean_id,
        })
        declare_operation_step(conn, op["operation_id"], new_cycle_step, {
            "protocol_release": proposal["protocol_release"],
            "proposal_id": clean_id,
        })
        declare_operation_step(conn, op["operation_id"], proposal_step, {
            "proposal_id": clean_id, "applying_agent": agent,
            "applying_run_id": run_id,
        })
        if candidate_already_live:
            # No external mutation is necessary: the live title/notes are byte-for-byte the
            # approved candidate.  It must still pass the same deterministic boundary that a
            # normal guarded write would enforce before Dish settles the proposal.
            check = validate_task_document(
                document, expected_schema_version=op["schema_version"], schema=schema
            )
            if not check.ok:
                raise DishRuleError(
                    "VALIDATION_FAILED",
                    "approved candidate already live but failed deterministic validation",
                    errors=[finding_payload(finding) for finding in check.findings],
                )
            if not recovered_confirmed_write:
                # Legacy/already-live reconciliation has no prior application write to reuse.
                # Persist the exact observed candidate and consume the still-unused approval
                # during finalization.  Confirmed-write recovery instead reuses the immutable
                # write-attempt/content-version evidence that already consumed authorization.
                confirmed_identity = confirm_task_content(
                    conn,
                    task_gid=op["task_gid"],
                    title=live.title,
                    notes=live.notes,
                    schema_version=op["schema_version"],
                    operation_id=op["operation_id"],
                    boundary="semantic_proposal_candidate_already_live",
                )
                if confirmed_identity.digest != live.identity:
                    raise DishRuleError(
                        "INTERNAL_ERROR",
                        "confirmed candidate identity changed while reconciling the approved proposal",
                        rule="semantic_proposal_reconciliation_identity_mismatch",
                    )
            confirmed = live
        else:
            confirmed = _write_document(
                conn, backend, op, live, document, schema=schema,
                authorization_ids=authorization_ids,
                write_context={
                    "semantic_proposal_application": {
                        "proposal_id": clean_id,
                        "candidate_identity": proposal["candidate_identity"],
                        "application_actor": agent,
                        "application_owner_id": owner_id,
                        "application_run_id": run_id,
                    },
                },
            )
            write_confirmed = True
        with savepoint_transaction(conn, "semantic_proposal_apply_finalize"):
            if candidate_already_live and not recovered_confirmed_write:
                consume_reserved_marco_authorizations(
                    conn,
                    operation_id=op["operation_id"],
                    authorization_ids=authorization_ids,
                    candidate_identity=confirmed.identity,
                )
            complete_operation_step(conn, op["operation_id"], write_step)
            record_actor_fact(
                conn, operation_id=op["operation_id"], task_gid=op["task_gid"],
                role="material_editor", agent=proposal["proposer_agent"],
                run_id=proposal["proposer_run_id"],
                independence_attestation=cycle["independence_attestation"],
                candidate_identity=confirmed.identity,
                source_cycle_id=cycle["cycle_id"],
            )
            complete_operation_step(conn, op["operation_id"], actor_step)
            conn.execute(
                """UPDATE operations
                      SET editor_agent=?, verifier_agent=NULL, run_id=?,
                          independence_attestation=?
                    WHERE operation_id=?""",
                (
                    proposal["proposer_agent"], proposal["proposer_run_id"],
                    cycle["independence_attestation"], op["operation_id"],
                ),
            )
            conn.execute(
                """UPDATE verification_cycles
                      SET correction_class='large', outcome='rejected', route=NULL,
                          resume_state=NULL, completed_at=?
                    WHERE cycle_id=? AND completed_at IS NULL""",
                (utc_now(), cycle["cycle_id"]),
            )
            complete_operation_step(conn, op["operation_id"], cycle_step)
            next_number = conn.execute(
                "SELECT COALESCE(MAX(cycle_number),0)+1 FROM verification_cycles WHERE task_gid=?",
                (op["task_gid"],),
            ).fetchone()[0]
            new_cycle = create_verification_cycle(
                conn, operation_id=op["operation_id"], task_gid=op["task_gid"],
                cycle_number=next_number,
                protocol_release=proposal["protocol_release"],
                protocol_text=proposal["protocol_text"], route=None,
            )
            complete_operation_step(conn, op["operation_id"], new_cycle_step)
            transition_operation(conn, op["operation_id"], phase="await_verification")
            if agent_attested_decisions:
                record_audit(
                    conn,
                    submission_id=None,
                    task_gid=op["task_gid"],
                    operation_id=op["operation_id"],
                    event_type="decision.agent_attested",
                    actor_agent=proposal["proposer_agent"],
                    actor_run_id=proposal["proposer_run_id"],
                    actor_attestation=cycle["independence_attestation"],
                    actor_source="agent-attested-conversation",
                    details={
                        "proposal_id": clean_id,
                        "cycle_id": cycle["cycle_id"],
                        "appended_decisions": list(agent_attested_decisions),
                        "formal_marco_authorization": False,
                    },
                    result_code="OK",
                    result_ok=True,
                    governed_kind="decision",
                    before_state={"Decisions": list(before_document.decisions)},
                    after_state={"Decisions": list(document.decisions)},
                )
            applied = mark_semantic_proposal_applied(
                conn, proposal_id=clean_id, owner_id=owner_id, run_id=run_id,
                applied_identity=confirmed.identity,
            )
            complete_operation_step(conn, op["operation_id"], proposal_step)
            mechanical = agent == MECHANICAL_PROPOSAL_AGENT
            record_audit(
                conn, submission_id=None, task_gid=op["task_gid"],
                operation_id=op["operation_id"],
                event_type="semantic_proposal.application_completed",
                actor_agent=None if mechanical else agent, actor_run_id=run_id,
                actor_source="dish-mechanical" if mechanical else "command",
                details={
                    "proposal_id": clean_id,
                    "proposer_agent": proposal["proposer_agent"],
                    "proposer_run_id": proposal["proposer_run_id"],
                    "application_actor": agent,
                    "applied_identity": confirmed.identity,
                    "new_cycle_id": new_cycle["cycle_id"],
                    "model": model,
                    "candidate_already_live": candidate_already_live,
                    "recovered_confirmed_write": recovered_confirmed_write,
                }, result_code="OK", result_ok=True,
            )
        return {
            "proposal": proposal_payload(conn, applied),
            "operation_id": op["operation_id"],
            "completed_cycle_id": cycle["cycle_id"],
            "new_cycle_id": new_cycle["cycle_id"],
            "applied_identity": confirmed.identity,
            "task": dataclasses.asdict(confirmed),
            "candidate_already_live": candidate_already_live,
            "recovered_confirmed_write": recovered_confirmed_write,
            "next_step": (
                (
                    "The exact approved candidate was already live and Dish reconciled it "
                    "without rewriting task content. A later genuinely fresh Verification run "
                    "must independently review the new cycle."
                )
                if candidate_already_live
                else (
                    "The exact approved candidate is installed. A later genuinely fresh "
                    "Verification run must independently review the new cycle."
                )
            ),
        }
    except DishRuleError as exc:
        if not was_claimed and not write_confirmed and exc.code != "BACKEND_UNCERTAIN":
            if authorization_ids:
                release_marco_authorization_reservations(
                    conn,
                    operation_id=str(proposal["operation_id"]),
                    authorization_ids=authorization_ids,
                )
            release_semantic_proposal_claim(
                conn, proposal_id=clean_id, owner_id=owner_id, run_id=run_id,
                reason=f"application failed before a confirmed write: {exc.rule or exc.code}",
            )
        raise



def _reset_path(document, category: str) -> dict[str, str]:
    if category == "method":
        return {"sections.HOW TO COOK IT": document.sections.get("HOW TO COOK IT", "")}
    if category == "evidence":
        return {"research_basis": "\n".join(document.research_basis)}
    if category == "premise":
        return {
            "recognition": document.recognition,
            "introduction": "\n".join(document.introduction),
            "sections.WHY COOK IT": document.sections.get("WHY COOK IT", ""),
            "planning.Purpose": document.planning_brief.values.get("Purpose", ""),
            "planning.Research emphasis": document.planning_brief.values.get("Research emphasis", ""),
            "decisions": "\n".join(document.decisions),
        }
    if category == "scope":
        return {
            "sections.QUANTITIES": document.sections.get("QUANTITIES", ""),
            "planning.Role": document.planning_brief.values.get("Role", ""),
            "planning.Purpose": document.planning_brief.values.get("Purpose", ""),
        }
    return {}



_RESET_QUANTITY_RE = re.compile(r"(?<!\w)(\d+(?:[.,]\d+)?)\s*(kg|g|mg|l|ml|cl)\b", re.I)


def _reset_normal_form(value: str) -> str:
    from decimal import Decimal

    def canonical_quantity(match: re.Match[str]) -> str:
        amount = Decimal(match.group(1).replace(",", "."))
        unit = match.group(2).casefold()
        if unit == "kg":
            amount *= 1000
            unit = "g"
        elif unit == "mg":
            amount /= 1000
            unit = "g"
        elif unit == "l":
            amount *= 1000
            unit = "ml"
        elif unit == "cl":
            amount *= 10
            unit = "ml"
        text = format(amount.normalize(), "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return f"{text}{unit}"

    value = str(value).casefold().replace("\u00a0", " ")
    value = _RESET_QUANTITY_RE.sub(canonical_quantity, value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _prove_reset(original, candidate, category: str, before: str, after: str) -> str:
    """Prove one operative replacement hunk at a category-owned canonical path."""
    before = str(before).strip()
    after = str(after).strip()
    before_norm = _reset_normal_form(before)
    after_norm = _reset_normal_form(after)
    old_paths = _reset_path(original, category)
    new_paths = _reset_path(candidate, category)
    matches: list[str] = []
    for path, old_value in old_paths.items():
        new_value = new_paths.get(path, "")
        if old_value == new_value:
            continue
        old_lines = [_reset_normal_form(line) for line in str(old_value).splitlines() if line.strip()]
        new_lines = [_reset_normal_form(line) for line in str(new_value).splitlines() if line.strip()]
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        path_matches = 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag not in {"replace", "delete", "insert"}:
                continue
            deleted = "\n".join(old_lines[i1:i2])
            inserted = "\n".join(new_lines[j1:j2])
            if (
                before_norm in deleted
                and after_norm in inserted
                and before_norm not in inserted
                and after_norm not in deleted
            ):
                path_matches += 1
        if path_matches == 1:
            matches.append(path)
    if len(matches) == 1:
        return matches[0]
    raise DishRuleError(
        "VALIDATION_FAILED",
        "corrected candidate must replace the declared operative value in one diff hunk",
        rule="verification_hold_reset_not_applied",
        details={"category": category, "before": before, "after": after, "matching_paths": matches},
    )

def reopen_verification_hold(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    category: str,
    before: str,
    after: str,
    editor: str,
    model: str,
    run_id: str,
    file_path: str,
    date: str,
    honest_root=None,
    schema=None,
):
    op = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if op is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    if category not in RESET_CATEGORIES or not all(str(x or "").strip() for x in (before, after, editor, run_id, file_path, date)):
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "reopen requires a corrected candidate, substantive reset, editor, and run proof",
            rule="substantive_reset_required",
        )
    if editor not in {"gpt", "codex", "claude"}:
        raise DishRuleError("INVALID_ARGUMENT", "reopen editor must be an agent", rule="reopen_editor_required")
    cycle = conn.execute(
        """SELECT * FROM verification_cycles
             WHERE operation_id=? AND outcome='verification-hold'
             ORDER BY cycle_number DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if cycle is None:
        raise DishRuleError("WRONG_STATE", "operation has no Verification hold", rule="verification_hold_required")
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    original = _held_document(conn, cycle=cycle, live=live)
    if original.state.values["Status"] != "pending-human-review" or original.state.values["Resume status"] != "pending-verification":
        raise DishRuleError("WRONG_STATE", "task is not on the Verification hold", rule="verification_hold_required")
    candidate = _candidate(file_path)
    candidate_state = dict(candidate.state.values)
    candidate_state["Researched by"] = original.state.values["Researched by"]
    candidate = dataclasses.replace(candidate, state=TaskState(candidate_state))
    candidate = preserve_material_change_history(original, candidate)
    changed_path = _prove_reset(original, candidate, category, before, after)
    if honest_root is None:
        previous = conn.execute(
            "SELECT protocol_release, protocol_text FROM verification_cycles WHERE operation_id=? ORDER BY cycle_number DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
        snapshot = type("Snapshot", (), {"identity": previous["protocol_release"], "text": previous["protocol_text"]})()
    else:
        snapshot = current_verification_protocol_release(honest_root)
    state_values = dict(candidate.state.values)
    state_values.update({
        "Status": "pending-verification",
        "Status detail": "None",
        "Resume status": "None",
        "Verification protocol release": snapshot.identity,
        "Verified by": "None",
        "Self-verified": material_editor_line(editor, model, date),
    })
    entry = material_change_line(
        editor,
        model,
        date,
        change=f"reset {category} at {changed_path} from {before} to {after}",
        reason="Marco-authorized substantive reset after a Verification hold",
        materiality="Large",
    )
    document = dataclasses.replace(
        candidate,
        state=TaskState(state_values),
        material_changes=tuple(candidate.material_changes) + (entry,),
    )
    check = validate_task_document(document, expected_schema_version=op["schema_version"], schema=schema)
    if not check.ok:
        raise DishRuleError(
            "VALIDATION_FAILED",
            "corrected reopen candidate failed deterministic validation",
            errors=[finding_payload(f) for f in check.findings],
        )
    authorization_ids = require_governed_authorization(
        conn, original, document, task_gid=op["task_gid"], operation_id=operation_id
    )
    intended_title, intended_notes = _render(document)
    intended_identity = content_identity(intended_title, intended_notes).digest
    declare_operation_step(conn, operation_id, "reopen_write", {"title": intended_title, "notes": intended_notes, "reset_path": changed_path})
    declare_operation_step(conn, operation_id, "reopen_reset", {
        "source_cycle_id": cycle["cycle_id"], "candidate_identity": intended_identity,
        "canonical_path": changed_path, "category": category,
        "before": before, "after": after,
    })
    declare_operation_step(conn, operation_id, "reopen_actor", {
        "role": "material_editor", "agent": editor, "run_id": run_id,
        "candidate_identity": intended_identity,
    })
    declare_operation_step(conn, operation_id, "reopen_cycle", {"protocol_release": snapshot.identity, "protocol_text": snapshot.text})
    declare_operation_step(conn, operation_id, "reopen_phase", {"phase": "await_verification"})
    confirmed = _write_document(
        conn, backend, op, live, document, schema=schema, authorization_ids=authorization_ids
    )
    complete_operation_step(conn, operation_id, "reopen_write")
    conn.execute(
        """INSERT OR IGNORE INTO verification_hold_resets(
               reset_id, operation_id, source_cycle_id, candidate_identity,
               canonical_path, category, before_json, after_json, created_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            str(uuid.uuid4()), operation_id, cycle["cycle_id"], confirmed.identity,
            changed_path, category, json.dumps(before), json.dumps(after), utc_now(),
        ),
    )
    complete_operation_step(conn, operation_id, "reopen_reset")
    record_actor_fact(
        conn, operation_id=operation_id, task_gid=op["task_gid"], role="material_editor",
        agent=editor, run_id=run_id, candidate_identity=confirmed.identity,
    )
    complete_operation_step(conn, operation_id, "reopen_actor")
    number = conn.execute(
        "SELECT COALESCE(MAX(cycle_number), 0) + 1 FROM verification_cycles WHERE task_gid = ?",
        (op["task_gid"],),
    ).fetchone()[0]
    cycle = create_verification_cycle(
        conn, operation_id=operation_id, task_gid=op["task_gid"], cycle_number=number,
        protocol_release=snapshot.identity, protocol_text=snapshot.text, route=None,
    )
    complete_operation_step(conn, operation_id, "reopen_cycle")
    conn.execute(
        "UPDATE operations SET editor_agent=?, verifier_agent=NULL, run_id=?, independence_attestation=NULL WHERE operation_id=?",
        (editor, run_id, operation_id),
    )
    transition_operation(conn, operation_id, phase="await_verification")
    complete_operation_step(conn, operation_id, "reopen_phase")
    return {
        "operation_id": operation_id,
        "cycle_id": cycle["cycle_id"],
        "task": dataclasses.asdict(confirmed),
        "material_change": entry,
    }


def resolve_hold(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    resolution_kind: str,
    detail: str,
    resume_status: str,
    honest_root,
    schema=None,
    file_path: str | None = None,
    editor: str | None = None,
    model: str | None = None,
    run_id: str | None = None,
    expected_task_gid: str | None = None,
    expected_cycle_id: str | None = None,
    expected_hold_identity: str | None = None,
    record_human_decision: bool = True,
):
    """Resolve an Evidence or Human Review hold from exact live state.

    A substantive resolution may resume to Research or Verification according
    to its stored route. Dismissal rejects the Human Review escalation itself
    and always returns the unchanged candidate to fresh Verification. A supplied
    candidate is treated as a material edit and freezes the current Verification
    release.
    """
    if resolution_kind not in {"evidence", "human_review"}:
        raise DishRuleError("INVALID_ARGUMENT", "invalid hold resolution kind", rule="invalid_hold_resolution")
    if not record_human_decision and resolution_kind != "human_review":
        raise DishRuleError("INVALID_ARGUMENT", "only Human Review holds can be dismissed", rule="hold_dismissal_kind_invalid")
    resolution_mode = "decision" if record_human_decision else "dismissal"
    if not record_human_decision:
        # Dismissal rejects the escalation itself; it does not resolve the issue
        # that the verifier claimed required Research or another human decision.
        # The only legal continuation is therefore a fresh Verification round.
        resume_status = "pending-verification"
    if resume_status not in {"pending-research", "pending-verification"}:
        raise DishRuleError("INVALID_ARGUMENT", "invalid hold resume status", rule="resume_status_required")
    resolution_outcome = hold_resolution_outcome(resume_status)
    clean_detail = str(detail or "").strip()
    if not clean_detail:
        raise DishRuleError("INVALID_ARGUMENT", "resolution detail is required", rule="resolution_detail_required")
    if clean_detail.startswith("<") and clean_detail.endswith(">"):
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "resolution detail still contains the unfilled command placeholder",
            rule="resolution_detail_placeholder",
        )
    op = conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if op is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    if expected_task_gid is not None and str(expected_task_gid).strip() != str(op["task_gid"]):
        raise DishRuleError("CONFLICT", "resolution command does not match the held task", rule="hold_task_mismatch", details={"expected": op["task_gid"], "actual": expected_task_gid})
    expected_phase = "held_evidence" if resolution_kind == "evidence" else "held_human"
    if op["status"] != "open" or op["phase"] != expected_phase:
        raise DishRuleError("WRONG_STATE", "operation is not on the requested hold", rule="hold_not_active")
    preconstruction = conn.execute(
        """SELECT intended_json FROM operation_steps
             WHERE operation_id=? AND step_name='research_preconstruction_hold'
               AND completed_at IS NOT NULL""",
        (operation_id,),
    ).fetchone()
    if preconstruction is not None:
        if not record_human_decision:
            raise DishRuleError(
                "WRONG_STATE",
                "pre-construction Human Review holds are not review-queue dismissals",
                rule="preconstruction_human_review_dismissal_unsupported",
            )
        hold_record = json.loads(preconstruction["intended_json"])
        if hold_record.get("route") != (
            "evidence" if resolution_kind == "evidence" else "human-review"
        ):
            raise DishRuleError(
                "WRONG_STATE",
                "hold resolution kind does not match the persisted Research hold",
                rule="hold_resolution_kind_mismatch",
            )
        if resume_status != "pending-research":
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "pre-construction Research holds must resume to pending-research",
                rule="preconstruction_resume_status_invalid",
                details={"expected": "pending-research", "actual": resume_status},
            )
        if file_path or editor or model:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "pre-construction Research hold resolution cannot install candidate content",
                rule="preconstruction_resolution_candidate_unexpected",
            )
        live = read_complete_task(
            backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID
        )
        if live.identity != op["expected_identity"] or live.section_gid != op["expected_section_gid"]:
            raise DishRuleError(
                "CONFLICT",
                "live task changed while Research was blocked before construction",
                rule="preconstruction_hold_baseline_drift",
                details={
                    "expected_identity": op["expected_identity"],
                    "actual_identity": live.identity,
                    "expected_section_gid": op["expected_section_gid"],
                    "actual_section_gid": live.section_gid,
                },
            )
        resolution = {
            "description": "Research block resolved before construction",
            "resolution_kind": resolution_kind,
            "detail": clean_detail,
            "resume_status": "pending-research",
            "candidate_content_existed": False,
        }
        abandoned_result = resolve_preconstruction_hold_to_successor(
            conn,
            operation_id=operation_id,
            resolution=resolution,
            live_identity=live.identity,
            live_section_gid=live.section_gid,
        )
        if abandoned_result is not None:
            abandoned_result["task"] = dataclasses.asdict(live)
            return abandoned_result
        declare_operation_step(
            conn, operation_id, "research_preconstruction_hold_resolution", resolution
        )
        transition_operation(conn, operation_id, phase="prepare_required")
        complete_operation_step(
            conn, operation_id, "research_preconstruction_hold_resolution"
        )
        record_audit(
            conn,
            submission_id=None,
            task_gid=op["task_gid"],
            operation_id=operation_id,
            event_type="research.preconstruction_resolved",
            actor_agent=editor if editor in {"gpt", "codex", "claude"} else None,
            actor_run_id=run_id,
            details=resolution,
            result_code="OK",
            result_ok=True,
            governed_kind="decision",
            before_state={"phase": expected_phase, "candidate_content_existed": False},
            after_state={"phase": "prepare_required", "resume_status": "pending-research"},
            actor_source="marco-hold-resolution",
        )
        return {
            "operation_id": operation_id,
            **resolution,
            "phase": "prepare_required",
            "task": dataclasses.asdict(live),
        }
    cycle = conn.execute(
        "SELECT * FROM verification_cycles WHERE operation_id=? AND route=? ORDER BY cycle_number DESC LIMIT 1",
        (operation_id, resolution_kind),
    ).fetchone()
    if cycle is None:
        raise DishRuleError("WRONG_STATE", "hold has no persisted Verification decision", rule="hold_cycle_missing")
    if expected_cycle_id is not None and str(expected_cycle_id).strip() != str(cycle["cycle_id"]):
        raise DishRuleError("CONFLICT", "resolution command does not match the active hold cycle", rule="hold_cycle_mismatch", details={"expected": cycle["cycle_id"], "actual": expected_cycle_id})
    if expected_hold_identity is not None and str(expected_hold_identity).strip() != str(cycle["hold_identity"]):
        raise DishRuleError("CONFLICT", "resolution command does not match the active hold identity", rule="hold_identity_mismatch", details={"expected": cycle["hold_identity"], "actual": expected_hold_identity})
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    before_doc = _held_document(conn, cycle=cycle, live=live)
    expected_status = "pending-evidence" if resolution_kind == "evidence" else "pending-human-review"
    if before_doc.state.values["Status"] != expected_status:
        raise DishRuleError("WRONG_STATE", "live task does not match the persisted hold", rule="hold_state_mismatch")
    original_reason = ""
    route_step = conn.execute(
        "SELECT intended_json FROM operation_steps WHERE operation_id=? AND step_name=? AND completed_at IS NOT NULL",
        (operation_id, f"route_cycle_finalize:{cycle['cycle_id']}"),
    ).fetchone()
    if route_step is not None:
        try:
            original_reason = str(json.loads(route_step["intended_json"] or "{}").get("decision_reason") or "").strip()
        except (TypeError, ValueError):
            original_reason = ""

    material = bool(file_path)
    if material and not record_human_decision:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "dismissing an invalid Human Review hold cannot install candidate content",
            rule="human_review_dismissal_candidate_forbidden",
        )
    snapshot = None
    if material:
        if not editor or editor not in {"gpt", "codex", "claude"}:
            raise DishRuleError("INVALID_ARGUMENT", "material hold resolution requires a named editor agent", rule="hold_editor_required")
        candidate = _candidate(file_path)
        candidate_state = dict(candidate.state.values)
        candidate_state["Researched by"] = before_doc.state.values["Researched by"]
        candidate = dataclasses.replace(candidate, state=TaskState(candidate_state))
        candidate = preserve_material_change_history(before_doc, candidate)
        snapshot = current_verification_protocol_release(honest_root)
        values = dict(candidate.state.values)
        values.update({
            "Status": resume_status,
            "Status detail": clean_detail if resume_status == "pending-research" else "None",
            "Resume status": "None",
            "Verified by": "None",
            "Verification protocol release": snapshot.identity if resume_status == "pending-verification" else "None",
            "Self-verified": material_editor_line(editor, model, utc_now()[:10]),
        })
        authorization_decisions = tuple(candidate.decisions)
        decisions = authorization_decisions
        if record_human_decision:
            decision = f"Human — Marco: {resolution_kind} resolved — {clean_detail}"
            if decision not in decisions:
                decisions += (decision,)
        document = dataclasses.replace(candidate, state=TaskState(values), decisions=decisions)
    else:
        values = dict(resumed(before_doc.state.values).values)
        values["Status"] = resume_status
        values["Status detail"] = clean_detail if resume_status == "pending-research" else "None"
        values["Verification protocol release"] = "None" if resume_status == "pending-research" else cycle["protocol_release"]
        authorization_decisions = tuple(before_doc.decisions)
        decisions = authorization_decisions
        if record_human_decision:
            decision = f"Human — Marco: {resolution_kind} resolved — {clean_detail}"
            if decision not in decisions:
                decisions += (decision,)
        document = dataclasses.replace(before_doc, state=TaskState(values), decisions=decisions)

    precheck = validate_task_document(document, expected_schema_version=op["schema_version"], schema=schema)
    if not precheck.ok:
        raise DishRuleError("VALIDATION_FAILED", "candidate failed deterministic validation", errors=[finding_payload(f) for f in precheck.findings])
    authorization_document = dataclasses.replace(document, decisions=authorization_decisions)
    authorization_ids = require_governed_authorization(
        conn, before_doc, authorization_document, task_gid=op["task_gid"], operation_id=operation_id
    )
    intended_title, intended_notes = _render(document)
    declare_operation_step(conn, operation_id, "hold_resolution_write", {"title": intended_title, "notes": intended_notes, "resolution_kind": resolution_kind, "resolution_mode": resolution_mode})
    declare_operation_step(conn, operation_id, "hold_resolution_decision", {
        "detail": clean_detail, "resume_status": resume_status, "material": material,
        "resolution_kind": resolution_kind, "resolution_mode": resolution_mode,
        "source_cycle_id": cycle["cycle_id"], "original_reason": original_reason,
    })
    if material:
        intended_identity = content_identity(intended_title, intended_notes).digest
        declare_operation_step(conn, operation_id, "hold_resolution_actor", {
            "role": "material_editor", "agent": editor, "run_id": run_id,
            "candidate_identity": intended_identity,
        })
    if resolution_outcome.operation_phase == "await_verification":
        if snapshot is None:
            next_release, next_text = cycle["protocol_release"], cycle["protocol_text"]
        else:
            next_release, next_text = snapshot.identity, snapshot.text
        declare_operation_step(conn, operation_id, "hold_resolution_cycle", {"protocol_release": next_release, "protocol_text": next_text})
        declare_operation_step(
            conn, operation_id, "hold_resolution_phase",
            {
                "phase": resolution_outcome.operation_phase,
                "status": resolution_outcome.operation_status,
            },
        )
    else:
        declare_operation_step(
            conn, operation_id, "hold_resolution_phase",
            {
                "phase": resolution_outcome.operation_phase,
                "status": resolution_outcome.operation_status,
                "terminal_outcome": f"{resolution_kind}_resolved_to_research",
            },
        )
    confirmed = _write_document(conn, backend, op, live, document, schema=schema, authorization_ids=authorization_ids)
    complete_operation_step(conn, operation_id, "hold_resolution_write")
    record_audit(
        conn, submission_id=None, task_gid=op["task_gid"], operation_id=operation_id,
        event_type="hold.resolved" if record_human_decision else "human_review.dismissed",
        actor_agent=editor if editor in {"gpt", "codex", "claude"} else None,
        details={
            "kind": resolution_kind, "mode": resolution_mode, "detail": clean_detail,
            "original_reason": original_reason, "source_cycle_id": cycle["cycle_id"],
            "resume_status": resume_status, "material": material, "identity": confirmed.identity,
        },
        result_code="OK", result_ok=True, governed_kind="decision" if record_human_decision else None,
        before_state={"status": expected_status, "resume_status": before_doc.state.values["Resume status"]},
        after_state={"status": resume_status, "identity": confirmed.identity, "human_decision_recorded": record_human_decision},
        actor_run_id=run_id,
        actor_source="marco-hold-resolution" if record_human_decision else "human-review-dismissal",
    )
    complete_operation_step(conn, operation_id, "hold_resolution_decision")
    if material:
        record_actor_fact(
            conn, operation_id=operation_id, task_gid=op["task_gid"], role="material_editor",
            agent=editor, run_id=run_id, candidate_identity=confirmed.identity,
        )
        complete_operation_step(conn, operation_id, "hold_resolution_actor")

    if resolution_outcome.operation_phase == "terminal":
        transition_operation(
            conn, operation_id,
            phase=resolution_outcome.operation_phase,
            status=resolution_outcome.operation_status,
            terminal_outcome=f"{resolution_kind}_resolved_to_research",
        )
        new_cycle = None
        complete_operation_step(conn, operation_id, "hold_resolution_phase")
    else:
        number = conn.execute(
            "SELECT COALESCE(MAX(cycle_number),0)+1 FROM verification_cycles WHERE task_gid=?",
            (op["task_gid"],),
        ).fetchone()[0]
        if snapshot is None:
            protocol_release = cycle["protocol_release"]
            protocol_text = cycle["protocol_text"]
        else:
            protocol_release = snapshot.identity
            protocol_text = snapshot.text
        new_cycle = create_verification_cycle(
            conn, operation_id=operation_id, task_gid=op["task_gid"], cycle_number=number,
            protocol_release=protocol_release, protocol_text=protocol_text,
        )
        complete_operation_step(conn, operation_id, "hold_resolution_cycle")
        conn.execute(
            "UPDATE operations SET verifier_agent=NULL, independence_attestation=NULL WHERE operation_id=?",
            (operation_id,),
        )
        transition_operation(conn, operation_id, phase=resolution_outcome.operation_phase)
        complete_operation_step(conn, operation_id, "hold_resolution_phase")
    return {
        "operation_id": operation_id,
        "resolution_kind": resolution_kind,
        "resolution_mode": resolution_mode,
        "dismissed_reason": None if record_human_decision else clean_detail,
        "dismissed_original_issue": None if record_human_decision else original_reason,
        "resume_status": resume_status,
        "material": material,
        "new_cycle_id": None if new_cycle is None else new_cycle["cycle_id"],
        "task": dataclasses.asdict(confirmed),
    }


def resolve_verification_hold(
    conn: sqlite3.Connection, backend: Any, *, operation_id: str, schema=None
):
    """Release a Verification hold without editing or approving its candidate."""
    op = conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if op is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    if op["status"] != "open" or op["phase"] != "held_human":
        raise DishRuleError("WRONG_STATE", "operation is not on a Verification hold", rule="verification_hold_required")
    cycle = conn.execute(
        "SELECT * FROM verification_cycles WHERE operation_id=? AND outcome='verification-hold' ORDER BY cycle_number DESC LIMIT 1",
        (operation_id,),
    ).fetchone()
    if cycle is None:
        raise DishRuleError("WRONG_STATE", "operation has no Verification hold", rule="verification_hold_required")
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    before_doc = _held_document(conn, cycle=cycle, live=live)
    if before_doc.state.values["Status"] != "pending-human-review" or before_doc.state.values["Resume status"] != "pending-verification":
        raise DishRuleError("WRONG_STATE", "live task does not match the Verification hold", rule="hold_state_mismatch")
    values = dict(before_doc.state.values)
    values.update({"Status": "pending-verification", "Status detail": "None", "Resume status": "None", "Verified by": "None", "Verification protocol release": cycle["protocol_release"]})
    document = dataclasses.replace(before_doc, state=TaskState(values))
    intended_title, intended_notes = _render(document)
    declare_operation_step(conn, operation_id, "verification_hold_release_write", {"title": intended_title, "notes": intended_notes, "source_cycle_id": cycle["cycle_id"]})
    declare_operation_step(conn, operation_id, "verification_hold_release_cycle", {"protocol_release": cycle["protocol_release"], "protocol_text": cycle["protocol_text"]})
    declare_operation_step(conn, operation_id, "verification_hold_release_phase", {"phase": "await_verification"})
    confirmed = _write_document(conn, backend, op, live, document, schema=schema)
    complete_operation_step(conn, operation_id, "verification_hold_release_write")
    number = conn.execute("SELECT COALESCE(MAX(cycle_number),0)+1 FROM verification_cycles WHERE task_gid=?", (op["task_gid"],)).fetchone()[0]
    new_cycle = create_verification_cycle(conn, operation_id=operation_id, task_gid=op["task_gid"], cycle_number=number, protocol_release=cycle["protocol_release"], protocol_text=cycle["protocol_text"])
    complete_operation_step(conn, operation_id, "verification_hold_release_cycle")
    conn.execute("UPDATE operations SET verifier_agent=NULL, independence_attestation=NULL WHERE operation_id=?", (operation_id,))
    transition_operation(conn, operation_id, phase="await_verification")
    complete_operation_step(conn, operation_id, "verification_hold_release_phase")
    record_audit(conn, submission_id=None, task_gid=op["task_gid"], operation_id=operation_id, event_type="verification.hold_released", actor_agent=None, details={"source_cycle_id": cycle["cycle_id"], "new_cycle_id": new_cycle["cycle_id"], "identity": confirmed.identity}, result_code="OK", result_ok=True, governed_kind="decision", before_state={"status": "pending-human-review", "outcome": "verification-hold"}, after_state={"status": "pending-verification", "approved": False}, actor_source="marco-admin")
    return {"operation_id": operation_id, "source_cycle_id": cycle["cycle_id"], "new_cycle_id": new_cycle["cycle_id"], "resume_status": "pending-verification", "candidate_identity": confirmed.identity, "approved": False, "signed_off": False, "task": dataclasses.asdict(confirmed)}
