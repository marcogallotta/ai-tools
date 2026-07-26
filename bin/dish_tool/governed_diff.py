"""Operation-aware protection for protocol-governed canonical facts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .database import consume_marco_authorization


@dataclass(frozen=True)
class GovernedChange:
    field: str
    before: object
    after: object


def governed_changes(before, after) -> tuple[GovernedChange, ...]:
    changes: list[GovernedChange] = []
    protected_planning = (
        "Purpose", "Role", "Locks", "Exemptions", "Research emphasis", "Destination section",
    )
    for field in protected_planning:
        old = before.planning_brief.values[field]
        new = after.planning_brief.values[field]
        if old != new:
            changes.append(GovernedChange(field, old, new))
    if tuple(before.decisions) != tuple(after.decisions):
        changes.append(GovernedChange("Decisions", tuple(before.decisions), tuple(after.decisions)))
    if before.state.values["Researched by"] != after.state.values["Researched by"]:
        changes.append(GovernedChange("Researched by", before.state.values["Researched by"], after.state.values["Researched by"]))
    return tuple(changes)


def require_governed_authorization(conn, before, after, *, task_gid: str, operation_id: str) -> tuple[GovernedChange, ...]:
    """Require independently persisted Marco facts for every governed change.

    Candidate-authored Decisions text is never authority. Authorizations are
    consumed exactly once and must match the exact before/after values.
    """
    changes = governed_changes(before, after)
    for change in changes:
        consume_marco_authorization(
            conn, task_gid=task_gid, operation_id=operation_id,
            field_name=change.field, before=change.before, after=change.after,
        )
    return changes


def require_small_scope(before, after) -> None:
    """Small correction may repair recipe content, not replace workflow facts."""
    immutable = {
        "title": (before.title, after.title),
        "planning_brief": (dict(before.planning_brief.values), dict(after.planning_brief.values)),
        "decisions": (tuple(before.decisions), tuple(after.decisions)),
        "research_basis": (tuple(before.research_basis), tuple(after.research_basis)),
        "researched_by": (before.state.values["Researched by"], after.state.values["Researched by"]),
    }
    changed = [name for name, (old, new) in immutable.items() if old != new]
    if changed:
        from .errors import DishRuleError
        raise DishRuleError("VALIDATION_FAILED", "Small correction exceeds its permitted scope", rule="small_correction_scope", details={"fields": changed})
