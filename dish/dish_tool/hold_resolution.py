"""Resolution of abandoned pre-construction Research holds."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Mapping

from .content_versions import confirmed_content_version
from .abandonment_succession import AbandonmentSuccessionSpec
from .database import (
    apply_operation_abandonment_succession_in_transaction,
    complete_operation_step,
    declare_operation_step,
    record_audit,
)
from .errors import DishRuleError
from .transactions import immediate_transaction


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
    source_version = confirmed_content_version(
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
        declare_operation_step(
            conn, operation_id, "research_preconstruction_hold_resolution", resolution
        )
        complete_operation_step(
            conn, operation_id, "research_preconstruction_hold_resolution"
        )
        record_audit(
            conn,
            submission_id=None,
            task_gid=source["task_gid"],
            operation_id=operation_id,
            event_type="research.preconstruction_resolved",
            actor_agent=None,
            details={
                **dict(resolution),
                "abandonment_id": abandonment["abandonment_id"],
            },
            result_code="OK",
            result_ok=True,
            governed_kind="decision",
            before_state={
                "phase": source["phase"],
                "candidate_content_existed": False,
            },
            after_state={
                "phase": "terminal",
                "resume_status": "pending-research",
                "successor_operation_id": successor_operation_id,
            },
            actor_source="marco-hold-resolution",
        )
        apply_operation_abandonment_succession_in_transaction(
            conn,
            AbandonmentSuccessionSpec(
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
            ),
        )
    return result
