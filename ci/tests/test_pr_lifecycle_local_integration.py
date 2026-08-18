from __future__ import annotations

from copy import deepcopy
import shlex
import sys
import threading
import time

import pytest

import test_pr_lifecycle as base
from pr_lifecycle_local_integration import LocalIntegrationFence, LocalIntegrationLauncher, checkpoint_claim
from test_pr_lifecycle_asana_writeback import FakeAsana, TASK

p = base.pr_lifecycle


def configure(lifecycle, launcher):
    lifecycle.local_integration_launcher = launcher
    return lifecycle


def test_launcher_unavailable_leaves_integration_ready_with_zero_merge():
    gh = base.FakeGitHub()
    gh.reviews = [base.review()]
    lifecycle = base.engine(gh, authority=True, capable=False)
    result = lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None)
    assert result.state == p.LifecycleState.INTEGRATION_READY
    assert "no remote/connector landing fallback" in (result.residual_reason or "")
    assert not any(event[0] == "merge" for event in gh.events)


def test_head_changing_local_reconciliation_stops_for_fresh_review(tmp_path, monkeypatch):
    monkeypatch.setenv("DISH_LOCAL_INTEGRATION_STATE_ROOT", str(tmp_path))
    gh = base.FakeGitHub()
    gh.reviews = [base.review()]
    launcher = base.FakeLocalIntegration(gh, outcome="head-change")
    lifecycle = configure(base.engine(gh, authority=True), launcher)
    result = lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None)
    assert result.head == base.NEW_HEAD
    assert result.state == p.LifecycleState.REVIEW_READY
    assert len(launcher.calls) == 1
    assert not any(event[0] == "merge" for event in gh.events)


def test_failed_exact_head_evidence_never_launches_local_integration(tmp_path, monkeypatch):
    monkeypatch.setenv("DISH_LOCAL_INTEGRATION_STATE_ROOT", str(tmp_path))
    gh = base.FakeGitHub()
    gh.reviews = [base.review()]
    gh.combined_status = base.status(state="failure")
    launcher = base.FakeLocalIntegration(gh, outcome="merge")
    lifecycle = configure(base.engine(gh, authority=True), launcher)
    current = lifecycle.inspect(gh.pr)
    assert current.state != p.LifecycleState.INTEGRATION_READY
    result = lifecycle.dispatch_one(current, workspace=None, local_reviewer=None)
    assert launcher.calls == []
    assert not any(event[0] == "merge" for event in gh.events)
    assert result.state != p.LifecycleState.MERGED


def test_stale_head_between_handoff_and_claim_prevents_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("DISH_LOCAL_INTEGRATION_STATE_ROOT", str(tmp_path))
    gh = base.FakeGitHub()
    gh.reviews = [base.review()]
    launcher = base.FakeLocalIntegration(gh, outcome="merge")
    lifecycle = configure(base.engine(gh, authority=True), launcher)
    original = lifecycle._ensure_local_integration_handoff

    def move_after_handoff(current):
        value = original(current)
        gh.pr["head"]["sha"] = base.NEW_HEAD
        return value

    monkeypatch.setattr(lifecycle, "_ensure_local_integration_handoff", move_after_handoff)
    result = lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None)
    assert result.head == base.NEW_HEAD
    assert result.state == p.LifecycleState.REVIEW_READY
    assert launcher.calls == []
    assert not any(event[0] == "merge" for event in gh.events)


def _fence(
    tmp_path,
    *,
    review_id=10,
    main_sha="c" * 40,
    handoff_comment_id=99,
    handoff_key_value="abc123",
):
    return LocalIntegrationFence(
        repository="marcogallotta/ai-tools",
        pr_number=31,
        branch="agent/test",
        head=base.HEAD,
        review_id=review_id,
        task_ids=[TASK],
        main_sha=main_sha,
        handoff_comment_id=handoff_comment_id,
        handoff_key_value=handoff_key_value,
        root=tmp_path,
    )


def test_two_local_starts_have_exactly_one_mutation_owner(tmp_path):
    first = _fence(tmp_path)
    second = _fence(tmp_path)
    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()
    assert second.acquire() is True
    second.release()


def test_recovery_reconstructs_checkpoint_after_prior_owner_disappears(tmp_path, monkeypatch):
    monkeypatch.setenv("DISH_LOCAL_INTEGRATION_STATE_ROOT", str(tmp_path))
    first = _fence(tmp_path)
    assert first.acquire() is True
    payload = first.payload()
    checkpoint_claim(
        claim_path=payload["state_path"],
        claim_id=payload["claim_id"],
        phase="reconciling",
        worktree=str(tmp_path / "worktree"),
        current_head=base.HEAD,
        main_sha="c" * 40,
        next_action="finish conflict-free rebase",
    )
    # Crash model: OS lock is released without a terminal finish checkpoint.
    first.release()

    replacement = _fence(tmp_path)
    assert replacement.acquire() is True
    try:
        recovered = replacement.payload()
        assert recovered["generation"] == 2
        assert recovered["recovery"]["phase"] == "reconciling"
        assert recovered["recovery"]["reconciliation_occurred"] is True
        assert recovered["recovery"]["worktree"].endswith("/worktree")
    finally:
        replacement.release()


def test_same_head_retry_refreshes_main_review_handoff_and_launches_once(tmp_path, monkeypatch):
    monkeypatch.setenv("DISH_LOCAL_INTEGRATION_STATE_ROOT", str(tmp_path))
    gh = base.FakeGitHub()
    gh.reviews = [base.review(review_id=10)]
    first_launcher = base.FakeLocalIntegration(gh, outcome="return")
    lifecycle = configure(base.engine(gh, authority=True), first_launcher)

    first = lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None)
    assert first.state == p.LifecycleState.INTEGRATION_READY
    assert len(first_launcher.calls) == 1
    first_claim = first_launcher.calls[0]["claim"]
    first_handoff = first_launcher.calls[0]["handoff"]

    gh.refs["heads/main"] = "e" * 40
    gh.reviews = [base.review(review_id=11)]
    second_launcher = base.FakeLocalIntegration(gh, outcome="return")
    lifecycle.local_integration_launcher = second_launcher

    second = lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None)
    assert second.state == p.LifecycleState.INTEGRATION_READY
    assert len(second_launcher.calls) == 1
    second_call = second_launcher.calls[0]
    second_claim = second_call["claim"]
    assert second_claim["generation"] == 2
    assert second_claim["main_sha"] == "e" * 40
    assert second_claim["review_id"] == 11
    assert second_call["handoff"]["observed_main_sha"] == "e" * 40
    assert second_claim["recovery"]["phase"] == "returned"
    assert second_claim["recovery"]["main_sha"] == "c" * 40
    assert second_claim["recovery"]["review_id"] == 10
    assert second_claim["recovery"]["handoff_comment_id"] == first_claim["handoff_comment_id"]
    assert second_claim["recovery"]["handoff_key"] == first_handoff["key"]
    assert second_claim["handoff_comment_id"] != first_claim["handoff_comment_id"]
    assert second_claim["handoff_key"] != first_claim["handoff_key"]


def test_equivalent_duplicate_handoff_comment_id_does_not_poison_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("DISH_LOCAL_INTEGRATION_STATE_ROOT", str(tmp_path))
    gh = base.FakeGitHub()
    gh.reviews = [base.review()]
    first_launcher = base.FakeLocalIntegration(gh, outcome="return")
    lifecycle = configure(base.engine(gh, authority=True), first_launcher)

    first = lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None)
    assert first.state == p.LifecycleState.INTEGRATION_READY
    assert len(first_launcher.calls) == 1
    first_claim = first_launcher.calls[0]["claim"]
    first_comment = next(
        comment for comment in gh.comments if comment["id"] == first_claim["handoff_comment_id"]
    )
    duplicate = deepcopy(first_comment)
    duplicate["id"] = max(comment["id"] for comment in gh.comments) + 1
    duplicate["created_at"] = "2026-08-13T08:00:01+00:00"
    duplicate["updated_at"] = duplicate["created_at"]
    gh.comments.append(duplicate)

    second_launcher = base.FakeLocalIntegration(gh, outcome="return")
    lifecycle.local_integration_launcher = second_launcher
    second = lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None)

    assert second.state == p.LifecycleState.INTEGRATION_READY
    assert len(second_launcher.calls) == 1
    second_claim = second_launcher.calls[0]["claim"]
    assert second_claim["generation"] == 2
    assert second_claim["handoff_comment_id"] == duplicate["id"]
    assert second_claim["recovery"]["handoff_comment_id"] == first_claim["handoff_comment_id"]
    assert second_claim["handoff_key"] == first_claim["handoff_key"]


class SemanticStopLauncher:
    command = "fake-local-integration"

    def __init__(self):
        self.calls = []

    def dispatch(self, context, *, lock_fd=None):
        self.calls.append(deepcopy(context))
        claim = context["claim"]
        checkpoint_claim(
            claim_path=claim["state_path"],
            claim_id=claim["claim_id"],
            phase="stopped-semantic",
            current_head=context["pull_request"]["head"],
            next_action="return semantic conflict to Implementation",
        )


def test_semantic_conflict_is_handed_to_local_integration_but_never_auto_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("DISH_LOCAL_INTEGRATION_STATE_ROOT", str(tmp_path))
    candidate = base.pr()
    candidate["mergeable"] = False
    candidate["mergeable_state"] = "dirty"
    gh = base.FakeGitHub(candidate)
    gh.reviews = [base.review()]
    launcher = SemanticStopLauncher()
    lifecycle = configure(base.engine(gh, authority=True), launcher)
    current = lifecycle.inspect(gh.pr)
    assert current.state == p.LifecycleState.REVIEW_PASSED
    assert "mergeab" in (current.residual_reason or "").lower() or "conflict" in (current.residual_reason or "").lower()
    result = lifecycle.dispatch_one(current, workspace=None, local_reviewer=None)
    assert len(launcher.calls) == 1
    assert "semantic" in launcher.calls[0]["instruction"].lower()
    assert not any(event[0] == "merge" for event in gh.events)
    assert result.state == p.LifecycleState.REVIEW_PASSED


def test_successful_local_merge_requires_readback_then_reconciles_asana(tmp_path, monkeypatch):
    monkeypatch.setenv("DISH_LOCAL_INTEGRATION_STATE_ROOT", str(tmp_path))
    gh = base.FakeGitHub()
    gh.reviews = [base.review()]
    asana = FakeAsana({
        "gid": TASK,
        "notes": "<!-- dish-source-work:v1 final_outstanding_gate=true -->",
        "completed": False,
        "dependents": [],
    })
    lifecycle = p.LifecycleEngine(
        gh,
        asana=asana,
        integration_authority=True,
        integration_capable=True,
        now=lambda: base.NOW,
    )
    launcher = base.FakeLocalIntegration(gh, outcome="merge")
    lifecycle.local_integration_launcher = launcher
    result = lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None)
    assert result.state == p.LifecycleState.MERGED
    assert asana.field_updates == [(TASK, {"completed": True})]
    assert len(asana.comments) == 1
    assert f"head={base.HEAD} merge={'d' * 40}" in asana.comments[0][1]


def test_legacy_dispatcher_merge_helper_is_fail_closed_even_with_capability():
    gh = base.FakeGitHub()
    gh.reviews = [base.review()]
    lifecycle = base.engine(gh, authority=True, capable=True)
    result = lifecycle._merge_exact_head(lifecycle.inspect(gh.pr))
    assert result.state == p.LifecycleState.INTEGRATION_READY
    assert "disabled by Integration V1-A" in (result.residual_reason or "")
    assert not any(event[0] == "merge" for event in gh.events)


def test_child_inherits_fence_so_parent_crash_does_not_create_second_owner(tmp_path):
    first = _fence(tmp_path)
    assert first.acquire() is True
    ready = tmp_path / "child-ready"
    code = (
        "import os,time,pathlib; "
        "fd=int(os.environ['DISH_LOCAL_INTEGRATION_LOCK_FD']); os.fstat(fd); "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); "
        "time.sleep(0.35)"
    )
    launcher = LocalIntegrationLauncher(f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}")
    errors = []

    def run_child():
        try:
            launcher.dispatch({"schema": "test"}, lock_fd=first.lock_fd())
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=run_child)
    thread.start()
    deadline = time.monotonic() + 2
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists(), "child did not inherit/start with the Integration fence"

    # Model abrupt parent death: close only the parent's descriptor copy without LOCK_UN.
    # The child copy must keep the kernel flock alive until the child exits.
    first._handle.close()
    first._handle = None
    contender = _fence(tmp_path)
    assert contender.acquire() is False

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert errors == []
    assert contender.acquire() is True
    contender.release()
