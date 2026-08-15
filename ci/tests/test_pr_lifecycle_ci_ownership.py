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


def _ownership_comment(classification, *, evidence="run:700/job:test/signature:x"):
    from urllib.parse import quote
    return {
        "id": 99,
        "body": (
            f"<!-- dish-ci-failure-ownership:v1 head={base.HEAD} "
            f"check={quote('Dish / exact-head certification', safe='')} "
            f"classification={classification} evidence={quote(evidence, safe='')} -->"
        ),
        "created_at": base.NOW.isoformat(),
        "updated_at": base.NOW.isoformat(),
    }


def _failed_ci_state(classification=None):
    gh = base.FakeGitHub()
    gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(conclusion="failure")
    if classification:
        gh.comments = [_ownership_comment(classification)]
    return base.engine(gh, authority=True).inspect(gh.pr)


def test_failed_ci_without_proven_ownership_fails_closed_before_semantic_fix():
    state = _failed_ci_state()
    assert state.state == pr_lifecycle.LifecycleState.REVIEW_PASSED
    assert state.gate["failure_ownership"] == "AMBIGUOUS"
    assert "before any semantic branch mutation" in state.residual_reason


def test_failed_ci_pr_owned_is_the_only_direct_fix_eligible_class():
    state = _failed_ci_state("PR_OWNED")
    assert state.state == pr_lifecycle.LifecycleState.CHANGES_REQUESTED
    assert state.gate["failure_ownership"] == "PR_OWNED"


def test_failed_ci_infrastructure_does_not_route_to_semantic_implementation():
    state = _failed_ci_state("INFRASTRUCTURE")
    assert state.state == pr_lifecycle.LifecycleState.REVIEW_PASSED
    assert state.gate["failure_ownership"] == "INFRASTRUCTURE"
    assert "INFRASTRUCTURE" in state.residual_reason


def test_failed_ci_proven_current_main_requires_external_owner_record_not_candidate_fix():
    state = _failed_ci_state("PROVEN_CURRENT_MAIN")
    assert state.state == pr_lifecycle.LifecycleState.REVIEW_PASSED
    assert state.gate["failure_ownership"] == "PROVEN_CURRENT_MAIN"
    assert "MAIN OWNED" in state.residual_reason
