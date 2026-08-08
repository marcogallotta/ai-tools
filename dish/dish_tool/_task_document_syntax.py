"""Canonical field, heading, and token syntax for Dish task documents."""

from __future__ import annotations

import re
import unicodedata
from typing import Sequence

PLANNING_FIELDS = (
    "Dish candidate", "Purpose", "Role", "Priors", "Locks", "Exemptions",
    "Research emphasis", "Destination section",
)
STATE_FIELDS = (
    "Status", "Status detail", "Resume status", "Verification protocol release",
    "Researched by", "Verified by", "Self-verified",
)
REQUIRED_SECTIONS = (
    "WHAT TO BUY", "QUANTITIES", "HOW TO COOK IT", "WHAT SUCCESS LOOKS LIKE",
)
OPTIONAL_SECTIONS = ("WHY COOK IT", "CHECK BEFORE COOKING", "WATCH OUT FOR", "STORAGE")
SECTION_ORDER = ("WHY COOK IT", "WHAT TO BUY", "CHECK BEFORE COOKING", "QUANTITIES", "HOW TO COOK IT", "WHAT SUCCESS LOOKS LIKE", "WATCH OUT FOR", "STORAGE")
ALLOWED_SECTIONS = frozenset(SECTION_ORDER)
TOP_LEVEL_HEADINGS = tuple(
    name if name == "WHY COOK IT" else f"## {name}"
    for name in SECTION_ORDER
)
PROCESS_HEADING = "## PROCESS RECORD"
PROCESS_SUBHEADINGS = (
    "### Planning brief",
    "### Decisions",
    "### Research basis",
    "### Material changes",
)
ALLOWED_STATUSES = frozenset(
    {"pending-research", "pending-evidence", "pending-human-review", "pending-verification", "ready"}
)
RESEARCH_BASIS_PREFIXES = (
    "Source-backed dish", "Halal port of ", "Intentional test dish, human-approved",
)
DESTINATION_RE = re.compile(r"^(?P<name>.+?)\s+—\s+(?P<gid>[0-9]+)$")
EXEMPTION_TAG_AT_START_RE = re.compile(r"\A\s*\[([^\]]+)\]")
ALLOWED_EXEMPTION_TAGS = frozenset(
    {"nutrition-kcal", "nutrition-protein", "nutrition-fat"}
)
ACTOR_NAME_PATTERN = r"(?:ChatGPT|Custom GPT|Claude|Codex)"
MODEL_PATTERN = r"[^,—]+"
DATE_PATTERN = r"\d{4}-\d{2}-\d{2}"
ACTOR_RE = re.compile(
    rf"^{ACTOR_NAME_PATTERN} — {MODEL_PATTERN}, {DATE_PATTERN}$"
)
MATERIAL_CHANGE_RE = re.compile(
    rf"^{DATE_PATTERN} — {ACTOR_NAME_PATTERN} — {MODEL_PATTERN} — "
    rf".+ — .+ — (?:Small|Large) — "
    rf"(?:pending-verification|verified — {ACTOR_NAME_PATTERN}, {MODEL_PATTERN}, {DATE_PATTERN})$"
)
MATERIAL_CHANGE_ACCEPTED_SYNTAX = (
    "<YYYY-MM-DD> — <ChatGPT|Custom GPT|Claude|Codex> — "
    "<self-reported model: model> — "
    "<change> — <reason> — <Small|Large> — "
    "<pending-verification|verified — <agent>, <self-reported model: model>, "
    "<YYYY-MM-DD>>"
)


def _authority_label_has_format_characters(label: str) -> bool:
    return any(unicodedata.category(character) == "Cf" for character in label)


def _normalized_authority_label(label: str) -> str:
    without_format_characters = "".join(
        character
        for character in label
        if unicodedata.category(character) != "Cf"
    )
    normalized = unicodedata.normalize("NFKC", without_format_characters)
    return " ".join(normalized.split()).casefold()


def _canonical_authority_label(label: str, names: Sequence[str]) -> str | None:
    normalized = _normalized_authority_label(label)
    return next(
        (name for name in names if _normalized_authority_label(name) == normalized),
        None,
    )


def _authority_field_match(line: str) -> tuple[re.Match[str] | None, bool]:
    """Return an ASCII-colon field match and whether compatibility folding was required."""
    match = re.match(r"^([^:]+):(?:\s*(.*))$", line)
    if match is not None:
        return match, False
    normalized = unicodedata.normalize("NFKC", line)
    if normalized == line:
        return None, False
    return re.match(r"^([^:]+):(?:\s*(.*))$", normalized), True


def _field_label_errors(
    lines: Sequence[str],
    names: Sequence[str],
    *,
    context: str,
    line_numbers: Sequence[int],
) -> list[dict[str, object]]:
    errors: list[dict[str, object]] = []
    for line, line_number in zip(lines, line_numbers):
        match, compatibility_folded = _authority_field_match(line)
        if match is None:
            continue
        label = match.group(1)
        canonical = _canonical_authority_label(label, names)
        if compatibility_folded and canonical is not None:
            errors.append(
                {
                    "rule": f"{context}_field_label_disguised",
                    "field": canonical,
                    "canonical_label": canonical,
                    "line": line_number,
                    "message": (
                        f"{context} field {canonical} uses Unicode compatibility syntax"
                    ),
                }
            )
            continue
        has_format_characters = _authority_label_has_format_characters(label)
        if has_format_characters:
            error: dict[str, object] = {
                "rule": f"{context}_field_label_format_character",
                "label": label,
                "line": line_number,
                "message": (
                    f"{context} field labels must not contain Unicode format characters"
                ),
            }
            if canonical is not None:
                error["field"] = canonical
                error["canonical_label"] = canonical
            errors.append(error)
            continue
        if canonical is None or label == canonical:
            continue
        errors.append(
            {
                "rule": f"{context}_field_label_noncanonical",
                "field": canonical,
                "label": label,
                "canonical_label": canonical,
                "line": line_number,
                "message": (
                    f"non-canonical {context} field label {label}; "
                    f"use {canonical}"
                ),
            }
        )
    return errors


def _canonical_authority_heading(
    heading: str, canonical_headings: Sequence[str]
) -> str | None:
    normalized = _normalized_authority_label(heading)
    return next(
        (
            canonical
            for canonical in canonical_headings
            if _normalized_authority_label(canonical) == normalized
        ),
        None,
    )


def _heading_occurrences(
    lines: Sequence[str],
    canonical_headings: Sequence[str],
    *,
    line_numbers: Sequence[int],
) -> dict[str, list[int]]:
    occurrences: dict[str, list[int]] = {}
    for line, line_number in zip(lines, line_numbers):
        canonical = _canonical_authority_heading(line, canonical_headings)
        if canonical is not None:
            occurrences.setdefault(canonical, []).append(line_number)
    return occurrences


def _duplicate_field_errors(
    lines: Sequence[str],
    names: Sequence[str],
    *,
    context: str,
    line_numbers: Sequence[int],
) -> list[dict[str, object]]:
    occurrences: dict[str, list[int]] = {}
    for line, line_number in zip(lines, line_numbers):
        match, _compatibility_folded = _authority_field_match(line)
        if match:
            canonical = _canonical_authority_label(match.group(1), names)
            if canonical is not None:
                occurrences.setdefault(canonical, []).append(line_number)
    return [
        {
            "rule": f"{context}_field_duplicate",
            "field": label,
            "occurrences": len(positions),
            "lines": positions,
            "message": f"duplicate {context} field {label}",
        }
        for label in names
        if len(positions := occurrences.get(label, [])) > 1
    ]
