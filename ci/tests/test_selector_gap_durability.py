from pathlib import Path

import pytest

import test_pr_certification as certification_tests
import test_impact_graph as graph

cert = certification_tests.module
CANDIDATE = certification_tests.CANDIDATE
ROOT = certification_tests.ROOT


def _gap(gap_id: str = "f" * 64):
    return {
        "gap_id": gap_id,
        "path": "scripts/example.py",
        "classification": "KNOWN_BOUNDARY_FALLBACK",
        "retained_boundaries": ["python-control-plane"],
        "missing_reason": "missing-exact-authoritative-mapping",
        "fallback_boundaries": ["python-control-plane"],
        "base_graph_identity": "a" * 64,
        "candidate_graph_identity": "b" * 64,
        "responsible_graph_surface": "ci/test-impact/edges.json",
        "recurrence_count": 1,
    }


def test_replay_corpus_is_graph_identity_and_self_change_authority():
    original = graph.REPLAY_PATH.read_bytes()
    before = graph.graph_identity()
    graph.REPLAY_PATH.write_bytes(original + b"\n")
    try:
        after = graph.graph_identity()
    finally:
        graph.REPLAY_PATH.write_bytes(original)
    assert before != after
    assert "ci/test-impact/replay.json" in graph.GRAPH_SELF_PATHS


def test_replay_only_candidate_cannot_narrow_its_own_proof_authority():
    path = "ci/test-impact/replay.json"
    base = graph.build_legacy_envelope([path], provenance="base")
    candidate = graph.build_legacy_envelope([path], provenance="candidate")
    plan = graph.build_graph_plan(
        [path], base_envelope=base, candidate_envelope=candidate,
        base_paths=[path], candidate_paths=[path],
    )
    assert plan["all_boundary_fallback"] is True
    assert plan["all_boundary_fallback_reasons"] == [
        "base-arbiter-union-unavailable-for-self-change"
    ]


def test_selector_gap_history_increments_and_dedupes_durable_comment():
    gap = _gap()
    comments = [{
        "id": 77,
        "body": f"<!-- dish-selector-gap:v1 gap={gap['gap_id']} recurrence=3 -->\nold",
    }]
    payload = {"selector_gaps": [gap]}
    identity = {"pr_number": 31, "review_id": 88}
    cert.bind_selector_gap_evidence(
        payload, identity=identity, candidate=CANDIDATE,
        run_id="9", run_attempt="1", comments=comments,
    )
    assert gap["recurrence_count"] == 4
    ops = cert.selector_gap_comment_operations(payload, comments)
    assert ops[0]["existing_comment_id"] == 77
    assert "recurrence 4" in ops[0]["body"]
    assert "1217519337411939" in ops[0]["body"]
    assert "1217627893179712" in ops[0]["body"]


def test_selector_gap_duplicate_durable_comments_fail_closed():
    gap_id = "f" * 64
    comments = [
        {"id": 1, "body": f"<!-- dish-selector-gap:v1 gap={gap_id} recurrence=1 -->"},
        {"id": 2, "body": f"<!-- dish-selector-gap:v1 gap={gap_id} recurrence=2 -->"},
    ]
    with pytest.raises(cert.PRCertificationError, match="duplicate durable comments"):
        cert._selector_gap_prior(comments)


def test_exact_head_workflow_persists_selector_gaps_on_existing_pr_surface():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "issues: write" in workflow
    assert "Load durable selector-gap history" in workflow
    assert "Persist durable selector-gap debt" in workflow
    assert "issues/comments/$comment_id" in workflow
