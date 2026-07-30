"""Step 7 exact-live Verification start/read and signoff."""
from __future__ import annotations

import dataclasses
import sqlite3
from typing import Any, Mapping

from .constants import COOKING_PROJECT_GID
from .database import (
    mark_operation_completion, record_audit, transition_operation, assert_fresh_verifier,
    record_actor_fact, declare_operation_step, complete_operation_step, content_identity,
    record_dish_inspect_fact, atomic_persistence, complete_abandonment_in_transaction,
)
from .errors import DishRuleError
from .models import (
    SectionRegistry,
    VerifierIdentity,
    utc_now,
    validate_independence_attestation,
    verification_actor_line,
)
from .lifecycle import assert_transition, ready, require_status
from .releases import resolve_verification_protocol
from .task_document import TaskState, parse_task_document, validate_task_document, finding_payload
from .task_store import read_complete_task, write_exact_content
from .step5 import verification_lineage


def _operation_and_cycle(
    conn: sqlite3.Connection,
    operation_id: str,
    *,
    target_cycle_id: str | None = None,
):
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
    if target_cycle_id is not None and cycle["cycle_id"] != target_cycle_id:
        raise DishRuleError(
            "WRONG_STATE",
            "Verification start target no longer names the current cycle",
            rule="verification_start_target_stale",
            details={
                "target_operation_id": operation_id,
                "target_cycle_id": target_cycle_id,
                "current_operation_id": operation_id,
                "current_cycle_id": cycle["cycle_id"],
            },
        )
    return op, cycle


def verification_start_abandonment_authority(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    cycle_id: str,
):
    """Return the abandonment authority that made this exact start actionable.

    Prepared Verification successors remain fenced by an awaiting abandonment.
    Route-preserved continuations are bound by the completed abandonment that
    selected the exact operation/cycle pair. Ordinary Verification starts have
    no abandonment authority row and remain untargeted-compatible.
    """

    prepared = conn.execute(
        """SELECT abandonment.*
             FROM operation_successions AS succession
             JOIN abandonment_attempts AS abandonment
               ON abandonment.abandonment_id=succession.abandonment_id
             JOIN operations AS successor
               ON successor.operation_id=succession.successor_operation_id
            WHERE succession.successor_operation_id=?
              AND succession.successor_cycle_id=?
              AND successor.successor_claim_mode='verifier'
              AND abandonment.status='awaiting_successor_claim'""",
        (operation_id, cycle_id),
    ).fetchone()
    if prepared is not None:
        return prepared
    return conn.execute(
        """SELECT * FROM abandonment_attempts
            WHERE status='completed'
              AND outcome='route_preserved'
              AND continuation_operation_id=?
              AND continuation_cycle_id=?
            ORDER BY completed_at DESC, rowid DESC LIMIT 1""",
        (operation_id, cycle_id),
    ).fetchone()


def resolve_verification_start_target(
    conn: sqlite3.Connection,
    *,
    task_gid: str,
    target_operation_id: str | None,
    target_cycle_id: str | None,
):
    """Resolve one exact Verification start target without external work."""

    clean_operation = str(target_operation_id or "").strip() or None
    clean_cycle = str(target_cycle_id or "").strip() or None
    if bool(clean_operation) != bool(clean_cycle):
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "Verification start requires both target_operation_id and target_cycle_id",
            rule="verification_start_target_pair_required",
        )
    if clean_operation is None:
        rows = conn.execute(
            """SELECT * FROM operations
                 WHERE task_gid=? AND status='open'
                 ORDER BY created_at DESC LIMIT 2""",
            (task_gid,),
        ).fetchall()
        if not rows:
            raise DishRuleError(
                "NOT_FOUND", "task has no open operation", rule="open_operation_missing"
            )
        if len(rows) != 1:
            raise DishRuleError(
                "CONFLICT",
                "task does not have one unique open Verification operation",
                rule="verification_start_operation_ambiguous",
            )
        operation = rows[0]
    else:
        operation = conn.execute(
            "SELECT * FROM operations WHERE operation_id=?",
            (clean_operation,),
        ).fetchone()
        if operation is None or operation["task_gid"] != task_gid:
            raise DishRuleError(
                "WRONG_STATE",
                "Verification start target no longer belongs to this task",
                rule="verification_start_target_stale",
                details={
                    "target_operation_id": clean_operation,
                    "target_cycle_id": clean_cycle,
                },
            )
    op, cycle = _operation_and_cycle(
        conn,
        operation["operation_id"],
        target_cycle_id=clean_cycle,
    )
    authority = verification_start_abandonment_authority(
        conn, operation_id=op["operation_id"], cycle_id=cycle["cycle_id"]
    )
    if authority is not None and clean_operation is None:
        raise DishRuleError(
            "WRONG_STATE",
            "this Verification continuation requires the exact abandonment target",
            rule="verification_start_target_required",
            details={
                "target_operation_id": op["operation_id"],
                "target_cycle_id": cycle["cycle_id"],
            },
        )
    return op, cycle, authority



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
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    operation_id: str,
    task_gid: str,
    identity: str,
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

def record_current_dish_inspect(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    agent: str,
    invocation_run_id: str | None,
    schema: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Record an inspect fact only for the exact current verifier/cycle/live head."""
    op = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if op is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    if op["status"] != "open" or op["phase"] != "await_verification":
        return None
    cycle = conn.execute(
        """SELECT * FROM verification_cycles
             WHERE operation_id=? AND completed_at IS NULL
             ORDER BY cycle_number DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if cycle is None or not (
        cycle["reviewed_content_version_id"]
        and cycle["reviewed_identity"]
        and cycle["verifier_agent"]
        and str(cycle["run_id"] or "").strip()
    ):
        return None
    # Inspection remains readable by any agent, but only the exact verifier
    # principal can create the decision-enabling fact. Local direct-mode calls
    # have no service principal run, so they use the already-persisted run proof.
    if agent != cycle["verifier_agent"]:
        return None
    principal_run = str(invocation_run_id or "").strip()
    if principal_run and principal_run != str(cycle["run_id"]).strip():
        return None
    live = read_complete_task(
        backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID
    )
    registry = SectionRegistry.from_sections(backend.list_sections(COOKING_PROJECT_GID))
    if (
        live.identity != cycle["reviewed_identity"]
        or live.section_gid != registry.verification_queue_gid
    ):
        return None
    try:
        document = parse_task_document(f"{live.title}\n{live.notes}")
    except Exception:
        return None
    validation = validate_task_document(
        document, expected_schema_version=op["schema_version"], schema=schema
    )
    if not validation.ok or document.state.values["Status"] != "pending-verification":
        return None
    fact = record_dish_inspect_fact(
        conn, cycle=cycle, section_gid=registry.verification_queue_gid
    )
    return {key: fact[key] for key in fact.keys()}


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
    target_operation_id: str | None = None,
    target_cycle_id: str | None = None,
) -> dict[str, Any]:
    if target_operation_id is not None and target_operation_id != operation_id:
        raise DishRuleError(
            "WRONG_STATE",
            "Verification start target no longer names this operation",
            rule="verification_start_target_stale",
            details={
                "target_operation_id": target_operation_id,
                "target_cycle_id": target_cycle_id,
                "current_operation_id": operation_id,
            },
        )
    op, cycle = _operation_and_cycle(
        conn, operation_id, target_cycle_id=target_cycle_id
    )
    abandonment = verification_start_abandonment_authority(
        conn, operation_id=operation_id, cycle_id=cycle["cycle_id"]
    )
    if abandonment is not None:
        if target_operation_id is None or target_cycle_id is None:
            raise DishRuleError(
                "WRONG_STATE",
                "this Verification continuation requires the exact abandonment target",
                rule="verification_start_target_required",
                details={
                    "target_operation_id": operation_id,
                    "target_cycle_id": cycle["cycle_id"],
                },
            )
        if str(run_id or "").strip() == str(abandonment["abandoned_run_id"] or "").strip():
            raise DishRuleError(
                "AGENT_MISMATCH",
                "the abandoned run cannot claim its replacement Verification attempt",
                rule="abandoned_run_claim_forbidden",
            )
    handoff = conn.execute("SELECT completed_at FROM operation_steps WHERE operation_id=? AND step_name='verification_handoff'", (operation_id,)).fetchone()
    if handoff is not None and handoff["completed_at"] is None:
        raise DishRuleError("WRONG_STATE", "Verification handoff is incomplete", rule="verification_handoff_incomplete")
    clean_attestation = validate_independence_attestation(independence_attestation)
    identity = VerifierIdentity(agent, run_id, clean_attestation)
    identity.validate(editor_agent=op["editor_agent"], researcher_agent=op["researcher_agent"], constructor_run_id=None)
    assert_fresh_verifier(conn, operation_id=operation_id, agent=agent, run_id=run_id, independence_attestation=clean_attestation)
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
        current_op, current_cycle = _operation_and_cycle(
            conn, operation_id, target_cycle_id=target_cycle_id
        )
        current_abandonment = verification_start_abandonment_authority(
            conn,
            operation_id=operation_id,
            cycle_id=current_cycle["cycle_id"],
        )
        if abandonment is not None:
            expected_claim_mode = (
                "verifier"
                if abandonment["status"] == "awaiting_successor_claim"
                else "none"
            )
            if (
                current_abandonment is None
                or current_abandonment["abandonment_id"] != abandonment["abandonment_id"]
                or current_op["successor_claim_mode"] != expected_claim_mode
            ):
                raise DishRuleError(
                    "WRONG_STATE",
                    "Verification abandonment target changed before claim",
                    rule="verification_start_target_stale",
                    details={
                        "target_operation_id": operation_id,
                        "target_cycle_id": cycle["cycle_id"],
                    },
                )
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
            """UPDATE operations
                  SET verifier_agent = ?, independence_attestation = ?,
                      successor_claim_mode = CASE
                          WHEN successor_claim_mode='verifier' THEN 'none'
                          ELSE successor_claim_mode
                      END
                WHERE operation_id = ?""",
            (agent, clean_attestation, operation_id),
        )
        conn.execute(
            "UPDATE verification_cycles SET verifier_agent = ?, run_id = ?, independence_attestation = ? WHERE cycle_id = ?",
            (agent, str(run_id or "").strip() or None, clean_attestation, cycle["cycle_id"]),
        )
        record_actor_fact(
            conn, operation_id=operation_id, task_gid=op["task_gid"], role="verifier",
            agent=agent, run_id=run_id, independence_attestation=clean_attestation,
            candidate_identity=live.identity, source_cycle_id=cycle["cycle_id"],
        )
        record_audit(
            conn, submission_id=None, task_gid=op["task_gid"], operation_id=operation_id,
            event_type="verification.review_started", actor_agent=agent,
            details={"cycle_id": cycle["cycle_id"], "reviewed_identity": live.identity, "reviewed_content_version_id": reviewed_version["content_version_id"]},
            result_code="OK", result_ok=True, actor_run_id=run_id,
            actor_attestation=clean_attestation,
        )
        if (
            abandonment is not None
            and abandonment["status"] == "awaiting_successor_claim"
        ):
            completion_result = {
                "abandonment_id": abandonment["abandonment_id"],
                "operation_id": operation_id,
                "cycle_id": cycle["cycle_id"],
                "outcome": "restarted",
            }
            complete_abandonment_in_transaction(
                conn,
                abandonment_id=abandonment["abandonment_id"],
                outcome="restarted",
                result=completion_result,
                continuation_operation_id=operation_id,
                continuation_cycle_id=cycle["cycle_id"],
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
        "verifier": {"agent": agent, "run_id": run_id, "independence_attestation": clean_attestation},
        "verification_lineage": verification_lineage(
            conn, operation_id, current_run_id=run_id
        ),
    }





def replay_verification_read(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    agent: str,
    run_id: str,
    target_cycle_id: str | None = None,
) -> dict[str, Any]:
    """Reconstruct a proven completed Verification read after response loss.

    This performs no workflow mutation. It succeeds only when the current open
    cycle, verifier actor fact, exact reviewed content, and live Verification
    placement all still prove that the original `start verification` applied.
    """
    op, cycle = _operation_and_cycle(
        conn, operation_id, target_cycle_id=target_cycle_id
    )
    if (
        str(cycle["verifier_agent"] or "").strip() != str(agent).strip()
        or str(cycle["run_id"] or "").strip() != str(run_id).strip()
        or not cycle["reviewed_identity"]
        or not cycle["reviewed_content_version_id"]
        or not cycle["protocol_text"]
    ):
        raise DishRuleError(
            "BACKEND_UNCERTAIN",
            "Verification start cannot be proven from durable review evidence",
            rule="service_request_pending",
        )
    actor = conn.execute(
        """SELECT 1 FROM operation_actor_facts
             WHERE operation_id=? AND role='verifier' AND agent=? AND run_id=?
               AND candidate_identity=? AND source_cycle_id=?
             LIMIT 1""",
        (operation_id, agent, run_id, cycle["reviewed_identity"], cycle["cycle_id"]),
    ).fetchone()
    if actor is None:
        raise DishRuleError(
            "BACKEND_UNCERTAIN",
            "Verification start lacks durable verifier lineage",
            rule="service_request_pending",
        )
    version = conn.execute(
        """SELECT 1 FROM content_versions
             WHERE content_version_id=? AND operation_id=? AND task_gid=?
               AND identity=? AND confirmed=1""",
        (
            cycle["reviewed_content_version_id"], operation_id, op["task_gid"],
            cycle["reviewed_identity"],
        ),
    ).fetchone()
    if version is None:
        raise DishRuleError(
            "BACKEND_UNCERTAIN",
            "Verification start lacks its confirmed reviewed content version",
            rule="service_request_pending",
        )
    live = read_complete_task(
        backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID
    )
    registry = SectionRegistry.from_sections(backend.list_sections(COOKING_PROJECT_GID))
    if (
        live.identity != cycle["reviewed_identity"]
        or live.section_gid != registry.verification_queue_gid
    ):
        raise DishRuleError(
            "BACKEND_UNCERTAIN",
            "live Verification state no longer matches the recorded review",
            rule="service_request_pending",
        )
    return {
        "operation_id": operation_id,
        "cycle_id": cycle["cycle_id"],
        "reviewed_identity": live.identity,
        "task": dataclasses.asdict(live),
        "verification_protocol": {
            "identity": cycle["protocol_release"],
            "text": cycle["protocol_text"],
        },
        "verifier": {
            "agent": cycle["verifier_agent"],
            "run_id": cycle["run_id"],
            "independence_attestation": cycle["independence_attestation"],
        },
        "verification_lineage": verification_lineage(
            conn, operation_id, current_run_id=run_id
        ),
    }

_INHERIT_ATTESTATION = object()


def assert_verifier_authority(
    cycle,
    *,
    agent: str,
    run_id: str | None,
    independence_attestation: str | None | object = _INHERIT_ATTESTATION,
) -> str:
    """Require the decision caller to match the persisted verifier agent and run.

    The attestation is authoritative only at Verification start. Decision calls
    inherit the exact persisted value rather than asking the caller to repeat it.
    """
    if cycle["verifier_agent"] != agent:
        raise DishRuleError(
            "AGENT_MISMATCH", "command agent is not the recorded verifier",
            rule="verifier_actor_mismatch",
        )
    recorded_run = str(cycle["run_id"] or "").strip()
    supplied_run = str(run_id or "").strip()
    if not recorded_run or supplied_run != recorded_run:
        raise DishRuleError(
            "AGENT_MISMATCH",
            "decision caller does not match the recorded verifier run",
            rule="verifier_proof_mismatch",
            details={"run_id_matches": supplied_run == recorded_run},
        )
    recorded_attestation = validate_independence_attestation(
        cycle["independence_attestation"]
    )
    if independence_attestation is not _INHERIT_ATTESTATION:
        supplied_attestation = validate_independence_attestation(
            independence_attestation
        )
        if supplied_attestation != recorded_attestation:
            raise DishRuleError(
                "AGENT_MISMATCH",
                "decision caller does not match the exact recorded verifier proof",
                rule="verifier_proof_mismatch",
                details={
                    "run_id_matches": True,
                    "independence_attestation_matches": False,
                },
            )
    return recorded_attestation

def _resume_approved_cycle(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    agent: str,
    reviewed_identity: str,
    semantic_review_complete: bool,
    provenance_complete: bool,
    correction_class: str,
    approval_candidate_identity: str | None,
    run_id: str | None,
) -> dict[str, Any] | None:
    """Finish or replay an approval whose signoff is already durable."""
    op = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if op is None or op["status"] != "open":
        return None
    cycle = conn.execute(
        """SELECT * FROM verification_cycles
             WHERE operation_id=? AND outcome='approved' AND completed_at IS NOT NULL
             ORDER BY completed_at DESC, rowid DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if cycle is None:
        return None
    attempt = conn.execute(
        """SELECT * FROM write_attempts
             WHERE operation_id=? AND purpose='signoff' AND outcome='confirmed'
               AND json_extract(context_json, '$.cycle_id')=?
             ORDER BY started_at DESC, rowid DESC LIMIT 1""",
        (operation_id, cycle["cycle_id"]),
    ).fetchone()
    if attempt is None:
        return None
    inherited_attestation = assert_verifier_authority(
        cycle, agent=agent, run_id=run_id,
    )
    if not semantic_review_complete or not provenance_complete:
        raise DishRuleError(
            "VALIDATION_FAILED",
            "explicit semantic self-review and provenance completion are required",
            rule="verification_inputs_incomplete",
        )
    if reviewed_identity != cycle["reviewed_identity"]:
        raise DishRuleError(
            "CONFLICT",
            "caller review identity does not match the persisted review",
            rule="reviewed_identity_mismatch",
            retryable=True,
        )
    if correction_class != cycle["correction_class"]:
        raise DishRuleError(
            "CONFLICT", "approval correction differs from durable signoff",
            rule="approval_replay_mismatch",
        )
    approved_candidate_identity = attempt["expected_identity"]
    if correction_class == "small":
        if approval_candidate_identity != approved_candidate_identity:
            raise DishRuleError(
                "CONFLICT", "corrected approval candidate differs from durable signoff",
                rule="approval_replay_mismatch",
            )
    elif approval_candidate_identity is not None:
        raise DishRuleError(
            "CONFLICT", "no-correction approval cannot substitute another candidate",
            rule="approval_replay_mismatch",
        )
    live = read_complete_task(
        backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID
    )
    if live.identity != cycle["signed_identity"]:
        raise DishRuleError(
            "CONFLICT", "live signed candidate differs from durable approval",
            rule="post_signoff_content_drift",
        )
    approved_version = _content_version_for_identity(
        conn, operation_id=operation_id, task_gid=op["task_gid"],
        identity=approved_candidate_identity,
    )
    signed_version = _content_version_for_identity(
        conn, operation_id=operation_id, task_gid=op["task_gid"],
        identity=cycle["signed_identity"],
    )
    prior = conn.execute(
        """SELECT 1 FROM audit_events
             WHERE operation_id=? AND event_type='verification.approved'
               AND json_extract(details, '$.cycle_id')=? LIMIT 1""",
        (operation_id, cycle["cycle_id"]),
    ).fetchone()
    with atomic_persistence(conn, "approval_replay_finalize"):
        complete_operation_step(conn, operation_id, "signoff_write")
        if op["phase"] != "await_submission":
            transition_operation(conn, operation_id, phase="await_submission")
        complete_operation_step(conn, operation_id, "signoff_finalize")
        if prior is None:
            record_audit(
                conn, submission_id=None, task_gid=op["task_gid"],
                operation_id=operation_id, event_type="verification.approved",
                actor_agent=agent,
                details={
                    "cycle_id": cycle["cycle_id"],
                    "reviewed_identity": cycle["reviewed_identity"],
                    "reviewed_content_version_id": cycle["reviewed_content_version_id"],
                    "approved_candidate_identity": approved_candidate_identity,
                    "approved_candidate_content_version_id": approved_version["content_version_id"],
                    "signed_identity": cycle["signed_identity"],
                    "signed_content_version_id": signed_version["content_version_id"],
                    "correction_class": correction_class,
                    "recovered": True,
                },
                result_code="OK", result_ok=True, governed_kind="decision",
                before_state={"outcome": None, "reviewed_identity": cycle["reviewed_identity"], "status": "pending-verification"},
                after_state={"outcome": "approved", "signed_identity": cycle["signed_identity"], "status": "ready"},
                actor_run_id=run_id, actor_attestation=inherited_attestation,
                actor_source="exact-replay",
            )
    return {
        "operation_id": operation_id,
        "cycle_id": cycle["cycle_id"],
        "reviewed_identity": cycle["reviewed_identity"],
        "approved_candidate_identity": approved_candidate_identity,
        "signed_identity": cycle["signed_identity"],
        "task": dataclasses.asdict(live),
        "approval_recovered": prior is None,
    }


def approve_live(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    agent: str,
    model: str | None = None,
    reviewed_identity: str,
    semantic_review_complete: bool,
    provenance_complete: bool,
    correction_class: str,
    approval_candidate_identity: str | None = None,
    run_id: str | None = None,
    schema: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    resumed = _resume_approved_cycle(
        conn, backend, operation_id=operation_id, agent=agent,
        reviewed_identity=reviewed_identity,
        semantic_review_complete=semantic_review_complete,
        provenance_complete=provenance_complete,
        correction_class=correction_class,
        approval_candidate_identity=approval_candidate_identity,
        run_id=run_id,
    )
    if resumed is not None:
        return resumed
    op, cycle = _operation_and_cycle(conn, operation_id)
    inherited_attestation = assert_verifier_authority(
        cycle, agent=agent, run_id=run_id,
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
        raise DishRuleError("CONFLICT", "caller review identity does not match the persisted review", rule="reviewed_identity_mismatch", retryable=True, details={"persisted_reviewed_identity": persisted_reviewed, "supplied_identity": reviewed_identity})
    if correction_class == "small":
        if not approval_candidate_identity:
            raise DishRuleError(
                "INTERNAL_ERROR",
                "Small-correction approval lacks its corrected candidate identity",
                rule="small_correction_candidate_missing",
            )
        expected_live_identity = approval_candidate_identity
    else:
        if approval_candidate_identity is not None:
            raise DishRuleError(
                "INTERNAL_ERROR",
                "No-correction approval cannot substitute another candidate identity",
                rule="approval_candidate_unexpected",
            )
        expected_live_identity = persisted_reviewed
    if live.identity != expected_live_identity:
        raise DishRuleError(
            "CONFLICT",
            (
                "live corrected candidate changed before approval"
                if correction_class == "small"
                else "live candidate changed after verifier review"
            ),
            rule=(
                "approval_candidate_drift"
                if correction_class == "small"
                else "stale_verifier_review"
            ),
            details={
                "reviewed_identity": persisted_reviewed,
                "expected_approval_candidate_identity": expected_live_identity,
                "actual_identity": live.identity,
            },
        )
    document = parse_task_document(f"{live.title}\n{live.notes}")
    check = validate_task_document(document, expected_schema_version=op["schema_version"], schema=schema)
    if not check.ok or document.state.values["Status"] != "pending-verification":
        raise DishRuleError("VALIDATION_FAILED", "exact live candidate failed pre-signoff validation", rule="pre_signoff_validation_failed", errors=[finding_payload(f) for f in check.findings])
    assert_transition(action="approve", before=document.state.values["Status"], after="ready")
    date = utc_now()[:10]
    signed = dataclasses.replace(
        document,
        state=ready(
            document.state.values,
            verified_by=verification_actor_line(agent, model, date),
        ),
    )
    if signed.material_changes:
        verified_state = (
            f"verified — {verification_actor_line(agent, model, date).replace(' — ', ', ', 1)}"
        )
        finalized_changes = tuple(
            (
                line.removesuffix("pending-verification") + verified_state
                if line.endswith(" — pending-verification")
                else line
            )
            for line in signed.material_changes
        )
        if finalized_changes != signed.material_changes:
            signed = dataclasses.replace(
                signed,
                material_changes=finalized_changes,
            )
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
    approved_candidate_version = _content_version_for_identity(
        conn, operation_id=operation_id, task_gid=op["task_gid"], identity=live.identity
    )
    signed_version = _content_version_for_identity(
        conn, operation_id=operation_id, task_gid=op["task_gid"], identity=confirmed.identity
    )
    with atomic_persistence(conn, "verification_approved_finalize"):
        complete_operation_step(conn, operation_id, "signoff_write")
        transition_operation(conn, operation_id, phase="await_submission")
        complete_operation_step(conn, operation_id, "signoff_finalize")
        record_audit(
            conn, submission_id=None, task_gid=live.gid, operation_id=operation_id,
            event_type="verification.approved", actor_agent=agent,
            details={
                "cycle_id": cycle["cycle_id"],
                "reviewed_identity": persisted_reviewed,
                "reviewed_content_version_id": cycle["reviewed_content_version_id"],
                "approved_candidate_identity": live.identity,
                "approved_candidate_content_version_id": approved_candidate_version["content_version_id"],
                "signed_identity": confirmed.identity,
                "signed_content_version_id": signed_version["content_version_id"],
                "correction_class": correction_class,
            }, result_code="OK", result_ok=True,
            governed_kind="decision",
            before_state={"outcome": None, "reviewed_identity": persisted_reviewed, "status": "pending-verification"},
            after_state={"outcome": "approved", "signed_identity": confirmed.identity, "status": "ready"},
            actor_run_id=run_id, actor_attestation=inherited_attestation,
        )
    return {
        "operation_id": operation_id,
        "cycle_id": cycle["cycle_id"],
        "reviewed_identity": persisted_reviewed,
        "approved_candidate_identity": live.identity,
        "signed_identity": confirmed.identity,
        "task": dataclasses.asdict(confirmed),
    }
