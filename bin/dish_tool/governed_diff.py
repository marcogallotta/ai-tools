"""Operation-aware protection for governed canonical facts."""
from __future__ import annotations

from dataclasses import dataclass

from .errors import DishRuleError


@dataclass(frozen=True)
class GovernedChange:
    field: str
    before: object
    after: object


def governed_changes(before, after) -> tuple[GovernedChange, ...]:
    changes: list[GovernedChange] = []
    protected_planning = ("Purpose", "Locks", "Exemptions", "Destination section")
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


def require_governed_authorization(before, after) -> tuple[GovernedChange, ...]:
    changes = governed_changes(before, after)
    if not changes:
        return changes
    added_decisions = set(after.decisions) - set(before.decisions)
    missing: list[str] = []
    for change in changes:
        if change.field == "Decisions":
            # Existing decisions are immutable. New decisions are allowed, but
            # removal/rewrite requires a dedicated Human reset entry.
            if not set(before.decisions).issubset(set(after.decisions)):
                missing.append("Decisions")
            continue
        prefix = f"Human — Marco: Authorizes {change.field} change:"
        if not any(item.startswith(prefix) for item in added_decisions):
            missing.append(change.field)
    if missing:
        raise DishRuleError(
            "VALIDATION_FAILED",
            "candidate changes governed facts without a concrete Human authorization",
            rule="governed_change_unauthorized",
            details={"fields": sorted(set(missing))},
        )
    return changes
