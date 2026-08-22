import test_pr_lifecycle as base
from ci_failure_fingerprint import causal_fingerprint

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


def _ownership_comment(
    classification, *, evidence="run:700/job:test/signature:x", fingerprint=None,
    identity=None, disposition=None, generation=None, basis=None, contrary=None,
    owner_task=None, main=None, main_reproduction=None, candidate_evidence=None,
    interaction=None, interaction_hypothesis=None, targeted_evidence=None,
    when=base.NOW, comment_id=99,
):
    from urllib.parse import quote

    identity = identity or {}
    fingerprint_field = ""
    if fingerprint:
        fingerprint_field = (
            f" fingerprint={fingerprint}"
            f" owner_surface={quote(str(identity.get('owner_surface') or ''), safe='')}"
            f" failure_surface={quote(str(identity.get('failure_surface') or ''), safe='')}"
            f" invariant={quote(str(identity.get('invariant') or ''), safe='')}"
            f" signature={quote(str(identity.get('signature') or ''), safe='')}"
        )
    extras = {
        "disposition": disposition, "generation": generation, "basis": basis,
        "contrary": contrary, "owner_task": owner_task, "main": main,
        "main_reproduction": main_reproduction, "candidate_evidence": candidate_evidence,
        "interaction": interaction, "interaction_hypothesis": interaction_hypothesis,
        "targeted_evidence": targeted_evidence,
    }
    extra_fields = "".join(
        f" {key}={quote(str(value), safe='')}" for key, value in extras.items() if value is not None
    )
    return {
        "id": comment_id,
        "body": (
            f"<!-- dish-ci-failure-ownership:v1 head={base.HEAD} "
            f"check={quote('Dish / exact-head certification', safe='')} "
            f"classification={classification} evidence={quote(evidence, safe='')}"
            f"{fingerprint_field}{extra_fields} -->"
        ),
        "created_at": when.isoformat(),
        "updated_at": when.isoformat(),
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


def test_proven_current_main_carries_valid_causal_fingerprint_to_recovery_gate():
    fingerprint, identity = causal_fingerprint(
        owner_surface="python-control-plane", failure_surface="pytest",
        invariant="tests/test_policy.py::test_owner", signature="test_failure: expected 5 got 8",
    )
    gh = base.FakeGitHub()
    gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(conclusion="failure")
    gh.comments = [_ownership_comment("PROVEN_CURRENT_MAIN", fingerprint=fingerprint, identity=identity)]
    state = base.engine(gh, authority=True).inspect(gh.pr)
    assert state.gate["failure_causal_fingerprint"] == fingerprint
    assert state.gate["failure_causal_identity"] == identity


def test_mismatched_causal_identity_fails_closed_as_ambiguous():
    fingerprint, identity = causal_fingerprint(
        owner_surface="python-control-plane", failure_surface="pytest",
        invariant="tests/test_policy.py::test_owner", signature="test_failure",
    )
    forged = dict(identity); forged["invariant"] = "tests/test_policy.py::test_other"
    gh = base.FakeGitHub(); gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(conclusion="failure")
    gh.comments = [_ownership_comment("PROVEN_CURRENT_MAIN", fingerprint=fingerprint, identity=forged)]
    state = base.engine(gh, authority=True).inspect(gh.pr)
    assert state.gate["failure_ownership"] == "AMBIGUOUS"
    assert "unverified causal identity" in state.gate["failure_ownership_evidence"]


def test_likely_non_pr_owned_continues_but_cannot_admit_red_final_landing():
    gh = base.FakeGitHub(); gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(conclusion="failure")
    gh.comments = [_ownership_comment(
        "LIKELY_NON_PR_OWNED",
        disposition="NON_BLOCKING_LIKELY_UNRELATED",
        generation="run-700-attempt-1",
        basis="same environment failure predates candidate and candidate cannot reach setup",
        contrary="none",
        owner_task="1217449623846547",
    )]
    class ActiveOwnerAsana:
        def get_task(self, gid): return {"gid": gid, "completed": False}
    state = pr_lifecycle.LifecycleEngine(
        gh, asana=ActiveOwnerAsana(), integration_authority=True, now=lambda: base.NOW
    ).inspect(gh.pr)
    assert state.state == pr_lifecycle.LifecycleState.REVIEW_PASSED
    assert state.gate["raw_gate_outcome"] == "FAILED"
    assert state.gate["candidate_disposition"] == "NON_BLOCKING_LIKELY_UNRELATED"
    assert "final landing is not admitted" in state.residual_reason


def test_likely_non_pr_owned_with_contrary_evidence_fails_closed():
    gh = base.FakeGitHub(); gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(conclusion="failure")
    gh.comments = [_ownership_comment(
        "LIKELY_NON_PR_OWNED",
        disposition="NON_BLOCKING_LIKELY_UNRELATED",
        generation="run-700-attempt-1", basis="similar historical failure",
        contrary="candidate-touches-shared-parser", owner_task="1217449623846547",
    )]
    state = base.engine(gh, authority=True).inspect(gh.pr)
    assert state.gate["failure_ownership"] == "AMBIGUOUS"
    assert state.gate["candidate_disposition"] == "BLOCKING"


def test_likely_non_pr_owned_from_an_older_workflow_attempt_fails_closed():
    gh = base.FakeGitHub(); gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(conclusion="failure")
    gh.comments = [_ownership_comment(
        "LIKELY_NON_PR_OWNED",
        disposition="NON_BLOCKING_LIKELY_UNRELATED",
        generation="run-699-attempt-1", basis="historical environment failure",
        contrary="none", owner_task="1217449623846547",
    )]
    state = base.engine(gh, authority=True).inspect(gh.pr)
    assert state.gate["failure_ownership"] == "AMBIGUOUS"
    assert state.gate["candidate_disposition"] == "BLOCKING"
    assert "stale workflow evidence generation" in state.gate["failure_ownership_evidence"]


def test_likely_non_pr_owned_without_readable_active_repair_owner_fails_closed():
    gh = base.FakeGitHub(); gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(conclusion="failure")
    gh.comments = [_ownership_comment(
        "LIKELY_NON_PR_OWNED", disposition="NON_BLOCKING_LIKELY_UNRELATED",
        generation="run-700-attempt-1", basis="historical environment failure",
        contrary="none", owner_task="1217449623846547",
    )]
    state = base.engine(gh, authority=True).inspect(gh.pr)
    assert state.gate["failure_ownership"] == "AMBIGUOUS"
    assert state.gate["candidate_disposition"] == "BLOCKING"


def test_newer_pr_owned_evidence_revokes_likely_disposition():
    gh = base.FakeGitHub(); gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(conclusion="failure")
    gh.comments = [
        _ownership_comment(
            "LIKELY_NON_PR_OWNED", disposition="NON_BLOCKING_LIKELY_UNRELATED",
            generation="run-700-attempt-1", basis="historical environment failure",
            contrary="none", owner_task="1217449623846547",
            when=base.NOW, comment_id=98,
        ),
        _ownership_comment(
            "PR_OWNED", evidence="targeted reproduction on exact candidate",
            when=base.NOW.replace(microsecond=1), comment_id=99,
        ),
    ]
    state = base.engine(gh, authority=True).inspect(gh.pr)
    assert state.state == pr_lifecycle.LifecycleState.CHANGES_REQUESTED
    assert state.gate["failure_ownership"] == "PR_OWNED"


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
        pr_lifecycle.LifecycleState.WAITING_INFRASTRUCTURE: "WAITING ON INFRASTRUCTURE",
        pr_lifecycle.LifecycleState.INTEGRATION_READY: "INTEGRATION READY",
        pr_lifecycle.LifecycleState.MERGING: "MERGING / INTEGRATION IN PROGRESS",
        pr_lifecycle.LifecycleState.MERGED: "MERGED",
        pr_lifecycle.LifecycleState.CLOSED: "CLOSED / SUPERSEDED",
    }
