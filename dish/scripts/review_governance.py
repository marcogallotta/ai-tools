#!/usr/bin/env python3
"""Mechanical Review V5 evidence/admission checks after semantic classification."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

SCHEMA = "dish-review-governance:v1"
PROJECTION_PATH = Path("dish/docs/agents/review-governance.json")

HUMAN_REQUIRED_CLASSIFICATIONS = {
    "SEMANTIC_REVIEW_GOVERNANCE_CHANGE",
    "CONSEQUENTIAL_HUMAN_OWNED_CHOICE",
}
ELIGIBLE_CLASSIFICATIONS = {
    "ROUTINE_CODE_CORRECTNESS",
    "MECHANICAL_SEMANTIC_PARITY",
    "REPAIRABLE_PROCESS_METADATA",
    "FOLLOW_UP_OR_OBSERVATION",
}
BLOCKING_CLASSIFICATIONS = {
    "SEMANTIC_CURRENT_RISK_BLOCKER",
    "HARD_ADMISSION_BLOCKER",
}
AUTHORIZED_CLASSIFICATIONS = (
    HUMAN_REQUIRED_CLASSIFICATIONS | ELIGIBLE_CLASSIFICATIONS | BLOCKING_CLASSIFICATIONS
)


class GovernanceError(ValueError):
    """The supplied projection or mechanical evidence is contradictory."""


@dataclass(frozen=True, slots=True)
class ReviewGovernanceEvidence:
    generation_id: str
    canonical_sha256: str
    current_generation_id: str
    current_canonical_sha256: str
    material_delta_set_sha256: str
    semantic_sha256: str
    independent_review_generation_id: str | None = None
    independent_review_canonical_sha256: str | None = None
    independent_review_verdict: str | None = None
    independent_review_is_independent: bool = False
    human_decision_generation_id: str | None = None
    human_decision_canonical_sha256: str | None = None
    human_decision_material_delta_set_sha256: str | None = None
    human_decision_ref: str | None = None
    human_decision_sha256: str | None = None
    human_decision_decided_by: str | None = None
    human_decision_recovered: bool = False
    superseded: bool = False


@dataclass(frozen=True, slots=True)
class ReviewGovernanceDecision:
    classification_input: str
    admission: str
    mechanical_evidence_sufficient: bool
    reasons: tuple[str, ...]


def _semantic_payload(projection: Mapping[str, Any]) -> bytes:
    payload = {
        "schema": projection.get("schema"),
        "generation": projection.get("generation"),
        "rules": projection.get("rules"),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def semantic_digest(projection: Mapping[str, Any]) -> str:
    return hashlib.sha256(_semantic_payload(projection)).hexdigest()


def load_projection(repo_root: Path) -> dict[str, Any]:
    projection = json.loads((repo_root / PROJECTION_PATH).read_text(encoding="utf-8"))
    if projection.get("schema") != SCHEMA:
        raise GovernanceError("unsupported Review-governance projection schema")
    rules = projection.get("rules")
    if not isinstance(rules, list) or not rules:
        raise GovernanceError("Review-governance projection requires rules")
    ids = [rule.get("id") for rule in rules]
    if any(not isinstance(rule_id, str) or not rule_id for rule_id in ids):
        raise GovernanceError("every Review-governance rule requires an id")
    if len(ids) != len(set(ids)):
        raise GovernanceError("Review-governance rule ids must be unique")
    actual = semantic_digest(projection)
    if projection.get("semantic_sha256") != actual:
        raise GovernanceError("Review-governance semantic digest mismatch")
    return projection


def validate_contract_parity(repo_root: Path, projection: Mapping[str, Any]) -> tuple[str, ...]:
    failures: list[str] = []
    for rule in projection["rules"]:
        text = rule.get("text")
        anchors = rule.get("anchors")
        if not isinstance(text, str) or not text:
            failures.append(f"{rule.get('id')}: missing rule text")
            continue
        if not isinstance(anchors, list) or not anchors:
            failures.append(f"{rule.get('id')}: missing contract anchors")
            continue
        for anchor in anchors:
            path = repo_root / str(anchor)
            if not path.is_file():
                failures.append(f"{rule['id']}: missing anchor {anchor}")
            elif text not in path.read_text(encoding="utf-8"):
                failures.append(f"{rule['id']}: anchor text mismatch in {anchor}")
    return tuple(failures)


def _exact_generation(evidence: ReviewGovernanceEvidence) -> bool:
    return (
        bool(evidence.generation_id)
        and _is_sha256(evidence.canonical_sha256)
        and _is_sha256(evidence.current_canonical_sha256)
        and _is_sha256(evidence.material_delta_set_sha256)
        and evidence.generation_id == evidence.current_generation_id
        and evidence.canonical_sha256 == evidence.current_canonical_sha256
        and not evidence.superseded
    )


def _independent_pass(evidence: ReviewGovernanceEvidence) -> bool:
    return (
        evidence.independent_review_verdict == "PASS"
        and evidence.independent_review_is_independent
        and evidence.independent_review_generation_id == evidence.generation_id
        and evidence.independent_review_canonical_sha256 == evidence.canonical_sha256
    )


def _exact_human_approval(evidence: ReviewGovernanceEvidence) -> bool:
    return (
        evidence.human_decision_recovered
        and evidence.human_decision_decided_by == "Marco"
        and bool(evidence.human_decision_ref)
        and _is_sha256(evidence.human_decision_sha256)
        and evidence.human_decision_generation_id == evidence.generation_id
        and evidence.human_decision_canonical_sha256 == evidence.canonical_sha256
        and evidence.human_decision_material_delta_set_sha256
        == evidence.material_delta_set_sha256
    )


def _is_sha256(value: str | None) -> bool:
    return bool(value and re.fullmatch(r"[0-9a-f]{64}", value))


def evaluate_admission(
    *,
    classification: str,
    evidence: ReviewGovernanceEvidence,
    projection: Mapping[str, Any],
    parity_failures: Sequence[str] = (),
) -> ReviewGovernanceDecision:
    """Evaluate mechanical admission after an authorized actor supplies classification."""
    if classification not in AUTHORIZED_CLASSIFICATIONS:
        raise GovernanceError("authorized semantic classification is required")
    reasons: list[str] = []
    if evidence.semantic_sha256 != projection.get("semantic_sha256"):
        reasons.append("semantic digest does not match the checked projection")
    if parity_failures:
        reasons.append("standing contract and Review-governance projection disagree")
    if reasons:
        return ReviewGovernanceDecision(
            classification, "MECHANICAL_EVIDENCE_BLOCKED", False, tuple(reasons)
        )
    if classification in BLOCKING_CLASSIFICATIONS:
        return ReviewGovernanceDecision(
            classification, "BLOCKED_BY_AUTHORIZED_CLASSIFICATION", True, ()
        )
    if classification in ELIGIBLE_CLASSIFICATIONS:
        return ReviewGovernanceDecision(classification, "ELIGIBLE_TO_CONTINUE", True, ())
    if not _exact_generation(evidence):
        return ReviewGovernanceDecision(
            classification,
            "MECHANICAL_EVIDENCE_BLOCKED",
            False,
            ("exact current unsuperseded generation identity is not established",),
        )
    if not _independent_pass(evidence):
        return ReviewGovernanceDecision(
            classification,
            "MECHANICAL_EVIDENCE_BLOCKED",
            False,
            ("fresh independent PASS for the exact generation is not established",),
        )
    if not _exact_human_approval(evidence):
        return ReviewGovernanceDecision(
            classification,
            "NEEDS_HUMAN_REVIEW",
            True,
            ("exact durable Marco approval for the current generation is absent or stale",),
        )
    return ReviewGovernanceDecision(classification, "ELIGIBLE_TO_CONTINUE", True, ())


def render_human_impact_report(
    *,
    decision: ReviewGovernanceDecision,
    protected_paths_touched: Sequence[str],
    base_semantic_sha256: str,
    head_semantic_sha256: str,
    parity_failures: Sequence[str],
) -> dict[str, Any]:
    """Render evidence without treating paths or digests as semantic classification."""
    return {
        "schema": "dish-review-governance-impact:v1",
        "classification_source": "authorized-semantic-input",
        "decision": asdict(decision),
        "protected_paths_touched": sorted(set(protected_paths_touched)),
        "base_semantic_sha256": base_semantic_sha256,
        "head_semantic_sha256": head_semantic_sha256,
        "semantic_digest_changed": base_semantic_sha256 != head_semantic_sha256,
        "contract_projection_parity": not parity_failures,
        "parity_failures": list(parity_failures),
        "semantic_classification_inferred": False,
    }
