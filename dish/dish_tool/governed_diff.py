"""Operation-aware protection for protocol-governed canonical facts."""
from __future__ import annotations

import dataclasses
import re
import unicodedata
from dataclasses import dataclass

from .database import reserve_marco_authorizations
from .errors import DishRuleError


@dataclass(frozen=True)
class GovernedChange:
    field: str
    before: object
    after: object


def preserve_material_change_history(before, after):
    """Normalize omitted tool-owned history and reject any supplied rewrite."""
    expected = tuple(before.material_changes)
    supplied = tuple(after.material_changes)
    if supplied == expected:
        return after
    if expected and not supplied:
        return dataclasses.replace(after, material_changes=expected)
    raise DishRuleError(
        "VALIDATION_FAILED",
        "candidate must omit Material changes or preserve every existing entry exactly",
        rule="material_change_history_modified",
        details={
            "expected_entry_count": len(expected),
            "supplied_entry_count": len(supplied),
            "authority": "Dish appends and finalizes the current workflow entry",
        },
    )


def governed_changes(before, after) -> tuple[GovernedChange, ...]:
    changes: list[GovernedChange] = []
    for field in (
        "Dish candidate", "Purpose", "Role", "Priors", "Locks", "Exemptions",
        "Research emphasis", "Destination section",
    ):
        old, new = before.planning_brief.values[field], after.planning_brief.values[field]
        if old != new:
            changes.append(GovernedChange(field, old, new))
    if tuple(before.decisions) != tuple(after.decisions):
        changes.append(GovernedChange("Decisions", tuple(before.decisions), tuple(after.decisions)))
    if before.state.values["Researched by"] != after.state.values["Researched by"]:
        changes.append(GovernedChange("Researched by", before.state.values["Researched by"], after.state.values["Researched by"]))
    return tuple(changes)



def validate_semantic_proposal(before, after) -> None:
    """Reject mechanically coherent but internally contradictory governed proposals."""
    role = str(after.planning_brief.values.get("Role", ""))
    exemptions = str(after.planning_brief.values.get("Exemptions", ""))
    title = str(after.title)
    non_main_role = "non-main" in role.casefold()
    non_main_marker = "[non-main]" in title.casefold()
    nutrition_exemption = any(tag in exemptions for tag in (
        "[nutrition-kcal]", "[nutrition-protein]", "[nutrition-fat]"
    ))
    contradictions = []
    if non_main_role and nutrition_exemption:
        contradictions.append("non-main role cannot rely on main-meal nutrition exemptions")
    if non_main_role and not non_main_marker:
        contradictions.append("non-main Role requires the [non-main] title marker")
    if non_main_marker and not non_main_role:
        contradictions.append("[non-main] title marker requires a matching non-main Role")
    if contradictions:
        raise DishRuleError(
            "VALIDATION_FAILED",
            "proposed governed changes are semantically inconsistent",
            rule="semantic_proposal_incoherent",
            details={
                "contradictions": contradictions,
                "required_action": (
                    "Reinterpret Marco's intent, resolve every linked contradiction in one candidate, "
                    "and only then request governed authorization."
                ),
            },
        )

def require_governed_authorization(conn, before, after, *, task_gid: str, operation_id: str, proposal_reason: str | None = None) -> tuple[str, ...]:
    validate_semantic_proposal(before, after)
    changes = governed_changes(before, after)
    if not changes:
        return ()
    rows = reserve_marco_authorizations(
        conn, task_gid=task_gid, operation_id=operation_id,
        changes=tuple({"field": c.field, "before": c.before, "after": c.after} for c in changes),
        proposal_reason=proposal_reason,
        linked_changes=tuple(
            {"path": path, "before": old, "after": new}
            for path, (old, new) in canonical_diff(before, after).items()
        ),
    )
    return tuple(row["authorization_id"] for row in rows)


# Canonical materiality is path-first. Text signatures only explain why a
# changed method is material; they never decide whether governed fields count.
_QUANTITY_RE = re.compile(r"(?<!\w)(?:\d+(?:[.,]\d+)?|\d+\s*/\s*\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(?:kg|g|mg|l|ml|cl|tsp|tbsp|teaspoons?|tablespoons?|cups?|oz|lb|eggs?|cloves?|pieces?|minutes?|mins?|hours?|hrs?)\b", re.I)
_RATIO_RE = re.compile(r"(?:\b(?:equal parts?|one|two|three|four|five|six|seven|eight|nine|ten|\d+(?:[.,]\d+)?)\b\s*(?::|/|\bto\b|\bparts?\b)\s*\b(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+(?:[.,]\d+)?)\b)", re.I)
_PORTION_RE = re.compile(r"\b(?:portion|portions|serving|servings|sittings?|feeds?|people|persons?)\b", re.I)
_PORTION_COUNT_RE = re.compile(
    r"(?<!\w)(?:\d+(?:[.,]\d+)?|one|two|three|four|five|six|seven|eight|nine|ten)\s*"
    r"(?:portions?|servings?|sittings?|people|persons?)\b",
    re.I,
)
_NUTRITION_RE = re.compile(r"\b(?:nutrition|nutritional|calorie|calories|kcal|protein|carbohydrate|carbs?|fat|fibre|fiber|sodium)\b", re.I)
_HALAL_SAFETY_RE = re.compile(r"\b(?:halal|haram|pork|bacon|ham|lard|prosciutto|salami|pepperoni|wine|beer|sherry|mirin|alcohol|allergen|allergy|unsafe|safety|cross[- ]contamination|internal temperature)\b", re.I)
_SOURCING_RE = re.compile(r"\b(?:source|sourcing|supplier|import|availability|available|unavailable|substitut(?:e|ion)|brand)\b", re.I)
_EQUIPMENT_RE = re.compile(r"\b(?:wok|oven|stovetop|hob|grill|broiler|air fryer|fryer|pressure cooker|instant pot|slow cooker|sous vide|blender|food processor|mortar|pan|pot)\b", re.I)
_RISK_RE = re.compile(r"\b(?:feasib(?:le|ility)|risk|constraint|temperature|timing|hold time)\b", re.I)
_SHELF_LIFE_RE = re.compile(
    r"(?<!\w)(?:\d+(?:[.,]\d+)?|one|two|three|four|five|six|seven|eight|nine|ten)\s*"
    r"(?:days?|weeks?|months?)\b|\bovernight\b",
    re.I,
)
_STORAGE_CONDITION_RE = re.compile(
    r"\b(?:room temperature|ambient temperature|on the counter|unrefrigerated|"
    r"chill(?:ed|ing)?|refrigerat(?:e|ed|ing|or)|fridge|freez(?:e|er|ing|en))\b",
    re.I,
)

_QUANTITY_MATERIAL_REASONS = frozenset({"quantities", "quantity", "portions"})

_PLANNING_MATERIAL = {
    "Dish candidate": "dish_candidate",
    "Purpose": "purpose_or_test",
    "Role": "role_or_identity",
    "Locks": "locks",
    "Exemptions": "exemptions",
    "Research emphasis": "research_basis",
    "Destination section": "destination",
}
_SECTION_MATERIAL = {
    "WHY COOK IT": "purpose_or_test",
    "WHAT TO BUY": "ingredient_identity",
    "QUANTITIES": "quantities",
    "WHAT SUCCESS LOOKS LIKE": "success_criteria",
}
_HANDLING_SECTIONS = {
    "CHECK BEFORE COOKING",
    "HOW TO COOK IT",
    "WATCH OUT FOR",
    "STORAGE",
}
_HANDLING_WORDS = {
    "gently", "carefully", "briefly", "fresh", "freshly", "separate", "separately",
    "serving", "serve", "sitting", "sittings", "per", "last", "minute", "minutes",
    "immediately", "just", "before", "finish", "finished", "finishing", "early",
    "ahead", "crisp", "raw", "undressed", "unmixed", "covered", "uncovered", "first",
}
_HANDLING_CUE_RE = re.compile(
    r"\b(?:fresh|freshly|reheat(?:ed|ing)?|warm(?:ed|ing)?|separate|separately|"
    r"per sitting|at serving|before serving|after reheating|last minute|until serving|"
    r"just before serving|finish(?:ed|ing)?|garnish(?:ed|ing)?)\b",
    re.I,
)
_HANDLING_SUBJECT_STOPWORDS = _HANDLING_WORDS | {
    "add", "after", "and", "batch", "before", "combine", "dish", "divide", "fold",
    "food", "into", "keep", "mix", "mixed", "reheat", "reheated", "reheating",
    "stored", "stir", "the", "then", "through", "instead", "preserve", "aroma",
}


def _canonical_body(document) -> dict[str, str]:
    values = {f"section.{name}": str(value) for name, value in document.sections.items()}
    values.update({
        "title": document.title,
        "recognition": document.recognition,
        "introduction": "\n".join(document.introduction),
        **{
            f"planning.{field}": str(document.planning_brief.values.get(field, ""))
            for field in document.planning_brief.values
            if field != "Priors"
        },
        "decisions": "\n".join(document.decisions),
        "research_basis": "\n".join(document.research_basis),
    })
    return values


def canonical_diff(before, after) -> dict[str, tuple[str, str]]:
    old, new = _canonical_body(before), _canonical_body(after)
    return {
        path: (old.get(path, ""), new.get(path, ""))
        for path in sorted(set(old) | set(new))
        if old.get(path, "") != new.get(path, "")
    }


def _signature(pattern: re.Pattern[str], text: str) -> tuple[str, ...]:
    return tuple(sorted({m.group(0).casefold().replace(" ", "") for m in pattern.finditer(text)}))


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z]+|\d+(?:[.,]\d+)?", text.casefold())


def _handling_only_change(old: str, new: str) -> bool:
    """Recognise bounded freshness and per-sitting handling without material drift."""
    if any(
        _signature(pattern, old) != _signature(pattern, new)
        for pattern in (
            _QUANTITY_RE,
            _RATIO_RE,
            _PORTION_COUNT_RE,
            _NUTRITION_RE,
            _HALAL_SAFETY_RE,
            _SOURCING_RE,
            _EQUIPMENT_RE,
            _RISK_RE,
            _SHELF_LIFE_RE,
            _STORAGE_CONDITION_RE,
        )
    ):
        return False

    def bounded(lines: list[str]) -> bool:
        return bool(lines) and all(_HANDLING_CUE_RE.search(line) for line in lines)

    def shares_subject(removed: list[str], added: list[str]) -> bool:
        old_subjects = {
            token for token in _tokens(" ".join(removed))
            if len(token) >= 3 and token not in _HANDLING_SUBJECT_STOPWORDS
        }
        new_subjects = {
            token for token in _tokens(" ".join(added))
            if len(token) >= 3 and token not in _HANDLING_SUBJECT_STOPWORDS
        }
        return bool(old_subjects & new_subjects)

    import difflib
    old_lines = [line.strip() for line in old.splitlines() if line.strip()]
    new_lines = [line.strip() for line in new.splitlines() if line.strip()]
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed = old_lines[i1:i2]
        added = new_lines[j1:j2]
        if tag == "insert" and bounded(added):
            continue
        if tag == "delete" and bounded(removed):
            continue
        if tag == "replace" and bounded(added) and (
            bounded(removed) or shares_subject(removed, added)
        ):
            continue
        old_tokens = _tokens(" ".join(removed))
        new_tokens = _tokens(" ".join(added))
        common = list(old_tokens)
        for token in new_tokens:
            if token in common:
                common.remove(token)
        added_tokens = list(new_tokens)
        for token in old_tokens:
            if token in added_tokens:
                added_tokens.remove(token)
        removed_tokens = common
        if not added_tokens and not removed_tokens:
            if old_tokens != new_tokens:
                return False
            continue
        if not set(added_tokens + removed_tokens) <= _HANDLING_WORDS:
            return False
    return True



def _canonical_title_identity(value: str) -> str:
    """Compare title/recognition identity without editorial punctuation or space."""
    return "".join(
        character
        for character in value
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    )


def explicit_material_reasons(before, after) -> tuple[str, ...]:
    """Classify protocol-defined material changes from canonical field ownership."""
    reasons: list[str] = []
    diff = canonical_diff(before, after)
    for path, (old, new) in diff.items():
        if path in {"title", "recognition"}:
            if _canonical_title_identity(old) != _canonical_title_identity(new):
                reasons.append("title_identity" if path == "title" else "title_or_identity")
            continue
        if path == "introduction":
            reasons.append("premise")
            continue
        if path == "decisions":
            reasons.append("decisions")
            continue
        if path == "research_basis":
            reasons.append("research_basis")
            continue
        if path.startswith("planning."):
            field = path.split(".", 1)[1]
            reason = _PLANNING_MATERIAL.get(field)
            if reason:
                reasons.append(reason)
            continue
        if path.startswith("section."):
            section = path.split(".", 1)[1]
            if section in _HANDLING_SECTIONS:
                if _handling_only_change(old, new):
                    continue
                reasons.append(
                    "method"
                    if section == "HOW TO COOK IT"
                    else f"section:{section.casefold().replace(' ', '_')}"
                )
            else:
                reasons.append(_SECTION_MATERIAL.get(section, f"section:{section.casefold().replace(' ', '_')}"))
        # Supplementary explanations for changed values. These do not exempt a
        # structurally material path when the same keyword remains on both sides.
        for name, pattern in (
            ("quantity", _QUANTITY_RE), ("ratio", _RATIO_RE),
            ("portions", _PORTION_RE), ("nutrition", _NUTRITION_RE),
            ("halal_or_safety", _HALAL_SAFETY_RE), ("sourcing", _SOURCING_RE),
            ("equipment_or_method", _EQUIPMENT_RE), ("feasibility_or_risk", _RISK_RE),
        ):
            if pattern.search(old) or pattern.search(new):
                if _signature(pattern, old) != _signature(pattern, new) or old != new:
                    reasons.append(name)
    return tuple(dict.fromkeys(reasons))


def effective_material_change_level(
    requested_level: str, material_reasons: tuple[str, ...]
) -> str:
    """Return the audit level required by the canonical material classifier."""

    if _QUANTITY_MATERIAL_REASONS.intersection(material_reasons):
        return "large"
    return requested_level


def require_small_scope(before, after) -> None:
    changed = []
    if before.state.values["Researched by"] != after.state.values["Researched by"]:
        changed.append("researched_by")
    material = list(explicit_material_reasons(before, after))
    if changed or material:
        from .errors import DishRuleError
        raise DishRuleError(
            "VALIDATION_FAILED",
            "Small correction exceeds its permitted handling-only scope and requires Large correction",
            rule="large_correction_required",
            details={"fields": changed, "material_reasons": material},
        )
