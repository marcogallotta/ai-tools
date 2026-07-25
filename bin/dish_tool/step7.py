"""Step 7 exact-live Verification start/read and signoff."""
from __future__ import annotations

import dataclasses
import sqlite3
from typing import Any

from .constants import COOKING_PROJECT_GID
from .database import mark_operation_completion, record_audit
from .errors import DishRuleError
from .models import VerifierIdentity, verification_actor_line, utc_now
from .releases import resolve_verification_protocol
from .task_document import TaskState, parse_task_document, validate_task_document
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


def verification_read(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    agent: str,
    honest_root,
    run_id: str | None,
    independence_attestation: str | None,
) -> dict[str, Any]:
    op, cycle = _operation_and_cycle(conn, operation_id)
    identity = VerifierIdentity(agent, run_id, independence_attestation)
    identity.validate(editor_agent=op["editor_agent"], researcher_agent=op["researcher_agent"])
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    document = parse_task_document(f"{live.title}\n{live.notes}")
    validation = validate_task_document(document, expected_schema_version=op["schema_version"])
    if not validation.ok or document.state.values["Status"] != "pending-verification":
        raise DishRuleError(
            "VALIDATION_FAILED", "live task is not a legal pending-verification candidate",
            rule="pending_verification_required",
            errors=[{"rule": f.rule, "kind": f.kind.value} for f in validation.findings],
        )
    recorded = document.state.values["Verification protocol release"]
    if recorded != cycle["protocol_release"]:
        raise DishRuleError("CONFLICT", "task and cycle Verification releases disagree", rule="verification_release_mismatch")
    snapshot = resolve_verification_protocol(honest_root, recorded)
    conn.execute(
        "UPDATE operations SET verifier_agent = ?, run_id = ?, independence_attestation = ? WHERE operation_id = ?",
        (agent, str(run_id or "").strip() or None, str(independence_attestation or "").strip() or None, operation_id),
    )
    conn.execute(
        "UPDATE verification_cycles SET verifier_agent = ?, run_id = ?, independence_attestation = ? WHERE cycle_id = ?",
        (agent, str(run_id or "").strip() or None, str(independence_attestation or "").strip() or None, cycle["cycle_id"]),
    )
    record_audit(
        conn, submission_id=None, task_gid=op["task_gid"], operation_id=operation_id,
        event_type="verification.review_started", actor_agent=agent,
        details={"cycle_id": cycle["cycle_id"], "reviewed_identity": live.identity}, result_code="OK", result_ok=True,
    )
    return {
        "operation_id": operation_id,
        "cycle_id": cycle["cycle_id"],
        "reviewed_identity": live.identity,
        "task": dataclasses.asdict(live),
        "verification_protocol": {"identity": snapshot.identity, "text": snapshot.text},
        "verifier": {"agent": agent, "run_id": run_id, "independence_attestation": independence_attestation},
    }


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
) -> dict[str, Any]:
    op, cycle = _operation_and_cycle(conn, operation_id)
    if op["verifier_agent"] != agent:
        raise DishRuleError("AGENT_MISMATCH", "approve agent is not the recorded verifier", rule="verifier_actor_mismatch")
    if not semantic_review_complete or not provenance_complete:
        raise DishRuleError("VALIDATION_FAILED", "explicit semantic self-review and provenance completion are required", rule="verification_inputs_incomplete")
    if correction_class not in {"none", "small"}:
        raise DishRuleError("INVALID_ARGUMENT", "approval correction must be none or small", rule="invalid_correction")
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    if live.identity != reviewed_identity:
        raise DishRuleError("CONFLICT", "live candidate changed after verifier review", rule="stale_verifier_review", details={"reviewed_identity": reviewed_identity, "actual_identity": live.identity})
    document = parse_task_document(f"{live.title}\n{live.notes}")
    check = validate_task_document(document, expected_schema_version=op["schema_version"])
    if not check.ok or document.state.values["Status"] != "pending-verification":
        raise DishRuleError("VALIDATION_FAILED", "exact live candidate failed pre-signoff validation", rule="pre_signoff_validation_failed", errors=[{"rule": f.rule, "kind": f.kind.value} for f in check.findings])
    state = dict(document.state.values)
    state.update({
        "Status": "ready", "Status detail": "None", "Resume status": "None",
        "Verified by": verification_actor_line(agent, utc_now()[:10]),
    })
    signed = dataclasses.replace(document, state=TaskState(state))
    final_check = validate_task_document(signed, expected_schema_version=op["schema_version"])
    if not final_check.ok:
        raise DishRuleError("VALIDATION_FAILED", "ready state failed deterministic validation", rule="ready_state_invalid", errors=[{"rule": f.rule, "kind": f.kind.value} for f in final_check.findings])
    lines = signed.render().splitlines()
    confirmed = write_exact_content(
        conn, backend, operation_id=operation_id, task_gid=live.gid, project_gid=COOKING_PROJECT_GID,
        expected_identity=live.identity, expected_section_gid=live.section_gid,
        title=lines[0], notes="\n".join(lines[1:]) + "\n", schema_version=op["schema_version"],
    )
    exact = parse_task_document(f"{confirmed.title}\n{confirmed.notes}")
    if exact.state.values["Status"] != "ready" or exact.state.values["Verified by"] == "None":
        raise DishRuleError("BACKEND_UNCERTAIN", "signoff reread did not confirm ready state", rule="signoff_not_confirmed")
    conn.execute(
        "UPDATE verification_cycles SET correction_class = ?, outcome = 'approved', completed_at = ? WHERE cycle_id = ?",
        (correction_class, utc_now(), cycle["cycle_id"]),
    )
    mark_operation_completion(conn, operation_id, "signoff")
    record_audit(
        conn, submission_id=None, task_gid=live.gid, operation_id=operation_id,
        event_type="verification.approved", actor_agent=agent,
        details={"cycle_id": cycle["cycle_id"], "signed_identity": confirmed.identity}, result_code="OK", result_ok=True,
    )
    return {"operation_id": operation_id, "cycle_id": cycle["cycle_id"], "signed_identity": confirmed.identity, "task": dataclasses.asdict(confirmed)}
