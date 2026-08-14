from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
BUNDLE_WORKFLOW = ROOT / ".github" / "workflows" / "repository-bundle.yml"
SCRIPT = ROOT / "scripts" / "pr_gate.py"
SPEC = importlib.util.spec_from_file_location("pr_gate", SCRIPT)
assert SPEC and SPEC.loader
pr_gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pr_gate
SPEC.loader.exec_module(pr_gate)

HEAD = "a" * 40
NEW_HEAD = "b" * 40
REVIEWED_AT = "2026-08-14T08:00:00Z"


def pr(*, draft: bool = False, head: str = HEAD, number: int = 31):
    return {"number": number, "state": "open", "draft": draft, "head": {"sha": head}}


def statuses(
    *, sha: str = HEAD, state: str | None = "success", run_id: int = 123,
    updated_at: str = "2026-08-14T08:05:00Z",
):
    values = []
    if state is not None:
        values.append({
            "context": pr_gate.REQUIRED_CERTIFICATION_CONTEXT,
            "state": state,
            "updated_at": updated_at,
            "target_url": f"https://github.com/marcogallotta/ai-tools/actions/runs/{run_id}",
        })
    return {"sha": sha, "statuses": values}


def run(
    *, run_id: int = 123, attempt: int = 1,
    started_at: str = "2026-08-14T08:01:00Z",
    status: str = "completed", conclusion: str | None = "success",
    number: int = 31, path: str = pr_gate.REQUIRED_CERTIFICATION_WORKFLOW_PATH,
    event: str = "pull_request_review", head_sha: str = NEW_HEAD,
):
    return {
        "id": run_id,
        "run_attempt": attempt,
        "path": path,
        "event": event,
        # Deliberately unrelated: Stage E must never use workflow head_sha as candidate identity.
        "head_sha": head_sha,
        "pull_requests": [{"number": number}],
        "status": status,
        "conclusion": conclusion,
        "run_started_at": started_at,
    }


def runs(*values):
    return {"workflow_runs": list(values or (run(),))}


def evaluate(*, candidate_pr=None, combined=None, workflow_runs=None, reviewed_at=REVIEWED_AT):
    return pr_gate.evaluate_integration_gate(
        candidate_pr or pr(),
        reviewed_head=HEAD,
        reviewed_at=reviewed_at,
        combined_status=combined or statuses(),
        workflow_runs=workflow_runs or runs(),
    )


def diagnose(*, candidate_pr=None, combined=None, workflow_runs=None, reviewed_at=REVIEWED_AT):
    return pr_gate.diagnose_integration_gate(
        candidate_pr or pr(),
        reviewed_head=HEAD,
        reviewed_at=reviewed_at,
        combined_status=combined or statuses(),
        workflow_runs=workflow_runs or runs(),
    )


def test_review_discovery_contract_unchanged():
    assert pr_gate.is_review_discoverable(pr(draft=False)) is True
    assert pr_gate.is_review_discoverable(pr(draft=True)) is False
    assert pr_gate.is_review_discoverable(pr(draft=True), allow_draft=True) is True


def test_workflow_is_review_planner_first_and_synchronize_only_cancels():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "pull_request_review:" in workflow
    assert "types: [submitted]" in workflow
    assert "types: [synchronize]" in workflow
    assert "github.event.review.commit_id" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "Build exact-head repository certification plan" in workflow
    assert "Validate governed selector map before heavy allocation" in workflow
    assert workflow.index("Build exact-head repository certification plan") < workflow.index(
        "Run selected certification groups"
    )
    assert "uses: ./.github/actions/run-certification" in workflow
    assert "Broad Python tests" not in workflow
    assert "Frontend and tooling" not in workflow
    assert "Native PostgreSQL\n" not in workflow
    assert "Browser acceptance\n" not in workflow
    assert "github.event.pull_request.head.sha" not in workflow


def test_workflow_status_targets_exact_actions_run_and_selected_runner_only():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert workflow.count("context='Dish / exact-head certification'") == 3
    assert workflow.count("actions/runs/$GITHUB_RUN_ID") == 3
    assert "steps.prepare.outputs.candidate_sha" in workflow
    assert "needs.plan.outputs.candidate_sha" in workflow
    assert "required_groups" not in workflow  # execution spec, not duplicated YAML lane authority
    assert "pr-certification-plan-${{ steps.prepare.outputs.candidate_sha }}-${{ github.run_id }}" in workflow
    assert "pr-certification-plan-${{ needs.plan.outputs.candidate_sha }}-${{ github.run_id }}" in workflow
    assert "overwrite: true" in workflow
    assert "if: needs.plan.outputs.run_certification == 'true'" in workflow


def test_repository_bundle_no_longer_triggers_on_pull_requests():
    workflow = BUNDLE_WORKFLOW.read_text(encoding="utf-8")
    trigger = workflow.split("permissions:", 1)[0]
    assert "pull_request:" not in trigger
    assert "push:" in trigger
    assert "workflow_dispatch:" in trigger


def test_gate_uses_formal_review_freshness_and_pr_number_not_workflow_head_sha():
    result = evaluate(workflow_runs=runs(run(head_sha="f" * 40)))
    assert result["ok"] is True
    assert result["certified_sha"] == HEAD
    assert result["required_workflow_run_id"] == 123


def test_moved_head_fails():
    result = diagnose(candidate_pr=pr(head=NEW_HEAD))
    assert result["diagnosis"] == pr_gate.GateDiagnosis.HEAD_MOVED.value
    with pytest.raises(pr_gate.GateError, match="PR head moved"):
        evaluate(candidate_pr=pr(head=NEW_HEAD))


def test_stale_same_head_success_before_newer_review_fails():
    stale = runs(run(started_at="2026-08-14T07:59:59Z"))
    result = diagnose(workflow_runs=stale)
    assert result["diagnosis"] == pr_gate.GateDiagnosis.EVIDENCE_MISSING_OR_STALE.value
    assert "at or after the formal Review" in result["reason"]


def test_same_run_rerun_requires_status_fresher_than_new_attempt():
    result = diagnose(
        combined=statuses(updated_at="2026-08-14T08:04:00Z"),
        workflow_runs=runs(run(attempt=2, started_at="2026-08-14T08:05:00Z")),
    )
    assert result["diagnosis"] == pr_gate.GateDiagnosis.EVIDENCE_MISSING_OR_STALE.value
    assert "absent or stale" in result["reason"]


def test_same_run_rerun_accepts_fresh_terminal_status():
    result = evaluate(
        combined=statuses(updated_at="2026-08-14T08:06:00Z"),
        workflow_runs=runs(run(attempt=2, started_at="2026-08-14T08:05:00Z")),
    )
    assert result["required_workflow_run_attempt"] == 2


def test_newest_review_generation_attempt_cannot_be_masked_by_older_success():
    workflow_runs = runs(
        run(run_id=123, started_at="2026-08-14T08:01:00Z", conclusion="success"),
        run(run_id=124, started_at="2026-08-14T08:10:00Z", conclusion="failure"),
    )
    combined = statuses(run_id=123, updated_at="2026-08-14T08:06:00Z")
    result = diagnose(combined=combined, workflow_runs=workflow_runs)
    assert result["diagnosis"] == pr_gate.GateDiagnosis.FAILED_REQUIRED_CI.value
    assert result["required_workflow_run_id"] == 124


def test_required_status_must_target_newest_exact_run():
    workflow_runs = runs(
        run(run_id=123, started_at="2026-08-14T08:01:00Z"),
        run(run_id=124, started_at="2026-08-14T08:10:00Z"),
    )
    combined = statuses(run_id=123, updated_at="2026-08-14T08:20:00Z")
    result = diagnose(combined=combined, workflow_runs=workflow_runs)
    assert result["diagnosis"] == pr_gate.GateDiagnosis.EVIDENCE_MISSING_OR_STALE.value


def test_required_skipped_or_failed_certification_run_fails():
    for conclusion in ("failure", "cancelled", "skipped"):
        result = diagnose(workflow_runs=runs(run(conclusion=conclusion)))
        assert result["diagnosis"] == pr_gate.GateDiagnosis.FAILED_REQUIRED_CI.value


def test_in_progress_new_review_attempt_is_pending_not_old_success():
    workflow_runs = runs(
        run(run_id=123, started_at="2026-08-14T08:01:00Z"),
        run(run_id=124, started_at="2026-08-14T08:10:00Z", status="in_progress", conclusion=None),
    )
    result = diagnose(workflow_runs=workflow_runs)
    assert result["diagnosis"] == pr_gate.GateDiagnosis.PENDING.value
    assert result["required_workflow_run_id"] == 124


def test_other_pr_or_other_workflow_cannot_certify_candidate():
    result = diagnose(workflow_runs=runs(run(number=999)))
    assert result["diagnosis"] == pr_gate.GateDiagnosis.EVIDENCE_MISSING_OR_STALE.value
    result = diagnose(workflow_runs=runs(run(path=".github/workflows/full-regression.yml")))
    assert result["diagnosis"] == pr_gate.GateDiagnosis.EVIDENCE_MISSING_OR_STALE.value
