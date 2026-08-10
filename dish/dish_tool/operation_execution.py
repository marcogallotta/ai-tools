"""Durable, request-scoped execution evidence for operation mutations."""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .errors import DishRuleError
from .transactions import immediate_transaction
from .models import ProcessIdentity, utc_now
from .human_actions import exact_action
from .recovery import current_process_identity, process_identity_is_live


@dataclass(frozen=True)
class OperationExecutionClaim:
    operation_id: str
    claim_id: str
    execution_id: str
    command: str
    request_id: str | None
    resuming_uncertain: bool = False


def _recover_command_guidance(operation_id: str) -> dict[str, object]:
    spec = exact_action(
        kind="reconcile-uncertain-effect",
        command="recover",
        positional=(operation_id,),
        summary="Automatically inspect and reconcile the interrupted execution.",
        effect=(
            "Use fresh live evidence to settle only mechanically proven recovery state; "
            "stop without guessing if the outcome remains ambiguous."
        ),
        after_success={
            "instruction": "Refresh the same operation and follow its returned continuation."
        },
        details=(
            "Automatic inspection is the normal recovery path.",
            "Manual --outcome applied / not-applied are advanced assertions only; never guess them.",
        ),
    )
    payload = spec.payload()
    payload["directive"] = (
        "Tell Marco only that an interrupted workflow execution needs admin recovery before "
        "this operation can continue. Keep the exact admin command available, but do not print "
        "it unless Marco asks how to run the recovery. Automatic inspection is the normal path; "
        "never guess applied or not-applied."
    )
    return payload


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


def _assert_current_service_authority(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    owner_id: str | None,
    run_id: str | None,
    authority_now: str | None = None,
    require_actor_lease: bool = True,
) -> None:
    """Fence a service mutation at the execution-claim transaction boundary.

    Connected-agent lease admission happens before command dispatch.  That check
    is advisory by the time a later SQLite writer transaction begins: Marco may
    revoke the exact run in between.  When a service principal is supplied, the
    execution claim therefore revalidates the same owner/run while holding the
    writer lock that creates the mutation claim, and normally revalidates its
    actor lease too. Approved mechanical proposal application deliberately omits
    only the actor-lease requirement; it still supplies its exact principal and
    is fenced by explicit revocation at this writer boundary.

    Low-level/admin recovery callers that supply no service principal retain
    their existing authority boundary.
    """

    clean_owner = str(owner_id or "").strip() or None
    clean_run = str(run_id or "").strip() or None
    if clean_owner is None and clean_run is None:
        return
    if clean_owner is None or clean_run is None:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "operation execution authority requires both service owner and run identity",
            rule="operation_execution_authority_identity_required",
        )

    revoked = conn.execute(
        """SELECT revocation_id,revoked_at
             FROM operation_run_revocations
            WHERE operation_id=? AND owner_id=? AND run_id=?""",
        (operation_id, clean_owner, clean_run),
    ).fetchone()
    if revoked is not None:
        raise DishRuleError(
            "AGENT_MISMATCH",
            "This Dish run has been killed.",
            rule="killed_run_revoked",
            details={
                "operation_id": operation_id,
                "revocation_id": revoked["revocation_id"],
                "revoked_at": revoked["revoked_at"],
            },
        )

    if not require_actor_lease:
        return

    lease = conn.execute(
        """SELECT * FROM service_leases
             WHERE operation_id=? AND lease_kind='actor' AND released_at IS NULL""",
        (operation_id,),
    ).fetchone()
    if lease is None:
        raise DishRuleError(
            "AGENT_MISMATCH",
            "this run no longer has current Dish mutation authority",
            rule="service_lease_missing",
            details={"operation_id": operation_id},
        )
    if lease["owner_id"] != clean_owner or lease["run_id"] != clean_run:
        raise DishRuleError(
            "AGENT_MISMATCH",
            "service lease belongs to another client run",
            rule="service_lease_owner_mismatch",
            details={
                "operation_id": operation_id,
                "owner_id": lease["owner_id"],
                "run_id": lease["run_id"],
            },
        )
    expired = conn.execute(
        "SELECT julianday(?)<=julianday(?)",
        (lease["expires_at"], authority_now or utc_now()),
    ).fetchone()[0]
    if expired:
        raise DishRuleError(
            "CONFLICT",
            "service lease expired and requires administrative recovery",
            rule="service_lease_expired",
            details={
                "operation_id": operation_id,
                "expires_at": lease["expires_at"],
            },
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
        "audit_provenance_version": 1,
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


def unresolved_operation_executions(
    conn: sqlite3.Connection, operation_id: str
) -> list[sqlite3.Row]:
    """Return executions whose command outcome is not durably settled."""
    return conn.execute(
        """SELECT execution_id,status,resolved_at,command FROM operation_executions
             WHERE operation_id=?
               AND (status='started' OR (status='uncertain' AND resolved_at IS NULL))
             ORDER BY created_at""",
        (operation_id,),
    ).fetchall()


def operation_recovery_pending(conn: sqlite3.Connection, operation_id: str) -> bool:
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


def live_operation_execution_claim(
    conn: sqlite3.Connection, *, operation_id: str
):
    """Return the operation claim only when its recorded process is still live."""

    row = conn.execute(
        "SELECT * FROM operation_execution_claims WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    if row is None or not process_identity_is_live(_identity(row)):
        return None
    return row


def claim_abandonment_execution(
    conn: sqlite3.Connection,
    *,
    abandonment_id: str,
    execution_id: str,
) -> OperationExecutionClaim:
    """Reclaim the exact crashed admin execution bound to an abandonment."""

    identity = current_process_identity()
    claim_id = str(uuid.uuid4())
    with immediate_transaction(conn, "claim_abandonment_execution"):
        abandonment = conn.execute(
            "SELECT * FROM abandonment_attempts WHERE abandonment_id=?",
            (abandonment_id,),
        ).fetchone()
        execution = conn.execute(
            "SELECT * FROM operation_executions WHERE execution_id=?",
            (execution_id,),
        ).fetchone()
        settled_replay = bool(
            abandonment is not None
            and abandonment["current_execution_id"] is None
            and abandonment["status"] in {
                "awaiting_successor_claim",
                "awaiting_hold_resolution",
                "completed",
            }
            and conn.execute(
                "SELECT 1 FROM operation_execution_claims WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            is not None
        )
        if (
            abandonment is None
            or execution is None
            or not (
                abandonment["current_execution_id"] == execution_id
                or settled_replay
            )
            or execution["operation_id"] != abandonment["source_operation_id"]
            or execution["command"] not in {
                "abandon-operation",
                "reconcile-abandonment",
            }
            or execution["status"] not in {"started", "uncertain"}
        ):
            raise DishRuleError(
                "CONFLICT",
                "abandonment execution is not the exact resumable authority",
                rule="abandonment_execution_not_resumable",
                details={"abandonment_id": abandonment_id, "execution_id": execution_id},
            )
        existing = conn.execute(
            "SELECT * FROM operation_execution_claims WHERE operation_id=?",
            (execution["operation_id"],),
        ).fetchone()
        if existing is not None:
            if existing["execution_id"] != execution_id:
                raise DishRuleError(
                    "CONFLICT",
                    "another mutation owns the abandonment source operation",
                    rule="operation_mutation_in_progress",
                    retryable=True,
                )
            if process_identity_is_live(_identity(existing)):
                raise DishRuleError(
                    "CONFLICT",
                    "the abandonment execution is still running",
                    rule="operation_mutation_in_progress",
                    retryable=True,
                    details={
                        "operation_id": execution["operation_id"],
                        "command": execution["command"],
                        "acquired_at": existing["acquired_at"],
                    },
                )
            conn.execute(
                "DELETE FROM operation_execution_claims WHERE operation_id=? AND claim_id=?",
                (execution["operation_id"], existing["claim_id"]),
            )

        resuming_uncertain = execution["status"] == "uncertain"
        if execution["status"] == "started" and operation_recovery_pending(
            conn, execution["operation_id"]
        ):
            evidence = execution_recovery_state(
                conn, execution_id=execution_id, failure_rule="process_terminated"
            )
            conn.execute(
                """UPDATE operation_executions
                      SET status='uncertain', evidence_json=?, completed_at=?
                    WHERE execution_id=? AND status='started'""",
                (
                    json.dumps(evidence or {}, sort_keys=True, separators=(",", ":")),
                    utc_now(),
                    execution_id,
                ),
            )
            resuming_uncertain = True
        conn.execute(
            """INSERT INTO operation_execution_claims(
                   operation_id,claim_id,command,hostname,pid,process_start,
                   acquired_at,execution_id
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                execution["operation_id"],
                claim_id,
                execution["command"],
                identity.hostname,
                identity.pid,
                identity.process_start,
                utc_now(),
                execution_id,
            ),
        )
        return OperationExecutionClaim(
            operation_id=execution["operation_id"],
            claim_id=claim_id,
            execution_id=execution_id,
            command=execution["command"],
            request_id=execution["request_id"],
            resuming_uncertain=resuming_uncertain,
        )



def _request_matches_execution(
    row: Mapping[str, Any] | None,
    *,
    command: str,
    request_id: str | None,
) -> bool:
    if row is None or row["command"] != command:
        return False
    recorded = row["request_id"]
    return (request_id is not None and recorded == request_id) or (
        request_id is None and recorded is None
    )


def _insert_execution_claim(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    claim_id: str,
    execution_id: str,
    command: str,
    request_id: str | None,
    identity: ProcessIdentity,
    resuming_uncertain: bool = False,
) -> OperationExecutionClaim:
    conn.execute(
        """INSERT INTO operation_execution_claims(
               operation_id,claim_id,command,hostname,pid,process_start,
               acquired_at,execution_id
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            operation_id,
            claim_id,
            command,
            identity.hostname,
            identity.pid,
            identity.process_start,
            utc_now(),
            execution_id,
        ),
    )
    return OperationExecutionClaim(
        operation_id=operation_id,
        claim_id=claim_id,
        execution_id=execution_id,
        command=command,
        request_id=request_id,
        resuming_uncertain=resuming_uncertain,
    )


def _raise_live_execution_conflict(existing: Mapping[str, Any]) -> None:
    raise DishRuleError(
        "CONFLICT",
        "another mutation is already executing for this operation",
        rule="operation_mutation_in_progress",
        retryable=True,
        details={
            "operation_id": existing["operation_id"],
            "command": existing["command"],
            "acquired_at": existing["acquired_at"],
        },
    )


def _stale_claim_recovery(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    existing: Mapping[str, Any],
    command: str,
    request_id: str | None,
    claim_id: str,
    identity: ProcessIdentity,
) -> OperationExecutionClaim | None:
    if process_identity_is_live(_identity(existing)):
        _raise_live_execution_conflict(existing)

    execution_id = existing["execution_id"]
    prior_recovery = (
        None
        if not execution_id
        else execution_recovery_state(
            conn, execution_id=execution_id, failure_rule="process_terminated"
        )
    )
    prior_execution = (
        None
        if not execution_id
        else conn.execute(
            "SELECT * FROM operation_executions WHERE execution_id=?",
            (execution_id,),
        ).fetchone()
    )
    exact_resume = _request_matches_execution(
        prior_execution, command=command, request_id=request_id
    )
    recovery_required = (
        operation_recovery_pending(conn, operation_id)
        if prior_recovery is None
        else bool(prior_recovery["recovery_required"])
    )
    if recovery_required:
        if command != "recover":
            details = dict(prior_recovery or {})
            details.update(
                {
                    "operation_id": operation_id,
                    "command": existing["command"],
                    "execution_id": execution_id,
                    "required_admin_action": "recover",
                    **_recover_command_guidance(operation_id),
                }
            )
            raise DishRuleError(
                "CONFLICT",
                "a crashed operation mutation requires recovery before another mutation",
                rule="operation_mutation_recovery_required",
                retryable=False,
                details=details,
            )
        if execution_id and prior_recovery is not None:
            conn.execute(
                """UPDATE operation_executions
                      SET status='uncertain', evidence_json=?, completed_at=?
                    WHERE execution_id=? AND status='started'""",
                (
                    json.dumps(
                        prior_recovery, sort_keys=True, separators=(",", ":")
                    ),
                    utc_now(),
                    execution_id,
                ),
            )
    elif exact_resume:
        conn.execute(
            "DELETE FROM operation_execution_claims WHERE operation_id=? AND claim_id=?",
            (operation_id, existing["claim_id"]),
        )
        return _insert_execution_claim(
            conn,
            operation_id=operation_id,
            claim_id=claim_id,
            execution_id=str(prior_execution["execution_id"]),
            command=command,
            request_id=request_id,
            identity=identity,
        )
    else:
        _complete_abandoned_execution(conn, execution_id)

    conn.execute(
        "DELETE FROM operation_execution_claims WHERE operation_id=? AND claim_id=?",
        (operation_id, existing["claim_id"]),
    )
    return None


def _claim_unresolved_execution(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    command: str,
    request_id: str | None,
    claim_id: str,
    identity: ProcessIdentity,
) -> OperationExecutionClaim | None:
    unresolved = conn.execute(
        """SELECT * FROM operation_executions
             WHERE operation_id=? AND status='uncertain' AND resolved_at IS NULL
             ORDER BY created_at, rowid""",
        (operation_id,),
    ).fetchall()
    if not unresolved:
        return None
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
                **_recover_command_guidance(operation_id),
            },
        )

    prior = unresolved[0]
    exact_resume = _request_matches_execution(
        prior, command=command, request_id=request_id
    )
    if not exact_resume and command != "recover":
        recovery = json.loads(prior["evidence_json"] or "{}")
        details = dict(recovery) if isinstance(recovery, dict) else {}
        details.update(
            {
                "operation_id": operation_id,
                "command": prior["command"],
                "execution_id": prior["execution_id"],
                "request_id": prior["request_id"],
                "required_admin_action": "recover",
                **_recover_command_guidance(operation_id),
            }
        )
        raise DishRuleError(
            "CONFLICT",
            "an unresolved uncertain mutation must be reconciled before another mutation",
            rule="operation_mutation_recovery_required",
            retryable=False,
            details=details,
        )
    return _insert_execution_claim(
        conn,
        operation_id=operation_id,
        claim_id=claim_id,
        execution_id=str(prior["execution_id"]),
        command=command,
        request_id=request_id,
        identity=identity,
        resuming_uncertain=True,
    )


def _create_operation_execution(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    command: str,
    request_id: str | None,
    claim_id: str,
    execution_id: str,
    identity: ProcessIdentity,
) -> OperationExecutionClaim:
    baseline = _execution_baseline(conn, operation_id)
    conn.execute(
        """INSERT INTO operation_executions(
               execution_id,operation_id,request_id,command,baseline_json,
               status,created_at
           ) VALUES(?,?,?,?,?,'started',?)""",
        (
            execution_id,
            operation_id,
            request_id,
            command,
            json.dumps(baseline, sort_keys=True, separators=(",", ":")),
            utc_now(),
        ),
    )
    return _insert_execution_claim(
        conn,
        operation_id=operation_id,
        claim_id=claim_id,
        execution_id=execution_id,
        command=command,
        request_id=request_id,
        identity=identity,
    )


def claim_operation_execution(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    command: str,
    request_id: str | None = None,
    owner_id: str | None = None,
    run_id: str | None = None,
    authority_now: str | None = None,
    require_actor_lease: bool = True,
) -> OperationExecutionClaim:
    """Atomically reserve an operation and persist this execution's baseline."""
    identity = current_process_identity()
    claim_id = str(uuid.uuid4())
    execution_id = str(uuid.uuid4())
    clean_command = str(command).strip()
    clean_request = str(request_id or "").strip() or None
    with immediate_transaction(conn, "claim_operation_execution"):
        operation = conn.execute(
            "SELECT status FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if operation is None:
            raise DishRuleError(
                "NOT_FOUND", "operation not found", rule="operation_not_found"
            )
        _assert_current_service_authority(
            conn,
            operation_id=operation_id,
            owner_id=owner_id,
            run_id=run_id,
            authority_now=authority_now,
            require_actor_lease=require_actor_lease,
        )
        existing = conn.execute(
            "SELECT * FROM operation_execution_claims WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        claim = None
        if existing is not None:
            claim = _stale_claim_recovery(
                conn,
                operation_id=operation_id,
                existing=existing,
                command=clean_command,
                request_id=clean_request,
                claim_id=claim_id,
                identity=identity,
            )
        if claim is None:
            claim = _claim_unresolved_execution(
                conn,
                operation_id=operation_id,
                command=clean_command,
                request_id=clean_request,
                claim_id=claim_id,
                identity=identity,
            )
        if claim is None:
            claim = _create_operation_execution(
                conn,
                operation_id=operation_id,
                command=clean_command,
                request_id=clean_request,
                claim_id=claim_id,
                execution_id=execution_id,
                identity=identity,
            )
        return claim



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



def _execution_row_for_recovery(
    conn: sqlite3.Connection,
    *,
    execution_id: str | None,
    request_id: str | None,
    include_completed: bool,
):
    if execution_id:
        return conn.execute(
            "SELECT * FROM operation_executions WHERE execution_id=?",
            (execution_id,),
        ).fetchone()
    if request_id:
        status_filter = "" if include_completed else " AND status<>'completed'"
        return conn.execute(
            "SELECT * FROM operation_executions WHERE request_id=?"
            + status_filter
            + " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (request_id,),
        ).fetchone()
    raise ValueError("execution_id or request_id is required")


def _completed_execution_recovery(row: Mapping[str, Any]) -> dict[str, Any]:
    evidence = json.loads(row["evidence_json"] or "{}")
    evidence.update(
        {
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
        }
    )
    return evidence


def _execution_changes(
    conn: sqlite3.Connection,
    *,
    row: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> dict[str, Any]:
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
    current_steps = _current_steps(conn, operation_id)
    changed_steps, step_scope = _execution_step_scope(
        command=row["command"], current=current_steps, baseline=baseline["steps"]
    )
    if int(baseline.get("audit_provenance_version") or 0) >= 1:
        workflow_audits = conn.execute(
            """SELECT rowid AS evidence_rowid,*
                 FROM audit_events
                WHERE operation_execution_id=?
                ORDER BY rowid""",
            (row["execution_id"],),
        ).fetchall()
    else:
        # Pre-provenance executions retain the conservative historical fallback
        # so migration cannot erase recovery evidence already in flight.
        audits = _rows_after(
            conn, "audit_events", operation_id, int(baseline["audit_rowid"])
        )
        workflow_audits = [
            audit
            for audit in audits
            if not str(audit["event_type"]).startswith(
                ("write_attempt.", "movement_attempt.", "dish.", "dish-admin.")
            )
        ]
    return {
        "writes": writes,
        "movements": movements,
        "cycles": cycles,
        "new_cycles": [
            cycle for cycle in cycles if cycle["cycle_id"] not in baseline["cycles"]
        ],
        "changed_steps": changed_steps,
        "step_scope": step_scope,
        "versions": _rows_after(
            conn,
            "content_versions",
            operation_id,
            int(baseline["content_rowid"]),
        ),
        "actors": _rows_after(
            conn,
            "operation_actor_facts",
            operation_id,
            int(baseline["actor_rowid"]),
        ),
        "workflow_audits": workflow_audits,
        "audit_provenance": (
            "operation_execution_id"
            if int(baseline.get("audit_provenance_version") or 0) >= 1
            else "legacy_operation_rowid"
        ),
    }


def _execution_recovery_classification(
    conn: sqlite3.Connection,
    *,
    operation: Mapping[str, Any],
    baseline: Mapping[str, Any],
    changes: Mapping[str, Any],
) -> dict[str, Any]:
    writes = changes["writes"]
    movements = changes["movements"]
    cycles = changes["cycles"]
    changed_steps = changes["changed_steps"]
    step_scope = changes["step_scope"]
    versions = changes["versions"]
    actors = changes["actors"]
    workflow_audits = changes["workflow_audits"]
    write_committed, write_state = _attempt_state(writes)
    move_committed, movement_state = _attempt_state(movements)
    pending_steps = [
        step["step_name"] for step in step_scope if step["completed_at"] is None
    ]
    committed_steps = [
        step["step_name"]
        for step in changed_steps
        if step["completed_at"] is not None
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
        or workflow_audits
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
    required_outcome = (
        "inspect"
        if unresolved
        else "not-applied"
        if proven_not_applied
        else "applied"
        if recovery_required
        else None
    )
    return {
        "write_committed": write_committed,
        "write_state": write_state,
        "move_committed": move_committed,
        "movement_state": movement_state,
        "pending_steps": pending_steps,
        "committed_steps": committed_steps,
        "authoritative_identity": authoritative_identity,
        "identity_source": identity_source,
        "content_version_id": content_version_id,
        "operation_changed": operation_changed,
        "effects_observed": effects_observed,
        "workflow_evidence_committed": workflow_evidence_committed,
        "committed_effects": committed_effects,
        "recovery_required": recovery_required,
        "required_outcome": required_outcome,
        "audit_provenance": changes["audit_provenance"],
    }


def _build_execution_recovery_state(
    *,
    row: Mapping[str, Any],
    changes: Mapping[str, Any],
    classification: Mapping[str, Any],
    failure_rule: str | None,
) -> dict[str, Any]:
    writes = changes["writes"]
    movements = changes["movements"]
    cycles = changes["cycles"]
    changed_steps = changes["changed_steps"]
    versions = changes["versions"]
    actors = changes["actors"]
    workflow_audits = changes["workflow_audits"]
    recovery_required = bool(classification["recovery_required"])
    state = {
        "execution_id": row["execution_id"],
        "operation_id": row["operation_id"],
        "request_id": row["request_id"],
        "command": row["command"],
        "write_committed": classification["write_committed"],
        "write_state": classification["write_state"],
        "move_committed": classification["move_committed"],
        "movement_state": classification["movement_state"],
        "cycle_created": bool(changes["new_cycles"]),
        "cycle_ids": [cycle["cycle_id"] for cycle in changes["new_cycles"]],
        "cycle_changed": bool(cycles),
        "changed_cycle_ids": [cycle["cycle_id"] for cycle in cycles],
        "committed_steps": classification["committed_steps"],
        "pending_steps": classification["pending_steps"],
        "local_state_committed": bool(
            cycles
            or changed_steps
            or versions
            or actors
            or workflow_audits
            or classification["operation_changed"]
        ),
        "failed_step": _failure_step(
            pending_steps=classification["pending_steps"],
            writes=writes,
            movements=movements,
            failure_rule=failure_rule,
        ),
        "authoritative_task_identity": classification["authoritative_identity"],
        "authoritative_content_version_id": classification["content_version_id"],
        "authoritative_identity_source": classification["identity_source"],
        "write_attempt_ids": [attempt["attempt_id"] for attempt in writes],
        "movement_attempt_ids": [attempt["attempt_id"] for attempt in movements],
        "required_admin_action": "recover" if recovery_required else None,
        "required_admin_outcome": classification["required_outcome"],
        "admin_recovery_lease_scope": (
            "exact_uncertain_execution" if recovery_required else None
        ),
        "admin_recovery_immediately_executable": recovery_required,
        "safe_to_retry": not recovery_required,
        "effects_observed": classification["effects_observed"],
        "committed_effects": classification["committed_effects"],
        "workflow_evidence_committed": classification[
            "workflow_evidence_committed"
        ],
        "recovery_required": recovery_required,
    }
    if failure_rule:
        state["original_failure_rule"] = failure_rule
    if recovery_required:
        state.update(_recover_command_guidance(row["operation_id"]))
    return state


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
    row = _execution_row_for_recovery(
        conn,
        execution_id=execution_id,
        request_id=request_id,
        include_completed=include_completed,
    )
    if row is None:
        return None
    if row["status"] == "completed":
        return _completed_execution_recovery(row) if include_completed else None
    if row["evidence_json"] and not refresh:
        return json.loads(row["evidence_json"])

    baseline = json.loads(row["baseline_json"])
    operation = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (row["operation_id"],)
    ).fetchone()
    if operation is None:
        raise DishRuleError(
            "CONFLICT",
            "operation execution lost its operation",
            rule="operation_execution_binding_invalid",
            details={"execution_id": row["execution_id"]},
        )
    changes = _execution_changes(conn, row=row, baseline=baseline)
    classification = _execution_recovery_classification(
        conn, operation=operation, baseline=baseline, changes=changes
    )
    return _build_execution_recovery_state(
        row=row,
        changes=changes,
        classification=classification,
        failure_rule=failure_rule,
    )



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
    with immediate_transaction(conn, "finish_operation_execution"):
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
        with immediate_transaction(conn, "resolve_unclaimed_execution"):
            updated = conn.execute(
                """UPDATE operation_executions
                      SET status='completed', resolution_evidence_json=?, resolved_at=?
                    WHERE execution_id=? AND status='uncertain'
                      AND resolved_at IS NULL AND request_id IS NULL""",
                (encoded, utc_now(), row["execution_id"]),
            )
        if updated.rowcount == 1:
            resolved.append(row["execution_id"])
    return resolved



def partial_write_error(
    error: Exception, recovery: Mapping[str, Any]
) -> DishRuleError:
    original_rule = error.rule if isinstance(error, DishRuleError) else None
    committed_effects = bool(recovery.get("recovery_required"))
    rule = (
        original_rule
        if isinstance(error, DishRuleError) and error.code == "BACKEND_UNCERTAIN"
        else (
            "operation_partial_write_failure"
            if committed_effects
            else "operation_exact_replay_required"
        )
    )
    message = (
        "operation effects were durably observed but command completion was not confirmed"
        if committed_effects
        else "operation committed no workflow effect but command completion was not confirmed; "
        "replay the exact request UUID"
    )
    return DishRuleError(
        "BACKEND_UNCERTAIN",
        message,
        rule=rule,
        retryable=not committed_effects,
        details=dict(recovery),
    )
