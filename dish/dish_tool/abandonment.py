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
    finalize_confirmed_movement_attempt,
    finalize_confirmed_write_attempt,
    finalize_not_applied_movement_attempt,
    finalize_not_applied_write_attempt,
    get_abandonment_attempt,
    mark_abandonment_awaiting_hold_in_transaction,
    mark_abandonment_blocked_in_transaction,
    record_audit,
)
from .errors import DishRuleError
from .models import SectionRegistry, utc_now
from .transactions import immediate_transaction
from .task_store import LiveTask, move_exact, read_complete_task, write_exact_content


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



def _abandonment_stage(abandonment, operation) -> str:
    if operation["operation_kind"] == "planning":
        return "planning"
    return "verification" if abandonment["attempt_cycle_id"] is not None else "research"


def _terminal_abandonment_frontier(
    *, abandonment, operation, stage: str
) -> AbandonmentFrontier | None:
    if operation["status"] != "completed" or operation["phase"] != "terminal":
        return None
    return AbandonmentFrontier(
        outcome="committed_finalized",
        stage=stage,
        source_operation_id=operation["operation_id"],
        source_cycle_id=abandonment["attempt_cycle_id"],
        completion_outcome="committed_finalized",
        reason="source route was already durably finalized",
        details={"terminal_outcome": operation["terminal_outcome"]},
    )


def _preconstruction_hold_frontier(
    conn: sqlite3.Connection,
    *,
    operation,
    live,
    stage: str,
    steps,
    effects,
) -> AbandonmentFrontier:
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
            conn, task_gid=operation["task_gid"], identity=live.identity
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


def _research_handoff_frontier(
    conn: sqlite3.Connection,
    *,
    operation,
    live,
    registry: SectionRegistry,
    steps,
    effects,
) -> AbandonmentFrontier | None:
    if operation["phase"] != "await_verification":
        return None
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
    if not (
        continuation is not None
        and continuation["run_id"] is None
        and not steps
        and not effects
        and head is not None
        and live.identity == head["last_confirmed_identity"]
        and live.section_gid == registry.verification_queue_gid
    ):
        return None
    return AbandonmentFrontier(
        outcome="committed_finalized",
        stage="research",
        source_operation_id=operation["operation_id"],
        completion_outcome="route_preserved",
        continuation_operation_id=operation["operation_id"],
        continuation_cycle_id=continuation["cycle_id"],
        reason="confirmed Research handoff already exposes an independent Verification continuation",
    )


def _completed_verification_frontier(
    *,
    operation,
    live,
    cycle,
    current_cycle,
    steps,
    effects,
) -> AbandonmentFrontier | None:
    if cycle["completed_at"] is None:
        return None
    if cycle["outcome"] in {"rejected", "two-pass-hold"}:
        if cycle["hold_identity"] is not None:
            if (
                live.identity != cycle["hold_identity"]
                or live.section_gid != cycle["hold_section_gid"]
            ):
                return _blocked(
                    stage="verification",
                    operation=operation,
                    cycle_id=cycle["cycle_id"],
                    reason="committed hold route no longer matches live state",
                )
            return AbandonmentFrontier(
                outcome="committed_finalized",
                stage="verification",
                source_operation_id=operation["operation_id"],
                source_cycle_id=cycle["cycle_id"],
                completion_outcome="route_preserved",
                reason="committed Verification hold route is preserved",
            )
        if current_cycle is not None and not steps and not effects:
            return AbandonmentFrontier(
                outcome="committed_finalized",
                stage="verification",
                source_operation_id=operation["operation_id"],
                source_cycle_id=cycle["cycle_id"],
                completion_outcome="route_preserved",
                continuation_operation_id=operation["operation_id"],
                continuation_cycle_id=current_cycle["cycle_id"],
                reason="committed rejection route already created its continuation cycle",
            )
    return _blocked(
        stage="verification",
        operation=operation,
        cycle_id=cycle["cycle_id"],
        reason="completed Verification decision has no safe launch continuation",
        details={"cycle_outcome": cycle["outcome"]},
    )


def _clean_restart_frontier(
    conn: sqlite3.Connection,
    *,
    operation,
    live,
    registry: SectionRegistry,
    stage: str,
    cycle,
) -> AbandonmentFrontier | None:
    if stage in {"planning", "research"}:
        clean = (
            operation["phase"] == "prepare_required"
            and live.identity == operation["expected_identity"]
            and live.section_gid == operation["expected_section_gid"]
        )
        identity = operation["expected_identity"]
    else:
        assert cycle is not None
        head = conn.execute(
            "SELECT last_confirmed_identity FROM task_content_state WHERE task_gid=?",
            (operation["task_gid"],),
        ).fetchone()
        candidate = cycle["reviewed_identity"] or head[0]
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
    if not clean:
        return None
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


def _committed_or_blocked_frontier(
    conn: sqlite3.Connection,
    *,
    operation,
    live,
    stage: str,
    cycle,
    steps,
    effects,
) -> AbandonmentFrontier:
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
    registry = SectionRegistry.from_sections(
        backend.list_sections(COOKING_PROJECT_GID)
    )
    steps = _pending_steps(conn, operation["operation_id"])
    effects = _unresolved_effects(conn, operation["operation_id"])
    stage = _abandonment_stage(abandonment, operation)

    terminal = _terminal_abandonment_frontier(
        abandonment=abandonment, operation=operation, stage=stage
    )
    if terminal is not None:
        return terminal
    if stage == "research" and _preconstruction_hold(conn, operation):
        return _preconstruction_hold_frontier(
            conn,
            operation=operation,
            live=live,
            stage=stage,
            steps=steps,
            effects=effects,
        )
    if stage == "research":
        handoff = _research_handoff_frontier(
            conn,
            operation=operation,
            live=live,
            registry=registry,
            steps=steps,
            effects=effects,
        )
        if handoff is not None:
            return handoff

    cycle = None
    if stage == "verification":
        cycle, current_cycle = _verification_stage(
            conn, abandonment=abandonment, operation=operation
        )
        completed = _completed_verification_frontier(
            operation=operation,
            live=live,
            cycle=cycle,
            current_cycle=current_cycle,
            steps=steps,
            effects=effects,
        )
        if completed is not None:
            return completed
    if not steps and not effects:
        clean = _clean_restart_frontier(
            conn,
            operation=operation,
            live=live,
            registry=registry,
            stage=stage,
            cycle=cycle,
        )
        if clean is not None:
            return clean
    return _committed_or_blocked_frontier(
        conn,
        operation=operation,
        live=live,
        stage=stage,
        cycle=cycle,
        steps=steps,
        effects=effects,
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
    with immediate_transaction(conn, "_prepare_stage_successor"):
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
    result["abandonment"] = {key: abandonment[key] for key in abandonment.keys()}
    result["successor"] = {key: successor[key] for key in successor.keys()}
    result["succession"] = {key: succession[key] for key in succession.keys()}
    return result


def _successor_actor_facts(
    conn: sqlite3.Connection, source_operation_id: str
) -> list[dict[str, Any]]:
    """Copy exact producer lineage without inheriting verifier authority."""

    rows = conn.execute(
        """SELECT role,agent,run_id,independence_attestation,
                  candidate_identity,source_cycle_id
             FROM operation_actor_facts
            WHERE operation_id=? AND role!='verifier'
            ORDER BY created_at, rowid""",
        (source_operation_id,),
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def _prepare_verification_successor(
    conn: sqlite3.Connection,
    *,
    abandonment_id: str,
    frontier: AbandonmentFrontier,
) -> dict[str, Any]:
    if frontier.stage != "verification":
        raise DishRuleError(
            "WRONG_STATE",
            "this successor path requires a Verification abandonment frontier",
            rule="abandonment_stage_mismatch",
        )
    if frontier.source_cycle_id is None or frontier.source_content_version_id is None:
        raise DishRuleError(
            "CONFLICT",
            "clean Verification abandonment lacks exact cycle or candidate evidence",
            rule="abandonment_verification_evidence_invalid",
        )
    source = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?",
        (frontier.source_operation_id,),
    ).fetchone()
    source_cycle = conn.execute(
        """SELECT * FROM verification_cycles
            WHERE cycle_id=? AND operation_id=?""",
        (frontier.source_cycle_id, frontier.source_operation_id),
    ).fetchone()
    if (
        source is None
        or source_cycle is None
        or source_cycle["completed_at"] is not None
        or not source_cycle["protocol_release"]
    ):
        raise DishRuleError(
            "CONFLICT",
            "only the exact incomplete Verification attempt can be replaced",
            rule="abandonment_cycle_not_incomplete",
        )

    successor_operation_id = str(uuid.uuid4())
    successor_content_version_id = str(uuid.uuid4())
    successor_cycle_id = str(uuid.uuid4())
    succession_id = str(uuid.uuid4())
    next_cycle_number = conn.execute(
        "SELECT COALESCE(MAX(cycle_number),0)+1 FROM verification_cycles WHERE task_gid=?",
        (source["task_gid"],),
    ).fetchone()[0]
    action = {
        "surface": "connected-agent",
        "command": "start",
        "arguments": {
            "task_gid": source["task_gid"],
            "kind": "verification",
            "target_operation_id": successor_operation_id,
            "target_cycle_id": successor_cycle_id,
        },
    }
    result = {
        "abandonment_id": abandonment_id,
        "classification": frontier.to_dict(),
        "succession_id": succession_id,
        "successor_operation_id": successor_operation_id,
        "successor_cycle_id": successor_cycle_id,
        "required_action": action,
    }
    completed_steps = (
        _completed_change_intent(conn, source["operation_id"])
        if source["operation_kind"] == "change"
        else {}
    )
    with immediate_transaction(conn, "_prepare_verification_successor"):
        abandonment, successor, succession = (
            apply_operation_abandonment_succession_in_transaction(
                conn,
                abandonment_id=abandonment_id,
                succession_id=succession_id,
                successor_operation_id=successor_operation_id,
                source_content_version_id=frontier.source_content_version_id,
                successor_content_version_id=successor_content_version_id,
                successor_operation_kind=source["operation_kind"],
                successor_phase="await_verification",
                successor_expected_section_gid=source["expected_section_gid"],
                successor_schema_version=source["schema_version"],
                successor_claim_mode="verifier",
                transition_reason="permanent_verifier_run_abandonment",
                candidate_transfer_kind="inherited_confirmed_candidate",
                source_cycle_id=source_cycle["cycle_id"],
                close_source_cycle_as_abandoned=True,
                successor_cycle_id=successor_cycle_id,
                successor_cycle_number=int(next_cycle_number),
                successor_protocol_release=source_cycle["protocol_release"],
                successor_protocol_text=source_cycle["protocol_text"],
                successor_editor_agent=source["editor_agent"],
                successor_researcher_agent=source["researcher_agent"],
                successor_run_id=source["run_id"],
                successor_actor_facts=_successor_actor_facts(
                    conn, source["operation_id"]
                ),
                successor_completed_steps=completed_steps,
                result=result,
            )
        )
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
    with immediate_transaction(conn, "resolve_preconstruction_hold_to_successor"):
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
    return result



def _prepared_stage_start_action(successor: sqlite3.Row) -> dict[str, Any]:
    kind = successor["operation_kind"]
    return {
        "surface": "connected-agent",
        "command": "start",
        "arguments": {
            "task_gid": successor["task_gid"],
            "kind": kind,
            "prepared_operation_id": successor["operation_id"],
        },
    }


def _post_succession_block_result(
    abandonment: sqlite3.Row,
    *,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    command = f'dish-admin reconcile-abandonment {abandonment["abandonment_id"]}'
    return {
        "abandonment_id": abandonment["abandonment_id"],
        "classification": {
            "outcome": "blocked_manual_reconciliation",
            "stage": str((details or {}).get("stage") or (
                "verification" if abandonment["attempt_cycle_id"] is not None else "research"
            )),
            "reason": reason,
            "details": {
                key: value for key, value in dict(details or {}).items()
                if key != "stage"
            },
        },
        "required_action": {
            "surface": "private-admin",
            "command": "reconcile-abandonment",
            "arguments": {"abandonment_id": abandonment["abandonment_id"]},
            "admin_command": command,
            "relay_text": (
                f"Tell the human to run: {command}\n"
                "Then wait for confirmation it succeeded and refresh the "
                "authoritative Dish action."
            ),
            "after_success": {
                "start_new_operation": False,
                "instruction": (
                    "Refresh the authoritative Dish action, then follow the exact "
                    "continuation returned."
                ),
            },
        },
    }


def _mark_post_succession_blocked(
    conn: sqlite3.Connection,
    *,
    abandonment_id: str,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    abandonment = get_abandonment_attempt(conn, abandonment_id)
    result = _post_succession_block_result(
        abandonment, reason=reason, details=details
    )
    with immediate_transaction(conn, "_mark_post_succession_blocked"):
        row = mark_abandonment_blocked_in_transaction(
            conn, abandonment_id=abandonment_id, result=result
        )
    result["abandonment"] = {key: row[key] for key in row.keys()}
    return result


def _reconcile_existing_successor_write(
    conn: sqlite3.Connection,
    *,
    successor: sqlite3.Row,
    baseline: sqlite3.Row,
    live: LiveTask,
) -> LiveTask:
    attempt = conn.execute(
        """SELECT * FROM write_attempts
             WHERE operation_id=? AND outcome IN ('started','uncertain')
             ORDER BY started_at LIMIT 1""",
        (successor["operation_id"],),
    ).fetchone()
    if attempt is None:
        return live
    if (
        attempt["purpose"] != "abandonment_successor_restore_content"
        or attempt["intended_identity"] != baseline["identity"]
        or attempt["intended_title"] != baseline["title"]
        or attempt["intended_notes"] != baseline["notes"]
    ):
        raise DishRuleError(
            "BACKEND_UNCERTAIN",
            "prepared successor has an unrelated unresolved content effect",
            rule="prepared_successor_reconciliation_effect_conflict",
            retryable=False,
            details={"attempt_id": attempt["attempt_id"]},
        )
    if (
        live.identity == baseline["identity"]
        and live.title == baseline["title"]
        and live.notes == baseline["notes"]
    ):
        finalize_confirmed_write_attempt(
            conn,
            attempt_id=attempt["attempt_id"],
            task_gid=successor["task_gid"],
            title=live.title,
            notes=live.notes,
            schema_version=successor["schema_version"],
        )
        return live
    if live.identity == attempt["expected_identity"]:
        finalize_not_applied_write_attempt(conn, attempt_id=attempt["attempt_id"])
        return live
    raise DishRuleError(
        "BACKEND_UNCERTAIN",
        "prepared successor content effect is contradictory",
        rule="prepared_successor_reconciliation_content_contradictory",
        retryable=False,
        details={
            "attempt_id": attempt["attempt_id"],
            "actual_identity": live.identity,
        },
    )


def _reconcile_existing_successor_movement(
    conn: sqlite3.Connection,
    *,
    successor: sqlite3.Row,
    live: LiveTask,
) -> LiveTask:
    attempt = conn.execute(
        """SELECT * FROM movement_attempts
             WHERE operation_id=? AND outcome IN ('started','uncertain')
             ORDER BY started_at LIMIT 1""",
        (successor["operation_id"],),
    ).fetchone()
    if attempt is None:
        return live
    if (
        attempt["purpose"] != "abandonment_successor_restore_placement"
        or attempt["intended_section_gid"] != successor["expected_section_gid"]
    ):
        raise DishRuleError(
            "BACKEND_UNCERTAIN",
            "prepared successor has an unrelated unresolved movement effect",
            rule="prepared_successor_reconciliation_effect_conflict",
            retryable=False,
            details={"attempt_id": attempt["attempt_id"]},
        )
    if live.section_gid == successor["expected_section_gid"]:
        finalize_confirmed_movement_attempt(
            conn,
            attempt_id=attempt["attempt_id"],
            live_section_gid=live.section_gid,
        )
        return live
    if live.section_gid == attempt["expected_section_gid"]:
        finalize_not_applied_movement_attempt(conn, attempt_id=attempt["attempt_id"])
        return live
    raise DishRuleError(
        "BACKEND_UNCERTAIN",
        "prepared successor movement effect is contradictory",
        rule="prepared_successor_reconciliation_placement_contradictory",
        retryable=False,
        details={
            "attempt_id": attempt["attempt_id"],
            "actual_section_gid": live.section_gid,
        },
    )


def _reconcile_prepared_stage_successor(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    abandonment_id: str,
) -> dict[str, Any] | None:
    """Restore one unclaimed stage successor to its immutable baseline/placement."""

    abandonment = get_abandonment_attempt(conn, abandonment_id)
    successor_id = abandonment["successor_operation_id"]
    if abandonment["status"] != "started" or not successor_id:
        return None
    successor = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (successor_id,)
    ).fetchone()
    succession = conn.execute(
        "SELECT * FROM operation_successions WHERE abandonment_id=?",
        (abandonment_id,),
    ).fetchone()
    if (
        successor is None
        or succession is None
        or successor["successor_claim_mode"] != "stage_actor"
        or successor["status"] != "open"
        or successor["phase"] != "prepare_required"
        or successor["run_id"] is not None
    ):
        return None
    baseline = conn.execute(
        "SELECT * FROM content_versions WHERE content_version_id=?",
        (succession["successor_content_version_id"],),
    ).fetchone()
    if (
        baseline is None
        or baseline["operation_id"] != successor_id
        or baseline["boundary"] != "successor_baseline"
        or baseline["confirmed"] != 1
        or baseline["identity"] != successor["expected_identity"]
    ):
        return _mark_post_succession_blocked(
            conn,
            abandonment_id=abandonment_id,
            reason="prepared successor baseline binding is corrupt",
            details={
                "stage": "planning" if successor["operation_kind"] == "planning" else "research",
                "successor_operation_id": successor_id,
            },
        )

    try:
        live = read_complete_task(
            backend, task_gid=successor["task_gid"], project_gid=COOKING_PROJECT_GID
        )
        live = _reconcile_existing_successor_write(
            conn, successor=successor, baseline=baseline, live=live
        )
        if live.identity != baseline["identity"]:
            live = write_exact_content(
                conn,
                backend,
                operation_id=successor_id,
                task_gid=successor["task_gid"],
                project_gid=COOKING_PROJECT_GID,
                expected_identity=live.identity,
                expected_section_gid=live.section_gid,
                title=baseline["title"],
                notes=baseline["notes"],
                schema_version=successor["schema_version"],
                purpose="abandonment_successor_restore_content",
                context={
                    "abandonment_id": abandonment_id,
                    "admin_execution_id": abandonment["current_execution_id"],
                },
            )
        live = _reconcile_existing_successor_movement(
            conn, successor=successor, live=live
        )
        if live.section_gid != successor["expected_section_gid"]:
            live = move_exact(
                conn,
                backend,
                operation_id=successor_id,
                task_gid=successor["task_gid"],
                project_gid=COOKING_PROJECT_GID,
                expected_identity=baseline["identity"],
                expected_section_gid=live.section_gid,
                intended_section_gid=successor["expected_section_gid"],
                purpose="abandonment_successor_restore_placement",
            )
        if (
            live.identity != baseline["identity"]
            or live.title != baseline["title"]
            or live.notes != baseline["notes"]
            or live.section_gid != successor["expected_section_gid"]
        ):
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "prepared successor reconciliation did not restore the exact target",
                rule="prepared_successor_reconciliation_incomplete",
                retryable=False,
            )
    except DishRuleError as exc:
        return _mark_post_succession_blocked(
            conn,
            abandonment_id=abandonment_id,
            reason="prepared successor reconciliation remains blocked",
            details={
                "stage": "planning" if successor["operation_kind"] == "planning" else "research",
                "rule": exc.rule,
                **dict(exc.details or {}),
            },
        )

    action = _prepared_stage_start_action(successor)
    result = {
        "abandonment_id": abandonment_id,
        "classification": {
            "outcome": "restart_prepared",
            "stage": "planning" if successor["operation_kind"] == "planning" else "research",
            "reason": "prepared successor baseline and placement restored",
            "details": {"successor_operation_id": successor_id},
        },
        "successor_operation_id": successor_id,
        "required_action": action,
    }
    with immediate_transaction(conn, "_reconcile_prepared_stage_successor"):
        current = get_abandonment_attempt(conn, abandonment_id)
        if current["status"] != "started" or current["successor_operation_id"] != successor_id:
            raise DishRuleError(
                "CONFLICT",
                "prepared successor reconciliation authority changed",
                rule="prepared_successor_reconciliation_stale",
            )
        conn.execute(
            """UPDATE abandonment_attempts
                  SET status='awaiting_successor_claim', outcome='restart_prepared',
                      current_execution_id=NULL, latest_result_json=?, updated_at=?
                WHERE abandonment_id=?""",
            (
                json.dumps(result, sort_keys=True, separators=(",", ":")),
                utc_now(),
                abandonment_id,
            ),
        )
        record_audit(
            conn,
            submission_id=None,
            task_gid=successor["task_gid"],
            operation_id=successor_id,
            event_type="operation.successor_reconciled",
            actor_agent=None,
            details={
                "abandonment_id": abandonment_id,
                "expected_identity": baseline["identity"],
                "expected_section_gid": successor["expected_section_gid"],
            },
            result_code="OK",
            result_ok=True,
        )
    return result


def settle_abandonment_frontier(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    abandonment_id: str,
    reason: str,
) -> dict[str, Any]:
    """Persist a safe frontier and create clean stage successors.

    Restoring an unclaimed prepared stage successor's baseline and placement
    is the sole compensating external write/movement this function performs;
    it never mutates or rebases the immutable succession edge itself.
    """

    prepared_reconciliation = _reconcile_prepared_stage_successor(
        conn, backend, abandonment_id=abandonment_id
    )
    if prepared_reconciliation is not None:
        return prepared_reconciliation

    frontier = classify_abandonment_frontier(
        conn, backend, abandonment_id=abandonment_id
    )
    result = {"abandonment_id": abandonment_id, "classification": frontier.to_dict()}

    if frontier.outcome == "restart_prepared":
        if frontier.stage in {"planning", "research"}:
            return _prepare_stage_successor(
                conn, abandonment_id=abandonment_id, frontier=frontier
            )
        return _prepare_verification_successor(
            conn, abandonment_id=abandonment_id, frontier=frontier
        )
    if frontier.outcome == "blocked_manual_reconciliation":
        with immediate_transaction(conn, "mark_abandonment_blocked"):
            row = mark_abandonment_blocked_in_transaction(
                conn, abandonment_id=abandonment_id, result=result
            )
        result["abandonment"] = {key: row[key] for key in row.keys()}
        return result
    if frontier.outcome == "awaiting_hold_resolution":
        with immediate_transaction(conn, "mark_abandonment_awaiting_hold"):
            row = mark_abandonment_awaiting_hold_in_transaction(
                conn, abandonment_id=abandonment_id, result=result
            )
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
    if continuation_operation_id and continuation_cycle_id:
        result["required_action"] = {
            "surface": "connected-agent",
            "command": "start",
            "arguments": {
                "task_gid": operation["task_gid"],
                "kind": "verification",
                "target_operation_id": continuation_operation_id,
                "target_cycle_id": continuation_cycle_id,
            },
        }
    with immediate_transaction(conn, "complete_abandonment"):
        row = complete_abandonment_in_transaction(
            conn,
            abandonment_id=abandonment_id,
            outcome=completion_outcome,
            result=result,
            continuation_operation_id=continuation_operation_id,
            continuation_cycle_id=continuation_cycle_id,
        )
    result["abandonment"] = {key: row[key] for key in row.keys()}
    return result
