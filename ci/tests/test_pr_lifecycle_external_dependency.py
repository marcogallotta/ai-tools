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


def external_dependency_comment(
    *,
    action="blocked",
    owner_pr=77,
    check="Dish%20%2F%20required%20ordinary%20CI",
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
    assert state.external_dependency["check"] == "Dish / required ordinary CI"
    assert state.human_action == (
        "Waiting on PR #77 / task 1217449623846547: "
        "Dish / required ordinary CI. No action for Marco."
    )


def test_resolved_external_record_reenters_gate_evaluation():
    gh = ExternalGitHub()
    gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(conclusion="failure")
    gh.comments = [
        external_dependency_comment(when=base.NOW - timedelta(minutes=1), comment_id=80),
        external_dependency_comment(action="resolved", when=base.NOW, comment_id=81),
    ]

    state = base.engine(gh).inspect(gh.pr)

    assert state.state == pr_lifecycle.LifecycleState.WAITING_CI
    assert state.external_dependency is None


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

    assert state.state == pr_lifecycle.LifecycleState.WAITING_CI
    assert "external dependency marker invalid" in state.residual_reason
