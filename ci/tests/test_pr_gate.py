from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
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
BASE = "d" * 40
COMPARISON = "c" * 40
REVIEWED_AT = "2026-08-14T08:00:00Z"


def pr(*, draft: bool = False, head: str = HEAD, number: int = 31, base: str = BASE):
    return {"number": number, "state": "open", "draft": draft, "head": {"sha": head}, "base": {"sha": base}}


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



def _quality_comment(*, head: str = HEAD, base: str = BASE, comparison: str = COMPARISON, permission: str = "admin", outcome: str = "PASS"):
    import code_quality_gate as cq
    policy_text = 'version = 1\nenabled = true\n'
    result = {
        "schema": cq.SCHEMA,
        "pr_number": 31,
        "target_base_sha": base,
        "head_sha": head,
        "comparison_base_sha": comparison,
        "policy_source_sha": comparison,
        "policy_digest": hashlib.sha256(policy_text.encode()).hexdigest(),
        "bootstrap": False,
        "outcome": outcome,
    }
    result["result_digest"] = cq._digest(result)
    comment = {
        "body": cq.render_comment(result),
        "user": {"login": "writer"},
        "updated_at": "2026-09-06T20:00:00Z",
    }
    evidence = {
        "comparison_base_sha": comparison,
        "compared_target_base_sha": base,
        "compared_head_sha": head,
        "policy_text": policy_text,
        "policy_source_sha": comparison,
        "bootstrap": False,
        "comments": [comment],
        "permissions": {"writer": permission},
    }
    return comment, evidence


def test_connector_quality_admission_disabled_policy_is_nonblocking():
    evidence = {
        "comparison_base_sha": COMPARISON,
        "compared_target_base_sha": BASE,
        "compared_head_sha": HEAD,
        "policy_text": 'version = 1\nenabled = false\n',
        "policy_source_sha": COMPARISON,
        "bootstrap": False,
        "comments": [],
        "permissions": {},
    }
    result = pr_gate.connector_code_quality_admission(pr(), evidence)
    assert result["admissible"] is True


def test_connector_quality_admission_requires_authorized_exact_head_pass():
    _comment, evidence = _quality_comment()
    assert pr_gate.connector_code_quality_admission(pr(), evidence)["admissible"] is True
    evidence["permissions"] = {"writer": "read"}
    rejected = pr_gate.connector_code_quality_admission(pr(), evidence)
    assert rejected["admissible"] is False
    assert "write permission" in rejected["reason"]


def test_connector_quality_admission_rejects_stale_result():
    _comment, evidence = _quality_comment(head=NEW_HEAD)
    evidence["compared_head_sha"] = HEAD
    rejected = pr_gate.connector_code_quality_admission(pr(), evidence)
    assert rejected["admissible"] is False


def test_local_and_connector_adapters_agree_on_authorized_and_unauthorized_result(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "ci").mkdir()
    policy_text = 'version = 1\nenabled = true\n'
    (repo / "ci/code-quality.toml").write_text(policy_text, encoding="utf-8")
    (repo / "seed.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (repo / "seed.txt").write_text("head\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "head"], cwd=repo, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    candidate = pr(head=head, base=base)
    comment, evidence = _quality_comment(head=head, base=base, comparison=base)
    evidence["policy_text"] = policy_text
    evidence["policy_source_sha"] = base
    local = pr_gate.local_code_quality_admission(repo, candidate, [comment], lambda _login: "admin")
    connector = pr_gate.connector_code_quality_admission(candidate, evidence)
    assert (local["admissible"], local["reason"]) == (connector["admissible"], connector["reason"])
    local_bad = pr_gate.local_code_quality_admission(repo, candidate, [comment], lambda _login: "read")
    evidence["permissions"] = {"writer": "read"}
    connector_bad = pr_gate.connector_code_quality_admission(candidate, evidence)
    assert (local_bad["admissible"], local_bad["reason"]) == (connector_bad["admissible"], connector_bad["reason"])


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
    assert 'target_url="$EVIDENCE_ARTIFACT_URL"' in workflow
    assert "steps.prepare.outputs.candidate_sha" in workflow
    assert "needs.plan.outputs.candidate_sha" in workflow
    assert "required_groups" not in workflow  # execution spec, not duplicated YAML lane authority
    assert "pr-certification-plan-${{ steps.prepare.outputs.candidate_sha }}-${{ github.run_id }}" in workflow
    assert "pr-certification-plan-${{ needs.plan.outputs.candidate_sha }}-${{ github.run_id }}" in workflow
    assert "pr-certification-evidence-${{ needs.plan.outputs.candidate_sha }}-${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert workflow.count("overwrite: true") == 1
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
