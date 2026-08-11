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




def _normalize_human_review_options(raw_options: object) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    if not isinstance(raw_options, list):
        return options
    for index, raw in enumerate(raw_options):
        if not isinstance(raw, Mapping):
            continue
        label = str(raw.get("label") or "").strip()
        decision = str(raw.get("decision") or "").strip()
        if not label or not decision:
            continue
        options.append({
            "option_id": str(raw.get("option_id") or chr(ord("A") + index)),
            "label": label,
            "decision": decision,
            "recommended": index == 0,
            "authorization": raw.get("authorization") if isinstance(raw.get("authorization"), Mapping) else None,
        })
    return options


def _human_review_summary(
    *, reason: str, basis: str | None, blocker: object, options: list[dict[str, Any]],
    resume_status: object, preconstruction: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    blocker_summary = _format_quantified_blocker(blocker)
    consequences = human_review_consequence_metadata(resume_status)
    review_summary = {
        "outcome": "needs Marco decision",
        "issue": reason,
        "quantified_blocker": blocker_summary,
        "decision": basis or reason,
        "simplest_next_step": (
            "Choose the recommended option, another agent-proposed option, or type your own instruction."
            if options
            else "Type Marco's instruction for the next agent; this older review has no stored agent options."
        ),
        "approval_consequence": consequences["approval"],
        "dismissal_consequence": consequences["dismissal"],
    }
    explanation = {
        "problem": reason,
        "cause": (
            "Research reached a durable Human Review stop before constructing a candidate."
            if preconstruction
            else "Verification reached a durable Human Review stop."
        ),
        "why_not_ordinary_correction": (
            "The agent found a real choice it is not authorized to make for Marco."
        ),
        "recommended_resolution": (
            "Choose A for the agent's recommended route, choose another stored option, or type a different instruction."
            if options
            else "Type the decision or instruction Marco wants the next agent to follow."
        ),
        "scope": (
            "This task and this exact blocked Research operation only."
            if preconstruction
            else "This task and this exact held Verification cycle only."
        ),
        "command_effect": (
            "Choosing a stored option records that exact Marco decision. If the option carries one exact governed-field "
            "authorization, Dish records that authorization for the continuation agent. Free text is recorded as instruction only."
            if not preconstruction
            else "Choosing a stored option or free text records Marco's instruction and returns this same operation to Research construction."
        ),
        "after_success": consequences["approval"]["instruction"],
        "dismissal_after_success": consequences["dismissal"]["instruction"],
    }
    return review_summary, explanation

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
    options = _normalize_human_review_options(intended.get("human_review_options"))
    return {
        "reason": reason,
        "human_review_basis": basis,
        "repairs_considered": repairs,
        "quantified_blocker": blocker if isinstance(blocker, Mapping) else None,
        "quantified_blocker_summary": _format_quantified_blocker(blocker),
        "human_review_options": options,
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
        options = context["human_review_options"] if kind == "human_review" else []
        if kind == "human_review":
            review_summary, explanation = _human_review_summary(
                reason=reason, basis=context["human_review_basis"],
                blocker=context["quantified_blocker"], options=options,
                resume_status=row["resume_state"],
            )
        else:
            review_summary = {
                "outcome": "verification hold", "issue": reason,
                "quantified_blocker": blocker_summary, "decision": None,
                "simplest_next_step": "Release the unchanged candidate to a fresh Verification round.",
                "approval_consequence": None, "dismissal_consequence": None,
            }
            explanation = None
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
                "human_review_options": options,
                "quantified_blocker": context["quantified_blocker"],
                "review_summary": review_summary,
                "resume_status": row["resume_state"],
                "hold_identity": row["hold_identity"],
                "created_at": row["cycle_created_at"] or row["created_at"],
                "explanation": explanation if kind == "human_review" else {
                    "problem": reason,
                    "cause": "Verification reached its durable loop-breaker hold.",
                    "recommended_resolution": "Release the three-round Verification hold into a fresh round.",
                    "scope": "This task and this exact held Verification cycle only.",
                    "command_effect": "The resolved command releases the unchanged candidate into a fresh Verification round.",
                    "after_success": (
                        "An eligible verifier may continue from the resumed operation; if the original verifier is still live "
                        "and made no material edit, that same run may continue from fresh durable state."
                    ),
                    "dismissal_after_success": None,
                },
                "changes": [],
                "linked_changes": [],
            }
        )
    return tuple(items)



def _preconstruction_human_review_items(conn: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
    rows = conn.execute(
        """SELECT operation.operation_id, operation.task_gid, operation.created_at,
                  operation.expected_identity, state.last_confirmed_title, step.intended_json
             FROM operations AS operation
             JOIN operation_steps AS step
               ON step.operation_id=operation.operation_id
              AND step.step_name='research_preconstruction_hold'
              AND step.completed_at IS NOT NULL
             LEFT JOIN task_content_state AS state ON state.task_gid=operation.task_gid
            WHERE operation.status='open' AND operation.phase='held_human'
            ORDER BY operation.created_at, operation.operation_id"""
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            intended = json.loads(row["intended_json"] or "{}")
        except (TypeError, ValueError):
            intended = {}
        if not isinstance(intended, dict) or intended.get("route") != "human-review":
            continue
        reason = str(intended.get("reason") or "").strip() or "Marco's decision is required before Research can continue."
        basis = str(intended.get("human_review_basis") or "").strip() or None
        repairs = str(intended.get("repairs_considered") or "").strip() or None
        blocker = intended.get("quantified_blocker")
        options = _normalize_human_review_options(intended.get("human_review_options"))
        review_summary, explanation = _human_review_summary(
            reason=reason, basis=basis, blocker=blocker, options=options,
            resume_status="pending-research", preconstruction=True,
        )
        items.append({
            "item_type": "human_review",
            "review_id": row["operation_id"],
            "proposal_id": row["operation_id"],
            "status": "pending",
            "task_gid": row["task_gid"],
            "operation_id": row["operation_id"],
            "cycle_id": None,
            "candidate_title": row["last_confirmed_title"],
            "proposal_reason": reason,
            "human_review_basis": basis,
            "repairs_considered": repairs,
            "human_review_options": options,
            "quantified_blocker": blocker if isinstance(blocker, Mapping) else None,
            "review_summary": review_summary,
            "resume_status": "pending-research",
            "hold_identity": row["expected_identity"],
            "created_at": row["created_at"],
            "preconstruction": True,
            "explanation": explanation,
            "changes": [],
            "linked_changes": [],
        })
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
        items.extend(_preconstruction_human_review_items(conn))
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
    for item in (*_human_review_items(conn), *_preconstruction_human_review_items(conn)):
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
