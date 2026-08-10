"""Mechanical safe-reclaim predicate and linked successor creation."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .constants import COOKING_PROJECT_GID
from .database import (
    complete_operation_step,
    declare_operation_step,
    operation_run_revocation,
    record_actor_fact,
    record_audit,
)
from .errors import DishRuleError
from .models import SectionRegistry, utc_now
from .operation_execution import unresolved_operation_executions
from .task_store import read_complete_task
from .transactions import immediate_transaction


@dataclass(frozen=True)
class SafeReclaimEligibility:
    eligible: bool
    operation_id: str
    task_gid: str
    lease_id: str | None
    previous_owner_id: str | None
    previous_run_id: str | None
    stage: str | None
    source_cycle_id: str | None
    source_content_version_id: str | None
    failed_clauses: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _stamp(value: datetime | None = None) -> str:
    moment = value or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _failure(rule: str, message: str, **details: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"rule": rule, "message": message}
    if details:
        payload["details"] = details
    return payload


def _selected_lease(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    lease_id: str | None,
) -> sqlite3.Row | None:
    if lease_id:
        return conn.execute(
            "SELECT * FROM service_leases WHERE lease_id=? AND operation_id=? AND lease_kind='actor'",
            (lease_id, operation_id),
        ).fetchone()
    rows = conn.execute(
        """SELECT * FROM service_leases
             WHERE operation_id=? AND lease_kind='actor'
             ORDER BY actor_attempt_seq DESC LIMIT 2""",
        (operation_id,),
    ).fetchall()
    return rows[0] if rows else None


def _confirmed_version(
    conn: sqlite3.Connection, *, task_gid: str, identity: str
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM content_versions
             WHERE task_gid=? AND identity=? AND confirmed=1
             ORDER BY created_at DESC,rowid DESC LIMIT 1""",
        (task_gid, identity),
    ).fetchone()


def _stage_and_baseline(
    conn: sqlite3.Connection,
    *,
    operation: sqlite3.Row,
    lease: sqlite3.Row,
    live: Any,
    registry: SectionRegistry,
) -> tuple[str | None, str | None, str | None, list[dict[str, Any]]]:
    failures: list[dict[str, Any]] = []
    if operation["operation_kind"] == "planning":
        stage = "planning"
    elif operation["phase"] == "await_verification":
        stage = "verification"
    else:
        stage = "research"

    cycle_id: str | None = None
    identity: str | None = None
    if stage in {"planning", "research"}:
        if operation["phase"] != "prepare_required":
            failures.append(
                _failure(
                    "safe_reclaim_frontier_not_restartable",
                    "operation is not at a clean Planning/Research restart frontier",
                    phase=operation["phase"],
                )
            )
        identity = operation["expected_identity"]
        if live.identity != identity or live.section_gid != operation["expected_section_gid"]:
            failures.append(
                _failure(
                    "safe_reclaim_live_frontier_drift",
                    "live task no longer matches the exact stage baseline and placement",
                    expected_identity=identity,
                    actual_identity=live.identity,
                    expected_section_gid=operation["expected_section_gid"],
                    actual_section_gid=live.section_gid,
                )
            )
        if lease["context_cycle_id"] is not None:
            failures.append(
                _failure(
                    "safe_reclaim_lease_context_mismatch",
                    "stage lease unexpectedly carries Verification cycle context",
                    cycle_id=lease["context_cycle_id"],
                )
            )
    else:
        cycle = conn.execute(
            """SELECT * FROM verification_cycles
                 WHERE operation_id=? AND completed_at IS NULL
                 ORDER BY cycle_number DESC LIMIT 1""",
            (operation["operation_id"],),
        ).fetchone()
        if cycle is None:
            failures.append(
                _failure(
                    "safe_reclaim_verification_cycle_missing",
                    "operation has no incomplete Verification cycle to replace",
                )
            )
        else:
            cycle_id = cycle["cycle_id"]
            if lease["context_cycle_id"] != cycle_id or cycle["run_id"] != lease["run_id"]:
                failures.append(
                    _failure(
                        "safe_reclaim_lease_context_mismatch",
                        "inactive lease does not name the current Verification attempt",
                        lease_cycle_id=lease["context_cycle_id"],
                        current_cycle_id=cycle_id,
                    )
                )
            decision = conn.execute(
                """SELECT step_name FROM operation_steps
                     WHERE operation_id=? AND (
                         step_name LIKE 'small_%'
                         OR (step_name LIKE 'route_%' AND step_name LIKE '%:' || ?)
                         OR step_name IN ('signoff_write','signoff_finalize')
                     ) LIMIT 1""",
                (operation["operation_id"], cycle_id),
            ).fetchone()
            if decision is not None:
                failures.append(
                    _failure(
                        "safe_reclaim_verification_decision_started",
                        "Verification already has decision/application work and cannot be safely restarted",
                        step_name=decision["step_name"],
                    )
                )
            head = conn.execute(
                "SELECT last_confirmed_identity FROM task_content_state WHERE task_gid=?",
                (operation["task_gid"],),
            ).fetchone()
            identity = cycle["reviewed_identity"] or (None if head is None else head[0])
            if (
                identity is None
                or operation["phase"] != "await_verification"
                or live.identity != identity
                or live.section_gid != registry.verification_queue_gid
            ):
                failures.append(
                    _failure(
                        "safe_reclaim_live_frontier_drift",
                        "live task no longer matches the exact Verification candidate frontier",
                        expected_identity=identity,
                        actual_identity=live.identity,
                        expected_section_gid=registry.verification_queue_gid,
                        actual_section_gid=live.section_gid,
                    )
                )

    version = None if identity is None else _confirmed_version(
        conn, task_gid=operation["task_gid"], identity=identity
    )
    if identity is not None and version is None:
        failures.append(
            _failure(
                "safe_reclaim_confirmed_baseline_missing",
                "clean restart frontier lacks a confirmed content version",
                identity=identity,
            )
        )
    return stage, cycle_id, None if version is None else version["content_version_id"], failures


def safe_reclaim_eligibility(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    requested_owner_id: str | None = None,
    requested_run_id: str | None = None,
    lease_id: str | None = None,
    now: datetime | None = None,
) -> SafeReclaimEligibility:
    """Evaluate the single SQLite safe-reclaim predicate without mutation."""

    operation = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if operation is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    failures: list[dict[str, Any]] = []
    lease = _selected_lease(conn, operation_id=operation_id, lease_id=lease_id)
    if operation["status"] != "open" or operation["phase"] == "terminal":
        failures.append(
            _failure(
                "safe_reclaim_source_not_open",
                "only an open nonterminal operation can be safely reclaimed",
                status=operation["status"],
                phase=operation["phase"],
            )
        )
    if lease is None:
        failures.append(
            _failure(
                "safe_reclaim_source_lease_missing",
                "no exact prior actor lease is available for safe reclaim",
            )
        )
        return SafeReclaimEligibility(
            False,
            operation_id,
            operation["task_gid"],
            None,
            None,
            None,
            None,
            None,
            None,
            tuple(failures),
        )

    clean_owner = str(requested_owner_id or "").strip()
    clean_run = str(requested_run_id or "").strip()
    if bool(clean_owner) != bool(clean_run):
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "safe reclaim principal inspection requires both owner and run identity",
            rule="service_principal_required",
        )
    if clean_run and clean_run == str(lease["run_id"]):
        failures.append(
            _failure(
                "safe_reclaim_same_run_forbidden",
                "the same run must use recover-lease rather than safe reclaim",
                previous_run_id=lease["run_id"],
            )
        )
    reference_time = _stamp(now)
    inactive = lease["released_at"] is not None or conn.execute(
        "SELECT julianday(?) <= julianday(?)", (lease["expires_at"], reference_time)
    ).fetchone()[0] == 1
    if not inactive:
        failures.append(
            _failure(
                "safe_reclaim_live_lease",
                "the prior actor lease is still live",
                lease_id=lease["lease_id"],
                expires_at=lease["expires_at"],
            )
        )

    later = conn.execute(
        """SELECT lease_id,run_id FROM service_leases
             WHERE operation_id=? AND lease_kind='actor' AND actor_attempt_seq>?
             ORDER BY actor_attempt_seq LIMIT 1""",
        (operation_id, lease["actor_attempt_seq"]),
    ).fetchone()
    if later is not None:
        failures.append(
            _failure(
                "safe_reclaim_source_attempt_superseded",
                "a later actor attempt already supersedes the selected lease",
                lease_id=later["lease_id"],
                run_id=later["run_id"],
            )
        )
    if conn.execute(
        "SELECT 1 FROM operation_successions WHERE source_operation_id=?",
        (operation_id,),
    ).fetchone() is not None or conn.execute(
        "SELECT 1 FROM safe_reclaims WHERE source_operation_id=?",
        (operation_id,),
    ).fetchone() is not None:
        failures.append(
            _failure(
                "safe_reclaim_successor_exists",
                "source operation already has a replacement lineage",
            )
        )

    pending_steps = conn.execute(
        "SELECT step_name FROM operation_steps WHERE operation_id=? AND completed_at IS NULL ORDER BY rowid",
        (operation_id,),
    ).fetchall()
    if pending_steps:
        failures.append(
            _failure(
                "safe_reclaim_incomplete_workflow_step",
                "an operation step is incomplete",
                steps=[row["step_name"] for row in pending_steps],
            )
        )
    unresolved_effects = conn.execute(
        """SELECT 'write' AS kind,attempt_id,outcome FROM write_attempts
             WHERE operation_id=? AND outcome IN ('started','uncertain')
           UNION ALL
           SELECT 'movement',attempt_id,outcome FROM movement_attempts
             WHERE operation_id=? AND outcome IN ('started','uncertain')""",
        (operation_id, operation_id),
    ).fetchall()
    if unresolved_effects:
        failures.append(
            _failure(
                "safe_reclaim_unresolved_external_effect",
                "an external effect lacks terminal applied/not-applied settlement",
                effects=[dict(row) for row in unresolved_effects],
            )
        )
    execution_claim = conn.execute(
        "SELECT claim_id,command FROM operation_execution_claims WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    if execution_claim is not None:
        failures.append(
            _failure(
                "safe_reclaim_execution_claim_live",
                "a consequential command still holds the operation execution claim",
                claim_id=execution_claim["claim_id"],
                command=execution_claim["command"],
            )
        )
    executions = unresolved_operation_executions(conn, operation_id)
    if executions:
        failures.append(
            _failure(
                "safe_reclaim_execution_unsettled",
                "a consequential command is running, pending, or uncertain",
                executions=[dict(row) for row in executions],
            )
        )
    requests = conn.execute(
        """SELECT request_id,command,status,resolved_at FROM service_requests
             WHERE operation_id=?
               AND (status='pending' OR (status='uncertain' AND resolved_at IS NULL))
             ORDER BY created_at""",
        (operation_id,),
    ).fetchall()
    if requests:
        failures.append(
            _failure(
                "safe_reclaim_request_unsettled",
                "a consequential service request is pending or uncertain",
                requests=[dict(row) for row in requests],
            )
        )
    proposals = conn.execute(
        """SELECT proposal_id,status FROM semantic_proposals
             WHERE operation_id=? AND status IN ('pending','approved','claimed')
             ORDER BY created_at""",
        (operation_id,),
    ).fetchall()
    if proposals:
        failures.append(
            _failure(
                "safe_reclaim_proposal_incomplete",
                "a semantic proposal/application is incomplete",
                proposals=[dict(row) for row in proposals],
            )
        )
    abandonment = conn.execute(
        """SELECT abandonment_id,status FROM abandonment_attempts
             WHERE source_operation_id=? AND status!='completed'
             ORDER BY created_at DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if abandonment is not None:
        failures.append(
            _failure(
                "safe_reclaim_abandonment_active",
                "formal abandonment is already active for this source operation",
                abandonment_id=abandonment["abandonment_id"],
                status=abandonment["status"],
            )
        )

    live = read_complete_task(
        backend, task_gid=operation["task_gid"], project_gid=COOKING_PROJECT_GID
    )
    registry = SectionRegistry.from_sections(backend.list_sections(COOKING_PROJECT_GID))
    stage, cycle_id, version_id, frontier_failures = _stage_and_baseline(
        conn, operation=operation, lease=lease, live=live, registry=registry
    )
    failures.extend(frontier_failures)
    return SafeReclaimEligibility(
        eligible=not failures,
        operation_id=operation_id,
        task_gid=operation["task_gid"],
        lease_id=lease["lease_id"],
        previous_owner_id=lease["owner_id"],
        previous_run_id=lease["run_id"],
        stage=stage,
        source_cycle_id=cycle_id,
        source_content_version_id=version_id,
        failed_clauses=tuple(failures),
    )


def _completed_change_intent(
    conn: sqlite3.Connection, operation_id: str
) -> dict[str, Mapping[str, Any]]:
    row = conn.execute(
        """SELECT intended_json FROM operation_steps
             WHERE operation_id=? AND step_name='change_intent' AND completed_at IS NOT NULL""",
        (operation_id,),
    ).fetchone()
    if row is None:
        return {}
    return {"change_intent": json.loads(row["intended_json"])}


def _copy_non_verifier_actor_facts(
    conn: sqlite3.Connection, *, source_operation_id: str, successor_operation_id: str, task_gid: str
) -> None:
    rows = conn.execute(
        """SELECT role,agent,run_id,independence_attestation,candidate_identity,source_cycle_id
             FROM operation_actor_facts WHERE operation_id=? AND role!='verifier'
             ORDER BY created_at,rowid""",
        (source_operation_id,),
    ).fetchall()
    for row in rows:
        record_actor_fact(
            conn,
            operation_id=successor_operation_id,
            task_gid=task_gid,
            role=row["role"],
            agent=row["agent"],
            run_id=row["run_id"],
            independence_attestation=row["independence_attestation"],
            candidate_identity=row["candidate_identity"],
            source_cycle_id=None,
        )


def _copy_unused_marco_authorizations(
    conn: sqlite3.Connection,
    *,
    source_operation_id: str,
    successor_operation_id: str,
    task_gid: str,
    reclaim_id: str,
    created_at: str,
) -> None:
    rows = conn.execute(
        """SELECT * FROM marco_authorizations
             WHERE task_gid=? AND operation_id=? AND consumed_at IS NULL
               AND (reserved_by_operation_id IS NULL OR reserved_by_operation_id=?)
             ORDER BY created_at,authorization_id""",
        (task_gid, source_operation_id, source_operation_id),
    ).fetchall()
    for row in rows:
        authorization_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO marco_authorizations(
                   authorization_id,task_gid,operation_id,field_name,before_json,
                   after_json,reason,actor_run_id,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                authorization_id,
                task_gid,
                successor_operation_id,
                row["field_name"],
                row["before_json"],
                row["after_json"],
                row["reason"],
                row["actor_run_id"],
                created_at,
            ),
        )
        record_audit(
            conn,
            submission_id=None,
            task_gid=task_gid,
            operation_id=successor_operation_id,
            event_type="marco.authorization",
            actor_agent=None,
            details={
                "authorization_id": authorization_id,
                "field": row["field_name"],
                "reason": row["reason"],
                "inherited_from_authorization_id": row["authorization_id"],
                "safe_reclaim_id": reclaim_id,
                "source_operation_id": source_operation_id,
            },
            result_code="OK",
            result_ok=True,
            governed_kind="decision",
            before_state={row["field_name"]: json.loads(row["before_json"])},
            after_state={row["field_name"]: json.loads(row["after_json"])},
            actor_run_id=row["actor_run_id"],
            actor_source="safe-reclaim",
        )


def _assert_reclaim_snapshot_unchanged(
    initial: SafeReclaimEligibility,
    current: SafeReclaimEligibility,
) -> None:
    """Require the commit-time predicate to describe the exact same frontier."""

    if not current.eligible:
        raise DishRuleError(
            "CONFLICT",
            "safe reclaim predicate changed before commit",
            rule="safe_reclaim_authority_changed",
            details={"eligibility": current.to_dict()},
        )
    fields = (
        "operation_id",
        "task_gid",
        "lease_id",
        "previous_owner_id",
        "previous_run_id",
        "stage",
        "source_cycle_id",
        "source_content_version_id",
    )
    changed = {
        field: {"initial": getattr(initial, field), "current": getattr(current, field)}
        for field in fields
        if getattr(initial, field) != getattr(current, field)
    }
    if changed:
        raise DishRuleError(
            "CONFLICT",
            "safe reclaim frontier changed before commit",
            rule="safe_reclaim_authority_changed",
            details={"changed": changed},
        )



def safe_reclaim_result_data(
    conn: sqlite3.Connection, *, request_id: str | None = None, reclaim_id: str | None = None
) -> dict[str, Any] | None:
    """Reconstruct the durable safe-reclaim result for exact response replay."""

    if bool(request_id) == bool(reclaim_id):
        raise ValueError("provide exactly one safe-reclaim identity")
    field = "request_id" if request_id else "reclaim_id"
    row = conn.execute(
        f"SELECT * FROM safe_reclaims WHERE {field}=?",
        (request_id or reclaim_id,),
    ).fetchone()
    if row is None:
        return None
    source = conn.execute(
        "SELECT operation_kind FROM operations WHERE operation_id=?",
        (row["source_operation_id"],),
    ).fetchone()
    if source is None:
        raise DishRuleError(
            "BACKEND_UNCERTAIN",
            "safe-reclaim lineage exists but its source operation is missing",
            rule="safe_reclaim_result_unprovable",
        )
    if row["stage"] == "verification":
        action = {
            "command": "start",
            "arguments": {
                "task_gid": row["task_gid"],
                "kind": "verification",
                "target_operation_id": row["successor_operation_id"],
                "target_cycle_id": row["successor_cycle_id"],
            },
        }
    else:
        start_arguments = {
            "task_gid": row["task_gid"],
            "kind": source["operation_kind"],
            "prepared_operation_id": row["successor_operation_id"],
        }
        if source["operation_kind"] == "change":
            intent = conn.execute(
                "SELECT intended_json FROM operation_steps "
                "WHERE operation_id=? AND step_name='change_intent' AND completed_at IS NOT NULL",
                (row["successor_operation_id"],),
            ).fetchone()
            if intent is None:
                raise DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "safe-reclaim change successor lacks its durable change intent",
                    rule="safe_reclaim_result_unprovable",
                )
            try:
                intended = json.loads(intent["intended_json"])
            except (TypeError, ValueError) as exc:
                raise DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "safe-reclaim change successor has invalid durable change intent",
                    rule="safe_reclaim_result_unprovable",
                ) from exc
            start_arguments["change_level"] = intended.get("level")
            start_arguments["change_reason"] = intended.get("reason")
        action = {"command": "start", "arguments": start_arguments}
    return {
        "task_gid": row["task_gid"],
        "safe_reclaim_id": row["reclaim_id"],
        "source_operation_id": row["source_operation_id"],
        "source_lease_id": row["source_lease_id"],
        "previous_owner_id": row["previous_owner_id"],
        "previous_run_id": row["previous_run_id"],
        "successor_operation_id": row["successor_operation_id"],
        "successor_cycle_id": row["successor_cycle_id"],
        "stage": row["stage"],
        "reason": row["reason"],
        "required_start_kind": action["arguments"]["kind"],
        "agent_action": action,
    }

def execute_safe_reclaim(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    lease_id: str,
    requested_owner_id: str,
    requested_run_id: str,
    requested_agent: str,
    request_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Atomically fence one clean inactive attempt and prepare its linked successor."""

    initial = safe_reclaim_eligibility(
        conn,
        backend,
        operation_id=operation_id,
        lease_id=lease_id,
        requested_owner_id=requested_owner_id,
        requested_run_id=requested_run_id,
        now=now,
    )
    if not initial.eligible:
        raise DishRuleError(
            "WRONG_STATE",
            "operation is not mechanically safe to reclaim",
            rule="safe_reclaim_not_eligible",
            details={"eligibility": initial.to_dict(), "required_admin_action": "inspect"},
        )
    assert initial.lease_id and initial.source_content_version_id and initial.stage

    reclaim_id = str(uuid.uuid4())
    successor_operation_id = str(uuid.uuid4())
    successor_content_version_id = str(uuid.uuid4())
    successor_cycle_id = str(uuid.uuid4()) if initial.stage == "verification" else None
    created_at = _stamp(now)
    reason = "expired_actor_lease" if conn.execute(
        "SELECT released_at FROM service_leases WHERE lease_id=?", (lease_id,)
    ).fetchone()[0] is None else "terminated_actor_lease"

    with immediate_transaction(conn, "execute_safe_reclaim"):
        revoked = operation_run_revocation(
            conn,
            operation_id=operation_id,
            owner_id=requested_owner_id,
            run_id=requested_run_id,
        )
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

        # Re-run the complete predicate while holding the SQLite writer lock.  This
        # revalidates every DB-owned eligibility fact and performs a final live-task
        # identity/placement read before the source can be terminalized.  Any drift
        # observed here rolls back without consuming the source operation.
        commit_eligibility = safe_reclaim_eligibility(
            conn,
            backend,
            operation_id=operation_id,
            lease_id=lease_id,
            requested_owner_id=requested_owner_id,
            requested_run_id=requested_run_id,
            now=now,
        )
        _assert_reclaim_snapshot_unchanged(initial, commit_eligibility)

        source = conn.execute(
            "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        source_version = conn.execute(
            "SELECT * FROM content_versions WHERE content_version_id=?",
            (commit_eligibility.source_content_version_id,),
        ).fetchone()
        source_cycle = None
        if commit_eligibility.source_cycle_id:
            source_cycle = conn.execute(
                "SELECT * FROM verification_cycles WHERE cycle_id=? AND operation_id=?",
                (commit_eligibility.source_cycle_id, operation_id),
            ).fetchone()
        if source is None or source_version is None:
            raise DishRuleError(
                "CONFLICT",
                "safe reclaim source evidence changed before commit",
                rule="safe_reclaim_source_changed",
            )

        cursor = conn.execute(
            """UPDATE operations SET status='cancelled',phase='terminal',terminal_outcome='safe_reclaimed',completed_at=?
                 WHERE operation_id=? AND status='open' AND phase!='terminal'""",
            (created_at, operation_id),
        )
        if cursor.rowcount != 1:
            raise DishRuleError("CONFLICT", "safe reclaim source changed before fencing", rule="safe_reclaim_source_changed")
        if source_cycle is not None:
            cursor = conn.execute(
                """UPDATE verification_cycles SET outcome='safe_reclaimed',completed_at=?
                     WHERE cycle_id=? AND operation_id=? AND completed_at IS NULL AND outcome IS NULL""",
                (created_at, source_cycle["cycle_id"], operation_id),
            )
            if cursor.rowcount != 1:
                raise DishRuleError("CONFLICT", "Verification attempt changed before safe reclaim", rule="safe_reclaim_cycle_changed")

        successor_phase = "await_verification" if initial.stage == "verification" else "prepare_required"
        claim_mode = "verifier" if initial.stage == "verification" else "stage_actor"
        conn.execute(
            """INSERT INTO operations(
                   operation_id,task_gid,operation_kind,status,editor_agent,researcher_agent,
                   verifier_agent,run_id,independence_attestation,expected_identity,schema_version,
                   expected_section_gid,phase,successor_claim_mode,created_at
               ) VALUES (?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?)""",
            (
                successor_operation_id,
                source["task_gid"],
                source["operation_kind"],
                source["editor_agent"] if initial.stage == "verification" else None,
                source["researcher_agent"] if initial.stage == "verification" else None,
                None,
                source["run_id"] if initial.stage == "verification" else None,
                None,
                source_version["identity"],
                source["schema_version"],
                source["expected_section_gid"],
                successor_phase,
                claim_mode,
                created_at,
            ),
        )
        conn.execute(
            """INSERT INTO content_versions(
                   content_version_id,task_gid,operation_id,boundary,identity,title,notes,confirmed,created_at
               ) VALUES (?,?,?,'successor_baseline',?,?,?,1,?)""",
            (
                successor_content_version_id,
                source["task_gid"],
                successor_operation_id,
                source_version["identity"],
                source_version["title"],
                source_version["notes"],
                created_at,
            ),
        )
        for step_name, intended in _completed_change_intent(conn, operation_id).items():
            declare_operation_step(conn, successor_operation_id, step_name, intended)
            complete_operation_step(conn, successor_operation_id, step_name)
        if initial.stage == "verification":
            assert source_cycle is not None and successor_cycle_id is not None
            next_cycle_number = conn.execute(
                "SELECT COALESCE(MAX(cycle_number),0)+1 FROM verification_cycles WHERE task_gid=?",
                (source["task_gid"],),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO verification_cycles(
                       cycle_id,operation_id,task_gid,cycle_number,protocol_release,protocol_text,created_at
                   ) VALUES (?,?,?,?,?,?,?)""",
                (
                    successor_cycle_id,
                    successor_operation_id,
                    source["task_gid"],
                    int(next_cycle_number),
                    source_cycle["protocol_release"],
                    source_cycle["protocol_text"],
                    created_at,
                ),
            )
            _copy_non_verifier_actor_facts(
                conn,
                source_operation_id=operation_id,
                successor_operation_id=successor_operation_id,
                task_gid=source["task_gid"],
            )
        _copy_unused_marco_authorizations(
            conn,
            source_operation_id=operation_id,
            successor_operation_id=successor_operation_id,
            task_gid=source["task_gid"],
            reclaim_id=reclaim_id,
            created_at=created_at,
        )
        lease = conn.execute(
            "SELECT * FROM service_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
        if lease is None:
            raise DishRuleError(
                "CONFLICT", "source lease changed before safe reclaim",
                rule="safe_reclaim_lease_changed",
            )
        if lease["released_at"] is None:
            cursor = conn.execute(
                """UPDATE service_leases SET released_at=?,release_reason='safe_reclaimed'
                     WHERE lease_id=? AND released_at IS NULL""",
                (created_at, lease_id),
            )
            if cursor.rowcount != 1:
                raise DishRuleError(
                    "CONFLICT", "source lease changed before safe reclaim",
                    rule="safe_reclaim_lease_changed",
                )
        conn.execute(
            """INSERT INTO safe_reclaims(
                   reclaim_id,task_gid,source_operation_id,request_id,source_lease_id,
                   previous_owner_id,previous_run_id,source_cycle_id,requested_owner_id,
                   requested_run_id,successor_operation_id,successor_cycle_id,
                   source_content_version_id,successor_content_version_id,stage,reason,status,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'prepared',?)""",
            (
                reclaim_id, source["task_gid"], operation_id, request_id, lease_id,
                initial.previous_owner_id, initial.previous_run_id, initial.source_cycle_id,
                requested_owner_id, requested_run_id, successor_operation_id, successor_cycle_id,
                initial.source_content_version_id, successor_content_version_id, initial.stage,
                reason, created_at,
            ),
        )
        record_audit(
            conn,
            submission_id=None,
            task_gid=source["task_gid"],
            operation_id=operation_id,
            event_type="operation.safe_reclaimed",
            actor_agent=requested_agent,
            actor_run_id=requested_run_id,
            actor_source="connected-agent",
            details={
                "safe_reclaim_id": reclaim_id,
                "source_lease_id": lease_id,
                "previous_owner_id": initial.previous_owner_id,
                "previous_run_id": initial.previous_run_id,
                "requested_owner_id": requested_owner_id,
                "requested_run_id": requested_run_id,
                "successor_operation_id": successor_operation_id,
                "successor_cycle_id": successor_cycle_id,
                "reason": reason,
            },
            result_code="OK",
            result_ok=True,
        )
        durable = safe_reclaim_result_data(conn, reclaim_id=reclaim_id)
    if durable is None:
        raise DishRuleError(
            "BACKEND_UNCERTAIN",
            "safe reclaim committed but its durable lineage could not be reread",
            rule="safe_reclaim_result_unprovable",
        )
    return durable
