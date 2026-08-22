#!/usr/bin/env python3
"""Mechanical Review V5 evidence/admission checks after semantic classification."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

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
CLASSIFICATION_RULES = {
    "SEMANTIC_REVIEW_GOVERNANCE_CHANGE": "RV5-HUMAN-01",
    "CONSEQUENTIAL_HUMAN_OWNED_CHOICE": "RV5-HUMAN-01",
    "ROUTINE_CODE_CORRECTNESS": "RV5-ROUTINE-01",
    "MECHANICAL_SEMANTIC_PARITY": "RV5-ROUTINE-01",
    "REPAIRABLE_PROCESS_METADATA": "RV5-TAXONOMY-01",
    "FOLLOW_UP_OR_OBSERVATION": "RV5-TAXONOMY-01",
    "SEMANTIC_CURRENT_RISK_BLOCKER": "RV5-TAXONOMY-01",
    "HARD_ADMISSION_BLOCKER": "RV5-TAXONOMY-01",
}


class GovernanceError(ValueError):
    """The supplied projection or mechanical evidence is contradictory."""


@dataclass(frozen=True, slots=True)
class ReviewGovernanceEvidenceRefs:
    """References resolved by the caller's authoritative Asana/Git read adapter."""

    classification_ref: str
    generation_ref: str
    independent_review_ref: str
    events_ref: str
    human_decision_provenance_ref: str


@dataclass(frozen=True, slots=True)
class ReviewGovernanceDecision:
    classification_input: str
    classification_authority_ref: str
    governing_rule_id: str
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


def _is_sha256(value: str | None) -> bool:
    return bool(value and re.fullmatch(r"[0-9a-f]{64}", value))


def _load_bytes(loader: Callable[[str], bytes], ref: str) -> bytes:
    if not isinstance(ref, str) or not ref.strip():
        raise GovernanceError("authoritative evidence reference is required")
    value = loader(ref)
    if not isinstance(value, bytes):
        raise GovernanceError(f"authoritative loader returned non-bytes for {ref}")
    return value


def _load_record(loader: Callable[[str], bytes], ref: str) -> dict[str, Any]:
    try:
        value = json.loads(_load_bytes(loader, ref))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GovernanceError(f"invalid authoritative JSON record at {ref}") from exc
    if not isinstance(value, dict):
        raise GovernanceError(f"authoritative record at {ref} must be an object")
    return value


def _identity(record: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    return record.get("task_gid"), record.get("generation_id"), record.get("canonical_sha256")


def _reconstruct_human_required_evidence(
    *,
    refs: ReviewGovernanceEvidenceRefs,
    loader: Callable[[str], bytes],
    classification: Mapping[str, Any],
) -> tuple[str, tuple[str, ...]]:
    generation = _load_record(loader, refs.generation_ref)
    if generation.get("schema") != "dish-design-generation:v1":
        return "MECHANICAL_EVIDENCE_BLOCKED", ("generation record schema is not Review V2",)
    expected = _identity(classification)
    if _identity(generation) != expected:
        return "MECHANICAL_EVIDENCE_BLOCKED", ("classification and generation identities disagree",)
    snapshot_ref = generation.get("canonical_snapshot_ref")
    if not isinstance(snapshot_ref, str):
        return "MECHANICAL_EVIDENCE_BLOCKED", (
            "generation lacks a durable canonical snapshot reference",
        )
    snapshot_sha = hashlib.sha256(_load_bytes(loader, snapshot_ref)).hexdigest()
    if snapshot_sha != generation.get("canonical_sha256"):
        return "MECHANICAL_EVIDENCE_BLOCKED", (
            "recovered canonical snapshot digest disagrees with generation",
        )

    review = _load_record(loader, refs.independent_review_ref)
    if review.get("schema") != "dish-review-governance-independent-review:v1":
        return "MECHANICAL_EVIDENCE_BLOCKED", ("independent Review record schema is invalid",)
    if _identity(review) != expected or review.get("verdict") != "PASS":
        return "MECHANICAL_EVIDENCE_BLOCKED", (
            "fresh independent PASS for the exact generation is not established",
        )
    if review.get("independence") != "INDEPENDENT" or not review.get("reviewer_identity"):
        return "MECHANICAL_EVIDENCE_BLOCKED", ("independent reviewer identity is not established",)

    events = _load_record(loader, refs.events_ref)
    if events.get("schema") != "dish-review-governance-events:v1":
        return "MECHANICAL_EVIDENCE_BLOCKED", ("generation event record schema is invalid",)
    if events.get("current_generation_id") != expected[1]:
        return "MECHANICAL_EVIDENCE_BLOCKED", (
            "exact current generation identity is not established",
        )
    items = events.get("events")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        return "MECHANICAL_EVIDENCE_BLOCKED", ("generation event record is malformed",)
    matching = [item for item in items if _identity(item) == expected]
    created = [item for item in matching if item.get("event_type") == "CREATED"]
    if len(created) != 1 or matching[0].get("event_type") != "CREATED":
        return "MECHANICAL_EVIDENCE_BLOCKED", (
            "generation event history lacks one leading CREATED event",
        )
    if any(item.get("event_type") in {"SUPERSEDED", "CANCELLED"} for item in matching):
        return "MECHANICAL_EVIDENCE_BLOCKED", (
            "the approved generation is superseded or cancelled",
        )
    approvals = [item for item in matching if item.get("event_type") == "MARCO_APPROVED"]
    if not approvals:
        return "NEEDS_HUMAN_REVIEW", (
            "exact durable Marco approval for the current generation is absent",
        )
    if len(approvals) != 1:
        return "MECHANICAL_EVIDENCE_BLOCKED", ("approval event history is contradictory",)
    approval = approvals[0]

    decision = _load_record(loader, refs.human_decision_provenance_ref)
    if decision.get("schema") != "dish-human-decision-provenance:v1":
        return "MECHANICAL_EVIDENCE_BLOCKED", ("human-decision provenance schema is invalid",)
    if _identity(decision) != expected:
        return "NEEDS_HUMAN_REVIEW", ("human-decision provenance is stale for this generation",)
    if decision.get("decision_kind") != "MARCO_APPROVAL" or decision.get("decided_by") != "Marco":
        return "NEEDS_HUMAN_REVIEW", ("durable explicit Marco approval is not established",)
    decision_ref = decision.get("decision_ref")
    decision_sha = decision.get("decision_sha256")
    if not isinstance(decision_ref, str) or not _is_sha256(decision_sha):
        return "MECHANICAL_EVIDENCE_BLOCKED", ("human-decision provenance identity is malformed",)
    if hashlib.sha256(_load_bytes(loader, decision_ref)).hexdigest() != decision_sha:
        return "MECHANICAL_EVIDENCE_BLOCKED", (
            "recovered human-decision payload digest disagrees with provenance",
        )
    material_delta = classification.get("material_delta_set_sha256")
    if not _is_sha256(material_delta):
        return "MECHANICAL_EVIDENCE_BLOCKED", (
            "classification lacks an exact material-delta identity",
        )
    if decision.get("material_delta_set_sha256") != material_delta:
        return "NEEDS_HUMAN_REVIEW", ("human decision does not bind the classified material delta",)
    if approval.get("material_delta_set_sha256") != material_delta:
        return "NEEDS_HUMAN_REVIEW", ("approval event does not bind the classified material delta",)
    if (
        approval.get("human_decision_ref") != decision_ref
        or approval.get("human_decision_sha256") != decision_sha
    ):
        return "MECHANICAL_EVIDENCE_BLOCKED", (
            "approval event disagrees with recovered human-decision provenance",
        )
    return "ELIGIBLE_TO_CONTINUE", ()


def _load_classification(
    *,
    refs: ReviewGovernanceEvidenceRefs,
    loader: Callable[[str], bytes],
    projection: Mapping[str, Any],
) -> dict[str, Any]:
    record = _load_record(loader, refs.classification_ref)
    if record.get("schema") != "dish-review-governance-classification:v1":
        raise GovernanceError("authorized semantic classification record is required")
    classification = record.get("classification")
    if classification not in AUTHORIZED_CLASSIFICATIONS:
        raise GovernanceError("authorized semantic classification is required")
    role = record.get("authorized_by_role")
    if role not in {"Review", "Coordinator", "Development Workflow"}:
        raise GovernanceError("classification lacks an authorized standing role")
    rule_id = record.get("governing_rule_id")
    rule_ids = {rule.get("id") for rule in projection.get("rules", [])}
    if rule_id not in rule_ids:
        raise GovernanceError("classification lacks an exact governing rule reference")
    if rule_id != CLASSIFICATION_RULES[classification]:
        raise GovernanceError("classification cites the wrong governing rule")
    if (
        not record.get("task_gid")
        or not record.get("generation_id")
        or not _is_sha256(record.get("canonical_sha256"))
    ):
        raise GovernanceError("classification lacks exact task/generation identity")
    return record


def evaluate_admission(
    *,
    evidence_refs: ReviewGovernanceEvidenceRefs,
    authoritative_loader: Callable[[str], bytes],
    projection: Mapping[str, Any],
    parity_failures: Sequence[str] = (),
) -> ReviewGovernanceDecision:
    """Evaluate admission from records reloaded through an authoritative adapter."""
    record = _load_classification(
        refs=evidence_refs,
        loader=authoritative_loader,
        projection=projection,
    )
    classification = str(record["classification"])
    authority_ref = evidence_refs.classification_ref
    rule_id = str(record["governing_rule_id"])
    reasons: list[str] = []
    if record.get("governance_semantic_sha256") != projection.get("semantic_sha256"):
        reasons.append("semantic digest does not match the checked projection")
    if parity_failures:
        reasons.append("standing contract and Review-governance projection disagree")
    if reasons:
        return ReviewGovernanceDecision(
            classification,
            authority_ref,
            rule_id,
            "MECHANICAL_EVIDENCE_BLOCKED",
            False,
            tuple(reasons),
        )
    if classification in BLOCKING_CLASSIFICATIONS:
        return ReviewGovernanceDecision(
            classification, authority_ref, rule_id, "BLOCKED_BY_AUTHORIZED_CLASSIFICATION", True, ()
        )
    if classification in ELIGIBLE_CLASSIFICATIONS:
        return ReviewGovernanceDecision(
            classification, authority_ref, rule_id, "ELIGIBLE_TO_CONTINUE", True, ()
        )
    evidence_admission, evidence_failures = _reconstruct_human_required_evidence(
        refs=evidence_refs,
        loader=authoritative_loader,
        classification=record,
    )
    if evidence_admission != "ELIGIBLE_TO_CONTINUE":
        return ReviewGovernanceDecision(
            classification,
            authority_ref,
            rule_id,
            evidence_admission,
            evidence_admission != "MECHANICAL_EVIDENCE_BLOCKED",
            evidence_failures,
        )
    return ReviewGovernanceDecision(
        classification, authority_ref, rule_id, "ELIGIBLE_TO_CONTINUE", True, ()
    )


def render_human_impact_report(
    *,
    decision: ReviewGovernanceDecision,
    evidence_refs: ReviewGovernanceEvidenceRefs,
    protected_paths_touched: Sequence[str],
    base_semantic_sha256: str,
    head_semantic_sha256: str,
    parity_failures: Sequence[str],
) -> dict[str, Any]:
    """Render evidence without treating paths or digests as semantic classification."""
    return {
        "schema": "dish-review-governance-impact:v1",
        "classification_source": decision.classification_authority_ref,
        "decision": asdict(decision),
        "authoritative_evidence_refs": asdict(evidence_refs),
        "protected_paths_touched": sorted(set(protected_paths_touched)),
        "base_semantic_sha256": base_semantic_sha256,
        "head_semantic_sha256": head_semantic_sha256,
        "semantic_digest_changed": base_semantic_sha256 != head_semantic_sha256,
        "contract_projection_parity": not parity_failures,
        "parity_failures": list(parity_failures),
        "semantic_classification_inferred": False,
    }
