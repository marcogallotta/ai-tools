from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
SCRIPT = ROOT / "scripts" / "pr_gate.py"
SPEC = importlib.util.spec_from_file_location("pr_gate", SCRIPT)
assert SPEC and SPEC.loader
pr_gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pr_gate)

HEAD = "a" * 40
NEW_HEAD = "b" * 40


def pr(*, draft: bool, head: str = HEAD):
    return {"state": "open", "draft": draft, "head": {"sha": head}}


def statuses(*, sha: str = HEAD, ordinary: str | None = "success", specialized: bool = False):
    values = []
    if specialized:
        values.append({"context": "Repository bundle publication", "state": "success"})
    if ordinary is not None:
        values.append(
            {
                "context": pr_gate.REQUIRED_ORDINARY_CI_CONTEXT,
                "state": ordinary,
                "updated_at": "2026-08-12T19:00:00Z",
                "target_url": "https://github.com/marcogallotta/ai-tools/actions/runs/123",
            }
        )
    return {"sha": sha, "statuses": values}


def test_draft_pr_is_not_review_discoverable_but_explicit_marco_override_is():
    candidate = pr(draft=True)
    assert pr_gate.is_review_discoverable(candidate) is False
    assert pr_gate.is_review_discoverable(candidate, allow_draft=True) is True


def test_ready_pr_is_review_discoverable():
    assert pr_gate.is_review_discoverable(pr(draft=False)) is True


def test_pr_workflow_candidate_identity_is_source_pr_head_sha():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "github.event.pull_request.head.sha" in workflow
    assert "CI_CANDIDATE_SHA:" in workflow
    assert "if: github.event_name != 'pull_request' || github.event.pull_request.draft == false" in workflow
    assert workflow.count("ref: ${{ env.CI_CANDIDATE_SHA }}") == 6
    assert "types: [opened, reopened, synchronize, ready_for_review]" in workflow


def test_evidence_and_required_status_bind_to_candidate_sha_not_github_sha():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "required-ordinary-ci-${{ env.CI_CANDIDATE_SHA }}" in workflow
    assert "python-tests-${{ env.CI_CANDIDATE_SHA }}" in workflow
    assert "frontend-tooling-${{ env.CI_CANDIDATE_SHA }}" in workflow
    assert "native-postgresql-${{ env.CI_CANDIDATE_SHA }}" in workflow
    assert "browser-acceptance-${{ env.CI_CANDIDATE_SHA }}" in workflow
    assert '"candidate_sha": candidate_sha' in workflow
    assert '"workflow_sha": os.environ["GITHUB_SHA"]' in workflow
    assert '"/repos/$GITHUB_REPOSITORY/statuses/$CI_CANDIDATE_SHA"' in workflow
    assert "${{ github.sha }}" not in workflow
    assert "POSTGRES_DB: dish_ai_tools_ci_test" in workflow
    assert "/dish_ai_tools_ci_test" in workflow
    assert workflow.count("uses: ./.github/actions/upload-test-evidence") == 5


def test_each_review_ready_attempt_invalidates_prior_success_and_always_finalizes():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    terminal_gate = workflow.split("  exact-head-ordinary-ci:\n", maxsplit=1)[1]
    assert "exact-head-ordinary-ci-start:" in workflow
    assert "needs: exact-head-ordinary-ci-start" in workflow
    assert "-f state=pending" in workflow
    assert "if: always() && (github.event_name != 'pull_request' || github.event.pull_request.draft == false)" in workflow
    assert terminal_gate.index("- uses: actions/checkout@v6") < terminal_gate.index(
        "- name: Write exact candidate evidence metadata"
    )
    assert "Publish terminal exact-head ordinary CI status" in workflow
    assert '-f state="$status_state"' in workflow
    assert "status_state=failure" in workflow


@pytest.mark.parametrize("new_state", ["pending", "failure", "error"])
def test_same_sha_new_attempt_supersedes_previous_success_and_refuses_integration(new_state):
    combined = {
        "sha": HEAD,
        "statuses": [
            {
                "context": pr_gate.REQUIRED_ORDINARY_CI_CONTEXT,
                "state": "success",
                "updated_at": "2026-08-12T19:00:00Z",
                "target_url": "https://github.com/marcogallotta/ai-tools/actions/runs/123",
            },
            {
                "context": pr_gate.REQUIRED_ORDINARY_CI_CONTEXT,
                "state": new_state,
                "updated_at": "2026-08-12T19:05:00Z",
                "target_url": "https://github.com/marcogallotta/ai-tools/actions/runs/124",
            },
        ],
    }
    with pytest.raises(pr_gate.GateError, match=f"required ordinary CI status is {new_state}"):
        pr_gate.evaluate_integration_gate(
            pr(draft=False), reviewed_head=HEAD, combined_status=combined
        )


def test_mismatched_or_stale_check_sha_refuses_integration():
    with pytest.raises(pr_gate.GateError, match="not reviewed head"):
        pr_gate.evaluate_integration_gate(
            pr(draft=False), reviewed_head=HEAD, combined_status=statuses(sha=NEW_HEAD)
        )


def test_specialized_green_workflow_cannot_replace_required_ordinary_ci():
    with pytest.raises(pr_gate.GateError, match="required ordinary CI status"):
        pr_gate.evaluate_integration_gate(
            pr(draft=False),
            reviewed_head=HEAD,
            combined_status=statuses(ordinary=None, specialized=True),
        )


def test_semantic_head_movement_does_not_transfer_review_or_evidence():
    with pytest.raises(pr_gate.GateError, match="PR head moved"):
        pr_gate.evaluate_integration_gate(
            pr(draft=False, head=NEW_HEAD),
            reviewed_head=HEAD,
            combined_status=statuses(sha=HEAD),
        )


def test_exact_reviewed_head_with_required_ordinary_ci_passes_gate():
    result = pr_gate.evaluate_integration_gate(
        pr(draft=False), reviewed_head=HEAD, combined_status=statuses()
    )
    assert result["ok"] is True
    assert result["certified_sha"] == HEAD
    assert result["required_status_context"] == pr_gate.REQUIRED_ORDINARY_CI_CONTEXT
