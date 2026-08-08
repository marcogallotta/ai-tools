"""Read-only aggregation and target resolution for Marco's review queue."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Sequence

from .errors import DishRuleError
from .semantic_proposals import get_semantic_proposal, list_semantic_proposals, proposal_payload
from .workflow_policy import hold_resolution_outcome

_ACTIVE_PROPOSAL_STATUSES = ("pending", "approved", "claimed")


def human_review_consequence_metadata(resume_status: object) -> dict[str, dict[str, Any]]:
    """Describe operator-facing consequences for approval vs dismissal of a Verification Human Review."""
    resume = str(resume_status or "pending-verification").strip()
    approval_state = hold_resolution_outcome(
        "pending-research" if resume == "pending-research" else "pending-verification"
    )
    approval = {
        "resume_status": approval_state.resume_status,
        "next_stage": approval_state.next_stage,
        "operation_status": approval_state.operation_status,
        "operation_phase": approval_state.operation_phase,
        "instruction": (
            "Marco's substantive decision returns the task to Research; the held Verification "
            "operation completes rather than opening a fresh Verification cycle."
            if approval_state.next_stage == "research"
            else "Marco's substantive decision releases the unchanged candidate to a fresh "
            "Verification cycle."
        ),
    }
    dismissal_state = hold_resolution_outcome("pending-verification")
    dismissal = {
        "resume_status": dismissal_state.resume_status,
        "next_stage": dismissal_state.next_stage,
        "operation_status": dismissal_state.operation_status,
        "operation_phase": dismissal_state.operation_phase,
        "instruction": (
            "Dismissing the unanswered escalation releases the unchanged candidate to fresh "
            "Verification; the escalation's stored resume status does not control dismissal."
        ),
    }
    return {"approval": approval, "dismissal": dismissal}


def _format_quantified_blocker(blocker: object) -> str | None:
    if not isinstance(blocker, Mapping):
        return None
    metric = str(blocker.get("metric") or "").strip()
    unit = str(blocker.get("unit") or "").strip()
    try:
        actual = float(blocker["actual"])
        limit = float(blocker["limit"])
        delta = float(blocker["delta"])
    except (KeyError, TypeError, ValueError):
        return None

    def number(value: float) -> str:
        return str(int(value)) if value.is_integer() else f"{value:g}"

    sign = "+" if delta > 0 else ""
    label = f"{metric}: " if metric else ""
    suffix = f" {unit}" if unit else ""
    return f"{label}{number(actual)}{suffix} vs {number(limit)}{suffix} ({sign}{number(delta)}{suffix})"


def _hold_context(conn: sqlite3.Connection, operation_id: str, cycle_id: str) -> dict[str, Any]:
    row = conn.execute(
        """SELECT intended_json FROM operation_steps
             WHERE operation_id=? AND step_name=? AND completed_at IS NOT NULL""",
        (operation_id, f"route_cycle_finalize:{cycle_id}"),
    ).fetchone()
    intended: dict[str, Any] = {}
    if row is not None:
        try:
            loaded = json.loads(row["intended_json"] or "{}")
        except (TypeError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            intended = loaded
    reason = str(intended.get("decision_reason") or "").strip() or (
        "Marco's decision is required before this task can continue."
    )
    basis = str(intended.get("human_review_basis") or "").strip() or None
    repairs = str(intended.get("repairs_considered") or "").strip() or None
    blocker = intended.get("quantified_blocker")
    return {
        "reason": reason,
        "human_review_basis": basis,
        "repairs_considered": repairs,
        "quantified_blocker": blocker if isinstance(blocker, Mapping) else None,
        "quantified_blocker_summary": _format_quantified_blocker(blocker),
    }


def _human_review_items(conn: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
    rows = conn.execute(
        """SELECT operation.operation_id, operation.task_gid, operation.created_at,
                  cycle.cycle_id, cycle.outcome, cycle.route, cycle.resume_state,
                  cycle.hold_identity, cycle.created_at AS cycle_created_at,
                  state.last_confirmed_title
             FROM operations AS operation
             JOIN verification_cycles AS cycle
               ON cycle.operation_id=operation.operation_id
             LEFT JOIN task_content_state AS state ON state.task_gid=operation.task_gid
            WHERE operation.status='open'
              AND operation.phase='held_human'
              AND cycle.completed_at IS NOT NULL
              AND (cycle.route='human_review' OR cycle.outcome='verification-hold')
              AND cycle.cycle_number=(
                    SELECT MAX(latest.cycle_number)
                      FROM verification_cycles AS latest
                     WHERE latest.operation_id=operation.operation_id
                  )
            ORDER BY cycle.created_at, cycle.cycle_id"""
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        kind = "verification_hold" if row["outcome"] == "verification-hold" else "human_review"
        context = _hold_context(conn, row["operation_id"], row["cycle_id"])
        reason = context["reason"]
        blocker_summary = context["quantified_blocker_summary"]
        decision = context["human_review_basis"] or reason
        consequences = human_review_consequence_metadata(row["resume_state"])
        review_summary = {
            "outcome": "needs Marco decision" if kind == "human_review" else "verification hold",
            "issue": reason,
            "quantified_blocker": blocker_summary,
            "decision": decision if kind == "human_review" else None,
            "simplest_next_step": (
                (
                    "Approve the substantive decision to return this task to "
                    f"{consequences['approval']['next_stage'].title()}, or dismiss the invalid escalation "
                    "to return the unchanged candidate to fresh Verification."
                )
                if kind == "human_review"
                else "Release the unchanged candidate to a fresh Verification round."
            ),
            "approval_consequence": consequences["approval"] if kind == "human_review" else None,
            "dismissal_consequence": consequences["dismissal"] if kind == "human_review" else None,
        }
        items.append(
            {
                "item_type": kind,
                "review_id": row["cycle_id"],
                "proposal_id": row["cycle_id"],  # compatibility for existing renderers
                "status": "pending",
                "task_gid": row["task_gid"],
                "operation_id": row["operation_id"],
                "cycle_id": row["cycle_id"],
                "candidate_title": row["last_confirmed_title"],
                "proposal_reason": reason,
                "human_review_basis": context["human_review_basis"],
                "repairs_considered": context["repairs_considered"],
                "quantified_blocker": context["quantified_blocker"],
                "review_summary": review_summary,
                "resume_status": row["resume_state"],
                "hold_identity": row["hold_identity"],
                "created_at": row["cycle_created_at"] or row["created_at"],
                "explanation": {
                    "problem": reason,
                    "cause": "Verification reached a durable Human Review stop.",
                    "why_not_ordinary_correction": (
                        "The verifier reported that a Marco-only choice remained, so Dish parked the operation. "
                        "The agent may not infer Marco's answer or mutate governed fields from that report."
                    ),
                    "recommended_resolution": (
                        "If the escalation is valid, record Marco's exact decision and reasoning. "
                        "If the agent-authored escalation itself is invalid and Marco has not made a substantive decision, "
                        "dismiss it with a reason and resume Verification."
                        if kind == "human_review"
                        else "Release the three-round Verification hold into a fresh round."
                    ),
                    "scope": "This task and this exact held Verification cycle only.",
                    "command_effect": (
                        "Recording a decision persists Marco's substantive decision; dismissing the escalation records only why the prior "
                        "agent-authored hold was invalid. Neither path edits or authorizes governed fields."
                        if kind == "human_review"
                        else "The resolved command releases the unchanged candidate into a fresh Verification round."
                    ),
                    "after_success": (
                        consequences["approval"]["instruction"]
                        if kind == "human_review"
                        else "A later fresh verifier may continue from the resumed operation."
                    ),
                    "dismissal_after_success": (
                        consequences["dismissal"]["instruction"] if kind == "human_review" else None
                    ),
                },
                "changes": [],
                "linked_changes": [],
            }
        )
    return tuple(items)


def list_review_items(
    conn: sqlite3.Connection,
    *,
    proposal_statuses: Sequence[str] = _ACTIVE_PROPOSAL_STATUSES,
    include_human_holds: bool = True,
) -> tuple[dict[str, Any], ...]:
    items: list[dict[str, Any]] = []
    for proposal in list_semantic_proposals(conn, statuses=proposal_statuses):
        item = dict(proposal)
        item["item_type"] = "semantic_proposal"
        item["review_id"] = item["proposal_id"]
        items.append(item)
    if include_human_holds and "pending" in proposal_statuses:
        items.extend(_human_review_items(conn))
    items.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("review_id") or "")))
    return tuple(items)


def resolve_review_item(
    conn: sqlite3.Connection,
    identifier: str,
    *,
    include_terminal_proposals: bool = True,
) -> dict[str, Any]:
    clean = str(identifier or "").strip()
    if not clean:
        raise DishRuleError("INVALID_ARGUMENT", "review item ID is required", rule="review_item_id_required")
    if clean.isdecimal():
        items = list_review_items(conn)
        index = int(clean)
        if index < 1 or index > len(items):
            raise DishRuleError(
                "NOT_FOUND", "review queue item number is out of range",
                rule="review_item_index_not_found",
                details={"requested_index": index, "queue_count": len(items)},
            )
        selected = dict(items[index - 1])
        if selected.get("item_type") == "semantic_proposal":
            row = get_semantic_proposal(conn, str(selected["proposal_id"]))
            selected = proposal_payload(conn, row)
            selected["item_type"] = "semantic_proposal"
            selected["review_id"] = selected["proposal_id"]
        return selected

    proposal_statuses = (
        ("pending", "approved", "claimed", "applied", "rejected", "stale")
        if include_terminal_proposals
        else _ACTIVE_PROPOSAL_STATUSES
    )
    try:
        row = get_semantic_proposal(conn, clean)
    except DishRuleError as exc:
        if exc.rule != "semantic_proposal_not_found":
            raise
    else:
        if row["status"] in proposal_statuses:
            item = proposal_payload(conn, row)
            item["item_type"] = "semantic_proposal"
            item["review_id"] = item["proposal_id"]
            return item
    for item in _human_review_items(conn):
        if item["review_id"] == clean:
            return dict(item)
    raise DishRuleError(
        "NOT_FOUND", "review item not found", rule="review_item_not_found",
        details={"review_id": clean},
    )


def review_item_operation_id(conn: sqlite3.Connection, identifier: str) -> str | None:
    try:
        return str(resolve_review_item(conn, identifier)["operation_id"])
    except DishRuleError:
        return None
