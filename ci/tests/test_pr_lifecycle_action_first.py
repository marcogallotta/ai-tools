from __future__ import annotations

import test_pr_lifecycle as base
import test_pr_lifecycle_external_dependency as external

p = base.pr_lifecycle


def rendered(gh, *, authority=False):
    lifecycle = base.engine(gh, authority=authority).inspect(gh.pr)
    return p.action_first_status(lifecycle)


def test_review_block_says_review_blocked_and_automatic_fix_is_next():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(verdict="BLOCK")]
    text = rendered(gh)
    assert text.startswith("Review blocked the candidate; automatic Implementation fix is next.")
    assert "Review blocked this exact candidate" in text
    assert "Next owner/system: mutation broker and Implementation/fix" in text


def test_review_pass_pending_certification_says_no_action_and_preserves_verdict():
    gh = base.FakeGitHub()
    gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(status_value="in_progress", conclusion=None)
    text = rendered(gh)
    assert text.startswith("No action for you — Review accepted the candidate")
    assert "Review accepted this exact candidate" in text
    assert "Integration/gate evaluation" in text


def test_external_dependency_is_explicit_wait_not_user_troubleshooting():
    gh = external.ExternalGitHub()
    gh.reviews = [base.review()]
    gh.workflow_runs = base.runs(conclusion="failure")
    owner = base.pr()
    owner["number"] = 77
    gh.other_prs[77] = owner
    gh.comments = [external.external_dependency_comment()]
    text = rendered(gh)
    assert text.startswith("No action for you — wait for the named external dependency owner.")


def test_long_local_certification_is_tests_only_not_heavy_implementation():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(body_tail="TESTS TO RUN: dish/scripts/native-certification --long")]
    text = rendered(gh)
    assert "LOCAL WORK TYPE: TESTS ONLY" in text
    assert "IMPLEMENTATION / PUBLICATION" not in text
    assert "heavy" not in text.lower()


def test_local_implementation_is_classified_as_implementation_publication():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(body_tail="LOCAL IMPLEMENTATION COMPLETION REQUIRED: regenerate and publish\nTESTS TO RUN: NONE.")]
    text = rendered(gh)
    assert "LOCAL WORK TYPE: IMPLEMENTATION / PUBLICATION" in text


def test_integration_ready_says_review_accepted_integration_next_and_no_user_action():
    gh = base.FakeGitHub()
    gh.reviews = [base.review()]
    text = rendered(gh, authority=True)
    assert text.startswith("No action for you — Review accepted the candidate; Integration is next.")
    assert "Review accepted this exact candidate" in text


def test_table_uses_same_action_first_renderer_as_notifications():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(verdict="BLOCK")]
    lifecycle = base.engine(gh).inspect(gh.pr)
    table = p._render_table([lifecycle])
    assert "Review blocked the candidate" in table
