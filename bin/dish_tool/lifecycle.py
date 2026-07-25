"""Authoritative protocol task-state transitions.

The live seven-field PROCESS RECORD is the workflow authority.  Local operation
status only tracks whether a guarded command envelope is open or complete.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .errors import DishRuleError
from .task_document import TaskState

NONE = "None"


@dataclass(frozen=True)
class Transition:
    action: str
    source: str | None
    target: str


_ALLOWED: dict[str, set[tuple[str | None, str]]] = {
    "research_handoff": {(None, "pending-verification"), ("pending-research", "pending-verification")},
    "material_edit": {("ready", "pending-verification"), ("pending-research", "pending-verification"), ("pending-verification", "pending-verification")},
    "non_material_edit": {("ready", "ready")},
    "approve": {("pending-verification", "ready")},
    "large_correction": {("pending-verification", "pending-verification")},
    "request_evidence": {("pending-research", "pending-evidence"), ("pending-verification", "pending-evidence")},
    "request_human_review": {("pending-research", "pending-human-review"), ("pending-verification", "pending-human-review")},
    "two_pass_hold": {("pending-verification", "pending-human-review")},
    "resume": {("pending-evidence", "pending-research"), ("pending-evidence", "pending-verification"), ("pending-human-review", "pending-research"), ("pending-human-review", "pending-verification")},
    "submit": {("ready", "ready")},
}


def status(values: Mapping[str, str] | TaskState) -> str:
    mapping = values.values if isinstance(values, TaskState) else values
    return str(mapping["Status"])


def require_status(values: Mapping[str, str] | TaskState, expected: set[str], *, action: str) -> str:
    actual = status(values)
    if actual not in expected:
        raise DishRuleError(
            "WRONG_STATE",
            f"{action} is not allowed from task Status {actual}",
            rule="illegal_task_state",
            details={"action": action, "actual": actual, "expected": sorted(expected)},
        )
    return actual


def assert_transition(*, action: str, before: str | None, after: str) -> Transition:
    allowed = _ALLOWED.get(action)
    if allowed is None:
        raise DishRuleError("INTERNAL_ERROR", f"unknown lifecycle action: {action}", rule="unknown_lifecycle_action")
    if (before, after) not in allowed:
        raise DishRuleError(
            "WRONG_STATE",
            f"illegal task transition for {action}: {before or 'no canonical state'} -> {after}",
            rule="illegal_task_transition",
            details={"action": action, "before": before, "after": after},
        )
    return Transition(action, before, after)


def pending_verification(values: Mapping[str, str], *, protocol_release: str) -> TaskState:
    state = dict(values)
    state.update({
        "Status": "pending-verification",
        "Status detail": NONE,
        "Resume status": NONE,
        "Verification protocol release": protocol_release,
        "Verified by": NONE,
    })
    return TaskState(state)


def ready(values: Mapping[str, str], *, verified_by: str) -> TaskState:
    state = dict(values)
    state.update({"Status": "ready", "Status detail": NONE, "Resume status": NONE, "Verified by": verified_by})
    return TaskState(state)


def hold(values: Mapping[str, str], *, target: str, detail: str, resume_status: str) -> TaskState:
    if target not in {"pending-evidence", "pending-human-review"}:
        raise DishRuleError("INTERNAL_ERROR", "invalid hold target", rule="invalid_hold_target")
    state = dict(values)
    state.update({"Status": target, "Status detail": detail, "Resume status": resume_status, "Verified by": NONE})
    if resume_status == "pending-research":
        state["Verification protocol release"] = NONE
    return TaskState(state)


def resumed(values: Mapping[str, str]) -> TaskState:
    source = str(values["Status"])
    target = str(values["Resume status"])
    assert_transition(action="resume", before=source, after=target)
    state = dict(values)
    state.update({"Status": target, "Status detail": NONE, "Resume status": NONE, "Verified by": NONE})
    if target == "pending-research":
        state["Verification protocol release"] = NONE
    return TaskState(state)
