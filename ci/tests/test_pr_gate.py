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


def statuses(
    *,
    sha: str = HEAD,
    ordinary: str | None = "success",
    run_id: int = 123,
    updated_at: str = "2026-08-12T19:05:00Z",
    specialized: bool = False,
):
    values = []
    if specialized:
        values.append({"context": "Repository bundle publication", "state": "success"})
    if ordinary is not None:
        values.append(
            {
                "context": pr_gate.REQUIRED_ORDINARY_CI_CONTEXT,
                "state": ordinary,
                "updated_at": updated_at,
                "target_url": f"https://github.com/marcogallotta/ai-tools/actions/runs/{run_id}",
            }
        )
    return {"sha": sha, "statuses": values}


def run(
    *,
    run_id: int = 123,
    attempt: int = 1,
    started_at: str = "2026-08-12T19:00:00Z",
    status: str = "completed",
    conclusion: str | None = "success",
    head: str = HEAD,
    path: str = pr_gate.REQUIRED_ORDINARY_CI_WORKFLOW_PATH,
):
    return {
        "id": run_id,
        "name": "CI",
        "path": path,
        "event": "pull_request",
        "head_sha": head,
        "status": status,
        "conclusion": conclusion,
        "run_attempt": attempt,
        "run_started_at": started_at,
    }


def runs(*values):
    if not values:
        values = (run(),)
    return {"workflow_runs": list(value)}


def evaluate(*, candidate_pr=None, combined=None, workflow_runs=None, reviewed_head=HEAD):
    return pr_gate.evaluate_integration_gate(
        candidate_pr or pr(draft=False),
        reviewed_head=reviewed_head,
        combined_status=combined or statuses(sha=reviewed_head),
        workflow_runs=workflow_runs or runs(run(head=reviewed_head)),
    )


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
    assert workflow.count("ref: ${{ env.CI_CANDIDATE_SHA }}") == 7
    assert "types: [opened, reopened, synchronize, ready_for_review]" in workflow


def test_evidence_and_required_status_bind_to_candidate_sha_not_github_sha():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "required-ordinary-ci-${{ env.CI_CANDIDATE_SHA }}" in workflow
    assert "preflight-${{ env.CI_CANDIDATE_SHA }}" in workflow
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
    assert workflow.count("uses: ./.github/actions/upload-test-evidence") == 6


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


@pytest.mark.parametrize(
    ("new_status", "new_conclusion"),
    [("queued", None), ("in_progress", None), ("completed", "failure"), ("completed", "cancelled")],
)
def test_prior_same_sha_success_cannot_survive_newer_attempt_without_new_status(
    new_status, new_conclusion
):
    combined = statuses(run_id=123, updated_at="2026-08-12T19:05:00Z")
    workflow_runs = runs(
        run(run_id=123, started_at="2026-08-12T19:00:00Z"),
        run(
            run_id=124,
            started_at="2026-08-12T19:10:00Z",
            status=new_status,
            conclusion=new_conclusion,
        ),
    )
    with pytest.raises(pr_gate.GateError, match="newest ordinary CI workflow attempt 124/1"):
        evaluate(combined=combined, workflow_runs=workflow_runs)


def test_same_run_rerun_requires_status_written_after_new_attempt_started():
    combined = statuses(run_id=123, updated_at="2026-08-12T19:15:00Z")
    workflow_runs = runs(
        run(
            run_id=123,
            attempt=2,
            started_at="2026-08-12T19:10:00Z",
            status="completed",
            conclusion="success",
        )
    )
    with pytest.raises(pr_gate.GateError, match="status for the newest workflow attempt is absent or stale"):
        evaluate(combined=combined, workflow_runs=workflow_runs)


def test_same_run_rerun_accepts_fresh_success_from_new_attempt():
    combined = statuses(run_id=123, updated_at="2026-08-12T19:15:00Z")
    workflow_runs = runs(
        run(
            run_id=123,
            attempt=2,
            started_at="2026-08-12T19:10:00Z",
            status="completed",
            conclusion="success",
        )
    )
    result = evaluate(combined=combined, workflow_runs=workflow_runs)
    assert result["required_workflow_run_id"] == 123
    assert result["required_workflow_run_attempt"] == 2


def test_overlapping_older_attempt_finishing_late_does_not_override_newer_attempt_success():
    combined = {
        "sha": HEAD,
        "statuses": [
            {
                "context": pr_gate.REQUIRED_ORDINARY_CI_CONTEXT,
                "state": "success",
                "updated_at": "2026-08-12T19:11:00Z",
                "target_url": "https://github.com/marcogallotta/ai-tools/actions/runs/124",
            },
            {
                "context": pr_gate.REQUIRED_ORDINARY_CI_CONTEXT,
                "state": "failure",
                "updated_at": "2026-08-12T19:20:00Z",
                "target_url": "https://github.com/marcogallotta/ai-tools/actions/runs/123",
            },
        ],
    }
    workflow_runs = runs(
        run(
            run_id=123,
            started_at="2026-08-12T19:00:00Z",
            status="completed",
            conclusion="failure",
        ),
        run(
            run_id=124,
            started_at="2026-08-12T19:10:00Z",
            status="completed",
            conclusion="success",
        ),
    )
    result = evaluate(combined=combined, workflow_runs=workflow_runs)
    assert result["required_workflow_run_id"] == 124
    assert result["required_status_state"] == "success"


@pytest.mark.parametrize("new_state", ["pending", "failure", "error"])
def test_newest_successful_attempt_still_requires_its_terminal_success_status(new_state):
    combined = statuses(
        ordinary=new_state,
        run_id=124,
        updated_at="2026-08-12T19:15:00Z",
    )
    workflow_runs = runs(
        run(run_id=124, started_at="2026-08-12T19:10:00Z", conclusion="success")
    )
    with pytest.raises(
        pr_gate.GateError,
        match=f"required ordinary CI status for newest workflow attempt is {new_state}",
    ):
        evaluate(combined=combined, workflow_runs=workflow_runs)


def test_status_targeting_older_run_cannot_certify_newest_successful_attempt():
    combined = statuses(run_id=123, updated_at="2026-08-12T19:15:00Z")
    workflow_runs = runs(
        run(run_id=123, started_at="2026-08-12T19:00:00Z"),
        run(run_id=124, started_at="2026-08-12T19:10:00Z"),
    )
    with pytest.raises(pr_gate.GateError, match="status for the newest workflow attempt is absent or stale"):
        evaluate(combined=combined, workflow_runs=workflow_runs)


def test_mismatched_or_stale_check_sha_refuses_integration():
    with pytest.raises(pr_gate.GateError, match="not reviewed head"):
        evaluate(combined=statuses(sha=NEW_HEAD)i


def test_specialized_green_workflow_cannot_replace_required_ordinary_ci():
    with pytest.raises(pr_gate.GateError, match="required ordinary CI status"):
        evaluate(combined=statuses(ordinary=None, specialized=True))


def test_newer_specialized_workflow_run_cannot_replace_required_ordinary_ci_attempt():
    workflow_runs = runs(
        run(
            run_id=123,
            started_at="2026-08-12T19:00:00Z",
            status="completed",
            conclusion="failure",
        ),
        run(
            run_id=999,
            started_at="2026-08-12T19:20:00Z",
            status="completed",
            conclusion="success",
            path=".github/workflows/repository-bundle.yml",
        ),
    )
    with pytest.raises(pr_gate.GateError, match="newest ordinary CI workflow attempt 123/1 concluded failure"):
        evaluate(workflow_runs=workflow_runs)


def test_semantic_head_movement_does_not_transfer_review_or_evidence():
    with pytest.raises(pr_gate.GateError, match="PR head moved"):
        evaluate(candidate_pr=pr(draft=False, head=NEW_HEAD))


def test_exact_reviewed_head_with_required_ordinary_ci_passes_gate():
    result = evaluate()
    assert result["ok"] is True
    assert result["certified_sha"] == HEAD
    assert result["required_status_context"] == pr_gate.REQUIRED_ORDINARY_CI_CONTEXT
    assert result["required_workflow_run_id"] == 123
    assert result["required_workflow_run_attempt"] == 1
