from copy import deepcopy
import test_pr_lifecycle as base

pr_lifecycle = base.pr_lifecycle


class RepairGitHub(base.FakeGitHub):
    def update_pr_body(self, number, body):
        assert number == self.pr["number"]
        self.events.append(("update-pr-body", body))
        self.pr["body"] = body
        return deepcopy(self.pr)


class RepairWorkspace:
    def __init__(self):
        self.calls = []

    def dispatch_worker(self, **kwargs):
        self.calls.append(kwargs)
        return type("Accepted", (), {"accepted": True})()


def dispatch(gh, *, workspace=None):
    state = base.engine(gh).inspect(gh.pr)
    return base.engine(gh).dispatch_one(
        state, workspace=workspace, local_reviewer=None, implementation_fixer=None, terminal_cleaner=None
    )


def test_explicit_owner_without_canonical_marker_is_auto_repaired_and_read_back():
    gh = RepairGitHub(base.pr(draft=True, body="Owning task: 1217443403986570\nFocused evidence: complete."))
    state = dispatch(gh)
    assert gh.pr["body"].startswith("<!-- dish-owning-task:v1 task=1217443403986570 -->")
    assert state.task_ids.owning_task_id == "1217443403986570"
    assert state.human_action is None
    assert any("AUTO_REPAIR" in body and '"readback_status":"VERIFIED"' in body for kind, body in gh.events if kind == "comment")


def test_conflicting_owner_routes_to_system_owner_without_marco_relay_or_guess():
    body = "<!-- dish-owning-task:v1 task=1217443403986570 -->\nOwning task: 1217443403986571"
    gh = RepairGitHub(base.pr(draft=True, body=body))
    workspace = RepairWorkspace()
    state = dispatch(gh, workspace=workspace)
    assert not [event for event in gh.events if event[0] == "update-pr-body"]
    comments = [body for kind, body in gh.events if kind == "comment"]
    assert len(comments) == 1
    assert "ROUTE_TO_OWNER" in comments[0]
    assert '"human_action_required":false' in comments[0]
    assert "Development Workflow / orchestration" in comments[0]
    assert state.human_action is None
    assert "conflicting" in state.residual_reason
    assert len(workspace.calls) == 1
    call = workspace.calls[0]
    assert call["role"] == "Development Workflow"
    assert call["phase"] == "handoff-repair"
    assert call["exact_context"] == {
        "schema": "dish-handoff-repair-dispatch-v1",
        "repository": "marcogallotta/ai-tools",
        "pr_number": 31,
        "pr_url": "https://github.com/marcogallotta/ai-tools/pull/31",
        "branch": "agent/test",
        "head": base.HEAD,
        "owning_task": None,
        "defect": "multiple conflicting explicit owning-task declarations: ['1217443403986570', '1217443403986571']",
        "repair_owner": "Development Workflow / orchestration",
        "next_action": (
            "recover the exact assignment identity from canonical handoff/worktree/Worker authority; "
            "repair the producer-owned handoff and read it back; never guess a task identity"
        ),
        "identity_basis": "unresolved; authoritative assignment recovery required",
        "required_readback": (
            "the same repository/PR/branch/head packet is accepted by the Development Workflow consumer, "
            "then repaired metadata survives authoritative GitHub readback"
        ),
    }


def test_missing_assignment_identity_routes_to_repair_owner_and_never_uses_related_task_reference():
    gh = RepairGitHub(base.pr(draft=True, body="Related investigation: 1217443403986570"))
    state = dispatch(gh, workspace=RepairWorkspace())
    comments = [body for kind, body in gh.events if kind == "comment"]
    assert len(comments) == 1
    assert '"task_gid":null' in comments[0]
    assert "never guess a task identity" in comments[0]
    assert state.human_action is None


def test_formal_block_remains_visible_while_repairable_owner_metadata_is_routed():
    gh = RepairGitHub(base.pr(body="Related investigation: 1217443403986570"))
    gh.reviews = [base.review(verdict="BLOCK")]
    state = dispatch(gh, workspace=RepairWorkspace())
    assert state.state == pr_lifecycle.LifecycleState.CHANGES_REQUESTED
    assert state.review_verdict == "BLOCK"
    assert state.human_action is None
    assert any("ROUTE_TO_OWNER" in body for kind, body in gh.events if kind == "comment")


def test_formal_merge_with_unresolved_owner_does_not_progress_to_integration():
    gh = RepairGitHub(base.pr(body="Related investigation: 1217443403986570"))
    gh.reviews = [base.review(verdict="MERGE")]
    state = dispatch(gh, workspace=RepairWorkspace())
    assert state.review_verdict == "MERGE"
    assert gh.events and gh.events[0][0] == "comment"
    assert not [event for event in gh.events if event[0] in {"merge", "local-integration"}]


def test_unambiguous_legacy_owner_on_reviewable_pr_does_not_interrupt_normal_dispatch():
    gh = RepairGitHub(base.pr(body="Owning task: 1217443403986570"))
    workspace = base.FakeWorkspace()
    state = base.engine(gh).dispatch(workspace=workspace, local_reviewer=None)[0]
    assert len(workspace.calls) == 1
    assert state.human_action is None
    assert not [event for event in gh.events if event[0] == "update-pr-body"]


def test_route_to_owner_marker_is_idempotent_on_duplicate_dispatch():
    gh = RepairGitHub(base.pr(draft=True, body="Related investigation: 1217443403986570"))
    lifecycle = base.engine(gh)
    workspace = RepairWorkspace()
    first = lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=workspace, local_reviewer=None)
    lifecycle.dispatch_one(first, workspace=workspace, local_reviewer=None)
    comments = [body for kind, body in gh.events if kind == "comment" and "dish-handoff-repair:v1" in body]
    assert len(comments) == 1
    assert len(workspace.calls) == 1


def test_route_to_owner_unavailable_transport_is_concrete_agent_owned_capability_blocker():
    gh = RepairGitHub(base.pr(draft=True, body="Related investigation: 1217443403986570"))
    state = dispatch(gh)
    comments = [body for kind, body in gh.events if kind == "comment"]
    assert len(comments) == 1
    assert '"readback_status":"CAPABILITY_BLOCKED"' in comments[0]
    assert '"missing_route":"WorkspaceAgentDispatcher.dispatch_worker"' in comments[0]
    assert '"development_workflow_owner":"Development Workflow / orchestration"' in comments[0]
    assert '"recovery_evidence":' in comments[0]
    assert state.human_action is None
    assert state.residual_reason.startswith("handoff repair capability blocker:")


def test_auto_repair_transport_unavailable_is_system_blocker_not_marco_action():
    gh = base.FakeGitHub(base.pr(draft=True, body="Owning task: 1217443403986570"))
    lifecycle = base.engine(gh)
    state = lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None)
    assert state.human_action is None
    assert state.residual_reason.startswith("handoff repair capability blocker:")
    comments = [body for kind, body in gh.events if kind == "comment"]
    assert len(comments) == 1
    assert '"missing_route":"GitHub update_pr_body"' in comments[0]
