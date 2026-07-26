"""Operation-aware protection for protocol-governed canonical facts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .database import reserve_marco_authorizations


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


def require_governed_authorization(conn, before, after, *, task_gid: str, operation_id: str) -> tuple[str, ...]:
    """Atomically reserve exact Marco facts for every governed change.

    Candidate-authored Decisions text is never authority. The reservation is
    all-or-nothing and is consumed only when the exact external write is
    confirmed.
    """
    changes = governed_changes(before, after)
    if not changes:
        return ()
    rows = reserve_marco_authorizations(
        conn,
        task_gid=task_gid,
        operation_id=operation_id,
        changes=tuple({"field": change.field, "before": change.before, "after": change.after} for change in changes),
    )
    return tuple(row["authorization_id"] for row in rows)




EXPLICIT_MATERIAL_SECTIONS = ("QUANTITIES",)
SENSITIVE_TERMS = (
    "portion", "serving", "ratio", "nutrition", "calorie", "protein",
    "halal", "allergen", "safety", "unsafe", "source", "sourcing",
    "feasibility", "risk", "equipment", "temperature",
)

def _changed_sensitive_lines(before_text: str, after_text: str) -> bool:
    before_lines = {line.strip().lower() for line in before_text.splitlines()}
    after_lines = {line.strip().lower() for line in after_text.splitlines()}
    changed = before_lines.symmetric_difference(after_lines)
    return any(any(term in line for term in SENSITIVE_TERMS) for line in changed)

def explicit_material_reasons(before, after) -> tuple[str, ...]:
    """Return protocol categories that are deterministically always material."""
    reasons: list[str] = []
    if before.title != after.title:
        reasons.append("title_or_identity")
    for name in EXPLICIT_MATERIAL_SECTIONS:
        if before.sections.get(name) != after.sections.get(name):
            reasons.append(name.lower().replace(" ", "_"))
    if _changed_sensitive_lines(before.render(), after.render()):
        reasons.append("safety_halal_sourcing_or_risk")
    return tuple(dict.fromkeys(reasons))

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
    material = list(explicit_material_reasons(before, after))
    if changed or material:
        from .errors import DishRuleError
        raise DishRuleError(
            "VALIDATION_FAILED",
            "Small correction exceeds its permitted scope and requires Large correction",
            rule="large_correction_required",
            details={"fields": changed, "material_reasons": material},
        )
