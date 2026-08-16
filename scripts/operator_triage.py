"""Deterministic three-bucket triage for Dish development orchestration.

The classifier consumes live-authority reconciliation supplied by Coordinator/Development
Workflow.  It does not create a queue, choose semantic priority, or turn research into
implementation authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TriageBucket(str, Enum):
    SEND_NOW = "SEND NOW"
    NEEDS_RESEARCH = "NEEDS RESEARCH"
    BLOCKED_WAITING = "BLOCKED / WAITING"


@dataclass(frozen=True)
class TriageInput:
    task_gid: str
    implementation_ready: bool = False
    research_needed: bool = False
    research_dispatchable: bool = False
    blocked_reason: str | None = None
    critical_research: bool = False
    live_contradiction: str | None = None


@dataclass(frozen=True)
class TriageResult:
    task_gid: str
    bucket: TriageBucket
    next_action: str
    surface_to_marco: bool = False
    reconciliation_required: bool = False


def classify(item: TriageInput) -> TriageResult:
    if item.live_contradiction:
        return TriageResult(
            item.task_gid,
            TriageBucket.BLOCKED_WAITING,
            f"reconcile stale queue state: {item.live_contradiction}",
            reconciliation_required=True,
        )
    if item.blocked_reason:
        return TriageResult(
            item.task_gid,
            TriageBucket.BLOCKED_WAITING,
            f"wait on {item.blocked_reason}",
        )
    if item.research_needed:
        action = (
            "dispatch the research/design work through its normal owner"
            if item.research_dispatchable
            else "resolve the research prerequisite before dispatch"
        )
        return TriageResult(
            item.task_gid,
            TriageBucket.NEEDS_RESEARCH,
            action,
            surface_to_marco=item.critical_research,
        )
    if item.implementation_ready:
        return TriageResult(
            item.task_gid,
            TriageBucket.SEND_NOW,
            "perform the normal final live sanity check, then dispatch Implementation",
        )
    return TriageResult(
        item.task_gid,
        TriageBucket.BLOCKED_WAITING,
        "reconcile readiness before dispatch",
        reconciliation_required=True,
    )
