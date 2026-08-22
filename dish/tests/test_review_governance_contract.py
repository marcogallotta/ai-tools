from __future__ import annotations

import csv
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import sys

import pytest

DISH_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = DISH_ROOT.parent
LINEAGE_PATH = REPO_ROOT / "scripts" / "review_design_lineage.py"
if not LINEAGE_PATH.is_file():
    pytest.skip(
        "Review-governance contract requires the enclosing repository's Review V2 source",
        allow_module_level=True,
    )


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lineage = _load_module(
    "review_design_lineage",
    LINEAGE_PATH,
)
governance = _load_module(
    "review_governance_contract_target",
    DISH_ROOT / "scripts" / "review_governance.py",
)
Event = lineage.Event
EventType = lineage.EventType
Generation = lineage.Generation
HumanDecisionProvenance = lineage.HumanDecisionProvenance
State = lineage.State
parse_record_envelope = lineage.parse_record_envelope

CONTRACT_TEST = "tests/test_review_governance_contract.py"
PROTECTED_PATHS = {
    "../scripts/review_design_lineage.py",
    "docs/agents/coordinator.md",
    "docs/agents/development-workflow-asana-mode.md",
    "docs/agents/development-workflow.md",
    "docs/agents/review-governance.json",
    "docs/agents/review.md",
    "docs/architecture/development-workflow/review-certification-integration.md",
    "docs/chatgpt-projects/audit.md",
    "docs/chatgpt-projects/coordinator.md",
    "docs/chatgpt-projects/development-workflow.md",
    "docs/chatgpt-projects/evals.json",
    "docs/chatgpt-projects/implementation.md",
    "docs/chatgpt-projects/integration.md",
    "docs/chatgpt-projects/manifest.json",
    "docs/chatgpt-projects/postgresql-dark-launch.md",
    "docs/chatgpt-projects/review.md",
    "docs/chatgpt-projects/source.json",
    "docs/chatgpt-projects/worker.md",
    "docs/chatgpt-projects/workflow.md",
    "scripts/chatgpt_project_kernels.py",
    "scripts/dish-asana-migration-plan",
    "scripts/review_governance.py",
    "test_selection/ownership.csv",
}
PRIMITIVES = (
    "AuthorizedClassification(",
    "ReconstructedReviewV2Evidence(",
    "ReviewGovernanceDecision(",
    "evaluate_admission(",
    "render_human_impact_report(",
    "HumanDecisionProvenance(",
    "human_decision_mapping(",
    "EventType.MARCO_APPROVED",
    "human_review_pending(",
    "apply_semantic_override(",
    "fast_track_use(",
)
ALLOWLIST = {
    "dish/scripts/chatgpt_project_kernels.py",
    "dish/scripts/dish-asana-migration-plan",
    "dish/scripts/review_governance.py",
    "scripts/review_design_lineage.py",
}
EXECUTABLE_SUFFIXES = {".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".ts", ".tsx"}
TASK = "1217743038152520"
GENERATION = "review-v5-g8"
BASELINE = "34caa1928d0d491c16b8ca5614e4fc4565ab96c0"
DELTA_SHA = "f" * 64


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _story_bundle(projection, *, include_approval=True, corrupt_payload=False):
    snapshot = b"sanitized exact Review V5 G8 canonical snapshot"
    canonical_sha = _sha(snapshot)
    identity = {
        "task_gid": TASK,
        "generation_id": GENERATION,
        "canonical_sha256": canonical_sha,
        "relevant_repo_baseline": BASELINE,
    }
    generation_record = {
        "schema": "dish-design-generation:v1",
        **identity,
        "predecessor_generation_id": "review-v5-g7",
        "canonical_snapshot_ref": "asana-story:snapshot",
        "created_at": "2026-08-22T20:04:58.001Z",
        "created_by": "Dish Agent: Development Workflow | ChatGPT",
    }
    created = {
        "schema": "dish-design-generation-event:v1",
        **identity,
        "event_gid": f"task:{TASK}:{GENERATION}:CREATED",
        "event_type": "CREATED",
        "occurred_at": "2026-08-22T20:04:58.001Z",
        "actor": "Dish Agent: Development Workflow | ChatGPT",
    }
    generation_story = (
        "REVIEW V2 FREEZE / LINEAGE / PROVENANCE\n\n"
        + json.dumps(generation_record, separators=(",", ":"))
        + "\n\n"
        + json.dumps(created, separators=(",", ":"))
        + "\n\nExact candidate frozen in immutable prose envelope."
    ).encode()

    decision_text = "Marco approves the exact sanitized G8 generation and material delta."
    decision_payload = decision_text.encode()
    decision_story = (
        "MARCO HUMAN DECISION — APPROVED\n\n"
        f"Exact decision payload:\n{decision_text}\n\n"
        "Decision SHA-256: durable-record-value"
    ).encode()
    decision_ref = "asana-story:decision#exact-decision-payload"
    decision_sha = _sha(decision_payload)
    provenance = {
        "schema": "dish-human-decision-provenance:v1",
        **identity,
        "decision_ref": decision_ref,
        "decision_sha256": decision_sha,
        "decision_kind": "MARCO_APPROVAL",
        "decided_by": "Marco",
        "material_delta_set_sha256": DELTA_SHA,
    }
    approval = {
        "schema": "dish-design-generation-event:v1",
        **identity,
        "event_gid": f"task:{TASK}:{GENERATION}:MARCO_APPROVED:corrected",
        "event_type": "MARCO_APPROVED",
        "occurred_at": "2026-08-22T21:33:09.526Z",
        "actor": "Dish Agent: Development Workflow | provenance reconciliation",
        "material_delta_set_sha256": DELTA_SHA,
        "human_decision_ref": decision_ref,
        "human_decision_sha256": decision_sha,
    }
    old_bad_provenance = {
        **provenance,
        "decision_ref": "asana-story:decision",
        "decision_sha256": "b" * 64,
    }
    old_bad_approval = {
        **approval,
        "event_gid": f"task:{TASK}:{GENERATION}:MARCO_APPROVED:bad-old-record",
        "human_decision_ref": "asana-story:decision",
        "human_decision_sha256": "b" * 64,
    }
    approval_story = (
        "REVIEW V2 APPROVAL PROVENANCE RECONCILIATION\n\n"
        + json.dumps(provenance, separators=(",", ":"))
        + "\n\n"
        + json.dumps(approval, separators=(",", ":"))
    ).encode()
    old_story = (
        json.dumps(old_bad_provenance, separators=(",", ":"))
        + "\n\n"
        + json.dumps(old_bad_approval, separators=(",", ":"))
    ).encode()
    review_story = f"""INDEPENDENT AGENTIC DESIGN REVIEW — PASS

VERDICT: PASS

EXACT CANDIDATE
- Task: {TASK}
- Generation: {GENERATION}
- Canonical SHA-256: {canonical_sha}
- Candidate baseline: {BASELINE}

INDEPENDENCE
This execution does not remember or recover material authorship of G8.

— Dish Agent: Development Workflow | ChatGPT | independent Design Review""".encode()

    records = list(parse_record_envelope(generation_story))
    if include_approval:
        records.extend(parse_record_envelope(old_story))
        records.extend(parse_record_envelope(approval_story))
    generation = next(value for value in records if isinstance(value, Generation))
    events = [value for value in records if isinstance(value, Event)]
    decisions = [value for value in records if isinstance(value, HumanDecisionProvenance)]
    independent = governance.recover_independent_design_review(
        identity=generation.identity,
        review_ref="asana-story:independent-review",
        review_payload=review_story,
        cumulative_material_authors=("— Dish Agent: Development Workflow | ChatGPT | author",),
    )
    payload = governance.extract_exact_decision_payload(decision_story)
    if corrupt_payload:
        payload = b"fabricated replacement"
    evidence = governance.reconstruct_review_v2_evidence(
        generation=generation,
        events=events,
        human_decisions=decisions,
        canonical_snapshot_payload=snapshot,
        decision_payloads={decision_ref: payload},
        independent_review=independent,
        source_refs=(
            "asana-story:generation",
            "asana-story:independent-review",
            "asana-story:approval-reconciliation",
        ),
    )
    classification = governance.AuthorizedClassification(
        classification="SEMANTIC_REVIEW_GOVERNANCE_CHANGE",
        authorized_by_role="Review",
        governing_rule_id="RV5-HUMAN-01",
        evidence_ref="github-review:formal-classification",
        governance_semantic_sha256=projection["semantic_sha256"],
        material_delta_set_sha256=DELTA_SHA,
    )
    return classification, evidence


def _classification(projection, value):
    return governance.AuthorizedClassification(
        classification=value,
        authorized_by_role="Review",
        governing_rule_id=governance.CLASSIFICATION_RULES[value],
        evidence_ref="github-review:classification",
        governance_semantic_sha256=projection["semantic_sha256"],
        material_delta_set_sha256=DELTA_SHA,
    )


def test_projection_digest_and_standing_contract_parity():
    projection = governance.load_projection(REPO_ROOT)
    assert governance.validate_contract_parity(REPO_ROOT, projection) == ()


def test_real_story_shape_uses_canonical_review_v2_and_exact_four_part_identity():
    projection = governance.load_projection(REPO_ROOT)
    classification, evidence = _story_bundle(projection)
    decision = governance.evaluate_admission(
        classification=classification,
        evidence=evidence,
        projection=projection,
    )
    assert decision.admission == "ELIGIBLE_TO_CONTINUE"
    assert evidence.generation.identity.tuple()[3] == BASELINE
    assert evidence.reconstruction_state is State.MARCO_APPROVED
    assert evidence.repairable_provenance_event_gids == (
        f"task:{TASK}:{GENERATION}:MARCO_APPROVED:bad-old-record",
    )


def test_arbitrary_loader_and_fabricated_wrapper_bundle_are_not_admission_inputs():
    projection = governance.load_projection(REPO_ROOT)
    classification = _classification(projection, "SEMANTIC_REVIEW_GOVERNANCE_CHANGE")
    assert "authoritative_loader" not in inspect.signature(governance.evaluate_admission).parameters
    decision = governance.evaluate_admission(
        classification=classification,
        evidence={"schema": "fabricated", "approval": True},
        projection=projection,
    )
    assert decision.admission == "MECHANICAL_EVIDENCE_BLOCKED"
    assert decision.mechanical_evidence_sufficient is False


def test_direct_construction_of_sealed_reconstruction_is_rejected():
    with pytest.raises(governance.GovernanceError, match="canonical reconstruction"):
        governance.ReconstructedReviewV2Evidence(
            generation=None,
            reconstruction_state=State.MARCO_APPROVED,
            valid_event_gids=(),
            current_approval_event=None,
            current_human_decision=None,
            independent_review=None,
            source_refs=(),
            repairable_provenance_event_gids=(),
            blocking_contradictions=(),
            _seal=object(),
        )


def test_missing_approval_needs_human_review_but_corrupt_claim_is_blocked():
    projection = governance.load_projection(REPO_ROOT)
    classification, missing = _story_bundle(projection, include_approval=False)
    result = governance.evaluate_admission(
        classification=classification,
        evidence=missing,
        projection=projection,
    )
    assert result.admission == "NEEDS_HUMAN_REVIEW"
    _, corrupt = _story_bundle(projection, corrupt_payload=True)
    result = governance.evaluate_admission(
        classification=classification,
        evidence=corrupt,
        projection=projection,
    )
    assert result.admission == "MECHANICAL_EVIDENCE_BLOCKED"


def test_superseded_generation_is_not_current():
    projection = governance.load_projection(REPO_ROOT)
    classification, evidence = _story_bundle(projection)
    generation = evidence.generation
    superseded = Event(
        event_gid="superseded",
        event_type=EventType.SUPERSEDED,
        identity=generation.identity,
        occurred_at="2026-08-22T22:00:00Z",
        actor="Dish Agent: Development Workflow",
        successor_generation_id="review-v5-g9",
    )
    rebuilt = governance.reconstruct_review_v2_evidence(
        generation=generation,
        events=[
            Event("created", EventType.CREATED, generation.identity, "1", "agent"),
            evidence.current_approval_event,
            superseded,
        ],
        human_decisions=[evidence.current_human_decision],
        canonical_snapshot_payload=b"sanitized exact Review V5 G8 canonical snapshot",
        decision_payloads={
            evidence.current_human_decision.decision_ref:
                b"Marco approves the exact sanitized G8 generation and material delta."
        },
        independent_review=evidence.independent_review,
        source_refs=evidence.source_refs,
    )
    result = governance.evaluate_admission(
        classification=classification,
        evidence=rebuilt,
        projection=projection,
    )
    assert result.admission == "MECHANICAL_EVIDENCE_BLOCKED"


@pytest.mark.parametrize("value", sorted(governance.ELIGIBLE_CLASSIFICATIONS))
def test_non_human_classes_do_not_invent_human_review(value):
    projection = governance.load_projection(REPO_ROOT)
    decision = governance.evaluate_admission(
        classification=_classification(projection, value),
        projection=projection,
    )
    assert decision.admission == "ELIGIBLE_TO_CONTINUE"


@pytest.mark.parametrize("value", sorted(governance.BLOCKING_CLASSIFICATIONS))
def test_hard_and_semantic_blockers_remain_blocking(value):
    projection = governance.load_projection(REPO_ROOT)
    decision = governance.evaluate_admission(
        classification=_classification(projection, value),
        projection=projection,
    )
    assert decision.admission == "BLOCKED_BY_AUTHORIZED_CLASSIFICATION"


def test_wrong_rule_and_projection_parity_fail_closed():
    projection = governance.load_projection(REPO_ROOT)
    wrong = governance.AuthorizedClassification(
        classification="ROUTINE_CODE_CORRECTNESS",
        authorized_by_role="Review",
        governing_rule_id="RV5-HUMAN-01",
        evidence_ref="github-review:classification",
        governance_semantic_sha256=projection["semantic_sha256"],
    )
    with pytest.raises(governance.GovernanceError, match="wrong governing rule"):
        governance.evaluate_admission(classification=wrong, projection=projection)
    decision = governance.evaluate_admission(
        classification=_classification(projection, "ROUTINE_CODE_CORRECTNESS"),
        projection=projection,
        parity_failures=("RV5-HUMAN-01 mismatch",),
    )
    assert decision.admission == "MECHANICAL_EVIDENCE_BLOCKED"


def test_human_impact_report_names_canonical_sources():
    projection = governance.load_projection(REPO_ROOT)
    classification, evidence = _story_bundle(projection)
    decision = governance.evaluate_admission(
        classification=classification,
        evidence=evidence,
        projection=projection,
    )
    report = governance.render_human_impact_report(
        decision=decision,
        evidence=evidence,
        protected_paths_touched=["dish/docs/agents/review.md"],
        base_semantic_sha256="b" * 64,
        head_semantic_sha256=projection["semantic_sha256"],
        parity_failures=(),
    )
    assert report["semantic_classification_inferred"] is False
    assert report["classification_source"] == "github-review:formal-classification"
    assert report["authoritative_review_v2_evidence"]["review_v2_identity"][3] == BASELINE


def test_protected_paths_and_ownership_map_select_this_contract_test():
    with (DISH_ROOT / "test_selection" / "ownership.csv").open(newline="") as handle:
        rows = {row["path"]: row for row in csv.DictReader(handle)}
    for path in PROTECTED_PATHS:
        selected = {value.strip() for value in rows[path]["critical_contract_tests"].split(";")}
        assert CONTRACT_TEST in selected, path


def _is_executable_source(path):
    if path.suffix.lower() in EXECUTABLE_SUFFIXES:
        return True
    if path.suffix:
        return False
    try:
        return path.read_bytes().startswith(b"#!")
    except OSError:
        return False


def _structural_violations(root):
    violations = []
    for path in root.rglob("*"):
        if not path.is_file() or not _is_executable_source(path):
            continue
        relative = path.relative_to(root).as_posix()
        if relative in ALLOWLIST:
            continue
        if any(part.startswith(".") for part in path.parts):
            continue
        if "tests" in path.parts or "node_modules" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for primitive in PRIMITIVES:
            if primitive in text:
                violations.append(f"{relative}: {primitive}")
    return violations


def test_protected_primitives_stay_inside_validator_executor_allowlist():
    assert _structural_violations(REPO_ROOT) == []


@pytest.mark.parametrize(
    "primitive",
    (
        "HumanDecisionProvenance(",
        "EventType.MARCO_APPROVED",
        "human_review_pending(",
        "apply_semantic_override(",
        "fast_track_use(",
        "evaluate_admission(",
    ),
)
def test_new_classified_no_extension_executable_trips_each_canonical_seam(
    tmp_path,
    primitive,
):
    consumer = tmp_path / "dish" / "new-consumer"
    consumer.parent.mkdir()
    consumer.write_text(f"#!/usr/bin/env python3\nvalue = {primitive}\n", encoding="utf-8")
    assert _structural_violations(tmp_path) == [f"dish/new-consumer: {primitive}"]
