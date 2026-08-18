from copy import deepcopy
from datetime import timedelta

import test_pr_lifecycle as base

pr_lifecycle = base.pr_lifecycle


class ExternalGitHub(base.FakeGitHub):
    def __init__(self, candidate=None):
        super().__init__(candidate)
        self.other_prs = {}

    def get_pr(self, number):
        if number == self.pr["number"]:
            return deepcopy(self.pr)
        if number in self.other_prs:
            return deepcopy(self.other_prs[number])
        raise pr_lifecycle.LifecycleError(f"PR #{number} unavailable")


class FakeFixer:
    command = "fake-fixer"

    def __init__(self):
        self.calls = []

    def dispatch(self, payload):
        self.calls.append(payload)


def external_dependency_comment(
    *,
    action="blocked",
    owner_pr=77,
    check="Dish%20%2F%20exact-head%20certification",
    main="d" * 40,
    evidence="task%3A1217449623846547",
    reason="baseline%20failure",
    when=base.NOW,
    comment_id=80,
):
    pr_field = f" pr={owner_pr}" if owner_pr is not None else ""
    return {
        "id": comment_id,
        "body": (
            f"<!-- dish-external-dependency:v1 action={action} task=1217449623846547{pr_field} "
            f"check={check} main={main} evidence={evidence} reason={reason} -->"
        ),
        "created_at": when.isoformat(),
        "updated_at": when.isoformat(),
    }


def test_failed_required_ci_with_valid_external_record_waits_on_external_dependency():
    gh = ExternalGitHub()
    gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(conclusion="failure")
    owner = base.pr()
    owner["number"] = 77
    owner["head"]["ref"] = "agent/external-owner"
    gh.other_prs[77] = owner
    gh.comments = [external_dependency_comment()]

    state = base.engine(gh).inspect(gh.pr)

    assert state.state == pr_lifecycle.LifecycleState.WAITING_EXTERNAL_DEPENDENCY
    assert state.external_dependency["task_gid"] == "1217449623846547"
    assert state.external_dependency["owner_pr"] == 77
    assert state.external_dependency["check"] == "Dish / exact-head certification"
    assert state.human_action == (
        "Waiting on PR #77 / task 1217449623846547: "
        "Dish / exact-head certification. No action for Marco."
    )


def test_supported_external_dependency_api_separates_record_history_from_active_state():
    comments = [
        external_dependency_comment(when=base.NOW - timedelta(minutes=1), comment_id=80),
        external_dependency_comment(action="resolved", when=base.NOW, comment_id=81),
    ]

    newest = pr_lifecycle.latest_external_dependency_record(comments)

    assert newest is not None and newest.action == "resolved"
    assert pr_lifecycle.resolve_external_dependency(comments) is None
    assert pr_lifecycle.parse_external_dependency(comments) is None


def test_resolved_external_record_reenters_gate_evaluation():
    gh = ExternalGitHub()
    gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(conclusion="failure")
    gh.comments = [
        external_dependency_comment(when=base.NOW - timedelta(minutes=1), comment_id=80),
        external_dependency_comment(action="resolved", when=base.NOW, comment_id=81),
    ]

    state = base.engine(gh).inspect(gh.pr)

    assert state.state == pr_lifecycle.LifecycleState.REVIEW_PASSED
    assert (
        state.gate["diagnosis"]
        == pr_lifecycle.pr_gate.GateDiagnosis.EVIDENCE_MISSING_OR_STALE.value
    )
    assert "refresh/re-run" in state.residual_reason
    assert state.external_dependency is None

    fixer = FakeFixer()
    result = base.engine(gh).dispatch_one(
        state,
        workspace=None,
        local_reviewer=None,
        implementation_fixer=fixer,
    )
    assert result.state == pr_lifecycle.LifecycleState.REVIEW_PASSED
    assert fixer.calls == []


def test_closed_unmerged_external_owner_remains_blocked_explicitly():
    gh = ExternalGitHub()
    gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(conclusion="failure")
    owner = base.pr(state="closed")
    owner["number"] = 77
    gh.other_prs[77] = owner
    gh.comments = [external_dependency_comment()]

    state = base.engine(gh).inspect(gh.pr)

    assert state.state == pr_lifecycle.LifecycleState.WAITING_EXTERNAL_DEPENDENCY
    assert "closed unmerged" in state.residual_reason


def test_malformed_external_record_fails_closed_without_external_ownership():
    gh = ExternalGitHub()
    gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(conclusion="failure")
    gh.comments = [external_dependency_comment(main="not-a-sha")]

    state = base.engine(gh).inspect(gh.pr)

    assert state.state == pr_lifecycle.LifecycleState.REVIEW_PASSED
    assert state.gate["diagnosis"] == pr_lifecycle.pr_gate.GateDiagnosis.FAILED_REQUIRED_CI.value
    assert state.gate["failure_ownership"] == "AMBIGUOUS"
    assert "external dependency marker invalid" in state.residual_reason


def test_merged_external_owner_reuses_integration_evidence_path():
    gh = ExternalGitHub()
    gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(conclusion="failure")
    owner = base.pr(state="closed", merged=True)
    owner["number"] = 77
    gh.other_prs[77] = owner
    gh.comments = [external_dependency_comment()]
    fixer = FakeFixer()
    lifecycle = base.engine(gh)

    result = lifecycle.dispatch_one(
        lifecycle.inspect(gh.pr),
        workspace=None,
        local_reviewer=None,
        implementation_fixer=fixer,
    )

    assert result.state == pr_lifecycle.LifecycleState.REVIEW_PASSED
    assert (
        result.gate["diagnosis"]
        == pr_lifecycle.pr_gate.GateDiagnosis.EVIDENCE_MISSING_OR_STALE.value
    )
    assert fixer.calls == []


class CompletedAsana:
    def get_task(self, gid):
        return {
            "gid": gid,
            "completed": True,
            "completed_at": base.NOW.isoformat(),
        }


def test_completed_external_owner_task_reuses_integration_evidence_path():
    gh = ExternalGitHub()
    gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(conclusion="failure")
    gh.comments = [external_dependency_comment(owner_pr=None)]
    fixer = FakeFixer()
    lifecycle = pr_lifecycle.LifecycleEngine(
        gh,
        asana=CompletedAsana(),
        now=lambda: base.NOW,
    )

    result = lifecycle.dispatch_one(
        lifecycle.inspect(gh.pr),
        workspace=None,
        local_reviewer=None,
        implementation_fixer=fixer,
    )

    assert result.state == pr_lifecycle.LifecycleState.REVIEW_PASSED
    assert (
        result.gate["diagnosis"]
        == pr_lifecycle.pr_gate.GateDiagnosis.EVIDENCE_MISSING_OR_STALE.value
    )
    assert fixer.calls == []


def test_new_failure_after_resolution_can_be_pr_owned():
    gh = ExternalGitHub()
    gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(conclusion="failure")
    gh.workflow_runs["workflow_runs"][0]["run_started_at"] = "2026-08-13T09:00:00Z"
    gh.comments = [
        external_dependency_comment(when=base.NOW - timedelta(minutes=1), comment_id=80),
        external_dependency_comment(action="resolved", when=base.NOW, comment_id=81),
        {
            "id": 82,
            "body": (
                f"<!-- dish-ci-failure-ownership:v1 head={base.HEAD} "
                "check=Dish%20%2F%20exact-head%20certification classification=PR_OWNED "
                "evidence=workflow%3A700%2Fjob%3Atest%2Fsignature%3Anew -->"
            ),
            "created_at": (base.NOW + timedelta(seconds=1)).isoformat(),
            "updated_at": (base.NOW + timedelta(seconds=1)).isoformat(),
        },
    ]
    fixer = FakeFixer()
    lifecycle = base.engine(gh)

    result = lifecycle.dispatch_one(
        lifecycle.inspect(gh.pr),
        workspace=None,
        local_reviewer=None,
        implementation_fixer=fixer,
    )

    assert result.state == pr_lifecycle.LifecycleState.CHANGES_REQUESTED
    assert len(fixer.calls) == 1
