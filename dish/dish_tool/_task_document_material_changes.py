"""Validation findings for canonical Material changes audit entries."""

from __future__ import annotations

import re

from ._task_document_syntax import (
    ACTOR_NAME_PATTERN,
    DATE_PATTERN,
    MATERIAL_CHANGE_ACCEPTED_SYNTAX,
    MODEL_PATTERN,
)
from ._task_document_types import DocumentFinding, FindingKind


def material_change_findings(line: str, *, index: int) -> tuple[DocumentFinding, ...]:
    """Return all detectable grammar defects for one seven-field audit entry."""
    findings: list[DocumentFinding] = []
    location = f"Material changes[{index}]"
    parts = line.split(" — ", 6)
    if len(parts) != 7:
        return (
            DocumentFinding(
                "material-changes.format",
                FindingKind.SYNTAX,
                f"Material changes entries require exactly seven fields in this order: {MATERIAL_CHANGE_ACCEPTED_SYNTAX}",
                location,
                current=line,
            ),
            DocumentFinding(
                "material-changes.field-count",
                FindingKind.SYNTAX,
                f"expected seven fields separated by ' — '; found {len(parts)}",
                location,
                current=line,
            ),
        )

    date, agent, model, change, reason, materiality, verification = parts
    if re.fullmatch(DATE_PATTERN, date) is None:
        findings.append(DocumentFinding(
            "material-changes.date", FindingKind.SYNTAX,
            "date must use YYYY-MM-DD", f"{location}.date",
            current=date,
        ))
    if re.fullmatch(ACTOR_NAME_PATTERN, agent) is None:
        findings.append(DocumentFinding(
            "material-changes.agent", FindingKind.SYNTAX,
            "agent must be ChatGPT, Custom GPT, Claude, or Codex", f"{location}.agent",
            current=agent,
        ))
    if not model.strip() or re.fullmatch(MODEL_PATTERN, model) is None:
        findings.append(DocumentFinding(
            "material-changes.model", FindingKind.SYNTAX,
            "model metadata is required and must not contain a comma or em dash", f"{location}.model",
            current=model,
        ))
    if not change.strip():
        findings.append(DocumentFinding(
            "material-changes.change", FindingKind.SYNTAX,
            "change must describe the concrete edit", f"{location}.change",
            current=change,
        ))
    if not reason.strip():
        findings.append(DocumentFinding(
            "material-changes.reason", FindingKind.SYNTAX,
            "reason is required", f"{location}.reason",
            current=reason,
        ))
    if materiality not in {"Small", "Large"}:
        findings.append(DocumentFinding(
            "material-changes.materiality", FindingKind.SYNTAX,
            "materiality must be Small or Large", f"{location}.materiality",
            current=materiality,
        ))

    if verification != "pending-verification":
        verified = re.fullmatch(
            rf"verified — (?P<agent>{ACTOR_NAME_PATTERN}), (?P<model>{MODEL_PATTERN}), (?P<date>{DATE_PATTERN})",
            verification,
        )
        if verified is None:
            findings.append(DocumentFinding(
                "material-changes.verification", FindingKind.SYNTAX,
                "verification must be pending-verification or verified — <agent>, <model metadata>, <YYYY-MM-DD>",
                f"{location}.verification",
                current=verification,
            ))

    if findings:
        findings.insert(0, DocumentFinding(
            "material-changes.format",
            FindingKind.SYNTAX,
            f"Material changes entry must use: {MATERIAL_CHANGE_ACCEPTED_SYNTAX}",
            location,
            current=line,
        ))
    return tuple(findings)
