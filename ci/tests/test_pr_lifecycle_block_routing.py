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
