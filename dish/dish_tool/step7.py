"""Step 7 exact-live Verification start/read and signoff."""
from __future__ import annotations

import dataclasses
import sqlite3
from typing import Any, Mapping

from .constants import COOKING_PROJECT_GID
from .database import mark_operation_completion, record_audit, transition_operation, assert_fresh_verifier, record_actor_fact, declare_operation_step, complete_operation_step, content_identity
from .errors import DishRuleError
from .models import VerifierIdentity, verification_actor_line, utc_now, SectionRegistry
from .lifecycle import assert_transition, ready, require_status
from .releases import resolve_verification_protocol
from .task_document import TaskState, parse_task_document, validate_task_document, finding_payload
from .task_store import read_complete_task, write_exact_content


def _operation_and_cycle(conn: sqlite3.Connection, operation_id: str):
    op = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if op is None:
        raise DishRuleError("NOT_FOUND", f"operation not found: {operation_id}", rule="operation_not_found")
    if op["status"] != "open":
        raise DishRuleError("WRONG_STATE", "operation is not open", rule="operation_not_open")
    cycle = conn.execute(
        "SELECT * FROM verification_cycles WHERE operation_id = ? AND completed_at IS NULL ORDER BY cycle_number DESC LIMIT 1",
        (operation_id,),
    ).fetchone()
    if cycle is None:
        raise DishRuleError("WRONG_STATE", "operation has no pending Verification cycle", rule="verification_cycle_missing")
    return op, cycle



def _content_version_for_identity(
    conn: sqlite3.Connection, *, operation_id: str, task_gid: str, identity: str
):
    row = conn.execute(
        """SELECT * FROM content_versions
             WHERE operation_id = ? AND task_gid = ? AND identity = ? AND confirmed = 1
             ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (operation_id, task_gid, identity),
    ).fetchone()
    if row is None:
        raise DishRuleError(
            "CONFLICT", "confirmed content version is missing",
            rule="content_version_missing", details={"identity": identity},
        )
    return row


def bind_cycle_review(
    conn: sqlite3.Connection, *, cycle_id: str, operation_id: str, task_gid: str, identity: str
):
    version = _content_version_for_identity(
        conn, operation_id=operation_id, task_gid=task_gid, identity=identity
    )
    conn.execute(
        """UPDATE verification_cycles
              SET reviewed_content_version_id = ?, reviewed_identity = ?
            WHERE cycle_id = ?""",
        (version["content_version_id"], identity, cycle_id),
    )
    return version

def verification_read(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    agent: str,
    honest_root,
    run_id: str | None,
    independence_attestation: str | None,
    schema: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    op, cycle = _operation_and_cycle(conn, operation_id)
    handoff = conn.execute("SELECT completed_at FROM operation_steps WHERE operation_id=? AND step_name='verification_handoff'", (operation_id,)).fetchone()
    if handoff is not None and handoff["completed_at"] is None:
        raise DishRuleError("WRONG_STATE", "Verification handoff is incomplete", rule="verification_handoff_incomplete")
    identity = VerifierIdentity(agent, run_id, independence_attestation)
    identity.validate(editor_agent=op["editor_agent"], researcher_agent=op["researcher_agent"], constructor_run_id=None)
    assert_fresh_verifier(conn, operation_id=operation_id, agent=agent, run_id=run_id, independence_attestation=independence_attestation)
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    registry = SectionRegistry.from_sections(backend.list_sections(COOKING_PROJECT_GID))
    if live.section_gid != registry.verification_queue_gid:
        raise DishRuleError("WRONG_STATE", "live task is not currently in Verification Queue", rule="verification_placement_required", details={"actual_section_gid": live.section_gid, "expected_section_gid": registry.verification_queue_gid})
    document = parse_task_document(f"{live.title}\n{live.notes}")
    validation = validate_task_document(document, expected_schema_version=op["schema_version"], schema=schema)
    if not validation.ok:
        raise DishRuleError(
            "VALIDATION_FAILED", "live task is not a legal pending-verification candidate",
            rule="pending_verification_required",
            errors=[finding_payload(f) for f in validation.findings],
        )
    require_status(document.state, {"pending-verification"}, action="verification read")
    recorded = document.state.values["Verification protocol release"]
    if recorded != cycle["protocol_release"]:
        raise DishRuleError("CONFLICT", "task and cycle Verification releases disagree", rule="verification_release_mismatch")
    if cycle["protocol_text"]:
        snapshot = type("Snapshot", (), {"identity": recorded, "text": cycle["protocol_text"], "source": "persisted"})()
    else:
        snapshot = resolve_verification_protocol(honest_root, recorded)

    # The external read is complete. Persist every local review-authority fact as
    # one atomic unit so a crash leaves either no review binding or a complete one.
    conn.execute("SAVEPOINT verification_read_local")
    try:
        if not cycle["protocol_text"]:
            conn.execute(
                "UPDATE verification_cycles SET protocol_text = ? WHERE cycle_id = ?",
                (snapshot.text, cycle["cycle_id"]),
            )
        reviewed_version = bind_cycle_review(
            conn, cycle_id=cycle["cycle_id"], operation_id=operation_id,
            task_gid=op["task_gid"], identity=live.identity,
        )
        conn.execute(
            "UPDATE operations SET verifier_agent = ?, independence_attestation = ? WHERE operation_id = ?",
            (agent, str(independence_attestation or "").strip() or None, operation_id),
        )
        conn.execute(
            "UPDATE verification_cycles SET verifier_agent = ?, run_id = ?, independence_attestation = ? WHERE cycle_id = ?",
            (agent, str(run_id or "").strip() or None, str(independence_attestation or "").strip() or None, cycle["cycle_id"]),
        )
        record_actor_fact(
            conn, operation_id=operation_id, task_gid=op["task_gid"], role="verifier",
            agent=agent, run_id=run_id, independence_attestation=independence_attestation,
            candidate_identity=live.identity, source_cycle_id=cycle["cycle_id"],
        )
        record_audit(
            conn, submission_id=None, task_gid=op["task_gid"], operation_id=operation_id,
            event_type="verification.review_started", actor_agent=agent,
            details={"cycle_id": cycle["cycle_id"], "reviewed_identity": live.identity, "reviewed_content_version_id": reviewed_version["content_version_id"]},
            result_code="OK", result_ok=True,
        )
    except Exception:
        conn.execute("ROLLBACK TO verification_read_local")
        conn.execute("RELEASE verification_read_local")
        raise
    else:
        conn.execute("RELEASE verification_read_local")
    return {
        "operation_id": operation_id,
        "cycle_id": cycle["cycle_id"],
        "reviewed_identity": live.identity,
        "task": dataclasses.asdict(live),
        "verification_protocol": {"identity": snapshot.identity, "text": snapshot.text},
        "verifier": {"agent": agent, "run_id": run_id, "independence_attestation": independence_attestation},
    }




def assert_verifier_authority(
    cycle, *, agent: str, run_id: str | None, independence_attestation: str | None
) -> None:
    """Require the decision caller to match the exact persisted verifier proof."""
    if cycle["verifier_agent"] != agent:
        raise DishRuleError(
            "AGENT_MISMATCH", "command agent is not the recorded verifier",
            rule="verifier_actor_mismatch",
        )
    recorded_run = str(cycle["run_id"] or "").strip()
    supplied_run = str(run_id or "").strip()
    recorded_attestation = str(cycle["independence_attestation"] or "").strip()
    supplied_attestation = str(independence_attestation or "").strip()
    if recorded_run:
        if supplied_run != recorded_run:
            raise DishRuleError(
                "AGENT_MISMATCH", "decision caller does not match the recorded verifier run",
                rule="verifier_proof_mismatch",
            )
        return
    if not recorded_attestation or supplied_attestation != recorded_attestation:
        raise DishRuleError(
            "AGENT_MISMATCH", "decision caller does not match the recorded verifier attestation",
            rule="verifier_proof_mismatch",
        )

def approve_live(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    agent: str,
    reviewed_identity: str,
    semantic_review_complete: bool,
    provenance_complete: bool,
    correction_class: str,
    run_id: str | None = None,
    independence_attestation: str | None = None,
    schema: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    op, cycle = _operation_and_cycle(conn, operation_id)
    assert_verifier_authority(
        cycle, agent=agent, run_id=run_id,
        independence_attestation=independence_attestation,
    )
    if not semantic_review_complete or not provenance_complete:
        raise DishRuleError("VALIDATION_FAILED", "explicit semantic self-review and provenance completion are required", rule="verification_inputs_incomplete")
    if correction_class not in {"none", "small"}:
        raise DishRuleError("INVALID_ARGUMENT", "approval correction must be none or small", rule="invalid_correction")
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    persisted_reviewed = cycle["reviewed_identity"]
    if not persisted_reviewed or not cycle["reviewed_content_version_id"]:
        raise DishRuleError("WRONG_STATE", "Verification cycle has no persisted reviewed content", rule="reviewed_content_missing")
    if reviewed_identity != persisted_reviewed:
        raise DishRuleError("CONFLICT", "caller review identity does not match the persisted review", rule="reviewed_identity_mismatch", details={"persisted_reviewed_identity": persisted_reviewed, "supplied_identity": reviewed_identity})
    if live.identity != persisted_reviewed:
        raise DishRuleError("CONFLICT", "live candidate changed after verifier review", rule="stale_verifier_review", details={"reviewed_identity": persisted_reviewed, "actual_identity": live.identity})
    document = parse_task_document(f"{live.title}\n{live.notes}")
    check = validate_task_document(document, expected_schema_version=op["schema_version"], schema=schema)
    if not check.ok or document.state.values["Status"] != "pending-verification":
        raise DishRuleError("VALIDATION_FAILED", "exact live candidate failed pre-signoff validation", rule="pre_signoff_validation_failed", errors=[finding_payload(f) for f in check.findings])
    assert_transition(action="approve", before=document.state.values["Status"], after="ready")
    signed = dataclasses.replace(document, state=ready(document.state.values, verified_by=verification_actor_line(agent, utc_now()[:10])))
    signed_lines = signed.render().splitlines()
    intended_title = signed_lines[0]
    intended_notes = "\n".join(signed_lines[1:]) + "\n"
    declare_operation_step(
        conn, operation_id, "signoff_write",
        {"title": intended_title, "notes": intended_notes, "cycle_id": cycle["cycle_id"], "correction_class": correction_class},
    )
    declare_operation_step(
        conn, operation_id, "signoff_finalize",
        {"phase": "await_submission", "cycle_id": cycle["cycle_id"]},
    )
    final_check = validate_task_document(signed, expected_schema_version=op["schema_version"], schema=schema)
    if not final_check.ok:
        raise DishRuleError("VALIDATION_FAILED", "ready state failed deterministic validation", rule="ready_state_invalid", errors=[finding_payload(f) for f in final_check.findings])
    lines = signed_lines
    confirmed = write_exact_content(
        conn, backend, operation_id=operation_id, task_gid=live.gid, project_gid=COOKING_PROJECT_GID,
        expected_identity=live.identity, expected_section_gid=live.section_gid,
        title=lines[0], notes="\n".join(lines[1:]) + "\n", schema_version=op["schema_version"],
        purpose="signoff", context={"cycle_id": cycle["cycle_id"], "correction_class": correction_class},
    )
    exact = parse_task_document(f"{confirmed.title}\n{confirmed.notes}")
    if exact.state.values["Status"] != "ready" or exact.state.values["Verified by"] == "None":
        raise DishRuleError("BACKEND_UNCERTAIN", "signoff reread did not confirm ready state", rule="signoff_not_confirmed")
    complete_operation_step(conn, operation_id, "signoff_write")
    signed_version = _content_version_for_identity(
        conn, operation_id=operation_id, task_gid=op["task_gid"], identity=confirmed.identity
    )
    transition_operation(conn, operation_id, phase="await_submission")
    complete_operation_step(conn, operation_id, "signoff_finalize")
    record_audit(
        conn, submission_id=None, task_gid=live.gid, operation_id=operation_id,
        event_type="verification.approved", actor_agent=agent,
        details={"cycle_id": cycle["cycle_id"], "signed_identity": confirmed.identity, "signed_content_version_id": signed_version["content_version_id"]}, result_code="OK", result_ok=True,
        governed_kind="decision",
        before_state={"outcome": None, "reviewed_identity": persisted_reviewed, "status": "pending-verification"},
        after_state={"outcome": "approved", "signed_identity": confirmed.identity, "status": "ready"},
        actor_run_id=run_id, actor_attestation=independence_attestation,
    )
    return {"operation_id": operation_id, "cycle_id": cycle["cycle_id"], "signed_identity": confirmed.identity, "task": dataclasses.asdict(confirmed)}
