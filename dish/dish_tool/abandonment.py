"""Internal clean-frontier policy for permanently abandoned agent attempts.

This module is deliberately not a command surface.  It classifies one already
persisted ``abandonment_attempts`` record from exact durable and live evidence,
and may settle only an already-committed route through the existing recovery
implementation.  Clean successor creation is owned by the later stage-specific
implementation.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .constants import COOKING_PROJECT_GID
from .database import (
    apply_operation_abandonment_succession_in_transaction,
    complete_abandonment_in_transaction,
    get_abandonment_attempt,
    mark_abandonment_awaiting_hold_in_transaction,
    mark_abandonment_blocked_in_transaction,
)
from .errors import DishRuleError
from .models import SectionRegistry
from .task_store import LiveTask, read_complete_task


@dataclass(frozen=True)
class AbandonmentFrontier:
    outcome: str
    stage: str
    source_operation_id: str
    source_cycle_id: str | None = None
    source_content_version_id: str | None = None
    recovery_required: bool = False
    completion_outcome: str | None = None
    continuation_operation_id: str | None = None
    continuation_cycle_id: str | None = None
    reason: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _assert_current_abandonment_authority(
    conn: sqlite3.Connection, abandonment: sqlite3.Row
) -> tuple[sqlite3.Row, sqlite3.Row]:
    operation = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?",
        (abandonment["source_operation_id"],),
    ).fetchone()
    lease = conn.execute(
        "SELECT * FROM service_leases WHERE lease_id=?",
        (abandonment["source_lease_id"],),
    ).fetchone()
    if operation is None or lease is None:
        raise DishRuleError(
            "CONFLICT",
            "abandonment source authority is no longer available",
            rule="abandonment_authority_changed",
        )
    if (
        operation["task_gid"] != abandonment["task_gid"]
        or operation["status"] not in {"open", "uncertain", "completed"}
        or (operation["status"] == "completed" and operation["phase"] != "terminal")
        or (operation["status"] in {"open", "uncertain"} and operation["phase"] == "terminal")
        or lease["operation_id"] != operation["operation_id"]
        or lease["task_gid"] != operation["task_gid"]
        or lease["owner_id"] != abandonment["abandoned_owner_id"]
        or lease["run_id"] != abandonment["abandoned_run_id"]
        or lease["lease_kind"] != "actor"
        or lease["context_cycle_id"] != abandonment["attempt_cycle_id"]
    ):
        raise DishRuleError(
            "CONFLICT",
            "abandonment no longer names the exact active actor attempt",
            rule="abandonment_authority_changed",
        )
    later = conn.execute(
        """SELECT lease_id FROM service_leases
             WHERE task_gid=? AND lease_kind='actor'
               AND actor_attempt_seq>?
             ORDER BY actor_attempt_seq LIMIT 1""",
        (operation["task_gid"], lease["actor_attempt_seq"]),
    ).fetchone()
    successor = conn.execute(
        "SELECT successor_operation_id FROM operation_successions WHERE source_operation_id=?",
        (operation["operation_id"],),
    ).fetchone()
    if later is not None or successor is not None:
        raise DishRuleError(
            "CONFLICT",
            "a later actor attempt or successor already exists",
            rule="abandonment_attempt_superseded",
        )
    return operation, lease


def _pending_steps(conn: sqlite3.Connection, operation_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM operation_steps
             WHERE operation_id=? AND completed_at IS NULL
             ORDER BY rowid""",
        (operation_id,),
    ).fetchall()


def _unresolved_effects(conn: sqlite3.Connection, operation_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT 'write' AS effect_kind, attempt_id, expected_identity,
                  intended_identity, intended_title, intended_notes,
                  NULL AS expected_section_gid, NULL AS intended_section_gid
             FROM write_attempts
            WHERE operation_id=? AND outcome IN ('started','uncertain')
           UNION ALL
           SELECT 'movement' AS effect_kind, attempt_id, NULL, NULL, NULL, NULL,
                  expected_section_gid, intended_section_gid
             FROM movement_attempts
            WHERE operation_id=? AND outcome IN ('started','uncertain')
           ORDER BY effect_kind, attempt_id""",
        (operation_id, operation_id),
    ).fetchall()


def _preconstruction_hold(conn: sqlite3.Connection, operation: sqlite3.Row) -> bool:
    if (
        operation["operation_kind"] != "initial"
        or operation["phase"] not in {"held_evidence", "held_human"}
        or operation["content_write_completed_at"] is not None
    ):
        return False
    return conn.execute(
        """SELECT 1 FROM operation_steps
             WHERE operation_id=?
               AND step_name='research_preconstruction_hold'
               AND completed_at IS NOT NULL""",
        (operation["operation_id"],),
    ).fetchone() is not None


def _confirmed_version_for_identity(
    conn: sqlite3.Connection, *, task_gid: str, identity: str
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM content_versions
             WHERE task_gid=? AND identity=? AND confirmed=1
             ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (task_gid, identity),
    ).fetchone()


def _verification_stage(
    conn: sqlite3.Connection,
    *,
    abandonment: sqlite3.Row,
    operation: sqlite3.Row,
) -> tuple[sqlite3.Row, sqlite3.Row | None]:
    cycle_id = abandonment["attempt_cycle_id"]
    if not cycle_id:
        raise DishRuleError(
            "CONFLICT",
            "Verification abandonment lacks exact cycle context",
            rule="abandonment_cycle_unprovable",
        )
    cycle = conn.execute(
        "SELECT * FROM verification_cycles WHERE cycle_id=? AND operation_id=?",
        (cycle_id, operation["operation_id"]),
    ).fetchone()
    if (
        cycle is None
        or cycle["task_gid"] != operation["task_gid"]
        or cycle["run_id"] != abandonment["abandoned_run_id"]
    ):
        raise DishRuleError(
            "CONFLICT",
            "Verification abandonment cycle does not match the abandoned run",
            rule="abandonment_cycle_unprovable",
        )
    current = conn.execute(
        """SELECT * FROM verification_cycles
             WHERE operation_id=? AND completed_at IS NULL
             ORDER BY cycle_number DESC LIMIT 1""",
        (operation["operation_id"],),
    ).fetchone()
    return cycle, current


def _blocked(
    *,
    stage: str,
    operation: sqlite3.Row,
    cycle_id: str | None,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> AbandonmentFrontier:
    return AbandonmentFrontier(
        outcome="blocked_manual_reconciliation",
        stage=stage,
        source_operation_id=operation["operation_id"],
        source_cycle_id=cycle_id,
        reason=reason,
        details=dict(details or {}),
    )


def _effects_prove_applied(effects: list[sqlite3.Row], live: LiveTask) -> bool:
    for effect in effects:
        if effect["effect_kind"] == "write":
            if (
                live.identity != effect["intended_identity"]
                or live.title != effect["intended_title"]
                or live.notes != effect["intended_notes"]
            ):
                return False
        elif live.section_gid != effect["intended_section_gid"]:
            return False
    return True


def _step_intent_matches_live(step: sqlite3.Row, live: LiveTask) -> bool:
    name = step["step_name"]
    intended = json.loads(step["intended_json"])
    if name in {
        "planning_write",
        "candidate_write",
        "handoff_validation",
        "signoff_write",
    } or name.startswith("route_write:"):
        return live.title == intended.get("title") and live.notes == intended.get("notes")
    if name in {"planning_handoff", "verification_handoff"}:
        return live.section_gid == intended.get("section_gid")
    if name in {"submission_terminal_intent", "submission_terminal"}:
        return (
            live.identity == intended.get("effective_identity")
            and live.section_gid == intended.get("section_gid")
        )
    return True


def _committed_recovery_supported(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    stage: str,
    steps: list[sqlite3.Row],
    effects: list[sqlite3.Row],
    live: LiveTask,
) -> bool:
    names = {row["step_name"] for row in steps}
    if not names:
        return False
    all_steps = {
        row["step_name"]: row
        for row in conn.execute(
            "SELECT * FROM operation_steps WHERE operation_id=? ORDER BY rowid",
            (operation_id,),
        ).fetchall()
    }
    if stage == "planning":
        allowed = {"planning_write", "planning_handoff", "planning_terminal"}
        if not names <= allowed or "planning_terminal" not in names:
            return False
        required_evidence = [
            all_steps.get("planning_write"),
            all_steps.get("planning_handoff"),
        ]
    elif stage == "research":
        verification = {
            "candidate_write",
            "handoff_validation",
            "verification_cycle",
            "verification_handoff",
            "verification_phase",
        }
        non_material = {"candidate_write", "handoff_validation", "non_material_terminal"}
        if names <= verification and "verification_phase" in names:
            required_evidence = [
                all_steps.get("candidate_write"),
                all_steps.get("handoff_validation"),
                all_steps.get("verification_handoff"),
            ]
        elif names <= non_material and "non_material_terminal" in names:
            required_evidence = [
                all_steps.get("candidate_write"),
                all_steps.get("handoff_validation"),
            ]
        else:
            return False
    else:
        route_names = all(
            name.startswith(
                (
                    "route_write:",
                    "route_actor:",
                    "route_cycle_finalize:",
                    "route_new_cycle:",
                    "route_phase:",
                )
            )
            for name in names
        )
        submission_names = names <= {"submission_terminal_intent", "submission_terminal"}
        if route_names and any(name.startswith("route_phase:") for name in names):
            suffixes = {name.split(":", 1)[1] for name in names}
            required_evidence = [
                all_steps.get(f"route_write:{suffix}") for suffix in suffixes
            ]
        elif submission_names and "submission_terminal" in names:
            required_evidence = [
                all_steps.get("submission_terminal_intent")
                or all_steps.get("submission_terminal")
            ]
        else:
            return False
    if any(step is None for step in required_evidence):
        return False
    if not _effects_prove_applied(effects, live):
        return False
    return all(_step_intent_matches_live(step, live) for step in required_evidence)


def classify_abandonment_frontier(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    abandonment_id: str,
) -> AbandonmentFrontier:
    """Classify one persisted abandonment without changing durable state."""

    abandonment = get_abandonment_attempt(conn, abandonment_id)
    if abandonment["status"] not in {
        "started",
        "blocked_manual_reconciliation",
        "awaiting_hold_resolution",
    }:
        raise DishRuleError(
            "WRONG_STATE",
            "abandonment is not at a classifiable frontier",
            rule="abandonment_not_classifiable",
            details={"status": abandonment["status"]},
        )
    operation, _lease = _assert_current_abandonment_authority(conn, abandonment)
    live = read_complete_task(
        backend, task_gid=operation["task_gid"], project_gid=COOKING_PROJECT_GID
    )
    registry = SectionRegistry.from_sections(backend.list_sections(COOKING_PROJECT_GID))
    steps = _pending_steps(conn, operation["operation_id"])
    effects = _unresolved_effects(conn, operation["operation_id"])

    stage = (
        "planning"
        if operation["operation_kind"] == "planning"
        else "verification"
        if abandonment["attempt_cycle_id"] is not None
        else "research"
    )

    # Existing committed recovery can finish before the abandonment command
    # records its own result (for example after process loss between the two
    # local commits).  Treat the already-terminal source as committed success,
    # not as an authority mismatch or a reason to repeat recovery.
    if operation["status"] == "completed" and operation["phase"] == "terminal":
        return AbandonmentFrontier(
            outcome="committed_finalized",
            stage=stage,
            source_operation_id=operation["operation_id"],
            source_cycle_id=abandonment["attempt_cycle_id"],
            completion_outcome="committed_finalized",
            reason="source route was already durably finalized",
            details={"terminal_outcome": operation["terminal_outcome"]},
        )

    if stage == "research" and _preconstruction_hold(conn, operation):
        if effects or steps:
            return _blocked(
                stage=stage,
                operation=operation,
                cycle_id=None,
                reason="pre-construction hold has unresolved local or external work",
                details={
                    "pending_steps": [row["step_name"] for row in steps],
                    "unresolved_effects": [row["attempt_id"] for row in effects],
                },
            )
        if (
            live.identity == operation["expected_identity"]
            and live.section_gid == operation["expected_section_gid"]
        ):
            version = _confirmed_version_for_identity(
                conn,
                task_gid=operation["task_gid"],
                identity=live.identity,
            )
            return AbandonmentFrontier(
                outcome="awaiting_hold_resolution",
                stage=stage,
                source_operation_id=operation["operation_id"],
                source_content_version_id=(
                    None if version is None else version["content_version_id"]
                ),
                reason="pre-construction Research hold remains authoritative",
            )
        return _blocked(
            stage=stage,
            operation=operation,
            cycle_id=None,
            reason="pre-construction hold baseline or placement changed",
        )

    if stage == "research" and operation["phase"] == "await_verification":
        continuation = conn.execute(
            """SELECT * FROM verification_cycles
                 WHERE operation_id=? AND completed_at IS NULL
                 ORDER BY cycle_number DESC LIMIT 1""",
            (operation["operation_id"],),
        ).fetchone()
        head = conn.execute(
            "SELECT last_confirmed_identity FROM task_content_state WHERE task_gid=?",
            (operation["task_gid"],),
        ).fetchone()
        if (
            continuation is not None
            and continuation["run_id"] is None
            and not steps
            and not effects
            and head is not None
            and live.identity == head["last_confirmed_identity"]
            and live.section_gid == registry.verification_queue_gid
        ):
            return AbandonmentFrontier(
                outcome="committed_finalized",
                stage=stage,
                source_operation_id=operation["operation_id"],
                completion_outcome="route_preserved",
                continuation_operation_id=operation["operation_id"],
                continuation_cycle_id=continuation["cycle_id"],
                reason="confirmed Research handoff already exposes an independent Verification continuation",
            )

    cycle = None
    current_cycle = None
    if stage == "verification":
        cycle, current_cycle = _verification_stage(
            conn, abandonment=abandonment, operation=operation
        )
        if cycle["completed_at"] is not None:
            if cycle["outcome"] in {"rejected", "two-pass-hold"}:
                if cycle["hold_identity"] is not None:
                    if (
                        live.identity != cycle["hold_identity"]
                        or live.section_gid != cycle["hold_section_gid"]
                    ):
                        return _blocked(
                            stage=stage,
                            operation=operation,
                            cycle_id=cycle["cycle_id"],
                            reason="committed hold route no longer matches live state",
                        )
                    return AbandonmentFrontier(
                        outcome="committed_finalized",
                        stage=stage,
                        source_operation_id=operation["operation_id"],
                        source_cycle_id=cycle["cycle_id"],
                        completion_outcome="route_preserved",
                        reason="committed Verification hold route is preserved",
                    )
                if current_cycle is not None and not steps and not effects:
                    return AbandonmentFrontier(
                        outcome="committed_finalized",
                        stage=stage,
                        source_operation_id=operation["operation_id"],
                        source_cycle_id=cycle["cycle_id"],
                        completion_outcome="route_preserved",
                        continuation_operation_id=operation["operation_id"],
                        continuation_cycle_id=current_cycle["cycle_id"],
                        reason="committed rejection route already created its continuation cycle",
                    )
            return _blocked(
                stage=stage,
                operation=operation,
                cycle_id=cycle["cycle_id"],
                reason="completed Verification decision has no safe launch continuation",
                details={"cycle_outcome": cycle["outcome"]},
            )

    if not steps and not effects:
        if stage in {"planning", "research"}:
            clean = (
                operation["phase"] == "prepare_required"
                and live.identity == operation["expected_identity"]
                and live.section_gid == operation["expected_section_gid"]
            )
            identity = operation["expected_identity"]
        else:
            assert cycle is not None
            candidate = (
                cycle["reviewed_identity"]
                or conn.execute(
                    "SELECT last_confirmed_identity FROM task_content_state WHERE task_gid=?",
                    (operation["task_gid"],),
                ).fetchone()[0]
            )
            decision_steps = conn.execute(
                """SELECT 1 FROM operation_steps
                     WHERE operation_id=? AND (
                         step_name LIKE 'small_%'
                         OR step_name LIKE 'route_%'
                         OR step_name IN ('signoff_write','signoff_finalize')
                     ) LIMIT 1""",
                (operation["operation_id"],),
            ).fetchone()
            clean = (
                cycle["completed_at"] is None
                and operation["phase"] == "await_verification"
                and decision_steps is None
                and live.identity == candidate
                and live.section_gid == registry.verification_queue_gid
            )
            identity = candidate
        if clean:
            version = _confirmed_version_for_identity(
                conn, task_gid=operation["task_gid"], identity=identity
            )
            if version is None:
                return _blocked(
                    stage=stage,
                    operation=operation,
                    cycle_id=None if cycle is None else cycle["cycle_id"],
                    reason="safe live frontier lacks a confirmed content version",
                )
            return AbandonmentFrontier(
                outcome="restart_prepared",
                stage=stage,
                source_operation_id=operation["operation_id"],
                source_cycle_id=None if cycle is None else cycle["cycle_id"],
                source_content_version_id=version["content_version_id"],
                reason="live task matches the exact clean restart frontier",
            )

    if _committed_recovery_supported(
        conn,
        operation_id=operation["operation_id"],
        stage=stage,
        steps=steps,
        effects=effects,
        live=live,
    ):
        return AbandonmentFrontier(
            outcome="committed_finalized",
            stage=stage,
            source_operation_id=operation["operation_id"],
            source_cycle_id=None if cycle is None else cycle["cycle_id"],
            recovery_required=True,
            completion_outcome="committed_finalized",
            reason="exact live evidence proves the existing committed recovery suffix",
            details={"pending_steps": [row["step_name"] for row in steps]},
        )

    return _blocked(
        stage=stage,
        operation=operation,
        cycle_id=None if cycle is None else cycle["cycle_id"],
        reason="attempt is not at a supported clean or committed frontier",
        details={
            "pending_steps": [row["step_name"] for row in steps],
            "unresolved_effects": [
                {"kind": row["effect_kind"], "attempt_id": row["attempt_id"]}
                for row in effects
            ],
            "live_identity": live.identity,
            "live_section_gid": live.section_gid,
        },
    )


def _completed_change_intent(
    conn: sqlite3.Connection, operation_id: str
) -> dict[str, Mapping[str, Any]]:
    row = conn.execute(
        """SELECT intended_json FROM operation_steps
             WHERE operation_id=? AND step_name='change_intent'
               AND completed_at IS NOT NULL""",
        (operation_id,),
    ).fetchone()
    if row is None:
        return {}
    try:
        intended = json.loads(row["intended_json"])
    except (TypeError, ValueError) as exc:
        raise DishRuleError(
            "CONFLICT",
            "completed Change intent is not reconstructable",
            rule="change_intent_invalid",
        ) from exc
    return {"change_intent": intended}


def _prepare_stage_successor(
    conn: sqlite3.Connection,
    *,
    abandonment_id: str,
    frontier: AbandonmentFrontier,
) -> dict[str, Any]:
    if frontier.stage not in {"planning", "research"}:
        raise DishRuleError(
            "WRONG_STATE",
            "this implementation stage prepares only Planning and Research successors",
            rule="abandonment_stage_not_implemented",
        )
    source = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?",
        (frontier.source_operation_id,),
    ).fetchone()
    if source is None or frontier.source_content_version_id is None:
        raise DishRuleError(
            "CONFLICT",
            "clean abandonment frontier lacks exact source evidence",
            rule="abandonment_source_baseline_invalid",
        )
    successor_operation_id = str(uuid.uuid4())
    successor_content_version_id = str(uuid.uuid4())
    succession_id = str(uuid.uuid4())
    start_kind = source["operation_kind"]
    action = {
        "surface": "connected-agent",
        "command": "start",
        "arguments": {
            "task_gid": source["task_gid"],
            "kind": start_kind,
            "prepared_operation_id": successor_operation_id,
        },
    }
    result = {
        "abandonment_id": abandonment_id,
        "classification": frontier.to_dict(),
        "succession_id": succession_id,
        "successor_operation_id": successor_operation_id,
        "successor_cycle_id": None,
        "required_action": action,
    }
    completed_steps = (
        _completed_change_intent(conn, source["operation_id"])
        if source["operation_kind"] == "change"
        else {}
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        abandonment, successor, succession = (
            apply_operation_abandonment_succession_in_transaction(
                conn,
                abandonment_id=abandonment_id,
                succession_id=succession_id,
                successor_operation_id=successor_operation_id,
                source_content_version_id=frontier.source_content_version_id,
                successor_content_version_id=successor_content_version_id,
                successor_operation_kind=source["operation_kind"],
                successor_phase="prepare_required",
                successor_expected_section_gid=source["expected_section_gid"],
                successor_schema_version=source["schema_version"],
                successor_claim_mode="stage_actor",
                transition_reason="permanent_agent_run_abandonment",
                candidate_transfer_kind="restored_stage_baseline",
                successor_completed_steps=completed_steps,
                result=result,
            )
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    result["abandonment"] = {key: abandonment[key] for key in abandonment.keys()}
    result["successor"] = {key: successor[key] for key in successor.keys()}
    result["succession"] = {key: succession[key] for key in succession.keys()}
    return result


def resolve_preconstruction_hold_to_successor(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    resolution: Mapping[str, Any],
    live_identity: str,
    live_section_gid: str,
) -> dict[str, Any] | None:
    """Resolve an abandoned pre-construction hold into a fresh Research attempt."""

    abandonment = conn.execute(
        """SELECT * FROM abandonment_attempts
             WHERE source_operation_id=? AND status='awaiting_hold_resolution'
             ORDER BY created_at DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if abandonment is None:
        return None
    source = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if source is None or source["operation_kind"] != "initial":
        raise DishRuleError(
            "CONFLICT",
            "abandonment hold source is not an initial Research attempt",
            rule="abandonment_hold_source_invalid",
        )
    if (
        live_identity != source["expected_identity"]
        or live_section_gid != source["expected_section_gid"]
    ):
        raise DishRuleError(
            "CONFLICT",
            "live task changed while the abandoned Research hold was pending",
            rule="preconstruction_hold_baseline_drift",
        )
    source_version = _confirmed_version_for_identity(
        conn, task_gid=source["task_gid"], identity=live_identity
    )
    if source_version is None:
        raise DishRuleError(
            "CONFLICT",
            "abandoned Research hold lacks a confirmed baseline",
            rule="abandonment_source_baseline_invalid",
        )
    successor_operation_id = str(uuid.uuid4())
    successor_content_version_id = str(uuid.uuid4())
    succession_id = str(uuid.uuid4())
    action = {
        "surface": "connected-agent",
        "command": "start",
        "arguments": {
            "task_gid": source["task_gid"],
            "kind": "initial",
            "prepared_operation_id": successor_operation_id,
        },
    }
    result = {
        "operation_id": successor_operation_id,
        "source_operation_id": operation_id,
        **dict(resolution),
        "phase": "prepare_required",
        "abandonment_id": abandonment["abandonment_id"],
        "succession_id": succession_id,
        "required_action": action,
    }
    conn.execute("BEGIN IMMEDIATE")
    try:
        from .database import complete_operation_step, declare_operation_step

        declare_operation_step(
            conn, operation_id, "research_preconstruction_hold_resolution", resolution
        )
        complete_operation_step(
            conn, operation_id, "research_preconstruction_hold_resolution"
        )
        from .database import record_audit

        record_audit(
            conn,
            submission_id=None,
            task_gid=source["task_gid"],
            operation_id=operation_id,
            event_type="research.preconstruction_resolved",
            actor_agent=None,
            details={**dict(resolution), "abandonment_id": abandonment["abandonment_id"]},
            result_code="OK",
            result_ok=True,
            governed_kind="decision",
            before_state={"phase": source["phase"], "candidate_content_existed": False},
            after_state={
                "phase": "terminal",
                "resume_status": "pending-research",
                "successor_operation_id": successor_operation_id,
            },
            actor_source="marco-hold-resolution",
        )
        apply_operation_abandonment_succession_in_transaction(
            conn,
            abandonment_id=abandonment["abandonment_id"],
            succession_id=succession_id,
            successor_operation_id=successor_operation_id,
            source_content_version_id=source_version["content_version_id"],
            successor_content_version_id=successor_content_version_id,
            successor_operation_kind="initial",
            successor_phase="prepare_required",
            successor_expected_section_gid=source["expected_section_gid"],
            successor_schema_version=source["schema_version"],
            successor_claim_mode="stage_actor",
            transition_reason="resolved_abandoned_preconstruction_hold",
            candidate_transfer_kind="restored_stage_baseline",
            result=result,
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return result


def settle_abandonment_frontier(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    abandonment_id: str,
    reason: str,
) -> dict[str, Any]:
    """Persist a safe frontier and create clean Planning/Research successors.

    Verification successor construction remains deferred to the next stage.
    This function performs no compensating external write or movement.
    """

    frontier = classify_abandonment_frontier(
        conn, backend, abandonment_id=abandonment_id
    )
    result = {"abandonment_id": abandonment_id, "classification": frontier.to_dict()}

    if frontier.outcome == "restart_prepared":
        if frontier.stage in {"planning", "research"}:
            return _prepare_stage_successor(
                conn, abandonment_id=abandonment_id, frontier=frontier
            )
        return result
    if frontier.outcome == "blocked_manual_reconciliation":
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = mark_abandonment_blocked_in_transaction(
                conn, abandonment_id=abandonment_id, result=result
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        result["abandonment"] = {key: row[key] for key in row.keys()}
        return result
    if frontier.outcome == "awaiting_hold_resolution":
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = mark_abandonment_awaiting_hold_in_transaction(
                conn, abandonment_id=abandonment_id, result=result
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        result["abandonment"] = {key: row[key] for key in row.keys()}
        return result

    recovery = None
    if frontier.recovery_required:
        from .step9 import recover_operation

        recovery = recover_operation(
            conn,
            backend,
            operation_id=frontier.source_operation_id,
            requested_outcome="applied",
            reason=reason,
        )
        # Re-read the resulting route.  Recovery is allowed only to finish the
        # exact existing suffix; it may not leave another pending local intent.
        pending = _pending_steps(conn, frontier.source_operation_id)
        effects = _unresolved_effects(conn, frontier.source_operation_id)
        if pending or effects:
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "committed abandonment recovery did not reach a stable frontier",
                rule="abandonment_recovery_incomplete",
                retryable=False,
            )

    operation = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?",
        (frontier.source_operation_id,),
    ).fetchone()
    continuation_operation_id = frontier.continuation_operation_id
    continuation_cycle_id = frontier.continuation_cycle_id
    completion_outcome = frontier.completion_outcome or "committed_finalized"
    if operation["status"] == "open" and operation["phase"] == "await_verification":
        next_cycle = conn.execute(
            """SELECT * FROM verification_cycles
                 WHERE operation_id=? AND completed_at IS NULL
                 ORDER BY cycle_number DESC LIMIT 1""",
            (operation["operation_id"],),
        ).fetchone()
        if next_cycle is not None and next_cycle["run_id"] is None:
            continuation_operation_id = operation["operation_id"]
            continuation_cycle_id = next_cycle["cycle_id"]
            completion_outcome = "route_preserved"
    elif operation["status"] == "open" and operation["phase"] in {
        "held_evidence",
        "held_human",
    }:
        completion_outcome = "route_preserved"
    elif operation["status"] not in {"completed", "cancelled"}:
        raise DishRuleError(
            "CONFLICT",
            "committed recovery did not reach a terminal or independent continuation",
            rule="abandonment_committed_route_incomplete",
            details={"status": operation["status"], "phase": operation["phase"]},
        )

    result.update(
        {
            "recovery": recovery,
            "continuation_operation_id": continuation_operation_id,
            "continuation_cycle_id": continuation_cycle_id,
        }
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = complete_abandonment_in_transaction(
            conn,
            abandonment_id=abandonment_id,
            outcome=completion_outcome,
            result=result,
            continuation_operation_id=continuation_operation_id,
            continuation_cycle_id=continuation_cycle_id,
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    result["abandonment"] = {key: row[key] for key in row.keys()}
    return result
