from __future__ import annotations

import test_pr_lifecycle as base
import test_pr_lifecycle_external_dependency as external

p = base.pr_lifecycle


def rendered(gh, *, authority=False):
    lifecycle = base.engine(gh, authority=authority).inspect(gh.pr)
    return p.action_first_status(lifecycle)


def assert_no_internal_default_jargon(text):
    for token in ("exact-head", "mutation broker", "Next owner/system", "LOCAL WORK TYPE", "LOCAL SCOPE"):
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
    assert "mutation broker" not in table
