from __future__ import annotations

import copy
import csv
import hashlib
import json
from pathlib import Path

import pytest

from scripts import review_governance as governance


DISH_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = DISH_ROOT.parent
CONTRACT_TEST = "tests/test_review_governance_contract.py"
PROTECTED_PATHS = {
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
    "scripts/review_governance.py",
    "test_selection/ownership.csv",
}
PRIMITIVES = (
    "ReviewGovernanceDecision(",
    "ReviewGovernanceEvidenceRefs(",
    "evaluate_admission(",
    "render_human_impact_report(",
    "human_decision_provenance_ref",
    "governance_semantic_sha256",
)
ALLOWLIST = {
    "dish/scripts/review_governance.py",
    "dish/tests/test_review_governance_contract.py",
}


def _bundle(projection, classification="SEMANTIC_REVIEW_GOVERNANCE_CHANGE"):
    snapshot = b"exact canonical Review V5 generation"
    decision_payload = b"Marco approves the exact generation and material delta"
    canonical_sha = hashlib.sha256(snapshot).hexdigest()
    decision_sha = hashlib.sha256(decision_payload).hexdigest()
    delta_sha = "b" * 64
    identity = {
        "task_gid": "1217743038152520",
        "generation_id": "review-v5-g8",
        "canonical_sha256": canonical_sha,
    }
    rule = governance.CLASSIFICATION_RULES[classification]
    records = {
        "authority:classification": {
            "schema": "dish-review-governance-classification:v1",
            **identity,
            "classification": classification,
            "authorized_by_role": "Review",
            "governing_rule_id": rule,
            "governance_semantic_sha256": projection["semantic_sha256"],
            "material_delta_set_sha256": delta_sha,
        },
        "authority:generation": {
            "schema": "dish-design-generation:v1",
            **identity,
            "canonical_snapshot_ref": "authority:snapshot",
        },
        "authority:review": {
            "schema": "dish-review-governance-independent-review:v1",
            **identity,
            "verdict": "PASS",
            "independence": "INDEPENDENT",
            "reviewer_identity": "review-agent-42",
        },
        "authority:events": {
            "schema": "dish-review-governance-events:v1",
            "current_generation_id": "review-v5-g8",
            "events": [
                {"schema": "dish-design-generation-event:v1", **identity, "event_type": "CREATED"},
                {
                    "schema": "dish-design-generation-event:v1",
                    **identity,
                    "event_type": "MARCO_APPROVED",
                    "material_delta_set_sha256": delta_sha,
                    "human_decision_ref": "authority:decision-payload",
                    "human_decision_sha256": decision_sha,
                },
            ],
        },
        "authority:provenance": {
            "schema": "dish-human-decision-provenance:v1",
            **identity,
            "decision_ref": "authority:decision-payload",
            "decision_sha256": decision_sha,
            "decision_kind": "MARCO_APPROVAL",
            "decided_by": "Marco",
            "material_delta_set_sha256": delta_sha,
        },
        "authority:snapshot": snapshot,
        "authority:decision-payload": decision_payload,
    }
    refs = governance.ReviewGovernanceEvidenceRefs(
        classification_ref="authority:classification",
        generation_ref="authority:generation",
        independent_review_ref="authority:review",
        events_ref="authority:events",
        human_decision_provenance_ref="authority:provenance",
    )
    return refs, records


def _loader(records):
    def load(ref):
        value = records[ref]
        return value if isinstance(value, bytes) else json.dumps(value, sort_keys=True).encode()

    return load


def _evaluate(
    projection,
    classification="SEMANTIC_REVIEW_GOVERNANCE_CHANGE",
    mutate=None,
    **kwargs,
):
    refs, records = _bundle(projection, classification)
    if mutate:
        mutate(records)
    return governance.evaluate_admission(
        evidence_refs=refs,
        authoritative_loader=_loader(records),
        projection=projection,
        **kwargs,
    )


def test_projection_digest_and_standing_contract_parity():
    projection = governance.load_projection(REPO_ROOT)
    assert projection["generation"] == "review-v5-g8"
    assert governance.validate_contract_parity(REPO_ROOT, projection) == ()


def test_helper_requires_authoritative_semantic_classification_record():
    projection = governance.load_projection(REPO_ROOT)
    refs, records = _bundle(projection)
    records["authority:classification"] = {
        "schema": "caller-assertion",
        "classification": "ROUTINE_CODE_CORRECTNESS",
    }
    with pytest.raises(governance.GovernanceError, match="classification"):
        governance.evaluate_admission(
            evidence_refs=refs,
            authoritative_loader=_loader(records),
            projection=projection,
        )


def test_fabricated_self_consistent_claim_object_is_not_an_admission_input():
    projection = governance.load_projection(REPO_ROOT)
    with pytest.raises(TypeError):
        governance.evaluate_admission(
            classification="SEMANTIC_REVIEW_GOVERNANCE_CHANGE",
            evidence={"human_decision_recovered": True, "decided_by": "Marco"},
            projection=projection,
        )


def test_semantic_governance_change_without_exact_approval_needs_human_review():
    projection = governance.load_projection(REPO_ROOT)

    def remove_approval(records):
        records["authority:events"]["events"] = records["authority:events"]["events"][:1]

    assert _evaluate(projection, mutate=remove_approval).admission == "NEEDS_HUMAN_REVIEW"


def test_predecessor_approval_and_superseded_generation_are_rejected():
    projection = governance.load_projection(REPO_ROOT)

    def stale(records):
        records["authority:provenance"]["generation_id"] = "review-v5-g7"

    assert _evaluate(projection, mutate=stale).admission == "NEEDS_HUMAN_REVIEW"

    def superseded(records):
        event = copy.deepcopy(records["authority:events"]["events"][0])
        event["event_type"] = "SUPERSEDED"
        records["authority:events"]["events"].append(event)

    assert _evaluate(projection, mutate=superseded).admission == "MECHANICAL_EVIDENCE_BLOCKED"


def test_human_approval_must_bind_recovered_payload_and_material_delta():
    projection = governance.load_projection(REPO_ROOT)

    def corrupt_payload(records):
        records["authority:decision-payload"] = b"invented replacement"

    assert _evaluate(projection, mutate=corrupt_payload).admission == "MECHANICAL_EVIDENCE_BLOCKED"

    def stale_delta(records):
        records["authority:provenance"]["material_delta_set_sha256"] = "d" * 64

    assert _evaluate(projection, mutate=stale_delta).admission == "NEEDS_HUMAN_REVIEW"


@pytest.mark.parametrize("classification", sorted(governance.ELIGIBLE_CLASSIFICATIONS))
def test_non_human_classes_do_not_invent_human_review(classification):
    projection = governance.load_projection(REPO_ROOT)
    assert _evaluate(projection, classification).admission == "ELIGIBLE_TO_CONTINUE"


def test_hard_and_semantic_blockers_remain_blocking():
    projection = governance.load_projection(REPO_ROOT)
    for classification in governance.BLOCKING_CLASSIFICATIONS:
        assert (
            _evaluate(projection, classification).admission
            == "BLOCKED_BY_AUTHORIZED_CLASSIFICATION"
        )


def test_parity_failure_blocks_without_choosing_an_authority():
    projection = governance.load_projection(REPO_ROOT)
    decision = _evaluate(
        projection,
        "ROUTINE_CODE_CORRECTNESS",
        parity_failures=("RV5-HUMAN-01 mismatch",),
    )
    assert decision.admission == "MECHANICAL_EVIDENCE_BLOCKED"


def test_human_impact_report_names_authoritative_classification_reference():
    projection = governance.load_projection(REPO_ROOT)
    refs, records = _bundle(projection)
    decision = governance.evaluate_admission(
        evidence_refs=refs,
        authoritative_loader=_loader(records),
        projection=projection,
    )
    report = governance.render_human_impact_report(
        decision=decision,
        evidence_refs=refs,
        protected_paths_touched=["dish/docs/agents/review.md"],
        base_semantic_sha256="b" * 64,
        head_semantic_sha256=projection["semantic_sha256"],
        parity_failures=(),
    )
    assert report["semantic_classification_inferred"] is False
    assert report["classification_source"] == "authority:classification"
    assert (
        report["authoritative_evidence_refs"]["human_decision_provenance_ref"]
        == "authority:provenance"
    )


def test_protected_paths_and_ownership_map_select_this_contract_test():
    with (DISH_ROOT / "test_selection/ownership.csv").open(newline="", encoding="utf-8") as handle:
        rows = {row["path"]: row for row in csv.DictReader(handle)}
    for path in PROTECTED_PATHS:
        selected = {value.strip() for value in rows[path]["critical_contract_tests"].split(";")}
        assert CONTRACT_TEST in selected, path


def _structural_violations(root):
    violations = []
    for path in root.rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        if relative in ALLOWLIST or any(part.startswith(".") for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        for primitive in PRIMITIVES:
            if primitive in text:
                violations.append(f"{relative}: {primitive}")
    return violations


def test_protected_primitives_stay_inside_validator_executor_allowlist():
    assert _structural_violations(REPO_ROOT) == []


def test_new_out_of_allowlist_evidence_consumer_trips_structural_guard(tmp_path):
    consumer = tmp_path / "dish" / "new_consumer.py"
    consumer.parent.mkdir()
    consumer.write_text("value = ReviewGovernanceEvidenceRefs(\n", encoding="utf-8")
    assert _structural_violations(tmp_path) == [
        "dish/new_consumer.py: ReviewGovernanceEvidenceRefs("
    ]
