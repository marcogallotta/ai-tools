"""Abandonment-specific overlays for authoritative workflow views."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

from .human_actions import exact_action, relay_text
from .workflow_policy import WorkflowSnapshot, legal_actions


def apply_abandonment_view(
    conn: sqlite3.Connection,
    operation_id: str,
    snapshot: WorkflowSnapshot,
    facts: dict[str, object],
) -> dict[str, object]:
    """Overlay abandonment state and legal actions onto base workflow facts."""
    abandonment = conn.execute(
        """SELECT abandonment.*, succession.successor_operation_id AS linked_successor_id
             FROM abandonment_attempts AS abandonment
             LEFT JOIN operation_successions AS succession
               ON succession.abandonment_id=abandonment.abandonment_id
            WHERE (
                    abandonment.status!='completed'
                    AND (abandonment.source_operation_id=?
                         OR succession.successor_operation_id=?)
                  )
               OR (
                    abandonment.status='completed'
                    AND abandonment.continuation_operation_id=?
                    AND abandonment.continuation_cycle_id IS NOT NULL
                  )
            ORDER BY CASE WHEN abandonment.status!='completed' THEN 0 ELSE 1 END,
                     abandonment.created_at DESC
            LIMIT 1""",
        (operation_id, operation_id, operation_id),
    ).fetchone()
    if abandonment is None:
        facts["legal_actions"] = legal_actions(snapshot)
        return facts

    if abandonment["status"] == "completed":
        continuation = conn.execute(
            """SELECT operation.status, operation.phase, cycle.run_id,
                      cycle.verifier_agent, cycle.completed_at
                 FROM operations AS operation
                 JOIN verification_cycles AS cycle
                   ON cycle.operation_id=operation.operation_id
                WHERE operation.operation_id=? AND cycle.cycle_id=?""",
            (
                abandonment["continuation_operation_id"],
                abandonment["continuation_cycle_id"],
            ),
        ).fetchone()
        if (
            continuation is None
            or continuation["status"] != "open"
            or continuation["phase"] != "await_verification"
            or continuation["completed_at"] is not None
            or continuation["run_id"] is not None
            or continuation["verifier_agent"] is not None
        ):
            facts["legal_actions"] = legal_actions(snapshot)
            return facts
        required_action = {
            "surface": "connected-agent",
            "command": "start",
            "arguments": {
                "task_gid": abandonment["task_gid"],
                "kind": "verification",
                "target_operation_id": abandonment["continuation_operation_id"],
                "target_cycle_id": abandonment["continuation_cycle_id"],
            },
        }
        snapshot = replace(
            snapshot,
            abandonment_status="completed",
            abandonment_required_command="start",
            abandonment_required_start_kind="verification",
            abandonment_continuation_ready=True,
        )
        facts.update(
            {
                "legal_actions": legal_actions(snapshot),
                "required_start_kind": "verification",
                "target_operation_id": abandonment["continuation_operation_id"],
                "target_cycle_id": abandonment["continuation_cycle_id"],
                "required_action": required_action,
                "connected_action_available": True,
                "abandonment_id": abandonment["abandonment_id"],
                "abandonment_status": "completed",
            }
        )
        return facts

    facts.update(
        {
            "abandonment_id": abandonment["abandonment_id"],
            "abandonment_status": abandonment["status"],
            "abandonment_source_operation_id": abandonment["source_operation_id"],
            "abandonment_successor_operation_id": abandonment["successor_operation_id"],
        }
    )
    try:
        stored = json.loads(abandonment["latest_result_json"] or "{}")
    except (TypeError, ValueError):
        stored = {}
    required_action = stored.get("required_action") if isinstance(stored, dict) else None
    if abandonment["status"] == "awaiting_successor_claim":
        required_command = (
            str(required_action.get("command"))
            if isinstance(required_action, dict) and required_action.get("command")
            else None
        )
        required_arguments = (
            required_action.get("arguments")
            if isinstance(required_action, dict)
            and isinstance(required_action.get("arguments"), dict)
            else {}
        )
        snapshot = replace(
            snapshot,
            abandonment_status="awaiting_successor_claim",
            abandonment_required_command=required_command,
            abandonment_required_start_kind=(
                str(required_arguments.get("kind"))
                if required_arguments.get("kind") is not None
                else None
            ),
        )
        facts["legal_actions"] = legal_actions(snapshot)
        if isinstance(required_action, dict):
            facts["required_action"] = required_action
            arguments = required_action.get("arguments")
            if isinstance(arguments, dict):
                if arguments.get("kind") is not None:
                    facts["required_start_kind"] = arguments.get("kind")
                for key in (
                    "prepared_operation_id",
                    "target_operation_id",
                    "target_cycle_id",
                ):
                    if arguments.get(key) is not None:
                        facts[key] = arguments[key]
        facts["recovery_required"] = False
        facts["connected_action_available"] = bool(facts["legal_actions"])
        return facts

    snapshot = replace(snapshot, abandonment_status=str(abandonment["status"]))
    facts["legal_actions"] = legal_actions(snapshot)
    facts["connected_action_available"] = False
    if abandonment["status"] in {"started", "blocked_manual_reconciliation"}:
        spec = exact_action(
            kind="reconcile-abandonment",
            command="reconcile-abandonment",
            positional=(abandonment["abandonment_id"],),
            summary="Continue a blocked abandonment reconciliation.",
            effect="Reclassify the persisted abandonment and prepare its safe continuation.",
            after_success={
                "instruction": "Refresh Dish and follow the returned continuation."
            },
        )
        command = spec.shell_command()
        directive = relay_text(
            spec,
            instruction=(
                "Wait for confirmation it succeeded, then refresh the authoritative Dish "
                "action before doing anything else."
            ),
        )
        facts.update(
            {
                "recovery_required": True,
                "recovery_reasons": ["abandonment_reconciliation_required"],
                "required_admin_action": "reconcile-abandonment",
                "resolver": "Marco/admin reconcile-abandonment",
                "continuation_surface": "private-admin",
                **spec.payload(),
                "directive": directive,
                "required_action": {
                    "surface": "private-admin",
                    "command": "reconcile-abandonment",
                    "arguments": {"abandonment_id": abandonment["abandonment_id"]},
                    **spec.payload(),
                    "relay_text": directive,
                    "after_success": {
                        "start_new_operation": False,
                        "instruction": (
                            "Refresh the authoritative Dish action, then follow "
                            "the exact continuation returned."
                        ),
                    },
                },
            }
        )
    return facts
