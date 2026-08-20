from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
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


@pytest.fixture(scope="module")
def worker_long_history_without_packet_reload():
    """Build long Worker history once; no role/procedure packet is loaded by this qualification."""
    gh = base.FakeGitHub(base.pr(draft=True))
    gh.comments = [
        {
            "id": index + 1,
            "body": f"historical worker context {index:02d}",
            "created_at": base.NOW.isoformat(),
            "updated_at": base.NOW.isoformat(),
        }
        for index in range(64)
    ]
    dispatcher = _worker(gh)
    attempt = dispatcher.dispatch_worker_durable(
        surface=gh,
        surface_id=31,
        role="Implementation",
        phase="implementation",
        exact_context=_worker_context(),
    )
    for mode in ("Code Review", "Design Review", "Audit", "Implementation"):
        switched = dispatcher.dispatch_worker_durable(
            surface=gh,
            surface_id=31,
            role=mode,
            phase="switch",
            exact_context=_worker_context(),
        )
        assert (switched.attempt_id, switched.generation) == (attempt.attempt_id, 1)
    return gh, attempt


@pytest.fixture
def omitted_packet_state(worker_long_history_without_packet_reload):
    gh, attempt = worker_long_history_without_packet_reload
    return deepcopy(gh), deepcopy(attempt)


def test_worker_omitted_packet_resume_adopt_uses_exact_identity_gate(omitted_packet_state):
    gh, attempt = omitted_packet_state
    dispatcher = _worker(gh)
    resumed = dispatcher.dispatch_worker_durable(
        surface=gh,
        surface_id=31,
        role="Implementation",
        phase="resume",
        exact_context=_worker_context(),
    )
    assert resumed.attempt_id == attempt.attempt_id
    with pytest.raises(p.LifecycleError, match="PR/branch/head moved"):
        dispatcher.dispatch_worker_durable(
            surface=gh,
            surface_id=31,
            role="Implementation",
            phase="resume",
            exact_context=_worker_context(base.NEW_HEAD),
        )


def test_worker_omitted_packet_semantic_publication_uses_current_head_gate(omitted_packet_state):
    gh, attempt = omitted_packet_state
    p.establish_worker_authorship_baseline(gh, 31, base.HEAD)
    gh.pr["head"]["sha"] = base.NEW_HEAD
    authors = p.record_worker_authorship(gh, 31, base.NEW_HEAD, attempt, prior_candidate=base.HEAD)
    assert attempt.attempt_id in authors
    with pytest.raises(p.LifecycleError, match="authoritative current PR head"):
        p.record_worker_authorship(gh, 31, base.HEAD, attempt)


def test_worker_omitted_packet_review_ready_uses_authoritative_draft_readback(omitted_packet_state):
    gh, _ = omitted_packet_state
    before = base.engine(gh).inspect(gh.pr)
    assert before.state == p.LifecycleState.AUTHORING
    gh.pr["draft"] = False
    ready = base.engine(gh).inspect(gh.pr)
    assert ready.state == p.LifecycleState.REVIEW_READY
    assert ready.head == base.HEAD


def test_worker_omitted_packet_final_handoff_uses_exact_candidate_preflight(omitted_packet_state):
    _, _attempt = omitted_packet_state
    handoff = f"Implementation handoff task 1217591724565043 PR #31 branch agent/test head {base.HEAD}"
    valid = handoff_preflight.validate_handoff(
        text=handoff,
        required_role="Review",
        destination_role="Review",
        required_task_gid="1217591724565043",
        task_readback_gid="1217591724565043",
        required_baseline=base.HEAD,
        baseline_readback=base.HEAD,
        required_identities={"pr": "31", "branch": "agent/test"},
    )
    assert valid.executable
    stale = handoff_preflight.validate_handoff(
        text=handoff,
        required_role="Review",
        destination_role="Review",
        required_task_gid="1217591724565043",
        task_readback_gid="1217591724565043",
        required_baseline=base.HEAD,
        baseline_readback=base.NEW_HEAD,
        required_identities={"pr": "31", "branch": "agent/test"},
    )
    assert not stale.executable


def test_worker_omitted_packet_code_review_verdict_uses_independence_and_formal_review_state(omitted_packet_state):
    gh, attempt = omitted_packet_state
    p.establish_worker_authorship_baseline(gh, 31, base.HEAD)
    p.record_worker_authorship(gh, 31, base.HEAD, attempt)
    with pytest.raises(p.LifecycleError, match="cannot independently Review"):
        p.assert_worker_review_independent(gh.get_comments(31), base.HEAD, attempt)

    reviewer = _worker(gh).dispatch_worker_durable(
        surface=gh,
        surface_id=31,
        role="Code Review",
        phase="review",
        exact_context=_worker_context(),
        replacement=True,
    )
    p.assert_worker_review_independent(gh.get_comments(31), base.HEAD, reviewer)
    gh.pr["draft"] = False
    gh.reviews = [base.review(head=base.HEAD, verdict="BLOCK", review_id=44)]
    reviewed = base.engine(gh).inspect(gh.pr)
    assert reviewed.state == p.LifecycleState.CHANGES_REQUESTED
    assert reviewed.reviewed_head == base.HEAD


class _DesignSurface:
    def __init__(self, notes, history):
        self.notes = notes
        self.stories = [{"text": body} for body in history]

    def get_stories(self, _):
        return list(self.stories)

    def add_comment(self, _, body):
        self.stories.append({"text": body})

    def get_task(self, _):
        return {
            "gid": "1217591724565043",
            "notes": self.notes,
            "modified_at": "2026-08-18T15:14:25.500Z",
        }


def test_worker_omitted_packet_design_review_verdict_uses_final_digest_reread(omitted_packet_state):
    gh, _ = omitted_packet_state
    notes = "canonical design R3"
    digest = hashlib.sha256(notes.encode()).hexdigest()
    surface = _DesignSurface(notes, [item["body"] for item in gh.comments])
    context = {
        "task": "1217591724565043",
        "design_revision": "R3",
        "design_digest": digest,
        "modified_at": "2026-08-18T15:14:25.500Z",
    }
    design = _worker(surface).dispatch_worker_durable(
        surface=surface,
        surface_id=context["task"],
        role="Design Review",
        phase="design-review",
        exact_context=context,
    )
    p.establish_worker_authorship_baseline(surface, context["task"], digest)

    surface.notes = "moved design R4"
    moved = hashlib.sha256(surface.get_task(context["task"])["notes"].encode()).hexdigest()
    if moved == context["design_digest"]:
        surface.add_comment(context["task"], "VERDICT: PASS")
    assert not any(story["text"] == "VERDICT: PASS" for story in surface.stories)

    surface.notes = notes
    current = hashlib.sha256(surface.get_task(context["task"])["notes"].encode()).hexdigest()
    assert current == context["design_digest"]
    records = [{"body": story["text"]} for story in surface.get_stories(context["task"])]
    p.assert_worker_review_independent(records, digest, design)
    surface.add_comment(context["task"], "VERDICT: PASS")
    assert surface.stories[-1]["text"] == "VERDICT: PASS"


@lru_cache(maxsize=1)
def _fast_track_module():
    path = base.ROOT / "dish" / "scripts" / "chatgpt_project_kernels.py"
    spec = importlib.util.spec_from_file_location("worker_fast_track_eval", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_omitted_packet_override_sensitive_action_uses_live_fast_track_gate(omitted_packet_state):
    gh, _ = omitted_packet_state
    assert len(gh.comments) >= 64
    ft = _fast_track_module()
    gate = ft.fast_track_gate_registry()["repository-context-bundle-witness"]
    version = int(gate["current_version"])
    semantic = gate["semantic_digest"]
    scope = f"repository-context-bundle-witness@{version}"
    overlay = {
        "version": "fasttrack-r3",
        "state": "ACTIVE",
        "generation": "worker-omitted-packet-eval",
        "scope": [scope],
        "gate_semantics": {scope: semantic},
        "expiry": None,
        "reason": "bounded omitted-packet qualification",
    }
    use = ft.fast_track_use(
        overlay,
        gate_id="repository-context-bundle-witness",
        gate_version=version,
        task="1217591724565043",
        candidate=f"PR#31@{base.HEAD}",
        action="worker omitted-packet override-sensitive qualification",
        raw_evidence="FAILED: bundle unavailable",
        now=datetime(2026, 8, 18, tzinfo=timezone.utc),
    )
    assert use["marker"] == "GATE WAIVED BY MARCO OVERRIDE"
    with pytest.raises(ft.KernelError, match="inactive"):
        ft.fast_track_use(
            {**overlay, "state": "INACTIVE"},
            gate_id="repository-context-bundle-witness",
            gate_version=version,
            task="1217591724565043",
            candidate=f"PR#31@{base.HEAD}",
            action="worker omitted-packet override-sensitive qualification",
            raw_evidence="FAILED: bundle unavailable",
            now=datetime(2026, 8, 18, tzinfo=timezone.utc),
        )


def test_manual_worker_review_needs_no_automated_attempt_or_authorship_record():
    gh = base.FakeGitHub()
    assert gh.comments == []
    assert p.assert_manual_worker_review_independent(remembers_material_authorship=False)


def test_manual_worker_formal_block_binds_same_pr_fix_without_second_prompt_or_attempt_record():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(head=base.HEAD, verdict="BLOCK", review_id=44)]
    fix = p.bind_manual_worker_block_fix(
        gh,
        31,
        task="1217657236042386",
        blocked_head=base.HEAD,
        block_review_id="44",
    )
    assert (fix.pr, fix.branch, fix.blocked_head, fix.block_review_id) == (31, "agent/test", base.HEAD, "44")
    assert not any("dish-worker-attempt:v1" in item["body"] for item in gh.comments)


def test_manual_worker_block_fix_fails_closed_when_exact_head_moves():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(head=base.HEAD, verdict="BLOCK", review_id=44)]
    gh.pr["head"]["sha"] = base.NEW_HEAD
    with pytest.raises(p.LifecycleError, match="candidate moved"):
        p.bind_manual_worker_block_fix(
            gh,
            31,
            task="1217657236042386",
            blocked_head=base.HEAD,
            block_review_id="44",
        )


def test_manual_worker_remembered_self_authorship_blocks_review_without_durable_taint():
    with pytest.raises(p.LifecycleError, match="remembers material authorship"):
        p.assert_manual_worker_review_independent(remembers_material_authorship=True)
    assert p.assert_manual_worker_review_independent(remembers_material_authorship=False)


def test_invalid_automated_worker_bookkeeping_does_not_gate_manual_review_or_fix():
    gh = base.FakeGitHub()
    gh.comments = [{
        "id": 1,
        "body": '<!-- dish-worker-attempt:v1 {"assignment_digest":"deadbeef","candidate_digest":"' + ("a" * 64) + '","attempt_id":"bad","mode":"Code Review","state":"accepted"} -->',
    }]
    gh.reviews = [base.review(head=base.HEAD, verdict="BLOCK", review_id=44)]
    assert p.assert_manual_worker_review_independent(remembers_material_authorship=False)
    assert p.bind_manual_worker_block_fix(
        gh,
        31,
        task="1217657236042386",
        blocked_head=base.HEAD,
        block_review_id="44",
    ).block_review_id == "44"
    with pytest.raises(p.LifecycleError, match="attempt generation"):
        p.recover_worker_attempt(gh.comments, "deadbeef")
