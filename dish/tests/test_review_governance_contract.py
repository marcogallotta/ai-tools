from __future__ import annotations

import ast
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
    "resolve_review_v2_admission(",
    "render_human_impact_report(",
    "HumanDecisionProvenance(",
    "human_decision_mapping(",
    "EventType.MARCO_APPROVED",
    "human_review_pending(",
    "apply_semantic_override(",
    "fast_track_use(",
)
CALL_SEAMS = {
    primitive.removesuffix("("): primitive
    for primitive in PRIMITIVES
    if primitive.endswith("(")
}
ATTRIBUTE_SEAMS = {"EventType": {"MARCO_APPROVED": "EventType.MARCO_APPROVED"}}
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


def _story_bundle(
    monkeypatch,
    projection,
    *,
    include_approval=True,
    corrupt_payload=False,
    extra_events=(),
):
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
        "canonical_snapshot_ref": "asana-story:1001",
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
    decision_ref = "asana-story:1003#exact-decision-payload"
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
        "decision_ref": "asana-story:1003",
        "decision_sha256": "b" * 64,
    }
    old_bad_approval = {
        **approval,
        "event_gid": f"task:{TASK}:{GENERATION}:MARCO_APPROVED:bad-old-record",
        "human_decision_ref": "asana-story:1003",
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

    if corrupt_payload:
        decision_story = decision_story.replace(decision_payload, b"fabricated replacement")
    stories = [
        {"gid": "1001", "text": snapshot.decode(), "resource_subtype": "comment_added"},
        {"gid": "1002", "text": generation_story.decode(), "resource_subtype": "comment_added"},
        {"gid": "1003", "text": decision_story.decode(), "resource_subtype": "comment_added"},
        {"gid": "1004", "text": review_story.decode(), "resource_subtype": "comment_added"},
    ]
    if include_approval:
        stories.extend(
            (
                {"gid": "1005", "text": old_story.decode(), "resource_subtype": "comment_added"},
                {"gid": "1006", "text": approval_story.decode(), "resource_subtype": "comment_added"},
            )
        )
    for index, event in enumerate(extra_events, start=10):
        stories.append(
            {
                "gid": str(1000 + index),
                "text": json.dumps(lineage.event_mapping(event), separators=(",", ":")),
                "resource_subtype": "comment_added",
            }
        )
    monkeypatch.setattr(
        governance,
        "_authoritative_task_stories",
        lambda task_gid: tuple(stories) if task_gid == TASK else (),
    )
    refs = governance.ReviewV2AuthorityRefs(
        task_gid=TASK,
        generation_story_gid="1002",
        independent_review_story_gid="1004",
    )
    evidence = governance.resolve_review_v2_evidence(refs=refs)
    classification = governance.AuthorizedClassification(
        classification="SEMANTIC_REVIEW_GOVERNANCE_CHANGE",
        authorized_by_role="Review",
        governing_rule_id="RV5-HUMAN-01",
        evidence_ref="github-review:formal-classification",
        governance_semantic_sha256=projection["semantic_sha256"],
        material_delta_set_sha256=DELTA_SHA,
    )
    return classification, evidence, refs


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


def test_real_story_shape_uses_canonical_review_v2_and_exact_four_part_identity(monkeypatch):
    projection = governance.load_projection(REPO_ROOT)
    classification, evidence, refs = _story_bundle(monkeypatch, projection)
    decision = governance.resolve_review_v2_admission(
        refs=refs,
        classification=classification,
        projection=projection,
    )
    assert decision.admission == "ELIGIBLE_TO_CONTINUE"
    assert evidence.generation.identity.tuple()[3] == BASELINE
    assert evidence.reconstruction_state is State.MARCO_APPROVED
    assert evidence.repairable_provenance_event_gids == (
        f"task:{TASK}:{GENERATION}:MARCO_APPROVED:bad-old-record",
    )


def test_fabricated_canonical_dataclasses_and_bytes_cannot_issue_admission_evidence():
    projection = governance.load_projection(REPO_ROOT)
    classification = _classification(projection, "SEMANTIC_REVIEW_GOVERNANCE_CHANGE")
    assert "authoritative_loader" not in inspect.signature(governance.evaluate_admission).parameters
    assert not hasattr(governance, "recover_independent_design_review")
    assert not hasattr(governance, "reconstruct_review_v2_evidence")
    snapshot = b"caller-fabricated canonical snapshot"
    fabricated_generation = Generation(
        task_gid=TASK,
        generation_id=GENERATION,
        predecessor_generation_id=None,
        canonical_sha256=_sha(snapshot),
        relevant_repo_baseline=BASELINE,
        created_at="2026-08-22T00:00:00Z",
        created_by="fabricated author",
        canonical_snapshot=snapshot.decode(),
    )
    fabricated_approval = Event(
        event_gid="fabricated-approval",
        event_type=EventType.MARCO_APPROVED,
        identity=fabricated_generation.identity,
        occurred_at="2026-08-22T00:00:01Z",
        actor="fabricated actor",
        material_delta_set_sha256=DELTA_SHA,
        human_decision_ref="asana-story:9999#exact-decision-payload",
        human_decision_sha256="a" * 64,
    )
    fabricated_decision = HumanDecisionProvenance(
        decision_ref="asana-story:9999#exact-decision-payload",
        decision_sha256="a" * 64,
        identity=fabricated_generation.identity,
        material_delta_set_sha256=DELTA_SHA,
    )
    with pytest.raises(TypeError):
        governance.resolve_review_v2_evidence(
            refs=governance.ReviewV2AuthorityRefs(TASK, "1002", "1004"),
            generation=fabricated_generation,
            events=(fabricated_approval,),
            human_decisions=(fabricated_decision,),
            canonical_snapshot_payload=snapshot,
            decision_payloads={fabricated_decision.decision_ref: b"fabricated"},
            source_refs=("asana-story:invented",),
        )
    fabricated_review = governance.RecoveredIndependentDesignReview(
        identity=fabricated_generation.identity,
        review_ref="asana-story:9998",
        review_sha256="b" * 64,
        reviewer_identity="fabricated reviewer",
        _seal=governance._REVIEW_SEAL,
    )
    fabricated_evidence = governance.ReconstructedReviewV2Evidence(
        generation=fabricated_generation,
        reconstruction_state=State.MARCO_APPROVED,
        valid_event_gids=(fabricated_approval.event_gid,),
        current_approval_event=fabricated_approval,
        current_human_decision=fabricated_decision,
        independent_review=fabricated_review,
        source_refs=("asana-story:invented",),
        repairable_provenance_event_gids=(),
        blocking_contradictions=(),
        _seal=governance._EVIDENCE_SEAL,
    )
    with pytest.raises(TypeError):
        governance.evaluate_admission(
            classification=classification,
            evidence=fabricated_evidence,
            projection=projection,
        )
    decision = governance.evaluate_admission(
        classification=classification,
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


def test_missing_approval_needs_human_review_but_corrupt_claim_is_blocked(monkeypatch):
    projection = governance.load_projection(REPO_ROOT)
    classification, missing, refs = _story_bundle(
        monkeypatch,
        projection,
        include_approval=False,
    )
    result = governance.resolve_review_v2_admission(
        refs=refs,
        classification=classification,
        projection=projection,
    )
    assert result.admission == "NEEDS_HUMAN_REVIEW"
    _, corrupt, refs = _story_bundle(monkeypatch, projection, corrupt_payload=True)
    result = governance.resolve_review_v2_admission(
        refs=refs,
        classification=classification,
        projection=projection,
    )
    assert result.admission == "MECHANICAL_EVIDENCE_BLOCKED"


def test_superseded_generation_is_not_current(monkeypatch):
    projection = governance.load_projection(REPO_ROOT)
    classification, evidence, _ = _story_bundle(monkeypatch, projection)
    generation = evidence.generation
    superseded = Event(
        event_gid="superseded",
        event_type=EventType.SUPERSEDED,
        identity=generation.identity,
        occurred_at="2026-08-22T22:00:00Z",
        actor="Dish Agent: Development Workflow",
        successor_generation_id="review-v5-g9",
    )
    _, rebuilt, refs = _story_bundle(monkeypatch, projection, extra_events=(superseded,))
    result = governance.resolve_review_v2_admission(
        refs=refs,
        classification=classification,
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


def test_human_impact_report_names_canonical_sources(monkeypatch):
    projection = governance.load_projection(REPO_ROOT)
    classification, evidence, refs = _story_bundle(monkeypatch, projection)
    decision = governance.resolve_review_v2_admission(
        refs=refs,
        classification=classification,
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


def _aliased_python_seams(text):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    imported_names = {}
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for imported in node.names:
                if imported.name in CALL_SEAMS or imported.name in ATTRIBUTE_SEAMS:
                    imported_names[imported.asname or imported.name] = imported.name
        elif isinstance(node, ast.Import):
            for imported in node.names:
                imported_modules.add(imported.asname or imported.name.split(".")[0])

    hits = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                original = imported_names.get(node.func.id)
                if original in CALL_SEAMS:
                    hits.add(CALL_SEAMS[original])
            elif (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in imported_modules
                and node.func.attr in CALL_SEAMS
            ):
                hits.add(CALL_SEAMS[node.func.attr])
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in imported_names
        ):
            original = imported_names[node.value.id]
            marker = ATTRIBUTE_SEAMS.get(original, {}).get(node.attr)
            if marker:
                hits.add(marker)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id in imported_modules
        ):
            marker = ATTRIBUTE_SEAMS.get(node.value.attr, {}).get(node.attr)
            if marker:
                hits.add(marker)
    return hits


def _structural_scan(root):
    violations = []
    scanned = []
    for path in root.rglob("*"):
        if not path.is_file() or not _is_executable_source(path):
            continue
        relative_path = path.relative_to(root)
        relative = relative_path.as_posix()
        if relative in ALLOWLIST:
            continue
        if any(part.startswith(".") for part in relative_path.parts):
            continue
        if "tests" in relative_path.parts or "node_modules" in relative_path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        scanned.append(relative)
        hits = {primitive for primitive in PRIMITIVES if primitive in text}
        if path.suffix.lower() == ".py" or not path.suffix:
            hits.update(_aliased_python_seams(text))
        violations.extend(f"{relative}: {primitive}" for primitive in sorted(hits))
    return violations, scanned


def test_protected_primitives_stay_inside_validator_executor_allowlist():
    violations, scanned = _structural_scan(REPO_ROOT)
    assert violations == []
    assert "dish/scripts/dish-test-plan" in scanned
    assert len(scanned) >= 25


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
    violations, scanned = _structural_scan(tmp_path)
    assert violations == [f"dish/new-consumer: {primitive}"]
    assert scanned == ["dish/new-consumer"]


@pytest.mark.parametrize("symbol", tuple(CALL_SEAMS) + tuple(ATTRIBUTE_SEAMS))
def test_aliased_direct_import_trips_every_protected_seam(tmp_path, symbol):
    consumer = tmp_path / "dish" / "new_consumer.py"
    consumer.parent.mkdir()
    if symbol in CALL_SEAMS:
        source = f"from authority import {symbol} as Alias\nAlias()\n"
        expected = CALL_SEAMS[symbol]
    else:
        source = f"from authority import {symbol} as Alias\nvalue = Alias.MARCO_APPROVED\n"
        expected = "EventType.MARCO_APPROVED"
    consumer.write_text(source, encoding="utf-8")
    violations, scanned = _structural_scan(tmp_path)
    assert violations == [f"dish/new_consumer.py: {expected}"]
    assert scanned == ["dish/new_consumer.py"]


def test_hidden_checkout_ancestor_does_not_suppress_repository_scan(tmp_path):
    root = tmp_path / ".local" / "share" / "owned-worktree"
    consumer = root / "dish" / "new_consumer.py"
    consumer.parent.mkdir(parents=True)
    consumer.write_text(
        "from review_design_lineage import HumanDecisionProvenance as HDP\nHDP()\n",
        encoding="utf-8",
    )
    violations, scanned = _structural_scan(root)
    assert violations == ["dish/new_consumer.py: HumanDecisionProvenance("]
    assert scanned == ["dish/new_consumer.py"]
