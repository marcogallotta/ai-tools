from __future__ import annotations

import csv
from pathlib import Path

import pytest

from scripts import review_governance as governance


DISH_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = DISH_ROOT.parent
CONTRACT_TEST = "tests/test_review_governance_contract.py"
PROTECTED_PATHS = {
    "docs/agents/review-governance.json",
    "scripts/review_governance.py",
    "test_selection/ownership.csv",
}
PRIMITIVES = (
    "ReviewGovernanceDecision(",
    "evaluate_admission(",
    "render_human_impact_report(",
)
ALLOWLIST = {
    "dish/scripts/review_governance.py",
    "dish/tests/test_review_governance_contract.py",
}


def _evidence(projection, **overrides):
    values = {
        "generation_id": "review-v5-g8",
        "canonical_sha256": "a" * 64,
        "current_generation_id": "review-v5-g8",
        "current_canonical_sha256": "a" * 64,
        "material_delta_set_sha256": "b" * 64,
        "semantic_sha256": projection["semantic_sha256"],
        "independent_review_generation_id": "review-v5-g8",
        "independent_review_canonical_sha256": "a" * 64,
        "independent_review_verdict": "PASS",
        "independent_review_is_independent": True,
        "human_decision_generation_id": "review-v5-g8",
        "human_decision_canonical_sha256": "a" * 64,
        "human_decision_material_delta_set_sha256": "b" * 64,
        "human_decision_ref": "asana:task:1217743038152520#approval",
        "human_decision_sha256": "c" * 64,
        "human_decision_decided_by": "Marco",
        "human_decision_recovered": True,
    }
    values.update(overrides)
    return governance.ReviewGovernanceEvidence(**values)


def test_projection_digest_and_standing_contract_parity():
    projection = governance.load_projection(REPO_ROOT)
    assert projection["generation"] == "review-v5-g8"
    assert governance.validate_contract_parity(REPO_ROOT, projection) == ()


def test_helper_requires_authorized_semantic_classification():
    projection = governance.load_projection(REPO_ROOT)
    with pytest.raises(governance.GovernanceError, match="classification"):
        governance.evaluate_admission(
            classification="",
            evidence=_evidence(projection),
            projection=projection,
        )


def test_semantic_governance_change_without_exact_approval_needs_human_review():
    projection = governance.load_projection(REPO_ROOT)
    decision = governance.evaluate_admission(
        classification="SEMANTIC_REVIEW_GOVERNANCE_CHANGE",
        evidence=_evidence(projection, human_decision_recovered=False),
        projection=projection,
    )
    assert decision.admission == "NEEDS_HUMAN_REVIEW"


def test_predecessor_approval_and_superseded_generation_are_rejected():
    projection = governance.load_projection(REPO_ROOT)
    stale = governance.evaluate_admission(
        classification="SEMANTIC_REVIEW_GOVERNANCE_CHANGE",
        evidence=_evidence(
            projection,
            human_decision_generation_id="review-v5-g7",
        ),
        projection=projection,
    )
    assert stale.admission == "NEEDS_HUMAN_REVIEW"
    superseded = governance.evaluate_admission(
        classification="SEMANTIC_REVIEW_GOVERNANCE_CHANGE",
        evidence=_evidence(projection, superseded=True),
        projection=projection,
    )
    assert superseded.admission == "MECHANICAL_EVIDENCE_BLOCKED"


def test_human_approval_must_bind_exact_material_delta_and_provenance():
    projection = governance.load_projection(REPO_ROOT)
    for overrides in (
        {"human_decision_material_delta_set_sha256": "d" * 64},
        {"human_decision_ref": None},
        {"human_decision_sha256": "not-a-digest"},
    ):
        decision = governance.evaluate_admission(
            classification="SEMANTIC_REVIEW_GOVERNANCE_CHANGE",
            evidence=_evidence(projection, **overrides),
            projection=projection,
        )
        assert decision.admission == "NEEDS_HUMAN_REVIEW"


@pytest.mark.parametrize(
    "classification",
    [
        "ROUTINE_CODE_CORRECTNESS",
        "MECHANICAL_SEMANTIC_PARITY",
        "REPAIRABLE_PROCESS_METADATA",
        "FOLLOW_UP_OR_OBSERVATION",
    ],
)
def test_non_human_classes_do_not_invent_human_review(classification):
    projection = governance.load_projection(REPO_ROOT)
    decision = governance.evaluate_admission(
        classification=classification,
        evidence=_evidence(
            projection,
            independent_review_verdict=None,
            human_decision_recovered=False,
        ),
        projection=projection,
    )
    assert decision.admission == "ELIGIBLE_TO_CONTINUE"


def test_hard_and_semantic_blockers_remain_blocking():
    projection = governance.load_projection(REPO_ROOT)
    for classification in ("HARD_ADMISSION_BLOCKER", "SEMANTIC_CURRENT_RISK_BLOCKER"):
        decision = governance.evaluate_admission(
            classification=classification,
            evidence=_evidence(projection),
            projection=projection,
        )
        assert decision.admission == "BLOCKED_BY_AUTHORIZED_CLASSIFICATION"


def test_parity_failure_blocks_without_choosing_an_authority():
    projection = governance.load_projection(REPO_ROOT)
    decision = governance.evaluate_admission(
        classification="ROUTINE_CODE_CORRECTNESS",
        evidence=_evidence(projection),
        projection=projection,
        parity_failures=("RV5-HUMAN-01 mismatch",),
    )
    assert decision.admission == "MECHANICAL_EVIDENCE_BLOCKED"


def test_human_impact_report_labels_classification_as_input():
    projection = governance.load_projection(REPO_ROOT)
    decision = governance.evaluate_admission(
        classification="SEMANTIC_REVIEW_GOVERNANCE_CHANGE",
        evidence=_evidence(projection),
        projection=projection,
    )
    report = governance.render_human_impact_report(
        decision=decision,
        protected_paths_touched=["dish/docs/agents/review.md"],
        base_semantic_sha256="b" * 64,
        head_semantic_sha256=projection["semantic_sha256"],
        parity_failures=(),
    )
    assert report["semantic_classification_inferred"] is False
    assert report["classification_source"] == "authorized-semantic-input"


def test_protected_paths_and_ownership_map_select_this_contract_test():
    with (DISH_ROOT / "test_selection/ownership.csv").open(newline="", encoding="utf-8") as handle:
        rows = {row["path"]: row for row in csv.DictReader(handle)}
    for path in PROTECTED_PATHS:
        selected = {value.strip() for value in rows[path]["critical_contract_tests"].split(";")}
        assert CONTRACT_TEST in selected


def test_protected_primitives_stay_inside_validator_executor_allowlist():
    violations = []
    for path in REPO_ROOT.rglob("*.py"):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in ALLOWLIST or any(part.startswith(".") for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        for primitive in PRIMITIVES:
            if primitive in text:
                violations.append(f"{relative}: {primitive}")
    assert violations == []
