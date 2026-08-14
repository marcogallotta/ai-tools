import test_pr_lifecycle as base

pr_lifecycle = base.pr_lifecycle


def test_missing_ci_after_merge_review_is_stale_evidence_not_pending_ci():
    gh = base.FakeGitHub()
    gh.reviews = [base.review()]
    gh.combined_status = {"sha": base.HEAD, "statuses": []}

    state = base.engine(gh, authority=True).inspect(gh.pr)

    assert state.state == pr_lifecycle.LifecycleState.REVIEW_PASSED
    assert (
        state.gate["diagnosis"]
        == pr_lifecycle.pr_gate.GateDiagnosis.EVIDENCE_MISSING_OR_STALE.value
    )
    assert "required certification status" in state.residual_reason


def test_pending_exact_head_ci_waits_in_integration_without_changing_review_verdict():
    gh = base.FakeGitHub()
    gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(status_value="in_progress", conclusion=None)

    state = base.engine(gh, authority=True).inspect(gh.pr)

    assert state.state == pr_lifecycle.LifecycleState.WAITING_CI
    assert state.review_verdict == "MERGE"
    assert state.gate["diagnosis"] == pr_lifecycle.pr_gate.GateDiagnosis.PENDING.value
