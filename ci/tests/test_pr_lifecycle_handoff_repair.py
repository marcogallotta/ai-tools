from copy import deepcopy
import test_pr_lifecycle as base

pr_lifecycle = base.pr_lifecycle


class RepairGitHub(base.FakeGitHub):
    def update_pr_body(self, number, body):
        assert number == self.pr["number"]
        self.events.append(("update-pr-body", body))
        self.pr["body"] = body
        return deepcopy(self.pr)


def dispatch(gh):
    state = base.engine(gh).inspect(gh.pr)
    return base.engine(gh).dispatch_one(
        state, workspace=None, local_reviewer=None, implementation_fixer=None, terminal_cleaner=None
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
    state = dispatch(gh)
    assert not [event for event in gh.events if event[0] == "update-pr-body"]
    comments = [body for kind, body in gh.events if kind == "comment"]
    assert len(comments) == 1
    assert "ROUTE_TO_OWNER" in comments[0]
    assert '"human_action_required":false' in comments[0]
    assert "Development Workflow / orchestration" in comments[0]
    assert state.human_action is None
    assert "conflicting" in state.residual_reason


def test_missing_assignment_identity_routes_to_repair_owner_and_never_uses_related_task_reference():
    gh = RepairGitHub(base.pr(draft=True, body="Related investigation: 1217443403986570"))
    state = dispatch(gh)
    comments = [body for kind, body in gh.events if kind == "comment"]
    assert len(comments) == 1
    assert '"task_gid":null' in comments[0]
    assert "never guess a task identity" in comments[0]
    assert state.human_action is None


def test_formal_block_remains_visible_while_repairable_owner_metadata_is_routed():
    gh = RepairGitHub(base.pr(body="Related investigation: 1217443403986570"))
    gh.reviews = [base.review(verdict="BLOCK")]
    state = dispatch(gh)
    assert state.state == pr_lifecycle.LifecycleState.CHANGES_REQUESTED
    assert state.review_verdict == "BLOCK"
    assert state.human_action is None
    assert any("ROUTE_TO_OWNER" in body for kind, body in gh.events if kind == "comment")


def test_formal_merge_with_unresolved_owner_does_not_progress_to_integration():
    gh = RepairGitHub(base.pr(body="Related investigation: 1217443403986570"))
    gh.reviews = [base.review(verdict="MERGE")]
    state = dispatch(gh)
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
    first = lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None)
    lifecycle.dispatch_one(first, workspace=None, local_reviewer=None)
    comments = [body for kind, body in gh.events if kind == "comment" and "dish-handoff-repair:v1" in body]
    assert len(comments) == 1


def test_auto_repair_transport_unavailable_is_system_blocker_not_marco_action():
    gh = base.FakeGitHub(base.pr(draft=True, body="Owning task: 1217443403986570"))
    lifecycle = base.engine(gh)
    state = lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None)
    assert state.human_action is None
    assert state.residual_reason == "handoff repair transport unavailable; owner: producer/finalizer"
