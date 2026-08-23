#!/usr/bin/env python3
"""Mechanical Review V5 evidence/admission checks after semantic classification."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
DISH_ROOT = Path(__file__).resolve().parents[1]
ROOT_SCRIPTS = REPO_ROOT / "scripts"
if str(DISH_ROOT) not in sys.path:
    sys.path.insert(0, str(DISH_ROOT))
if str(ROOT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(ROOT_SCRIPTS))

from review_design_lineage import (  # noqa: E402
    Event,
    EventType,
    Generation,
    HumanDecisionProvenance,
    Identity,
    State,
    digest,
    parse_record_envelope,
    reconstruct,
)

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
AUTHORIZED_CLASSIFICATION_ROLES = {"Review", "Coordinator", "Development Workflow"}
_EVIDENCE_SEAL = object()
_REVIEW_SEAL = object()


class GovernanceError(ValueError):
    """The supplied projection or mechanical evidence is contradictory."""


@dataclass(frozen=True, slots=True)
class AuthorizedClassification:
    """Semantic input supplied by a governing standing role, never inferred here."""

    classification: str
    authorized_by_role: str
    governing_rule_id: str
    evidence_ref: str
    governance_semantic_sha256: str
    material_delta_set_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewV2AuthorityRefs:
    """Exact durable references accepted by the concrete Asana resolver."""

    task_gid: str
    generation_story_gid: str
    independent_review_story_gid: str


@dataclass(frozen=True, slots=True)
class RecoveredIndependentDesignReview:
    identity: Identity
    review_ref: str
    review_sha256: str
    reviewer_identity: str
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _REVIEW_SEAL:
            raise GovernanceError(
                "independent Review evidence must come from durable-payload reconstruction"
            )


@dataclass(frozen=True, slots=True)
class ReconstructedReviewV2Evidence:
    """Sealed output of canonical Review V2 reconstruction and payload validation."""

    generation: Generation
    reconstruction_state: State | None
    valid_event_gids: tuple[str, ...]
    current_approval_event: Event | None
    current_human_decision: HumanDecisionProvenance | None
    independent_review: RecoveredIndependentDesignReview
    source_refs: tuple[str, ...]
    repairable_provenance_event_gids: tuple[str, ...]
    blocking_contradictions: tuple[str, ...]
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _EVIDENCE_SEAL:
            raise GovernanceError("Review V2 evidence must come from canonical reconstruction")


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
    if projection.get("semantic_sha256") != semantic_digest(projection):
        raise GovernanceError("Review-governance semantic digest mismatch")
    return projection


def validate_contract_parity(
    repo_root: Path,
    projection: Mapping[str, Any],
) -> tuple[str, ...]:
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


def _recover_independent_design_review(
    *,
    identity: Identity,
    review_ref: str,
    review_payload: bytes,
    cumulative_material_authors: Sequence[str],
) -> RecoveredIndependentDesignReview:
    """Bind independent PASS evidence to durable bytes and exact four-part identity."""
    try:
        text = review_payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GovernanceError("independent Review payload must be UTF-8") from exc
    required = (
        "INDEPENDENT AGENTIC DESIGN REVIEW — PASS",
        "VERDICT: PASS",
        f"- Task: {identity.task_gid}",
        f"- Generation: {identity.generation_id}",
        f"- Canonical SHA-256: {identity.canonical_sha256}",
        "INDEPENDENCE",
        "does not remember or recover material authorship",
    )
    if identity.relevant_repo_baseline is not None:
        required += (f"- Candidate baseline: {identity.relevant_repo_baseline}",)
    missing = [token for token in required if token not in text]
    if missing:
        raise GovernanceError(
            "durable independent Review evidence is incomplete: " + ", ".join(missing)
        )
    reviewer = next(
        (line.strip() for line in reversed(text.splitlines()) if line.startswith("— Dish Agent:")),
        "",
    )
    if not reviewer:
        raise GovernanceError("durable independent Review lacks reviewer attribution")
    authors = {value.strip() for value in cumulative_material_authors if value.strip()}
    if reviewer in authors:
        raise GovernanceError("material author cannot supply independent Design Review")
    return RecoveredIndependentDesignReview(
        identity=identity,
        review_ref=review_ref,
        review_sha256=digest(review_payload),
        reviewer_identity=reviewer,
        _seal=_REVIEW_SEAL,
    )


def extract_exact_decision_payload(story_payload: bytes) -> bytes:
    """Recover the exact decision paragraph from the durable Asana story envelope."""
    try:
        text = story_payload.decode("utf-8")
        tail = text.split("Exact decision payload:\n", 1)[1]
        decision = tail.split("\n\nDecision SHA-256:", 1)[0]
    except (UnicodeDecodeError, IndexError) as exc:
        raise GovernanceError("durable story lacks the exact decision-payload boundary") from exc
    if not decision.strip():
        raise GovernanceError("durable decision payload is empty")
    return decision.encode()


def _reconstruct_review_v2_evidence(
    *,
    generation: Generation,
    events: Sequence[Event],
    human_decisions: Sequence[HumanDecisionProvenance],
    canonical_snapshot_payload: bytes,
    decision_payloads: Mapping[str, bytes],
    independent_review: RecoveredIndependentDesignReview,
    source_refs: Sequence[str],
) -> ReconstructedReviewV2Evidence:
    """Use canonical Review V2 types/reconstruction; reject invented wrapper schemas."""
    if digest(canonical_snapshot_payload) != generation.canonical_sha256:
        raise GovernanceError("recovered canonical snapshot digest disagrees with Review V2")
    if independent_review.identity != generation.identity:
        raise GovernanceError("independent Review identity disagrees with Review V2")

    recovered_decisions: dict[str, HumanDecisionProvenance] = {}
    invalid_decision_refs: set[str] = set()
    for decision in human_decisions:
        payload = decision_payloads.get(decision.decision_ref)
        if payload is None or digest(payload) != decision.decision_sha256:
            invalid_decision_refs.add(decision.decision_ref)
            continue
        if decision.identity != generation.identity:
            invalid_decision_refs.add(decision.decision_ref)
            continue
        recovered_decisions[decision.decision_ref] = decision

    rebuilt = reconstruct(generation, events, recovered_decisions)
    valid = set(rebuilt.valid_event_gids)
    valid_approvals = [
        event
        for event in events
        if event.event_gid in valid and event.event_type is EventType.MARCO_APPROVED
    ]
    approval = valid_approvals[-1] if valid_approvals else None
    decision = (
        recovered_decisions.get(approval.human_decision_ref)
        if approval is not None and approval.human_decision_ref is not None
        else None
    )
    repairable: list[str] = []
    blocking: list[str] = []
    for contradiction in rebuilt.contradictions:
        if contradiction.code == "invalid-marco-approval-provenance" and approval is not None:
            repairable.append(contradiction.source)
        else:
            blocking.append(f"{contradiction.code}:{contradiction.source}")
    if invalid_decision_refs and approval is None:
        blocking.extend(f"unrecovered-decision:{ref}" for ref in sorted(invalid_decision_refs))

    return ReconstructedReviewV2Evidence(
        generation=generation,
        reconstruction_state=rebuilt.state,
        valid_event_gids=rebuilt.valid_event_gids,
        current_approval_event=approval,
        current_human_decision=decision,
        independent_review=independent_review,
        source_refs=tuple(source_refs),
        repairable_provenance_event_gids=tuple(repairable),
        blocking_contradictions=tuple(blocking),
        _seal=_EVIDENCE_SEAL,
    )


def _authoritative_task_stories(task_gid: str) -> tuple[Mapping[str, Any], ...]:
    """Read the complete task story collection through the repository Asana backend."""
    try:
        import asana
        from dish_tool.backend import AsanaBackend
    except ImportError as exc:
        raise GovernanceError("the repository Asana authority resolver is unavailable") from exc

    options: dict[str, Any] = {
        "opt_fields": "gid,text,created_by.name,created_at,resource_subtype",
        "limit": 100,
    }
    stories: list[Mapping[str, Any]] = []
    seen_offsets: set[str] = set()
    with AsanaBackend() as backend:
        function = asana.StoriesApi(backend.client()).get_stories_for_task
        while True:
            envelope = backend.call_envelope(
                function,
                task_gid,
                options,
                context=f"Review V2 authority stories for task {task_gid}",
            )
            page = envelope.get("data")
            if not isinstance(page, list) or not all(isinstance(item, Mapping) for item in page):
                raise GovernanceError("Asana returned malformed task-story authority data")
            stories.extend(page)
            next_page = envelope.get("next_page")
            if next_page is None:
                break
            if not isinstance(next_page, Mapping):
                raise GovernanceError("Asana returned malformed task-story pagination")
            offset = str(next_page.get("offset") or "").strip()
            if not offset or offset in seen_offsets:
                raise GovernanceError("Asana task-story pagination did not advance")
            seen_offsets.add(offset)
            options = {**options, "offset": offset}
    return tuple(stories)


def _story_payloads_by_gid(
    task_gid: str,
    stories: Sequence[Mapping[str, Any]],
) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    for story in stories:
        gid = str(story.get("gid") or "").strip()
        text = story.get("text")
        if not gid or not isinstance(text, str):
            continue
        if gid in payloads:
            raise GovernanceError(f"duplicate Asana story authority record {gid}")
        payloads[gid] = text.encode()
    if not payloads:
        raise GovernanceError(f"task {task_gid} has no readable Asana story authority")
    return payloads


def _asana_story_gid(reference: str, *, exact_decision_payload: bool = False) -> str:
    suffix = "#exact-decision-payload" if exact_decision_payload else ""
    match = re.fullmatch(rf"asana-story:(\d+){re.escape(suffix)}", reference)
    if match is None:
        boundary = " with the exact decision-payload boundary" if exact_decision_payload else ""
        raise GovernanceError(f"Review V2 requires an exact Asana story reference{boundary}")
    return match.group(1)


def resolve_review_v2_evidence(
    *,
    refs: ReviewV2AuthorityRefs,
) -> ReconstructedReviewV2Evidence:
    """Resolve and verify Review V2 evidence from one authoritative Asana task history.

    Callers name durable identities only. Canonical records, snapshot bytes, decision
    bytes, source membership, and author evidence are all recovered inside this
    concrete repository-owned authority boundary.
    """
    if not isinstance(refs, ReviewV2AuthorityRefs):
        raise GovernanceError("exact Review V2 authority references are required")
    for label, value in (
        ("task", refs.task_gid),
        ("generation story", refs.generation_story_gid),
        ("independent Review story", refs.independent_review_story_gid),
    ):
        if re.fullmatch(r"\d+", value) is None:
            raise GovernanceError(f"{label} GID must be an exact Asana identifier")

    payloads = _story_payloads_by_gid(
        refs.task_gid,
        _authoritative_task_stories(refs.task_gid),
    )
    try:
        generation_payload = payloads[refs.generation_story_gid]
        review_payload = payloads[refs.independent_review_story_gid]
    except KeyError as exc:
        raise GovernanceError(
            "named Review V2 authority story is not a member of the exact Asana task"
        ) from exc

    generation_records = [
        record
        for record in parse_record_envelope(generation_payload)
        if isinstance(record, Generation)
    ]
    if len(generation_records) != 1:
        raise GovernanceError("generation authority story must contain exactly one Generation")
    generation = generation_records[0]
    if generation.task_gid != refs.task_gid:
        raise GovernanceError("generation authority belongs to a different Asana task")

    records = [
        record
        for payload in payloads.values()
        for record in parse_record_envelope(payload)
    ]
    events = [
        record
        for record in records
        if isinstance(record, Event) and record.identity == generation.identity
    ]
    decisions = [
        record
        for record in records
        if isinstance(record, HumanDecisionProvenance)
        and record.identity == generation.identity
    ]

    if generation.canonical_snapshot is not None:
        snapshot_payload = generation.canonical_snapshot.encode()
        snapshot_ref = f"asana-story:{refs.generation_story_gid}#inline-canonical-snapshot"
    else:
        assert generation.canonical_snapshot_ref is not None
        snapshot_gid = _asana_story_gid(generation.canonical_snapshot_ref)
        try:
            snapshot_payload = payloads[snapshot_gid]
        except KeyError as exc:
            raise GovernanceError(
                "canonical snapshot story is not a member of the exact Asana task"
            ) from exc
        snapshot_ref = generation.canonical_snapshot_ref

    decision_payloads: dict[str, bytes] = {}
    for decision in decisions:
        try:
            decision_gid = _asana_story_gid(
                decision.decision_ref,
                exact_decision_payload=True,
            )
            decision_payloads[decision.decision_ref] = extract_exact_decision_payload(
                payloads[decision_gid]
            )
        except (GovernanceError, KeyError):
            # Historical malformed records remain reconstruction evidence, but
            # cannot become recovered human-decision authority.
            continue

    material_authors = {
        generation.created_by.strip(),
        *(event.actor.strip() for event in events),
    }
    independent = _recover_independent_design_review(
        identity=generation.identity,
        review_ref=f"asana-story:{refs.independent_review_story_gid}",
        review_payload=review_payload,
        cumulative_material_authors=tuple(material_authors),
    )
    source_refs = {
        f"asana-story:{refs.generation_story_gid}",
        f"asana-story:{refs.independent_review_story_gid}",
        snapshot_ref,
    }
    for gid, payload in payloads.items():
        if any(
            getattr(record, "identity", None) == generation.identity
            for record in parse_record_envelope(payload)
        ):
            source_refs.add(f"asana-story:{gid}")
    source_refs.update(decision_payloads)

    return _reconstruct_review_v2_evidence(
        generation=generation,
        events=events,
        human_decisions=decisions,
        canonical_snapshot_payload=snapshot_payload,
        decision_payloads=decision_payloads,
        independent_review=independent,
        source_refs=tuple(sorted(source_refs)),
    )


def _validate_classification(
    classification: AuthorizedClassification,
    projection: Mapping[str, Any],
) -> None:
    if not isinstance(classification, AuthorizedClassification):
        raise GovernanceError("authorized semantic classification object is required")
    if classification.classification not in AUTHORIZED_CLASSIFICATIONS:
        raise GovernanceError("authorized semantic classification is required")
    if classification.authorized_by_role not in AUTHORIZED_CLASSIFICATION_ROLES:
        raise GovernanceError("classification lacks an authorized standing role")
    if not classification.evidence_ref.strip():
        raise GovernanceError("classification lacks an exact evidence reference")
    rule_ids = {rule.get("id") for rule in projection.get("rules", [])}
    if classification.governing_rule_id not in rule_ids:
        raise GovernanceError("classification lacks an exact governing rule reference")
    if classification.governing_rule_id != CLASSIFICATION_RULES[classification.classification]:
        raise GovernanceError("classification cites the wrong governing rule")
    if (
        classification.material_delta_set_sha256 is not None
        and not _is_sha256(classification.material_delta_set_sha256)
    ):
        raise GovernanceError("classification material-delta identity is malformed")


def _human_required_admission(
    classification: AuthorizedClassification,
    evidence: ReconstructedReviewV2Evidence | None,
) -> tuple[str, bool, tuple[str, ...]]:
    if not isinstance(evidence, ReconstructedReviewV2Evidence):
        return (
            "MECHANICAL_EVIDENCE_BLOCKED",
            False,
            ("canonical reconstructed Review V2 evidence is required",),
        )
    if evidence.blocking_contradictions:
        return (
            "MECHANICAL_EVIDENCE_BLOCKED",
            False,
            ("Review V2 reconstruction contains unresolved contradictions",),
        )
    if evidence.reconstruction_state in {State.SUPERSEDED, State.CANCELLED}:
        return (
            "MECHANICAL_EVIDENCE_BLOCKED",
            False,
            ("the approved Review V2 generation is no longer current",),
        )
    approval = evidence.current_approval_event
    decision = evidence.current_human_decision
    if approval is None or decision is None:
        return (
            "NEEDS_HUMAN_REVIEW",
            True,
            ("exact durable Marco approval for the current generation is absent",),
        )
    material_delta = classification.material_delta_set_sha256
    if material_delta is None:
        return (
            "MECHANICAL_EVIDENCE_BLOCKED",
            False,
            ("human-required classification lacks exact material-delta identity",),
        )
    if (
        approval.identity != evidence.generation.identity
        or decision.identity != evidence.generation.identity
        or approval.material_delta_set_sha256 != material_delta
        or decision.material_delta_set_sha256 != material_delta
    ):
        return (
            "NEEDS_HUMAN_REVIEW",
            True,
            ("approval does not bind the exact Review V2 identity and material delta",),
        )
    return "ELIGIBLE_TO_CONTINUE", True, ()


def evaluate_admission(
    *,
    classification: AuthorizedClassification,
    projection: Mapping[str, Any],
    parity_failures: Sequence[str] = (),
) -> ReviewGovernanceDecision:
    """Evaluate classes that do not consume human-decision evidence.

    Human-required admission is deliberately unavailable on this surface: only
    ``resolve_review_v2_admission`` may retrieve authority and issue that result.
    """
    _validate_classification(classification, projection)
    reasons: list[str] = []
    if classification.governance_semantic_sha256 != projection.get("semantic_sha256"):
        reasons.append("semantic digest does not match the checked projection")
    if parity_failures:
        reasons.append("standing contract and Review-governance projection disagree")
    if reasons:
        admission, sufficient = "MECHANICAL_EVIDENCE_BLOCKED", False
    elif classification.classification in BLOCKING_CLASSIFICATIONS:
        admission, sufficient = "BLOCKED_BY_AUTHORIZED_CLASSIFICATION", True
    elif classification.classification in ELIGIBLE_CLASSIFICATIONS:
        admission, sufficient = "ELIGIBLE_TO_CONTINUE", True
    else:
        admission, sufficient = "MECHANICAL_EVIDENCE_BLOCKED", False
        reasons.append("the authoritative Asana admission resolver is required")
    return ReviewGovernanceDecision(
        classification.classification,
        classification.evidence_ref,
        classification.governing_rule_id,
        admission,
        sufficient,
        tuple(reasons),
    )


def resolve_review_v2_admission(
    *,
    refs: ReviewV2AuthorityRefs,
    classification: AuthorizedClassification,
    projection: Mapping[str, Any],
    parity_failures: Sequence[str] = (),
) -> ReviewGovernanceDecision:
    """Issue admission after retrieving human-review authority inside the resolver."""
    _validate_classification(classification, projection)
    if classification.classification not in HUMAN_REQUIRED_CLASSIFICATIONS:
        return evaluate_admission(
            classification=classification,
            projection=projection,
            parity_failures=parity_failures,
        )
    reasons: list[str] = []
    if classification.governance_semantic_sha256 != projection.get("semantic_sha256"):
        reasons.append("semantic digest does not match the checked projection")
    if parity_failures:
        reasons.append("standing contract and Review-governance projection disagree")
    if reasons:
        admission, sufficient = "MECHANICAL_EVIDENCE_BLOCKED", False
    else:
        evidence = resolve_review_v2_evidence(refs=refs)
        admission, sufficient, failures = _human_required_admission(
            classification,
            evidence,
        )
        reasons.extend(failures)
    return ReviewGovernanceDecision(
        classification.classification,
        classification.evidence_ref,
        classification.governing_rule_id,
        admission,
        sufficient,
        tuple(reasons),
    )


def render_human_impact_report(
    *,
    decision: ReviewGovernanceDecision,
    evidence: ReconstructedReviewV2Evidence | None,
    protected_paths_touched: Sequence[str],
    base_semantic_sha256: str,
    head_semantic_sha256: str,
    parity_failures: Sequence[str],
) -> dict[str, Any]:
    """Render evidence without treating paths or digests as semantic classification."""
    authoritative = None
    if evidence is not None:
        authoritative = {
            "review_v2_identity": evidence.generation.identity.tuple(),
            "source_refs": list(evidence.source_refs),
            "independent_review_ref": evidence.independent_review.review_ref,
            "repairable_provenance_event_gids": list(
                evidence.repairable_provenance_event_gids
            ),
        }
    return {
        "schema": "dish-review-governance-impact:v1",
        "classification_source": decision.classification_authority_ref,
        "decision": asdict(decision),
        "authoritative_review_v2_evidence": authoritative,
        "protected_paths_touched": sorted(set(protected_paths_touched)),
        "base_semantic_sha256": base_semantic_sha256,
        "head_semantic_sha256": head_semantic_sha256,
        "semantic_digest_changed": base_semantic_sha256 != head_semantic_sha256,
        "contract_projection_parity": not parity_failures,
        "parity_failures": list(parity_failures),
        "semantic_classification_inferred": False,
    }
