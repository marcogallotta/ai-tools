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
    gh = base.FakeGitHub(); gh.reviews = [base.review(verdict="BLOCK")]
    text = rendered(gh); assert text.startswith("Review found a code problem. A fix is next. Nothing for you to do."); assert_no_internal_default_jargon(text)


def test_review_pass_pending_certification_says_no_action_plainly():
    gh = base.FakeGitHub(); gh.reviews = [base.review()]; gh.workflow_runs = base.runs(status_value="in_progress", conclusion=None)
    text = rendered(gh); assert text.startswith("Review passed. Automated landing checks are still pending. Nothing for you to do."); assert_no_internal_default_jargon(text)


def test_external_dependency_is_explicit_wait_not_user_troubleshooting():
    gh = external.ExternalGitHub(); gh.reviews = [base.review()]; gh.workflow_runs = base.runs(conclusion="failure")
    owner = base.pr(); owner["number"] = 77; gh.other_prs[77] = owner; gh.comments = [external.external_dependency_comment()]
    text = rendered(gh); assert text.startswith("This is waiting on an external dependency. Nothing for you to do."); assert_no_internal_default_jargon(text)


def test_long_local_certification_is_plain_local_test_action():
    gh = base.FakeGitHub(); gh.reviews = [base.review(body_tail="TESTS TO RUN: dish/scripts/native-certification --long")]
    text = rendered(gh); assert "needs a test on the local machine" in text and "local test runner" in text; assert_no_internal_default_jargon(text)


def test_local_implementation_is_plain_code_publication_action():
    gh = base.FakeGitHub(); gh.reviews = [base.review(body_tail="LOCAL IMPLEMENTATION COMPLETION REQUIRED: regenerate and publish\nTESTS TO RUN: NONE.")]
    text = rendered(gh); assert "needs a code or publication fix on the local machine" in text and "local coding agent" in text; assert_no_internal_default_jargon(text)


def test_integration_ready_says_checks_passed_and_no_user_action():
    gh = base.FakeGitHub(); gh.reviews = [base.review()]
    text = rendered(gh, authority=True); assert text.startswith("Review and required checks passed. Source integration is next. Nothing for you to do."); assert_no_internal_default_jargon(text)


def test_real_human_action_still_overrides_automatic_headline():
    gh = base.FakeGitHub(); gh.reviews = [base.review()]; lifecycle = base.engine(gh).inspect(gh.pr); lifecycle.human_action = "approve the production activation decision"
    assert p.action_first_status(lifecycle).startswith("Your next action: approve the production activation decision")


def test_table_uses_same_plain_renderer_as_notifications():
    gh = base.FakeGitHub(); gh.reviews = [base.review(verdict="BLOCK")]
    assert "Review found a code problem" in p._render_table([base.engine(gh).inspect(gh.pr)])


def _worker(gh):
    return p.WorkspaceAgentDispatcher(access_token="secret", review_trigger_id="agtch_review", worker_trigger_id="agtch_worker", http=base.EmptyAcceptedHTTP())

def _ctx(head=base.HEAD): return {"task":"1217591724565043","pr":31,"branch":"agent/test","head":head,"review_id":"4963493162"}


def test_worker_attempt_recovery_switch_replacement_authorship_and_design_surface():
    gh=base.FakeGitHub(); d=_worker(gh); c=_ctx()
    r1=d.dispatch_worker_durable(surface=gh,surface_id=31,role="Code Review",phase="review",exact_context=c)
    retry=d.dispatch_worker_durable(surface=gh,surface_id=31,role="Code Review",phase="review",exact_context=c)
    fix=d.dispatch_worker_durable(surface=gh,surface_id=31,role="Implementation",phase="fix",exact_context=c)
    assert (retry.attempt_id,retry.generation)==(r1.attempt_id,1)==(fix.attempt_id,fix.generation)
    assert len({call[3]["conversation_key"] for call in d.http.calls})==1
    p.establish_worker_authorship_baseline(gh,31,base.HEAD)
    p.record_worker_authorship(gh,31,base.NEW_HEAD,fix,prior_candidate=base.HEAD)
    with pytest.raises(p.LifecycleError,match="cannot independently Review"): p.assert_worker_review_independent(gh.get_comments(31),base.NEW_HEAD,fix)
    replacement=d.dispatch_worker_durable(surface=gh,surface_id=31,role="Code Review",phase="review",exact_context=c,replacement=True)
    assert replacement.generation==2 and replacement.attempt_id!=r1.attempt_id
    p.assert_worker_review_independent(gh.get_comments(31),base.NEW_HEAD,replacement)
    assert p.recover_worker_attempt(gh.get_comments(31),r1.assignment_digest).attempt_id==replacement.attempt_id

    class StorySurface:
        def __init__(self): self.stories=[]
        def get_stories(self,_): return list(self.stories)
        def add_comment(self,_,body): self.stories.append({"text":body})
    s=StorySurface(); design={"task":"1217591724565043","design_revision":"R3","design_digest":"d"*64,"modified_at":"2026-08-18T15:14:25.500Z"}
    dr=d.dispatch_worker_durable(surface=s,surface_id=design["task"],role="Design Review",phase="design-review",exact_context=design)
    assert dr.accepted and dr.generation==1 and any(p.WORKER_ATTEMPT_MARKER in x["text"] for x in s.stories)


def test_worker_long_horizon_omitted_reload_behavioral_matrix_never_adds_authority():
    gh=base.FakeGitHub(); d=_worker(gh); c=_ctx(); attempt=d.dispatch_worker_durable(surface=gh,surface_id=31,role="Implementation",phase="implementation",exact_context=c)
    for i in range(16):
        mode=("Implementation","Code Review","Audit","Design Review")[i%4]
        assert d.dispatch_worker_durable(surface=gh,surface_id=31,role=mode,phase="switch",exact_context=c).attempt_id==attempt.attempt_id
    cases={
        "resume_adopt":("Implementation",{}),
        "semantic_publication":("Implementation",{"write_fence_verified":True}),
        "draft_to_review_ready":("Implementation",{"write_fence_verified":True,"authoritative_readback":True}),
        "final_handoff":("Implementation",{"write_fence_verified":True,"authoritative_readback":True}),
        "code_review_verdict":("Code Review",{"independent":True}),
        "design_review_verdict":("Design Review",{"independent":True}),
        "override_sensitive":("Implementation",{"scoped_override":True}),
    }
    for action,(mode,extra) in cases.items():
        common=dict(mode=mode,attempt_accepted=True,identity_current=True,**extra)
        omitted=p.qualify_worker_late_action(action,fresh_packet_loaded=False,**common)
        loaded=p.qualify_worker_late_action(action,fresh_packet_loaded=True,**common)
        assert omitted==loaded and omitted.allowed
        broken=dict(common)
        if action=="resume_adopt": broken["identity_current"]=False
        elif action=="semantic_publication": broken["write_fence_verified"]=False
        elif action in {"draft_to_review_ready","final_handoff"}: broken["authoritative_readback"]=False
        elif action in {"code_review_verdict","design_review_verdict"}: broken["independent"]=False
        else: broken["scoped_override"]=False
        assert p.qualify_worker_late_action(action,fresh_packet_loaded=False,**broken)==p.qualify_worker_late_action(action,fresh_packet_loaded=True,**broken)
        assert not p.qualify_worker_late_action(action,fresh_packet_loaded=False,**broken).allowed
