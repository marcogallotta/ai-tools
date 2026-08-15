from __future__ import annotations

import test_pr_lifecycle as base

p = base.pr_lifecycle


class RecordingFixRouter:
    def __init__(self, *, chatgpt=True, local=True):
        self.commands = {
            "CHATGPT_IMPLEMENTATION": "chatgpt" if chatgpt else None,
            "LOCAL_IMPLEMENTATION": "local" if local else None,
        }
        self.calls = []

    @property
    def command(self):
        return next((v for v in self.commands.values() if v), None)

    def command_for(self, host):
        return self.commands.get(host)

    def dispatch(self, context, *, host):
        self.calls.append((host, context))


class RecordingReview:
    command = "configured"

    def __init__(self):
        self.calls = []

    def dispatch(self, context):
        self.calls.append(context)


class RecordingWorkspace:
    def __init__(self):
        self.calls = []

    def dispatch(self, **kwargs):
        self.calls.append(kwargs)
        return p.WorkspaceDispatchResult("key", None, None)


def test_block_fix_defaults_to_chatgpt_implementation():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(verdict="BLOCK", body_tail="TESTS TO RUN: NONE.")]
    fixer = RecordingFixRouter()
    lifecycle = base.engine(gh)
    lifecycle.dispatch_one(
        lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None, implementation_fixer=fixer
    )
    assert [host for host, _ in fixer.calls] == ["CHATGPT_IMPLEMENTATION"]


def test_exact_class_b_block_can_select_local_implementation():
    gh = base.FakeGitHub()
    gh.reviews = [
        base.review(
            verdict="BLOCK",
            body_tail=(
                "LOCAL IMPLEMENTATION COMPLETION REQUIRED: "
                "IMPLEMENTATION / PUBLICATION — hosted publication cannot write governed path; "
                "fallbacks exhausted: connector update, Git data API\n"
                "TESTS TO RUN: NONE."
            ),
        )
    ]
    fixer = RecordingFixRouter()
    lifecycle = base.engine(gh)
    lifecycle.dispatch_one(
        lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None, implementation_fixer=fixer
    )
    assert [host for host, _ in fixer.calls] == ["LOCAL_IMPLEMENTATION"]


def test_remote_consumer_unavailable_never_falls_back_to_configured_local_consumer():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(verdict="BLOCK", body_tail="TESTS TO RUN: NONE.")]
    fixer = RecordingFixRouter(chatgpt=False, local=True)
    lifecycle = base.engine(gh)
    result = lifecycle.dispatch_one(
        lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None, implementation_fixer=fixer
    )
    assert fixer.calls == []
    assert result.state == p.LifecycleState.CHANGES_REQUESTED
    assert "CHATGPT_IMPLEMENTATION" in (result.residual_reason or "")


def test_bounded_local_review_requires_positive_chatgpt_implementation_witness():
    candidate = base.pr(body="Owning task: 1217443403986570\nREVIEW CLASS: focused")
    gh = base.FakeGitHub(candidate)
    local = RecordingReview()
    workspace = RecordingWorkspace()
    lifecycle = base.engine(gh)
    lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=workspace, local_reviewer=local)
    assert local.calls == []
    assert len(workspace.calls) == 1


def test_positive_prepr_chatgpt_witness_allows_bounded_local_review():
    candidate = base.pr(
        body=(
            "Owning task: 1217443403986570\nREVIEW CLASS: focused\n"
            f"<!-- dish-implementation-host-witness:v1 head={base.HEAD} host=chatgpt "
            "source=orchestration launcher=dispatch-123 -->"
        )
    )
    gh = base.FakeGitHub(candidate)
    local = RecordingReview()
    workspace = RecordingWorkspace()
    lifecycle = base.engine(gh)
    lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=workspace, local_reviewer=local)
    assert len(local.calls) == 1
    assert workspace.calls == []


def test_positive_local_implementation_witness_forces_chatgpt_review():
    candidate = base.pr(
        body=(
            "Owning task: 1217443403986570\nREVIEW CLASS: focused\n"
            f"<!-- dish-implementation-host-witness:v1 head={base.HEAD} host=local "
            "source=orchestration launcher=dispatch-456 -->"
        )
    )
    gh = base.FakeGitHub(candidate)
    local = RecordingReview()
    workspace = RecordingWorkspace()
    lifecycle = base.engine(gh)
    lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=workspace, local_reviewer=local)
    assert local.calls == []
    assert len(workspace.calls) == 1
