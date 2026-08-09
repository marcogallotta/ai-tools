"""Factual, non-authorizing Stage 4 detail disclosures."""
from __future__ import annotations

from datetime import timezone

from dish_pg.frontend_detail_query import DetailFacts
from dish_service.frontend_contract import DISCLOSURE_BY_CODE, DISCLOSURE_PRESENTATIONS


def detail_disclosures(facts: DetailFacts) -> list[dict[str, str]]:
    by_code: dict[str, list[dict[str, str]]] = {item.code: [] for item in DISCLOSURE_PRESENTATIONS}
    if facts.lease is not None:
        role = facts.lease.actor_role or "actor"
        expiry = facts.lease.expires_at.astimezone(timezone.utc).isoformat(timespec="seconds")
        _add(by_code, "lease", f"{role} lease is {facts.lease.state}; expiry {expiry}.")
    if facts.verification is not None:
        detail = f"Verification cycle is {facts.verification.lifecycle}."
        if facts.verification.outcome:
            detail += " A recorded outcome is present."
        _add(by_code, "verification", detail)
    elif facts.verification_attention:
        _add(by_code, "verification", "Verification is awaiting human review.")
    for hold in facts.holds:
        kind = "Evidence" if hold.kind == "evidence" else "Two-pass"
        _add(by_code, "hold", f"{kind} hold is {hold.state}.")
    if facts.recovery_required:
        _add(by_code, "recovery", "A task-scoped command outcome is uncertain and has no recorded uncertainty resolution.")
    if facts.abandonment is not None:
        _add(by_code, "abandonment", f"Abandonment workflow is {facts.abandonment.state}.")
    if facts.succession_active:
        _add(by_code, "succession", "A successor workflow operation is active.")
    return [item for category in DISCLOSURE_PRESENTATIONS for item in by_code[category.code]]


def _add(target: dict[str, list[dict[str, str]]], code: str, detail: str) -> None:
    target[code].append({"code": code, "label": DISCLOSURE_BY_CODE[code].label, "detail": detail})
