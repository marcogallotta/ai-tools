from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util

import pytest
import test_pr_lifecycle as base
import test_pr_lifecycle_external_dependency as external
import handoff_preflight

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


def _ctx(head=base.HEAD):
    return {"task":"1217591724565043","pr":31,"branch":"agent/test","head":head,"review_id":"4963493162"}


def _fast_track_module():
    path=base.ROOT/"dish"/"scripts"/"chatgpt_project_kernels.py"
    spec=importlib.util.spec_from_file_location("worker_fast_track_eval",path); assert spec and spec.loader
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


def test_worker_attempt_survives_authored_head_transition_then_replacement_gets_new_generation():
    gh=base.FakeGitHub(); d=_worker(gh)
    review=d.dispatch_worker_durable(surface=gh,surface_id=31,role="Code Review",phase="review",exact_context=_ctx())
    retry=d.dispatch_worker_durable(surface=gh,surface_id=31,role="Code Review",phase="review",exact_context=_ctx())
    fix=d.dispatch_worker_durable(surface=gh,surface_id=31,role="Implementation",phase="fix",exact_context=_ctx())
    assert (retry.attempt_id,retry.generation)==(review.attempt_id,1)==(fix.attempt_id,fix.generation)

    p.establish_worker_authorship_baseline(gh,31,base.HEAD)
    gh.pr["head"]["sha"]=base.NEW_HEAD
    authors=p.record_worker_authorship(gh,31,base.NEW_HEAD,fix,prior_candidate=base.HEAD)
    assert fix.attempt_id in authors

    # Compaction/re-ground uses the newly authored exact H2, but assignment continuity
    # is task+PR+branch, so it must recover the authoring attempt rather than mint one.
    resumed=d.dispatch_worker_durable(surface=gh,surface_id=31,role="Implementation",phase="resume",exact_context=_ctx(base.NEW_HEAD))
    assert resumed.assignment_digest==fix.assignment_digest
    assert resumed.candidate_digest!=fix.candidate_digest
    assert (resumed.attempt_id,resumed.generation)==(fix.attempt_id,1)
    with pytest.raises(p.LifecycleError,match="cannot independently Review"):
        p.assert_worker_review_independent(gh.get_comments(31),base.NEW_HEAD,resumed)

    replacement=d.dispatch_worker_durable(surface=gh,surface_id=31,role="Code Review",phase="review",exact_context=_ctx(base.NEW_HEAD),replacement=True)
    assert replacement.generation==2 and replacement.attempt_id!=fix.attempt_id
    p.assert_worker_review_independent(gh.get_comments(31),base.NEW_HEAD,replacement)


def test_worker_long_horizon_omitted_reload_drives_real_governed_action_seams():
    # Deliberately no role/procedure packet reload occurs in this test. Every step
    # invokes the production durable/gate seam that governs the late action itself.
    gh=base.FakeGitHub(base.pr(draft=True)); d=_worker(gh); c=_ctx()
    attempt=d.dispatch_worker_durable(surface=gh,surface_id=31,role="Implementation",phase="implementation",exact_context=c)
    for i in range(16):
        mode=("Implementation","Code Review","Audit","Design Review")[i%4]
        switched=d.dispatch_worker_durable(surface=gh,surface_id=31,role=mode,phase="switch",exact_context=c)
        assert (switched.attempt_id,switched.generation)==(attempt.attempt_id,1)

    # resume/adopt: exact authoritative identity is required; a moved supplied head is rejected.
    resumed=d.dispatch_worker_durable(surface=gh,surface_id=31,role="Implementation",phase="resume",exact_context=c)
    assert resumed.attempt_id==attempt.attempt_id
    with pytest.raises(p.LifecycleError,match="PR/branch/head moved"):
        d.dispatch_worker_durable(surface=gh,surface_id=31,role="Implementation",phase="resume",exact_context=_ctx(base.NEW_HEAD))

    # semantic publication: the durable authorship gate only accepts the authoritative new head.
    p.establish_worker_authorship_baseline(gh,31,base.HEAD)
    gh.pr["head"]["sha"]=base.NEW_HEAD
    p.record_worker_authorship(gh,31,base.NEW_HEAD,resumed,prior_candidate=base.HEAD)
    with pytest.raises(p.LifecycleError,match="authoritative current PR head"):
        p.record_worker_authorship(gh,31,base.HEAD,resumed)
    resumed_h2=d.dispatch_worker_durable(surface=gh,surface_id=31,role="Implementation",phase="resume",exact_context=_ctx(base.NEW_HEAD))
    assert resumed_h2.attempt_id==attempt.attempt_id

    # draft -> review-ready: lifecycle state changes only after authoritative draft readback on H2.
    before_ready=base.engine(gh).inspect(gh.pr)
    assert before_ready.state==p.LifecycleState.AUTHORING and before_ready.head==base.NEW_HEAD
    gh.pr["draft"]=False
    ready=base.engine(gh).inspect(gh.pr)
    assert ready.state==p.LifecycleState.REVIEW_READY and ready.head==base.NEW_HEAD

    # final handoff: the existing handoff preflight rejects stale readback identities.
    handoff=f"Implementation handoff task 1217591724565043 PR #31 branch agent/test head {base.NEW_HEAD}"
    valid=handoff_preflight.validate_handoff(
        text=handoff,required_role="Review",destination_role="Review",
        required_task_gid="1217591724565043",task_readback_gid="1217591724565043",
        required_baseline=base.NEW_HEAD,baseline_readback=base.NEW_HEAD,
        required_identities={"pr":"31","branch":"agent/test"},
    )
    assert valid.executable
    stale=handoff_preflight.validate_handoff(
        text=handoff,required_role="Review",destination_role="Review",
        required_task_gid="1217591724565043",task_readback_gid="1217591724565043",
        required_baseline=base.NEW_HEAD,baseline_readback=base.HEAD,
        required_identities={"pr":"31","branch":"agent/test"},
    )
    assert not stale.executable

    # Code Review verdict: cumulative authorship rejects the author before any formal review is durable.
    with pytest.raises(p.LifecycleError,match="cannot independently Review"):
        p.assert_worker_review_independent(gh.get_comments(31),base.NEW_HEAD,resumed_h2)
    assert gh.reviews==[]
    reviewer=d.dispatch_worker_durable(surface=gh,surface_id=31,role="Code Review",phase="review",exact_context=_ctx(base.NEW_HEAD),replacement=True)
    p.assert_worker_review_independent(gh.get_comments(31),base.NEW_HEAD,reviewer)
    gh.reviews=[base.review(head=base.NEW_HEAD,verdict="BLOCK",review_id=44)]
    reviewed=base.engine(gh).inspect(gh.pr)
    assert reviewed.state==p.LifecycleState.CHANGES_REQUESTED and reviewed.reviewed_head==base.NEW_HEAD

    # Design Review verdict: final canonical task reread/digest movement prevents a stale verdict write.
    class DesignSurface:
        def __init__(self,notes): self.notes=notes; self.stories=[]
        def get_stories(self,_): return list(self.stories)
        def add_comment(self,_,body): self.stories.append({"text":body})
        def get_task(self,_): return {"gid":"1217591724565043","notes":self.notes,"modified_at":"2026-08-18T15:14:25.500Z"}
    notes="canonical design R3"; digest=hashlib.sha256(notes.encode()).hexdigest(); surface=DesignSurface(notes)
    design_context={"task":"1217591724565043","design_revision":"R3","design_digest":digest,"modified_at":"2026-08-18T15:14:25.500Z"}
    design=d.dispatch_worker_durable(surface=surface,surface_id=design_context["task"],role="Design Review",phase="design-review",exact_context=design_context)
    p.establish_worker_authorship_baseline(surface,design_context["task"],digest)
    surface.notes="moved design R4"
    reread=surface.get_task(design_context["task"]); current_digest=hashlib.sha256(reread["notes"].encode()).hexdigest()
    if current_digest==design_context["design_digest"]:
        surface.add_comment(design_context["task"],"VERDICT: PASS")
    assert not any("VERDICT:" in s["text"] for s in surface.stories)
    surface.notes=notes; reread=surface.get_task(design_context["task"]); current_digest=hashlib.sha256(reread["notes"].encode()).hexdigest()
    assert current_digest==design_context["design_digest"]
    design_records=[{"body":s["text"]} for s in surface.get_stories(design_context["task"])]
    p.assert_worker_review_independent(design_records,digest,design)
    surface.add_comment(design_context["task"],"VERDICT: PASS")
    assert any(s["text"]=="VERDICT: PASS" for s in surface.stories)

    # override-sensitive action: execute the real fast-track gate; inactive authority stays rejected.
    ft=_fast_track_module(); gate=ft.fast_track_gate_registry()["repository-context-bundle-witness"]; version=int(gate["current_version"]); semantic=gate["semantic_digest"]
    overlay={"version":"fasttrack-r3","state":"ACTIVE","generation":"worker-omitted-packet-eval","scope":[f"repository-context-bundle-witness@{version}"],"gate_semantics":{f"repository-context-bundle-witness@{version}":semantic},"expiry":None,"reason":"behavioral qualification"}
    use=ft.fast_track_use(overlay,gate_id="repository-context-bundle-witness",gate_version=version,task="1217591724565043",candidate=f"PR#31@{base.NEW_HEAD}",action="worker omitted-packet override-sensitive qualification",raw_evidence="FAILED: bundle unavailable",now=datetime(2026,8,18,tzinfo=timezone.utc))
    assert use["marker"]=="GATE WAIVED BY MARCO OVERRIDE"
    with pytest.raises(ft.KernelError,match="inactive"):
        ft.fast_track_use({**overlay,"state":"INACTIVE"},gate_id="repository-context-bundle-witness",gate_version=version,task="1217591724565043",candidate=f"PR#31@{base.NEW_HEAD}",action="worker omitted-packet override-sensitive qualification",raw_evidence="FAILED: bundle unavailable",now=datetime(2026,8,18,tzinfo=timezone.utc))
