from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
BASE_SPEC = importlib.util.spec_from_file_location(
    "pr_lifecycle_base_tests", ROOT / "ci" / "tests" / "test_pr_lifecycle.py"
)
assert BASE_SPEC and BASE_SPEC.loader
base = importlib.util.module_from_spec(BASE_SPEC)
sys.modules[BASE_SPEC.name] = base
BASE_SPEC.loader.exec_module(base)

pr_lifecycle = base.pr_lifecycle
HEAD = base.HEAD
NEW_HEAD = base.NEW_HEAD


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


def external_dependency_comment():
    return {
        "id": 80,
        "body": (
            f"<!-- dish-external-dependency:v1 action=blocked task=1217449623846547 pr=77 "
            f"check=Dish%20%2F%20required%20ordinary%20CI main={'d' * 40} "
            "evidence=task%3A1217449623846547 reason=baseline%20failure -->"
        ),
        "created_at": base.NOW.isoformat(),
        "updated_at": base.NOW.isoformat(),
    }


class FakeFixer:
    def __init__(self, gh):
        self.command = "fake-fixer"
        self.gh = gh
        self.calls = []

    def dispatch(self, context):
        self.calls.append(deepcopy(context))
        self.gh.events.append(("fix-dispatch", deepcopy(context)))


def test_exact_head_block_dispatches_existing_fix_consumer_after_durable_fix_lease():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(verdict="BLOCK")]
    fixer = FakeFixer(gh)

    result = base.engine(gh).dispatch_one(
        base.engine(gh).inspect(gh.pr),
        workspace=None,
        local_reviewer=None,
        implementation_fixer=fixer,
    )

    assert len(fixer.calls) == 1
    context = fixer.calls[0]
    assert context["schema"] == "dish-pr-fix-dispatch-v1"
    assert context["blocked_head"] == HEAD
    assert context["formal_block_review"]["verdict"] == "BLOCK"
    lease_index = next(
        index
        for index, event in enumerate(gh.events)
        if event[0] == "comment" and "phase=fix" in event[1]
    )
    dispatch_index = next(index for index, event in enumerate(gh.events) if event[0] == "fix-dispatch")
    assert lease_index < dispatch_index
    assert result.state == pr_lifecycle.LifecycleState.CHANGES_REQUESTED
    assert any(lease["phase"] == "fix" for lease in result.active_leases)


@pytest.mark.parametrize("phase", ["fix", "implementation"])
def test_active_fix_or_implementation_lease_prevents_block_redispatch(phase):
    gh = base.FakeGitHub()
    gh.reviews = [base.review(verdict="BLOCK")]
    gh.comments = [base.lease_comment(phase=phase)]
    fixer = FakeFixer(gh)

    result = base.engine(gh).dispatch_one(
        base.engine(gh).inspect(gh.pr),
        workspace=None,
        local_reviewer=None,
        implementation_fixer=fixer,
    )

    assert result.state == pr_lifecycle.LifecycleState.CHANGES_REQUESTED
    assert fixer.calls == []


def test_duplicate_poll_does_not_duplicate_active_fix_dispatch():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(verdict="BLOCK")]
    fixer = FakeFixer(gh)
    lifecycle = base.engine(gh)

    lifecycle.dispatch_one(
        lifecycle.inspect(gh.pr),
        workspace=None,
        local_reviewer=None,
        implementation_fixer=fixer,
    )
    lifecycle.dispatch_one(
        lifecycle.inspect(gh.pr),
        workspace=None,
        local_reviewer=None,
        implementation_fixer=fixer,
    )

    assert len(fixer.calls) == 1
    fix_lease_comments = [
        event for event in gh.events if event[0] == "comment" and "phase=fix" in event[1]
    ]
    assert len(fix_lease_comments) == 1


def test_head_movement_invalidates_old_block_and_fix_route():
    gh = base.FakeGitHub(base.pr(head=NEW_HEAD))
    gh.reviews = [base.review(head=HEAD, verdict="BLOCK")]
    gh.comments = [base.lease_comment(head=HEAD, phase="fix")]
    fixer = FakeFixer(gh)
    workspace = base.FakeWorkspace()
    lifecycle = base.engine(gh)

    result = lifecycle.dispatch_one(
        lifecycle.inspect(gh.pr),
        workspace=workspace,
        local_reviewer=None,
        implementation_fixer=fixer,
    )

    assert fixer.calls == []
    assert result.head == NEW_HEAD
    assert not [lease for lease in result.active_leases if lease["phase"] == "fix"]
    assert len(workspace.calls) == 1
    assert workspace.calls[0]["head"] == NEW_HEAD


def test_pr_owned_exact_head_ci_failure_dispatches_fix_without_rewriting_review_verdict():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(verdict="MERGE")]
    gh.workflow_runs = base.runs(conclusion="failure")
    fixer = FakeFixer(gh)
    lifecycle = base.engine(gh)

    initial = lifecycle.inspect(gh.pr)
    assert initial.state == pr_lifecycle.LifecycleState.CHANGES_REQUESTED
    assert initial.review_verdict == "MERGE"
    assert initial.gate["diagnosis"] == pr_lifecycle.pr_gate.GateDiagnosis.FAILED_REQUIRED_CI.value

    result = lifecycle.dispatch_one(
        initial,
        workspace=None,
        local_reviewer=None,
        implementation_fixer=fixer,
    )

    assert len(fixer.calls) == 1
    context = fixer.calls[0]
    assert context["formal_block_review"] is None
    assert context["pr_owned_ci_failure"]["diagnosis"] == "FAILED_REQUIRED_CI"
    assert result.state == pr_lifecycle.LifecycleState.CHANGES_REQUESTED
    assert any(lease["phase"] == "fix" for lease in result.active_leases)


def test_external_exact_head_ci_failure_never_dispatches_blocked_pr_fixer():
    gh = ExternalGitHub()
    gh.reviews = [base.review(verdict="MERGE")]
    gh.workflow_runs = base.runs(conclusion="failure")
    owner = base.pr()
    owner["number"] = 77
    gh.other_prs[77] = owner
    gh.comments = [external_dependency_comment()]
    fixer = FakeFixer(gh)
    lifecycle = base.engine(gh)

    initial = lifecycle.inspect(gh.pr)
    assert initial.state == pr_lifecycle.LifecycleState.WAITING_EXTERNAL_DEPENDENCY
    result = lifecycle.dispatch_one(
        initial,
        workspace=None,
        local_reviewer=None,
        implementation_fixer=fixer,
    )

    assert result.state == pr_lifecycle.LifecycleState.WAITING_EXTERNAL_DEPENDENCY
    assert fixer.calls == []
