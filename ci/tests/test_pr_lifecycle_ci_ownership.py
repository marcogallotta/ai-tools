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


def test_pending_ci_preserves_review_passed_headline():
    gh = base.FakeGitHub()
    gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(status_value="in_progress", conclusion=None)

    state = base.engine(gh, authority=True).inspect(gh.pr)

    assert pr_lifecycle.STATE_LABELS[state.state] == "REVIEW PASSED / CERTIFICATION PENDING"
    assert state.review_verdict == "MERGE"

class RecordingLocalReviewer:
    command = "recording-local-reviewer"

    def __init__(self):
        self.calls = []

    def dispatch(self, context):
        self.calls.append(context)


def test_self_asserted_host_marker_does_not_create_local_review_context():
    gh = base.FakeGitHub(
        base.pr(
            body=(
                "Owning task: 1217443403986570\nREVIEW CLASS: focused\n"
                f"<!-- dish-implementation-host-witness:v1 head={base.HEAD} host=chatgpt "
                "source=orchestration launcher=dispatch-ci-ownership -->"
            )
        )
    )
    reviewer = RecordingLocalReviewer()
    lifecycle = base.engine(gh)

    result = lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=None, local_reviewer=reviewer)

    assert result.state == pr_lifecycle.LifecycleState.REVIEW_READY
    assert reviewer.calls == []


def test_workspace_review_prompt_is_remote_and_role_aware():
    http = base.RecordingHTTP()
    dispatcher = pr_lifecycle.WorkspaceAgentDispatcher(
        access_token="secret",
        review_trigger_id="agtch_review",
        http=http,
    )
    dispatcher.dispatch(
        repository="marcogallotta/ai-tools",
        pr_number=31,
        pr_url="https://github.com/marcogallotta/ai-tools/pull/31",
        head=base.HEAD,
        review_class="substantive",
        task_ids=["1217443403986570"],
    )
    prompt = http.calls[0][3]["input"]
    assert "Execution host: ChatGPT remote Review" in prompt
    assert "local Review agent" in prompt
    assert "semantic fixes belong to Implementation" in prompt
    assert "Integration-only actions belong to Integration" in prompt


def test_local_handoff_names_responsible_role_before_notice():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(body_tail="TESTS TO RUN: dish/scripts/dish-pg-native-certification --candidate aaaaa")]
    notices = []
    lifecycle = base.engine(gh)

    result = lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None, notify=notices.append)

    handoff = next(event[1] for event in gh.events if event[0] == "comment" and "dish-local-handoff:v1" in event[1])
    assert "Role: Integration" in handoff
    assert notices == [
        "Your next action: give PR #31 to a local Integration agent for exact-head certification; full handoff is on the PR "
        "The repository has already recorded the complete handoff."
    ]
    assert result.human_action == "give PR #31 to a local Integration agent for exact-head certification; full handoff is on the PR"


def test_local_implementation_handoff_names_implementation_role():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(body_tail="LOCAL IMPLEMENTATION COMPLETION REQUIRED: run local generator\nTESTS TO RUN: NONE.")]
    notices = []
    lifecycle = base.engine(gh)

    result = lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None, notify=notices.append)

    handoff = next(event[1] for event in gh.events if event[0] == "comment" and "dish-local-handoff:v1" in event[1])
    assert "Role: Implementation" in handoff
    assert notices == [
        "Your next action: give PR #31 to a local Implementation agent; full handoff is on the PR "
        "The repository has already recorded the complete handoff."
    ]
    assert result.human_action == "give PR #31 to a local Implementation agent; full handoff is on the PR"


def test_combined_pending_ci_and_local_certification_write_one_integration_handoff_with_both_gates():
    command = "dish/scripts/dish-pg-native-certification --candidate aaaaa"
    gh = base.FakeGitHub()
    gh.reviews = [base.review(body_tail=f"TESTS TO RUN: {command}")]
    gh.workflow_runs = base.runs(status_value="in_progress", conclusion=None)
    notices = []
    lifecycle = base.engine(gh)

    initial = lifecycle.inspect(gh.pr)

    assert initial.state == pr_lifecycle.LifecycleState.LOCAL_CERTIFICATION_REQUIRED
    assert initial.gate["diagnosis"] == pr_lifecycle.pr_gate.GateDiagnosis.PENDING.value

    result = lifecycle.dispatch_one(
        initial, workspace=None, local_reviewer=None, notify=notices.append
    )

    handoffs = [
        event[1]
        for event in gh.events
        if event[0] == "comment" and "dish-local-handoff:v1" in event[1]
    ]
    assert len(handoffs) == 1
    handoff = handoffs[0]
    assert "Role: Integration" in handoff
    assert f"Action: `{command}`" in handoff
    assert "Remaining exact-head CI gate: `Dish / exact-head certification` — PENDING." in handoff
    assert "Integration owns both remaining gates on this exact head" in handoff
    assert result.state == pr_lifecycle.LifecycleState.LOCAL_CERTIFICATION_REQUIRED
    assert result.gate["diagnosis"] == pr_lifecycle.pr_gate.GateDiagnosis.PENDING.value
    assert notices == [
        "Your next action: give PR #31 to a local Integration agent for exact-head certification; full handoff is on the PR "
        "The repository has already recorded the complete handoff."
    ]


def test_terminal_state_labels_are_distinct_and_role_aware():
    assert pr_lifecycle.STATE_LABELS == {
        pr_lifecycle.LifecycleState.AUTHORING: "AUTHORING / IMPLEMENTATION IN PROGRESS",
        pr_lifecycle.LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED: "IMPLEMENTATION CONTINUATION REQUIRED",
        pr_lifecycle.LifecycleState.REVIEW_READY: "REVIEW READY",
        pr_lifecycle.LifecycleState.REVIEW_IN_PROGRESS: "REVIEW IN PROGRESS",
        pr_lifecycle.LifecycleState.CHANGES_REQUESTED: "CHANGES REQUESTED / FIX IN PROGRESS",
        pr_lifecycle.LifecycleState.REVIEW_PASSED: "REVIEW PASSED / EVALUATING GATES",
        pr_lifecycle.LifecycleState.LOCAL_IMPLEMENTATION_REQUIRED: "LOCAL IMPLEMENTATION COMPLETION REQUIRED",
        pr_lifecycle.LifecycleState.LOCAL_CERTIFICATION_REQUIRED: "REVIEW PASSED / LOCAL INTEGRATION CERTIFICATION REQUIRED",
        pr_lifecycle.LifecycleState.WAITING_CI: "REVIEW PASSED / CERTIFICATION PENDING",
        pr_lifecycle.LifecycleState.WAITING_EXTERNAL_DEPENDENCY: "WAITING ON EXTERNAL DEPENDENCY",
        pr_lifecycle.LifecycleState.INTEGRATION_READY: "INTEGRATION READY",
        pr_lifecycle.LifecycleState.MERGING: "MERGING / INTEGRATION IN PROGRESS",
        pr_lifecycle.LifecycleState.MERGED: "MERGED",
        pr_lifecycle.LifecycleState.CLOSED: "CLOSED / SUPERSEDED",
    }
