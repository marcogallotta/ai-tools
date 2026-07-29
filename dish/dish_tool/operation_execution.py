"""Durable, request-scoped execution evidence for operation mutations."""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .errors import DishRuleError
from .models import ProcessIdentity, utc_now
from .recovery import current_process_identity, process_identity_is_live


@dataclass(frozen=True)
class OperationExecutionClaim:
    operation_id: str
    claim_id: str
    execution_id: str
    command: str
    request_id: str | None
    resuming_uncertain: bool = False


_OPERATION_FIELDS = (
    "status",
    "phase",
    "editor_agent",
    "researcher_agent",
    "verifier_agent",
    "run_id",
    "independence_attestation",
    "expected_identity",
    "expected_section_gid",
    "content_write_completed_at",
    "signoff_completed_at",
    "movement_completed_at",
    "destination_movement_attempt_id",
    "terminal_outcome",
    "inherited_signoff_cycle_id",
    "migration_reconciliation_required",
    "migration_reconciliation_reason",
    "completed_at",
)
_WRITE_FIELDS = ("outcome", "finished_at", "confirmed_content_version_id")
_MOVEMENT_FIELDS = ("outcome", "finished_at", "confirmed_section_gid")
_CYCLE_FIELDS = (
    "verifier_agent",
    "run_id",
    "independence_attestation",
    "correction_class",
    "outcome",
    "route",
    "resume_state",
    "reviewed_content_version_id",
    "reviewed_identity",
    "signed_content_version_id",
    "signed_identity",
    "hold_content_version_id",
    "hold_identity",
    "hold_section_gid",
    "completed_at",
)


def _identity(row: Mapping[str, Any]) -> ProcessIdentity:
    return ProcessIdentity(
        hostname=row["hostname"],
        pid=int(row["pid"]),
        process_start=row["process_start"],
    )


def _max_rowid(conn: sqlite3.Connection, table: str, operation_id: str) -> int:
    row = conn.execute(
        f"SELECT COALESCE(MAX(rowid), 0) FROM {table} WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    return int(row[0])


def _snapshot_by_key(
    conn: sqlite3.Connection,
    table: str,
    operation_id: str,
    key: str,
    fields: Sequence[str],
) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE operation_id=? ORDER BY rowid",
        (operation_id,),
    ).fetchall()
    return {
        str(row[key]): {field: row[field] for field in fields}
        for row in rows
    }


def _execution_baseline(conn: sqlite3.Connection, operation_id: str) -> dict[str, Any]:
    operation = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if operation is None:
        raise DishRuleError(
            "NOT_FOUND", "operation not found", rule="operation_not_found"
        )
    task_head = conn.execute(
        """SELECT last_confirmed_identity,last_confirmed_content_version_id
             FROM task_content_state WHERE task_gid=?""",
        (operation["task_gid"],),
    ).fetchone()
    return {
        "writes": _snapshot_by_key(
            conn, "write_attempts", operation_id, "attempt_id", _WRITE_FIELDS
        ),
        "movements": _snapshot_by_key(
            conn,
            "movement_attempts",
            operation_id,
            "attempt_id",
            _MOVEMENT_FIELDS,
        ),
        "cycles": _snapshot_by_key(
            conn, "verification_cycles", operation_id, "cycle_id", _CYCLE_FIELDS
        ),
        "steps": _snapshot_by_key(
            conn, "operation_steps", operation_id, "step_name", ("completed_at",)
        ),
        "content_rowid": _max_rowid(conn, "content_versions", operation_id),
        "actor_rowid": _max_rowid(conn, "operation_actor_facts", operation_id),
        "audit_rowid": _max_rowid(conn, "audit_events", operation_id),
        "task_gid": operation["task_gid"],
        "task_identity": (
            operation["expected_identity"]
            if task_head is None
            else task_head["last_confirmed_identity"]
        ),
        "task_content_version_id": (
            None
            if task_head is None
            else task_head["last_confirmed_content_version_id"]
        ),
        "operation": {name: operation[name] for name in _OPERATION_FIELDS},
    }


def _recovery_pending(conn: sqlite3.Connection, operation_id: str) -> bool:
    return conn.execute(
        """SELECT 1 FROM write_attempts
             WHERE operation_id=? AND outcome IN ('started','uncertain')
           UNION ALL
           SELECT 1 FROM movement_attempts
             WHERE operation_id=? AND outcome IN ('started','uncertain')
           UNION ALL
           SELECT 1 FROM operation_steps
             WHERE operation_id=? AND completed_at IS NULL
           LIMIT 1""",
        (operation_id, operation_id, operation_id),
    ).fetchone() is not None


def _complete_abandoned_execution(
    conn: sqlite3.Connection, execution_id: str | None
) -> None:
    if not execution_id:
        return
    evidence = execution_recovery_state(conn, execution_id=execution_id)
    if evidence is None:
        raise DishRuleError(
            "CONFLICT",
            "abandoned execution evidence is missing",
            rule="operation_execution_binding_invalid",
            details={"execution_id": execution_id},
        )
    conn.execute(
        """UPDATE operation_executions
              SET status='completed', evidence_json=?, completed_at=?
            WHERE execution_id=? AND status='started'""",
        (
            json.dumps(evidence, sort_keys=True, separators=(",", ":")),
            utc_now(),
            execution_id,
        ),
    )


def execution_claim_is_live(
    conn: sqlite3.Connection, *, execution_id: str
) -> bool:
    """Return whether the exact execution still has a live process claim."""
    row = conn.execute(
        "SELECT * FROM operation_execution_claims WHERE execution_id=?",
        (execution_id,),
    ).fetchone()
    return bool(row is not None and process_identity_is_live(_identity(row)))


def claim_operation_execution(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    command: str,
    request_id: str | None = None,
) -> OperationExecutionClaim:
    """Atomically reserve an operation and persist this execution's baseline."""
    identity = current_process_identity()
    claim_id = str(uuid.uuid4())
    execution_id = str(uuid.uuid4())
    clean_command = str(command).strip()
    clean_request = str(request_id or "").strip() or None
    conn.execute("BEGIN IMMEDIATE")
    try:
        operation = conn.execute(
            "SELECT status FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if operation is None:
            raise DishRuleError(
                "NOT_FOUND", "operation not found", rule="operation_not_found"
            )
        existing = conn.execute(
            "SELECT * FROM operation_execution_claims WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if existing is not None:
            if process_identity_is_live(_identity(existing)):
                raise DishRuleError(
                    "CONFLICT",
                    "another mutation is already executing for this operation",
                    rule="operation_mutation_in_progress",
                    retryable=True,
                    details={
                        "operation_id": operation_id,
                        "command": existing["command"],
                        "acquired_at": existing["acquired_at"],
                    },
                )
            prior_recovery = (
                None
                if not existing["execution_id"]
                else execution_recovery_state(
                    conn,
                    execution_id=existing["execution_id"],
                    failure_rule="process_terminated",
                )
            )
            prior_execution = (
                None
                if not existing["execution_id"]
                else conn.execute(
                    "SELECT * FROM operation_executions WHERE execution_id=?",
                    (existing["execution_id"],),
                ).fetchone()
            )
            exact_resume = bool(
                prior_execution is not None
                and prior_execution["command"] == clean_command
                and (
                    (clean_request is not None and prior_execution["request_id"] == clean_request)
                    or (clean_request is None and prior_execution["request_id"] is None)
                )
            )
            recovery_required = (
                _recovery_pending(conn, operation_id)
                if prior_recovery is None
                else bool(prior_recovery["recovery_required"])
            )
            if recovery_required:
                if clean_command != "recover":
                    details = dict(prior_recovery or {})
                    details.update({
                        "operation_id": operation_id,
                        "command": existing["command"],
                        "execution_id": existing["execution_id"],
                        "required_admin_action": "recover",
                    })
                    raise DishRuleError(
                        "CONFLICT",
                        "a crashed operation mutation requires recovery before another mutation",
                        rule="operation_mutation_recovery_required",
                        retryable=False,
                        details=details,
                    )
                if existing["execution_id"] and prior_recovery is not None:
                    conn.execute(
                        """UPDATE operation_executions
                              SET status='uncertain', evidence_json=?, completed_at=?
                            WHERE execution_id=? AND status='started'""",
                        (
                            json.dumps(
                                prior_recovery,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            utc_now(),
                            existing["execution_id"],
                        ),
                    )
            else:
                if exact_resume:
                    conn.execute(
                        "DELETE FROM operation_execution_claims "
                        "WHERE operation_id=? AND claim_id=?",
                        (operation_id, existing["claim_id"]),
                    )
                    conn.execute(
                        """INSERT INTO operation_execution_claims(
                               operation_id,claim_id,command,hostname,pid,process_start,
                               acquired_at,execution_id
                           ) VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            operation_id,
                            claim_id,
                            clean_command,
                            identity.hostname,
                            identity.pid,
                            identity.process_start,
                            utc_now(),
                            prior_execution["execution_id"],
                        ),
                    )
                    conn.execute("COMMIT")
                    return OperationExecutionClaim(
                        operation_id=operation_id,
                        claim_id=claim_id,
                        execution_id=prior_execution["execution_id"],
                        command=clean_command,
                        request_id=clean_request,
                    )
                _complete_abandoned_execution(conn, existing["execution_id"])
            conn.execute(
                "DELETE FROM operation_execution_claims WHERE operation_id=? AND claim_id=?",
                (operation_id, existing["claim_id"]),
            )

        unresolved = conn.execute(
            """SELECT * FROM operation_executions
                 WHERE operation_id=? AND status='uncertain'
                   AND resolved_at IS NULL
                 ORDER BY created_at, rowid""",
            (operation_id,),
        ).fetchall()
        if unresolved:
            if len(unresolved) != 1:
                raise DishRuleError(
                    "CONFLICT",
                    "multiple unresolved operation executions require operator review",
                    rule="operation_execution_recovery_ambiguous",
                    retryable=False,
                    details={
                        "operation_id": operation_id,
                        "execution_ids": [row["execution_id"] for row in unresolved],
                        "required_admin_action": "recover",
                    },
                )
            prior = unresolved[0]
            exact_resume = bool(
                prior["command"] == clean_command
                and (
                    (clean_request is not None and prior["request_id"] == clean_request)
                    or (clean_request is None and prior["request_id"] is None)
                )
            )
            if not exact_resume and clean_command != "recover":
                recovery = json.loads(prior["evidence_json"] or "{}")
                details = dict(recovery) if isinstance(recovery, dict) else {}
                details.update({
                    "operation_id": operation_id,
                    "command": prior["command"],
                    "execution_id": prior["execution_id"],
                    "request_id": prior["request_id"],
                    "required_admin_action": "recover",
                })
                raise DishRuleError(
                    "CONFLICT",
                    "an unresolved uncertain mutation must be reconciled before another mutation",
                    rule="operation_mutation_recovery_required",
                    retryable=False,
                    details=details,
                )
            conn.execute(
                """INSERT INTO operation_execution_claims(
                       operation_id,claim_id,command,hostname,pid,process_start,
                       acquired_at,execution_id
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    operation_id, claim_id, clean_command, identity.hostname,
                    identity.pid, identity.process_start, utc_now(),
                    prior["execution_id"],
                ),
            )
            conn.execute("COMMIT")
            return OperationExecutionClaim(
                operation_id=operation_id, claim_id=claim_id,
                execution_id=prior["execution_id"], command=clean_command,
                request_id=clean_request, resuming_uncertain=True,
            )

        baseline = _execution_baseline(conn, operation_id)
        conn.execute(
            """INSERT INTO operation_executions(
                   execution_id,operation_id,request_id,command,baseline_json,
                   status,created_at
               ) VALUES(?,?,?,?,?,'started',?)""",
            (
                execution_id,
                operation_id,
                clean_request,
                clean_command,
                json.dumps(baseline, sort_keys=True, separators=(",", ":")),
                utc_now(),
            ),
        )
        conn.execute(
            """INSERT INTO operation_execution_claims(
                   operation_id,claim_id,command,hostname,pid,process_start,
                   acquired_at,execution_id
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                operation_id,
                claim_id,
                clean_command,
                identity.hostname,
                identity.pid,
                identity.process_start,
                utc_now(),
                execution_id,
            ),
        )
        conn.execute("COMMIT")
        return OperationExecutionClaim(
            operation_id=operation_id,
            claim_id=claim_id,
            execution_id=execution_id,
            command=clean_command,
            request_id=clean_request,
        )
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _rows_after(
    conn: sqlite3.Connection,
    table: str,
    operation_id: str,
    baseline_rowid: int,
) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT rowid AS evidence_rowid, * FROM {table} "
        "WHERE operation_id=? AND rowid>? ORDER BY rowid",
        (operation_id, baseline_rowid),
    ).fetchall()


def _changed_rows(
    conn: sqlite3.Connection,
    *,
    table: str,
    operation_id: str,
    key: str,
    fields: Sequence[str],
    baseline: Mapping[str, Mapping[str, Any]],
) -> list[sqlite3.Row]:
    rows = conn.execute(
        f"SELECT rowid AS evidence_rowid, * FROM {table} "
        "WHERE operation_id=? ORDER BY rowid",
        (operation_id,),
    ).fetchall()
    changed: list[sqlite3.Row] = []
    for row in rows:
        prior = baseline.get(str(row[key]))
        if prior is None or any(row[field] != prior.get(field) for field in fields):
            changed.append(row)
    return changed


def _attempt_state(rows: Sequence[sqlite3.Row]) -> tuple[bool, str]:
    outcomes = [row["outcome"] for row in rows]
    committed = "confirmed" in outcomes
    if any(outcome in {"started", "uncertain"} for outcome in outcomes):
        return committed, "uncertain"
    if committed:
        return True, "confirmed"
    if outcomes and all(outcome == "not_applied" for outcome in outcomes):
        return False, "not_applied"
    return False, "not_started"


def _operation_changed(row: sqlite3.Row, baseline: Mapping[str, Any]) -> bool:
    return any(row[name] != baseline.get(name) for name in _OPERATION_FIELDS)


def _current_steps(conn: sqlite3.Connection, operation_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT rowid AS evidence_rowid,* FROM operation_steps "
        "WHERE operation_id=? ORDER BY rowid",
        (operation_id,),
    ).fetchall()


def _execution_step_scope(
    *,
    command: str,
    current: Sequence[sqlite3.Row],
    baseline: Mapping[str, Mapping[str, Any]],
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    changed: list[sqlite3.Row] = []
    for row in current:
        prior = baseline.get(str(row["step_name"]))
        if prior is None or row["completed_at"] != prior.get("completed_at"):
            changed.append(row)
    if command == "recover":
        scope = [
            row
            for row in current
            if row["step_name"] not in baseline
            or baseline[row["step_name"]].get("completed_at") is None
        ]
    else:
        scope = [row for row in current if row["step_name"] not in baseline]
    return changed, scope


def _confirmed_identity_from_execution(
    conn: sqlite3.Connection,
    *,
    versions: Sequence[sqlite3.Row],
    writes: Sequence[sqlite3.Row],
    baseline: Mapping[str, Any],
) -> tuple[str | None, str, str | None]:
    if any(attempt["outcome"] in {"started", "uncertain"} for attempt in writes):
        return None, "unresolved_external_write", None
    latest_version = next(
        (version for version in reversed(versions) if int(version["confirmed"]) == 1),
        None,
    )
    if latest_version is not None:
        return (
            latest_version["identity"],
            "execution_confirmed_content_version",
            latest_version["content_version_id"],
        )
    confirmed_write = next(
        (
            attempt
            for attempt in reversed(writes)
            if attempt["outcome"] == "confirmed"
            and attempt["confirmed_content_version_id"]
        ),
        None,
    )
    if confirmed_write is not None:
        version = conn.execute(
            "SELECT * FROM content_versions WHERE content_version_id=?",
            (confirmed_write["confirmed_content_version_id"],),
        ).fetchone()
        if version is not None and int(version["confirmed"]) == 1:
            return (
                version["identity"],
                "execution_confirmed_write_binding",
                version["content_version_id"],
            )
    return (
        baseline["task_identity"],
        "execution_baseline",
        baseline.get("task_content_version_id"),
    )


def _failure_step(
    *,
    pending_steps: Sequence[str],
    writes: Sequence[sqlite3.Row],
    movements: Sequence[sqlite3.Row],
    failure_rule: str | None,
) -> str:
    if pending_steps:
        # Report the authoritative suffix that failed, not an earlier effect
        # intent that remains pending only because the atomic finalizer rolled
        # back.  This preserves stable recovery diagnostics while allowing
        # intent steps to fence and reconstruct the decision.
        if "signoff_finalize" in pending_steps:
            return "signoff_finalize"
        route_new_cycle = next(
            (step for step in pending_steps if step.startswith("route_new_cycle:")),
            None,
        )
        if route_new_cycle is not None:
            return route_new_cycle
        return pending_steps[0]
    unresolved_write = next(
        (row for row in writes if row["outcome"] in {"started", "uncertain"}),
        None,
    )
    if unresolved_write is not None:
        return f"write:{unresolved_write['purpose']}"
    unresolved_move = next(
        (row for row in movements if row["outcome"] in {"started", "uncertain"}),
        None,
    )
    if unresolved_move is not None:
        return f"move:{unresolved_move['purpose']}"
    if failure_rule == "handoff_validation_failed":
        return "handoff_validation"
    if failure_rule:
        return failure_rule
    return "response_persistence"


def execution_recovery_state(
    conn: sqlite3.Connection,
    *,
    execution_id: str | None = None,
    request_id: str | None = None,
    failure_rule: str | None = None,
    include_completed: bool = False,
    refresh: bool = False,
) -> dict[str, Any] | None:
    """Reconstruct only durable effects caused by one exact execution."""
    if execution_id:
        row = conn.execute(
            "SELECT * FROM operation_executions WHERE execution_id=?",
            (execution_id,),
        ).fetchone()
    elif request_id:
        status_filter = "" if include_completed else " AND status<>'completed'"
        row = conn.execute(
            "SELECT * FROM operation_executions "
            "WHERE request_id=?" + status_filter +
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (request_id,),
        ).fetchone()
    else:
        raise ValueError("execution_id or request_id is required")
    if row is None:
        return None
    if row["status"] == "completed":
        if not include_completed:
            return None
        evidence = json.loads(row["evidence_json"] or "{}")
        evidence.update({
            "execution_id": row["execution_id"],
            "operation_id": row["operation_id"],
            "request_id": row["request_id"],
            "command": row["command"],
            "execution_status": "completed",
            "result_persistence_missing": True,
            "failed_step": "response_persistence",
            "required_admin_action": None,
            "required_admin_outcome": None,
            "required_next_action": "inspect",
            "safe_to_retry": False,
            "recovery_required": False,
        })
        return evidence
    if row["evidence_json"] and not refresh:
        return json.loads(row["evidence_json"])

    baseline = json.loads(row["baseline_json"])
    operation_id = row["operation_id"]
    writes = _changed_rows(
        conn,
        table="write_attempts",
        operation_id=operation_id,
        key="attempt_id",
        fields=_WRITE_FIELDS,
        baseline=baseline["writes"],
    )
    movements = _changed_rows(
        conn,
        table="movement_attempts",
        operation_id=operation_id,
        key="attempt_id",
        fields=_MOVEMENT_FIELDS,
        baseline=baseline["movements"],
    )
    cycles = _changed_rows(
        conn,
        table="verification_cycles",
        operation_id=operation_id,
        key="cycle_id",
        fields=_CYCLE_FIELDS,
        baseline=baseline["cycles"],
    )
    new_cycles = [
        cycle for cycle in cycles if cycle["cycle_id"] not in baseline["cycles"]
    ]
    current_steps = _current_steps(conn, operation_id)
    changed_steps, step_scope = _execution_step_scope(
        command=row["command"], current=current_steps, baseline=baseline["steps"]
    )
    versions = _rows_after(
        conn, "content_versions", operation_id, int(baseline["content_rowid"])
    )
    actors = _rows_after(
        conn, "operation_actor_facts", operation_id, int(baseline["actor_rowid"])
    )
    audits = _rows_after(
        conn, "audit_events", operation_id, int(baseline["audit_rowid"])
    )
    workflow_audits = [
        audit
        for audit in audits
        if not str(audit["event_type"]).startswith(
            ("write_attempt.", "movement_attempt.")
        )
    ]
    operation = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if operation is None:
        raise DishRuleError(
            "CONFLICT",
            "operation execution lost its operation",
            rule="operation_execution_binding_invalid",
            details={"execution_id": row["execution_id"]},
        )

    write_committed, write_state = _attempt_state(writes)
    move_committed, movement_state = _attempt_state(movements)
    pending_steps = [
        step["step_name"] for step in step_scope if step["completed_at"] is None
    ]
    committed_steps = [
        step["step_name"] for step in changed_steps if step["completed_at"] is not None
    ]
    authoritative_identity, identity_source, content_version_id = (
        _confirmed_identity_from_execution(
            conn, versions=versions, writes=writes, baseline=baseline
        )
    )
    operation_changed = _operation_changed(operation, baseline["operation"])
    effects_observed = bool(
        writes
        or movements
        or cycles
        or changed_steps
        or versions
        or actors
        or audits
        or operation_changed
    )
    workflow_evidence_committed = bool(
        cycles or committed_steps or versions or actors or workflow_audits
    )
    committed_effects = bool(
        write_state in {"confirmed", "uncertain"}
        or movement_state in {"confirmed", "uncertain"}
        or workflow_evidence_committed
        or operation_changed
    )
    recovery_required = bool(committed_effects or pending_steps)
    unresolved = write_state == "uncertain" or movement_state == "uncertain"
    proven_not_applied = any(
        attempt["outcome"] == "not_applied" for attempt in [*writes, *movements]
    )
    if unresolved:
        required_outcome = "inspect"
    elif proven_not_applied:
        # A confirmed non-application must not be turned into an instruction to
        # finalize the workflow as applied merely because declared suffix steps
        # remain pending. Recovery clears the dead execution; the normal command
        # may then retry the exact external effect.
        required_outcome = "not-applied"
    elif recovery_required:
        required_outcome = "applied"
    else:
        required_outcome = None

    state = {
        "execution_id": row["execution_id"],
        "operation_id": operation_id,
        "request_id": row["request_id"],
        "command": row["command"],
        "write_committed": write_committed,
        "write_state": write_state,
        "move_committed": move_committed,
        "movement_state": movement_state,
        "cycle_created": bool(new_cycles),
        "cycle_ids": [cycle["cycle_id"] for cycle in new_cycles],
        "cycle_changed": bool(cycles),
        "changed_cycle_ids": [cycle["cycle_id"] for cycle in cycles],
        "committed_steps": committed_steps,
        "pending_steps": pending_steps,
        "local_state_committed": bool(
            cycles
            or changed_steps
            or versions
            or actors
            or workflow_audits
            or operation_changed
        ),
        "failed_step": _failure_step(
            pending_steps=pending_steps,
            writes=writes,
            movements=movements,
            failure_rule=failure_rule,
        ),
        "authoritative_task_identity": authoritative_identity,
        "authoritative_content_version_id": content_version_id,
        "authoritative_identity_source": identity_source,
        "write_attempt_ids": [attempt["attempt_id"] for attempt in writes],
        "movement_attempt_ids": [attempt["attempt_id"] for attempt in movements],
        "required_admin_action": "recover" if recovery_required else None,
        "required_admin_outcome": required_outcome,
        "admin_recovery_lease_scope": (
            "exact_uncertain_execution" if recovery_required else None
        ),
        "admin_recovery_immediately_executable": recovery_required,
        "safe_to_retry": not recovery_required,
        "effects_observed": effects_observed,
        "committed_effects": committed_effects,
        "workflow_evidence_committed": workflow_evidence_committed,
        "recovery_required": recovery_required,
    }
    if failure_rule:
        state["original_failure_rule"] = failure_rule
    return state


def finish_operation_execution(
    conn: sqlite3.Connection,
    claim: OperationExecutionClaim,
    *,
    status: str,
    evidence: Mapping[str, Any] | None = None,
) -> None:
    if status not in {"completed", "uncertain"}:
        raise ValueError("operation execution status must be completed or uncertain")
    if status == "uncertain" and evidence is None:
        raise ValueError("uncertain operation execution requires recovery evidence")
    final_evidence = (
        execution_recovery_state(conn, execution_id=claim.execution_id)
        if evidence is None
        else dict(evidence)
    )
    if final_evidence is None:
        raise DishRuleError(
            "CONFLICT",
            "operation execution evidence is missing",
            rule="operation_execution_binding_invalid",
            details={"execution_id": claim.execution_id},
        )
    encoded = json.dumps(
        dict(final_evidence), sort_keys=True, separators=(",", ":")
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = conn.execute(
            "SELECT status FROM operation_executions WHERE execution_id=?",
            (claim.execution_id,),
        ).fetchone()
        if current is None:
            raise DishRuleError(
                "CONFLICT", "operation execution is missing",
                rule="operation_execution_binding_invalid",
                details={"execution_id": claim.execution_id},
            )
        if current["status"] == "started":
            updated = conn.execute(
                """UPDATE operation_executions
                      SET status=?, evidence_json=?, completed_at=?
                    WHERE execution_id=? AND status='started'""",
                (status, encoded, utc_now(), claim.execution_id),
            )
        elif current["status"] == "uncertain" and status == "completed":
            updated = conn.execute(
                """UPDATE operation_executions
                      SET status='completed', resolution_evidence_json=?, resolved_at=?
                    WHERE execution_id=? AND status='uncertain' AND resolved_at IS NULL""",
                (encoded, utc_now(), claim.execution_id),
            )
        elif current["status"] == "uncertain" and status == "uncertain":
            updated = None
        else:
            updated = None
            raise DishRuleError(
                "CONFLICT",
                "operation execution changed before completion",
                rule="operation_execution_completion_lost",
                retryable=False,
                details={"execution_id": claim.execution_id},
            )
        if updated is not None and updated.rowcount != 1:
            raise DishRuleError(
                "CONFLICT",
                "operation execution changed before completion",
                rule="operation_execution_completion_lost",
                retryable=False,
                details={"execution_id": claim.execution_id},
            )
        deleted = conn.execute(
            "DELETE FROM operation_execution_claims WHERE operation_id=? AND claim_id=?",
            (claim.operation_id, claim.claim_id),
        )
        if deleted.rowcount != 1:
            raise DishRuleError(
                "CONFLICT",
                "operation execution claim changed before release",
                rule="operation_execution_claim_lost",
                retryable=False,
                details={"operation_id": claim.operation_id},
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def resolve_recovered_unclaimed_local_executions(
    conn: sqlite3.Connection, *, operation_id: str
) -> list[str]:
    """Resolve requestless uncertain executions after direct authoritative recovery.

    Service-journalled requests are resolved by exact replay so their authoritative
    result envelope remains reconstructable. This helper exists for local direct
    mode and tests, where no service request row owns the execution.
    """
    rows = conn.execute(
        """SELECT execution_id FROM operation_executions AS execution
             WHERE operation_id=? AND status='uncertain' AND resolved_at IS NULL
               AND request_id IS NULL
               AND NOT EXISTS (
                   SELECT 1 FROM operation_execution_claims AS claim
                    WHERE claim.execution_id=execution.execution_id
               )
             ORDER BY created_at, rowid""",
        (operation_id,),
    ).fetchall()
    resolved: list[str] = []
    for row in rows:
        state = execution_recovery_state(
            conn, execution_id=row["execution_id"], refresh=True
        )
        if (
            state is None
            or state.get("pending_steps")
            or state.get("write_state") == "uncertain"
            or state.get("movement_state") == "uncertain"
        ):
            continue
        encoded = json.dumps(state, sort_keys=True, separators=(",", ":"))
        conn.execute("BEGIN IMMEDIATE")
        try:
            updated = conn.execute(
                """UPDATE operation_executions
                      SET status='completed', resolution_evidence_json=?, resolved_at=?
                    WHERE execution_id=? AND status='uncertain'
                      AND resolved_at IS NULL AND request_id IS NULL""",
                (encoded, utc_now(), row["execution_id"]),
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        if updated.rowcount == 1:
            resolved.append(row["execution_id"])
    return resolved



def partial_write_error(
    error: Exception, recovery: Mapping[str, Any]
) -> DishRuleError:
    original_rule = error.rule if isinstance(error, DishRuleError) else None
    rule = (
        original_rule
        if isinstance(error, DishRuleError) and error.code == "BACKEND_UNCERTAIN"
        else "operation_partial_write_failure"
    )
    return DishRuleError(
        "BACKEND_UNCERTAIN",
        "operation effects were durably observed but command completion was not confirmed",
        rule=rule,
        retryable=False,
        details=dict(recovery),
    )
