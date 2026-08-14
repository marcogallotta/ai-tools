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
            f"check=Dish%20%2F%20exact-head%20certification main={'d' * 40} "
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

def _draft_with_pending_evidence():
    return base.pr(
        draft=True,
        body=(
            "Owning task: 1217450869324199\n"
            "Focused evidence: complete.\n"
            "IMPLEMENTATION EVIDENCE PENDING: required smoke"
        ),
    )


def test_draft_missing_authoring_evidence_dispatches_implementation_continuation_after_handoff():
    gh = base.FakeGitHub(_draft_with_pending_evidence())
    fixer = FakeFixer(gh)
    lifecycle = base.engine(gh)

    result = lifecycle.dispatch_one(
        lifecycle.inspect(gh.pr),
        workspace=None,
        local_reviewer=None,
        implementation_fixer=fixer,
    )

    assert len(fixer.calls) == 1
    context = fixer.calls[0]
    assert context["schema"] == "dish-pr-implementation-continuation-v1"
    assert context["branch"] == "agent/test"
    assert context["head"] == HEAD
    assert context["unfinished_authoring_evidence"] == "required smoke"
    handoff_index = next(
        i for i, event in enumerate(gh.events)
        if event[0] == "comment" and "dish-implementation-continuation:v1" in event[1]
    )
    lease_index = next(
        i for i, event in enumerate(gh.events)
        if event[0] == "comment" and "phase=implementation" in event[1]
    )
    dispatch_index = next(i for i, event in enumerate(gh.events) if event[0] == "fix-dispatch")
    assert handoff_index < lease_index < dispatch_index
    assert result.state == pr_lifecycle.LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED
    assert any(lease["phase"] == "implementation" for lease in result.active_leases)
    assert not any("dish-local-handoff:v1" in event[1] for event in gh.events if event[0] == "comment")


def test_active_implementation_owner_prevents_duplicate_draft_continuation_dispatch():
    gh = base.FakeGitHub(_draft_with_pending_evidence())
    gh.comments = [base.lease_comment(phase="implementation")]
    fixer = FakeFixer(gh)
    lifecycle = base.engine(gh)

    result = lifecycle.dispatch_one(
        lifecycle.inspect(gh.pr),
        workspace=None,
        local_reviewer=None,
        implementation_fixer=fixer,
    )

    assert result.state == pr_lifecycle.LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED
    assert fixer.calls == []


def test_missing_continuation_consumer_uses_only_required_human_message():
    gh = base.FakeGitHub(_draft_with_pending_evidence())
    notices = []
    lifecycle = base.engine(gh)

    result = lifecycle.dispatch_one(
        lifecycle.inspect(gh.pr),
        workspace=None,
        local_reviewer=None,
        implementation_fixer=None,
        notify=notices.append,
    )

    assert result.state == pr_lifecycle.LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED
    assert notices == ["PR #31 still needs Implementation to finish required smoke."]


def _claim(*, state="review-ready", sync="synced", task_gid="1217443403986570", branch="agent/test", pr_number=31, pr_head=HEAD):
    return {
        "repository": "marcogallotta/ai-tools",
        "task_gid": task_gid,
        "role": "Implementation",
        "generation": 4,
        "claim_id": "claim-generation-4",
        "state": state,
        "asana_sync_state": sync,
        "branch": branch,
        "pr_number": pr_number,
        "pr_head": pr_head,
    }


def test_review_ready_global_claim_is_exact_takeover_input_for_block_fix():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(verdict="BLOCK")]
    fixer = FakeFixer(gh)
    guard = base.FakeClaimGuard(
        {"dispatchable": False, "reason": "durable claim lineage exists", "claim": _claim()}
    )
    lifecycle = base.engine(gh, claim_guard=guard)

    lifecycle.dispatch_one(
        lifecycle.inspect(gh.pr),
        workspace=None,
        local_reviewer=None,
        implementation_fixer=fixer,
    )

    assert guard.calls == ["1217443403986570"]
    assert len(fixer.calls) == 1
    handoff = fixer.calls[0]["global_implementation_claim"]
    assert handoff["mode"] == "takeover"
    assert handoff["expected_claim_id"] == "claim-generation-4"


def test_active_global_claim_blocks_duplicate_fix_before_lease_or_consumer():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(verdict="BLOCK")]
    fixer = FakeFixer(gh)
    guard = base.FakeClaimGuard(
        {"dispatchable": False, "reason": "active", "claim": _claim(state="claimed")}
    )
    lifecycle = base.engine(gh, claim_guard=guard)

    with pytest.raises(pr_lifecycle.LifecycleError, match="still actively writable"):
        lifecycle.dispatch_one(
            lifecycle.inspect(gh.pr),
            workspace=None,
            local_reviewer=None,
            implementation_fixer=fixer,
        )

    assert fixer.calls == []
    assert not any(event[0] == "comment" and "phase=fix" in event[1] for event in gh.events)


def test_unsynchronized_global_claim_fails_closed_before_dispatch():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(verdict="BLOCK")]
    fixer = FakeFixer(gh)
    guard = base.FakeClaimGuard(
        {"dispatchable": False, "reason": "sync pending", "claim": _claim(sync="pending")}
    )
    lifecycle = base.engine(gh, claim_guard=guard)

    with pytest.raises(pr_lifecycle.LifecycleError, match="unresolved Asana synchronization"):
        lifecycle.dispatch_one(
            lifecycle.inspect(gh.pr),
            workspace=None,
            local_reviewer=None,
            implementation_fixer=fixer,
        )

    assert fixer.calls == []


def test_global_claim_guard_unavailability_fails_closed():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(verdict="BLOCK")]
    fixer = FakeFixer(gh)
    guard = base.FakeClaimGuard(error=pr_lifecycle.LifecycleError("claim service unavailable"))
    lifecycle = base.engine(gh, claim_guard=guard)

    with pytest.raises(pr_lifecycle.LifecycleError, match="claim service unavailable"):
        lifecycle.dispatch_one(
            lifecycle.inspect(gh.pr),
            workspace=None,
            local_reviewer=None,
            implementation_fixer=fixer,
        )

    assert fixer.calls == []


def test_global_claim_lineage_mismatch_blocks_fix_dispatch():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(verdict="BLOCK")]
    fixer = FakeFixer(gh)
    guard = base.FakeClaimGuard(
        {"dispatchable": False, "reason": "lineage", "claim": _claim(branch="agent/other")}
    )
    lifecycle = base.engine(gh, claim_guard=guard)

    with pytest.raises(pr_lifecycle.LifecycleError, match="bound to branch"):
        lifecycle.dispatch_one(
            lifecycle.inspect(gh.pr),
            workspace=None,
            local_reviewer=None,
            implementation_fixer=fixer,
        )

    assert fixer.calls == []
