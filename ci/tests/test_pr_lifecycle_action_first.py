from __future__ import annotations

import pytest
import test_pr_lifecycle as base
import test_pr_lifecycle_external_dependency as external

p = base.pr_lifecycle


def rendered(gh, *, authority=False):
    lifecycle = base.engine(gh, authority=authority).inspect(gh.pr)
    return p.action_first_status(lifecycle)


def assert_no_internal_default_jargon(text):
    for token in ("exact-head", "Next owner/system", "LOCAL WORK TYPE", "LOCAL SCOPE"):
        assert token not in text
    assert " @ " not in text


def test_review_block_says_plain_problem_and_automatic_fix_is_next():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(verdict="BLOCK")]
    text = rendered(gh)
    assert text.startswith("Review found a code problem. A fix is next. Nothing for you to do.")
    assert_no_internal_default_jargon(text)


def test_review_pass_pending_certification_says_no_action_plainly():
    gh = base.FakeGitHub()
    gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(status_value="in_progress", conclusion=None)
    text = rendered(gh)
    assert text.startswith("Review passed. Automated landing checks are still pending. Nothing for you to do.")
    assert_no_internal_default_jargon(text)


def test_external_dependency_is_explicit_wait_not_user_troubleshooting():
    gh = external.ExternalGitHub()
    gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(conclusion="failure")
    owner = base.pr(); owner["number"] = 77; gh.other_prs[77] = owner
    gh.comments = [external.external_dependency_comment()]
    text = rendered(gh)
    assert text.startswith("This is waiting on an external dependency. Nothing for you to do.")
    assert_no_internal_default_jargon(text)


def test_long_local_certification_is_plain_local_test_action():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(body_tail="TESTS TO RUN: dish/scripts/native-certification --long")]
    text = rendered(gh)
    assert "needs a test on the local machine" in text
    assert "local test runner" in text
    assert_no_internal_default_jargon(text)


def test_local_implementation_is_plain_code_publication_action():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(body_tail="LOCAL IMPLEMENTATION COMPLETION REQUIRED: regenerate and publish\nTESTS TO RUN: NONE.")]
    text = rendered(gh)
    assert "needs a code or publication fix on the local machine" in text
    assert "local coding agent" in text
    assert_no_internal_default_jargon(text)


def test_integration_ready_says_checks_passed_and_no_user_action():
    gh = base.FakeGitHub(); gh.reviews = [base.review()]
    text = rendered(gh, authority=True)
    assert text.startswith("Review and required checks passed. Source integration is next. Nothing for you to do.")
    assert_no_internal_default_jargon(text)


def test_real_human_action_still_overrides_automatic_headline():
    gh = base.FakeGitHub(); gh.reviews = [base.review()]
    lifecycle = base.engine(gh).inspect(gh.pr)
    lifecycle.human_action = "approve the production activation decision"
    text = p.action_first_status(lifecycle)
    assert text.startswith("Your next action: approve the production activation decision")


def test_table_uses_same_plain_renderer_as_notifications():
    gh = base.FakeGitHub(); gh.reviews = [base.review(verdict="BLOCK")]
    lifecycle = base.engine(gh).inspect(gh.pr)
    table = p._render_table([lifecycle])
    assert "Review found a code problem" in table


def _worker(gh):
    return p.WorkspaceAgentDispatcher(
        access_token="secret",
        review_trigger_id="agtch_review",
        worker_trigger_id="agtch_worker",
        http=base.EmptyAcceptedHTTP(),
    )


def _worker_context(head=base.HEAD):
    return {
        "task": "1217591724565043",
        "pr": 31,
        "branch": "agent/test",
        "head": head,
        "review_id": "4963493162",
    }


def test_worker_attempt_survives_authored_head_transition_and_replacement_is_new_generation():
    gh = base.FakeGitHub()
    dispatcher = _worker(gh)

    review = dispatcher.dispatch_worker_durable(
        surface=gh, surface_id=31, role="Code Review", phase="review", exact_context=_worker_context()
    )
    fix = dispatcher.dispatch_worker_durable(
        surface=gh, surface_id=31, role="Implementation", phase="fix", exact_context=_worker_context()
    )
    assert (fix.attempt_id, fix.generation) == (review.attempt_id, 1)

    p.establish_worker_authorship_baseline(gh, 31, base.HEAD)
    gh.pr["head"]["sha"] = base.NEW_HEAD
    p.record_worker_authorship(gh, 31, base.NEW_HEAD, fix, prior_candidate=base.HEAD)

    resumed = dispatcher.dispatch_worker_durable(
        surface=gh,
        surface_id=31,
        role="Implementation",
        phase="resume",
        exact_context=_worker_context(base.NEW_HEAD),
    )
    assert resumed.assignment_digest == fix.assignment_digest
    assert resumed.candidate_digest != fix.candidate_digest
    assert (resumed.attempt_id, resumed.generation) == (fix.attempt_id, 1)
    with pytest.raises(p.LifecycleError, match="cannot independently Review"):
        p.assert_worker_review_independent(gh.get_comments(31), base.NEW_HEAD, resumed)

    replacement = dispatcher.dispatch_worker_durable(
        surface=gh,
        surface_id=31,
        role="Code Review",
        phase="review",
        exact_context=_worker_context(base.NEW_HEAD),
        replacement=True,
    )
    assert replacement.generation == 2
    assert replacement.attempt_id != fix.attempt_id
    p.assert_worker_review_independent(gh.get_comments(31), base.NEW_HEAD, replacement)


def test_worker_omitted_reload_uses_persistent_exactness_and_authorship_gates_without_long_loop():
    gh = base.FakeGitHub(base.pr(draft=True))
    dispatcher = _worker(gh)
    attempt = dispatcher.dispatch_worker_durable(
        surface=gh,
        surface_id=31,
        role="Implementation",
        phase="implementation",
        exact_context=_worker_context(),
    )

    # One explicit pass through the other modes is enough to prove a mode switch does
    # not mint attempt identity. Do not simulate long history by repeatedly dispatching.
    for mode in ("Code Review", "Design Review", "Audit", "Implementation"):
        switched = dispatcher.dispatch_worker_durable(
            surface=gh,
            surface_id=31,
            role=mode,
            phase="switch",
            exact_context=_worker_context(),
        )
        assert (switched.attempt_id, switched.generation) == (attempt.attempt_id, 1)

    with pytest.raises(p.LifecycleError, match="PR/branch/head moved"):
        dispatcher.dispatch_worker_durable(
            surface=gh,
            surface_id=31,
            role="Implementation",
            phase="resume",
            exact_context=_worker_context(base.NEW_HEAD),
        )

    p.establish_worker_authorship_baseline(gh, 31, base.HEAD)
    gh.pr["head"]["sha"] = base.NEW_HEAD
    p.record_worker_authorship(gh, 31, base.NEW_HEAD, attempt, prior_candidate=base.HEAD)
    with pytest.raises(p.LifecycleError, match="authoritative current PR head"):
        p.record_worker_authorship(gh, 31, base.HEAD, attempt)

    resumed = dispatcher.dispatch_worker_durable(
        surface=gh,
        surface_id=31,
        role="Implementation",
        phase="resume",
        exact_context=_worker_context(base.NEW_HEAD),
    )
    with pytest.raises(p.LifecycleError, match="cannot independently Review"):
        p.assert_worker_review_independent(gh.get_comments(31), base.NEW_HEAD, resumed)

    # Review-ready is still derived from authoritative PR draft/head state; no packet
    # flag participates in the decision.
    assert base.engine(gh).inspect(gh.pr).state == p.LifecycleState.AUTHORING
    gh.pr["draft"] = False
    ready = base.engine(gh).inspect(gh.pr)
    assert ready.state == p.LifecycleState.REVIEW_READY
    assert ready.head == base.NEW_HEAD
