"""Operation-aware protection for protocol-governed canonical facts."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .database import reserve_marco_authorizations


@dataclass(frozen=True)
class GovernedChange:
    field: str
    before: object
    after: object


def governed_changes(before, after) -> tuple[GovernedChange, ...]:
    changes: list[GovernedChange] = []
    for field in ("Purpose", "Role", "Locks", "Exemptions", "Research emphasis", "Destination section"):
        old, new = before.planning_brief.values[field], after.planning_brief.values[field]
        if old != new:
            changes.append(GovernedChange(field, old, new))
    if tuple(before.decisions) != tuple(after.decisions):
        changes.append(GovernedChange("Decisions", tuple(before.decisions), tuple(after.decisions)))
    if before.state.values["Researched by"] != after.state.values["Researched by"]:
        changes.append(GovernedChange("Researched by", before.state.values["Researched by"], after.state.values["Researched by"]))
    return tuple(changes)


def require_governed_authorization(conn, before, after, *, task_gid: str, operation_id: str) -> tuple[str, ...]:
    changes = governed_changes(before, after)
    if not changes:
        return ()
    rows = reserve_marco_authorizations(
        conn, task_gid=task_gid, operation_id=operation_id,
        changes=tuple({"field": c.field, "before": c.before, "after": c.after} for c in changes),
    )
    return tuple(row["authorization_id"] for row in rows)


# Structured signatures are intentionally derived from canonical content fields,
# not from caller classifications or a handful of literal changed lines.
_QUANTITY_RE = re.compile(r"(?<!\w)(?:\d+(?:[.,]\d+)?|\d+\s*/\s*\d+)\s*(?:kg|g|mg|l|ml|cl|tsp|tbsp|teaspoons?|tablespoons?|cups?|oz|lb|°c|°f|minutes?|mins?|hours?|hrs?)\b", re.I)
_RATIO_RE = re.compile(r"(?<!\w)\d+(?:[.,]\d+)?\s*(?::|/|\bto\b)\s*\d+(?:[.,]\d+)?", re.I)
_PORTION_RE = re.compile(r"\b(?:portion|portions|serving|servings|sittings?|feeds?|people|persons?)\b", re.I)
_NUTRITION_RE = re.compile(r"\b(?:nutrition|nutritional|calorie|calories|kcal|protein|carbohydrate|carbs?|fat|fibre|fiber|sodium)\b", re.I)
_HALAL_SAFETY_RE = re.compile(r"\b(?:halal|haram|pork|bacon|ham|lard|wine|beer|sherry|mirin|alcohol|allergen|allergy|unsafe|safety|cross[- ]contamination|raw|internal temperature)\b", re.I)
_SOURCING_RE = re.compile(r"\b(?:source|sourcing|supplier|import|availability|available|unavailable|substitut(?:e|ion)|brand)\b", re.I)
_EQUIPMENT_RE = re.compile(r"\b(?:wok|oven|stovetop|hob|grill|broiler|air fryer|fryer|pressure cooker|instant pot|slow cooker|sous vide|blender|food processor|mortar|pan|pot)\b", re.I)
_RISK_RE = re.compile(r"\b(?:feasib(?:le|ility)|risk|constraint|equipment|temperature|timing|hold time)\b", re.I)


def _canonical_body(document) -> dict[str, str]:
    values = {f"section.{name}": str(value) for name, value in document.sections.items()}
    values.update({
        "title": document.title,
        "recognition": document.recognition,
        "introduction": "\n".join(document.introduction),
        "planning.Purpose": document.planning_brief.values.get("Purpose", ""),
        "planning.Role": document.planning_brief.values.get("Role", ""),
        "planning.Locks": document.planning_brief.values.get("Locks", ""),
        "planning.Exemptions": document.planning_brief.values.get("Exemptions", ""),
        "planning.Research emphasis": document.planning_brief.values.get("Research emphasis", ""),
        "decisions": "\n".join(document.decisions),
        "research_basis": "\n".join(document.research_basis),
    })
    return values


def canonical_diff(before, after) -> dict[str, tuple[str, str]]:
    old, new = _canonical_body(before), _canonical_body(after)
    return {path: (old.get(path, ""), new.get(path, "")) for path in sorted(set(old) | set(new)) if old.get(path, "") != new.get(path, "")}


def _signature(pattern: re.Pattern[str], text: str) -> tuple[str, ...]:
    return tuple(sorted({m.group(0).lower().replace(" ", "") for m in pattern.finditer(text)}))


def explicit_material_reasons(before, after) -> tuple[str, ...]:
    reasons: list[str] = []
    diff = canonical_diff(before, after)
    if "title" in diff or "recognition" in diff:
        reasons.append("title_or_identity")
    if "section.QUANTITIES" in diff:
        reasons.append("quantities")
    for _path, (old, new) in diff.items():
        if _signature(_QUANTITY_RE, old) != _signature(_QUANTITY_RE, new):
            reasons.append("quantity")
        if _signature(_RATIO_RE, old) != _signature(_RATIO_RE, new):
            reasons.append("ratio")
        for name, pattern in (
            ("portions", _PORTION_RE), ("nutrition", _NUTRITION_RE),
            ("halal_or_safety", _HALAL_SAFETY_RE), ("sourcing", _SOURCING_RE),
            ("equipment_or_method", _EQUIPMENT_RE), ("feasibility_or_risk", _RISK_RE),
        ):
            if _signature(pattern, old) != _signature(pattern, new):
                reasons.append(name)
    return tuple(dict.fromkeys(reasons))


def require_small_scope(before, after) -> None:
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
            "VALIDATION_FAILED", "Small correction exceeds its permitted scope and requires Large correction",
            rule="large_correction_required", details={"fields": changed, "material_reasons": material},
        )
