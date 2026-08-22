from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from pr_lifecycle_v4 import (
    POLICY_GENERATION,
    V4Reconciler,
    V4StateStore,
    WakeBridge,
    actionable_version,
    ingest_event,
    verify_asana_signature,
    verify_github_signature,
    wake_packet,
)


CASE = {
    "case_key": "case-a",
    "reason_class": "CI_RED_CURRENT_MAIN_OR_EXTERNAL",
    "repository": "marcogallotta/ai-tools",
    "pr": 203,
    "task": "1217687603325381",
    "head": "a" * 40,
    "evidence": {"check": "Dish / exact-head certification", "last_changed": "volatile-a", "retry_after": 15},
    "next_owner": "Integrator",
    "next_action": "diagnose current-main/external failure before landing",
}


class FakeAppServer:
    def __init__(self):
        self.status = "idle"
        self.started = []
        self.history = []
        self.fail_after_accept = False
        self.turn_status = {}
        self.resumed = []

    def thread_read(self, thread_id, *, include_turns):
        turns = []
        if include_turns:
            turns = [
                {
                    "items": [{"type": "userMessage", "clientId": value}],
                    "status": {"type": self.turn_status.get(value, "in_progress")},
                }
                for value in self.history
            ]
        return {"thread": {"id": thread_id, "status": {"type": self.status}, "turns": turns}}

    def thread_resume(self, thread_id):
        self.resumed.append(thread_id)
        if self.status == "notLoaded":
            self.status = "idle"
        return {"thread": {"id": thread_id, "status": {"type": self.status}}}

    def turn_start(self, thread_id, packet, *, client_user_message_id):
        self.started.append(client_user_message_id)
        if self.fail_after_accept:
            self.history.append(client_user_message_id)
            raise ConnectionError("response lost after acceptance")
        self.history.append(client_user_message_id)
        return {"turn": {"id": f"turn-{len(self.started)}"}}


def store(tmp_path):
    return V4StateStore(tmp_path / "v4.json")


def bridge(tmp_path, state, app):
    return WakeBridge(store=state, app_server=app, thread_id="lifecycle-thread", fence_path=tmp_path / "wake.lock")


def test_idle_heartbeat_starts_zero_model_turns(tmp_path):
    calls = []
    state = store(tmp_path)
    app = FakeAppServer()
    reconciler = V4Reconciler(
        store=state,
        authoritative_cases=lambda snapshot: calls.append(snapshot) or [CASE],
        bridge=bridge(tmp_path, state, app),
    )
    assert reconciler.reconcile() == {
        "dirty": 0,
        "prepared": 0,
        "wake_results": [],
        "model_turns_started": 0,
    }
    assert calls == []
    assert app.started == []


def test_cold_start_baselines_existing_case_without_model_turn(tmp_path):
    state = store(tmp_path)
    app = FakeAppServer()
    current = [dict(CASE)]
    reconciler = V4Reconciler(
        store=state,
        authoritative_cases=lambda snapshot: current,
        bridge=bridge(tmp_path, state, app),
    )
    state.mark_dirty(
        provider="github",
        resource_kind="repository",
        resource_id="marcogallotta/ai-tools",
        delivery_id="arrived-during-commissioning",
    )
    assert reconciler.baseline_current() == {
        "actionable_cases": 1,
        "baselined": 1,
        "prepared": 0,
        "wake_results": [],
        "model_turns_started": 0,
    }
    assert app.started == []
    assert len(state.snapshot_dirty().resources) == 1

    assert reconciler.reconcile()["model_turns_started"] == 0
    assert app.started == []

    changed = dict(CASE)
    changed["evidence"] = {"check": "Dish / exact-head certification", "failure": "new actionable evidence"}
    current[:] = [changed]
    state.mark_dirty(
        provider="github",
        resource_kind="repository",
        resource_id="marcogallotta/ai-tools",
        delivery_id="changed",
    )
    result = reconciler.reconcile()
    assert result["model_turns_started"] == 1
    assert len(app.started) == 1


def test_cold_start_baseline_is_one_shot_and_refuses_existing_wake_history(tmp_path):
    state = store(tmp_path)
    reconciler = V4Reconciler(store=state, authoritative_cases=lambda snapshot: [CASE], bridge=None)
    assert reconciler.baseline_current()["baselined"] == 1

    changed = dict(CASE)
    changed["next_action"] = "new action must not be silently baselined"
    reconciler = V4Reconciler(store=state, authoritative_cases=lambda snapshot: [changed], bridge=None)
    assert reconciler.baseline_current()["baselined"] == 0
    assert state.prepare_wakes([changed])[0]["status"] == "PREPARED"

    fresh = store(tmp_path / "other")
    fresh.prepare_wakes([CASE])
    with pytest.raises(ValueError, match="after wake history"):
        fresh.baseline_current([CASE])


def test_duplicate_and_out_of_order_webhooks_coalesce_to_one_authoritative_wake(tmp_path):
    state = store(tmp_path)
    app = FakeAppServer()
    payload = {"repository": {"full_name": "marcogallotta/ai-tools"}, "pull_request": {"number": 203}}
    ingest_event(state, provider="github", payload=payload, delivery_id="newer")
    ingest_event(state, provider="github", payload=payload, delivery_id="older")
    ingest_event(state, provider="github", payload=payload, delivery_id="newer")
    calls = []
    reconciler = V4Reconciler(
        store=state,
        authoritative_cases=lambda snapshot: calls.append(snapshot) or [CASE],
        bridge=bridge(tmp_path, state, app),
    )
    result = reconciler.reconcile()
    assert result["model_turns_started"] == 1
    assert len(app.started) == 1
    assert len(calls) == 1
    assert {item["resource_kind"] for item in calls[0].resources} == {"pull_request", "repository"}
    assert state.snapshot_dirty().resources == ()
    ingest_event(state, provider="github", payload=payload, delivery_id="dup-after")
    result = reconciler.reconcile()
    assert result["model_turns_started"] == 0
    assert len(app.started) == 1


def test_failed_authoritative_refresh_retains_dirty_and_starts_zero_turns(tmp_path):
    state = store(tmp_path)
    app = FakeAppServer()
    state.mark_dirty(
        provider="asana", resource_kind="task", resource_id="1217762116932884", delivery_id="1",
    )
    reconciler = V4Reconciler(
        store=state,
        authoritative_cases=lambda snapshot: (_ for _ in ()).throw(RuntimeError("budget exhausted")),
        bridge=bridge(tmp_path, state, app),
    )
    with pytest.raises(RuntimeError, match="budget exhausted"):
        reconciler.reconcile()
    assert len(state.snapshot_dirty().resources) == 1
    assert app.started == []


def test_actionable_version_ignores_volatile_timing_but_changes_on_semantic_action():
    first = dict(CASE)
    first["evidence"] = {
        "check": "Dish / exact-head certification",
        "workflow_run_id": 700,
        "workflow_run_attempt": 1,
        "job_id": 800,
        "delivery_id": "delivery-1",
    }
    second = dict(CASE)
    second["evidence"] = {
        "check": "Dish / exact-head certification",
        "last_changed": "volatile-b",
        "retry_after": 90,
        "workflow_run_id": 701,
        "workflow_run_attempt": 2,
        "job_id": 801,
        "delivery_id": "delivery-2",
    }
    assert actionable_version(first) == actionable_version(second)
    packet = wake_packet(
        owner="Integrator",
        cases=[first],
        versions=[actionable_version(first)],
        policy_generation=POLICY_GENERATION,
    )
    assert packet["cases"][0]["evidence"]["workflow_run_attempt"] == 1
    second["next_action"] = "different material next action"
    assert actionable_version(first) != actionable_version(second)


def test_new_material_case_for_same_owner_creates_second_wake(tmp_path):
    state = store(tmp_path)
    app = FakeAppServer()
    state.mark_dirty(provider="github", resource_kind="repository", resource_id="marcogallotta/ai-tools", delivery_id="1")
    current = [dict(CASE)]
    reconciler = V4Reconciler(store=state, authoritative_cases=lambda snapshot: current, bridge=bridge(tmp_path, state, app))
    assert reconciler.reconcile()["model_turns_started"] == 1
    changed = dict(CASE)
    changed["evidence"] = {"check": "Dish / exact-head certification", "failure": "new material root cause"}
    current[:] = [changed]
    state.mark_dirty(provider="github", resource_kind="repository", resource_id="marcogallotta/ai-tools", delivery_id="2")
    assert reconciler.reconcile()["model_turns_started"] == 1
    assert len(app.started) == 2


def test_multiple_new_cases_for_one_owner_coalesce_into_one_wake(tmp_path):
    state = store(tmp_path)
    app = FakeAppServer()
    other = dict(CASE)
    other["case_key"] = "case-b"
    other["pr"] = 204
    other["head"] = "b" * 40
    state.mark_dirty(provider="github", resource_kind="repository", resource_id="marcogallotta/ai-tools", delivery_id="1")
    reconciler = V4Reconciler(store=state, authoritative_cases=lambda snapshot: [CASE, other], bridge=bridge(tmp_path, state, app))
    result = reconciler.reconcile()
    assert result["model_turns_started"] == 1
    receipt = next(iter(state.read()["receipts"].values()))
    assert len(receipt["actionable_versions"]) == 2


def test_coordinator_case_is_dormant_in_phase_b(tmp_path):
    state = store(tmp_path)
    app = FakeAppServer()
    coordinator = dict(CASE)
    coordinator["next_owner"] = "Coordinator"
    state.mark_dirty(provider="asana", resource_kind="task", resource_id="121", delivery_id="1")
    reconciler = V4Reconciler(store=state, authoritative_cases=lambda snapshot: [coordinator], bridge=bridge(tmp_path, state, app))
    result = reconciler.reconcile()
    assert result["prepared"] == 0
    assert result["model_turns_started"] == 0
    assert app.started == []


def test_human_active_thread_fence_starts_zero_turns_until_idle(tmp_path):
    state = store(tmp_path)
    app = FakeAppServer()
    app.status = "active"
    state.mark_dirty(provider="github", resource_kind="repository", resource_id="marcogallotta/ai-tools", delivery_id="1")
    reconciler = V4Reconciler(store=state, authoritative_cases=lambda snapshot: [CASE], bridge=bridge(tmp_path, state, app))
    result = reconciler.reconcile()
    assert result["model_turns_started"] == 0
    assert result["wake_results"][0]["result"] == "thread-not-idle"
    app.status = "idle"
    result = reconciler.reconcile(force=True)
    assert result["model_turns_started"] == 1


def test_acceptance_ambiguity_never_blindly_replays_and_history_recovers(tmp_path):
    state = store(tmp_path)
    app = FakeAppServer()
    app.fail_after_accept = True
    state.mark_dirty(provider="github", resource_kind="repository", resource_id="marcogallotta/ai-tools", delivery_id="1")
    wake = bridge(tmp_path, state, app)
    reconciler = V4Reconciler(store=state, authoritative_cases=lambda snapshot: [CASE], bridge=wake)
    first = reconciler.reconcile()
    assert first["model_turns_started"] == 0
    assert len(app.started) == 1
    receipt = next(iter(state.read()["receipts"].values()))
    assert receipt["status"] == "AMBIGUOUS"
    app.fail_after_accept = False
    second = reconciler.reconcile(force=True)
    assert len(app.started) == 1
    receipt = next(iter(state.read()["receipts"].values()))
    assert receipt["status"] == "ACCEPTED"
    assert second["model_turns_started"] == 0


def test_uncertain_non_acceptance_requires_history_proof_before_retry(tmp_path):
    state = store(tmp_path)

    class LostBeforeAccept(FakeAppServer):
        def turn_start(self, thread_id, packet, *, client_user_message_id):
            self.started.append(client_user_message_id)
            raise ConnectionError("lost before server acceptance")

    app = LostBeforeAccept()
    state.mark_dirty(provider="github", resource_kind="repository", resource_id="marcogallotta/ai-tools", delivery_id="1")
    reconciler = V4Reconciler(store=state, authoritative_cases=lambda snapshot: [CASE], bridge=bridge(tmp_path, state, app))
    reconciler.reconcile()
    assert len(app.started) == 1
    result = reconciler.reconcile(force=True)
    assert len(app.started) == 2
    assert result["wake_results"][0]["result"] == "not-accepted-proved"


def test_compare_and_clear_does_not_drop_event_arriving_during_reconcile(tmp_path):
    state = store(tmp_path)
    state.mark_dirty(provider="github", resource_kind="repository", resource_id="marcogallotta/ai-tools", delivery_id="1")
    snap = state.snapshot_dirty()
    state.mark_dirty(provider="github", resource_kind="repository", resource_id="marcogallotta/ai-tools", delivery_id="2")
    state.compare_and_clear(snap)
    remaining = state.snapshot_dirty().resources
    assert len(remaining) == 1
    assert remaining[0]["delivery_ids"] == ["1", "2"]


def test_ambiguous_no_marker_and_active_status_never_replays(tmp_path):
    state = store(tmp_path)

    class LostBeforeAccept(FakeAppServer):
        def turn_start(self, thread_id, packet, *, client_user_message_id):
            self.started.append(client_user_message_id)
            raise ConnectionError("lost before server acceptance")

    app = LostBeforeAccept()
    state.mark_dirty(provider="github", resource_kind="repository", resource_id="marcogallotta/ai-tools", delivery_id="1")
    wake = bridge(tmp_path, state, app)
    reconciler = V4Reconciler(store=state, authoritative_cases=lambda snapshot: [CASE], bridge=wake)
    first = reconciler.reconcile()
    assert first["model_turns_started"] == 0
    assert len(app.started) == 1
    receipt = next(iter(state.read()["receipts"].values()))
    assert receipt["status"] == "AMBIGUOUS"

    # No marker exists in history, but the thread is active/unknown (not idle):
    # this must NOT be treated as mechanical proof of non-acceptance, and the
    # receipt must stay AMBIGUOUS with zero replay.
    app.status = "active"
    second = reconciler.reconcile(force=True)
    assert len(app.started) == 1
    receipt = next(iter(state.read()["receipts"].values()))
    assert receipt["status"] == "AMBIGUOUS"
    assert second["wake_results"][0]["result"] == "still-ambiguous"
    assert second["model_turns_started"] == 0


def test_not_loaded_thread_is_resumed_before_admission(tmp_path):
    state = store(tmp_path)
    app = FakeAppServer()
    app.status = "notLoaded"
    state.mark_dirty(provider="github", resource_kind="repository", resource_id="marcogallotta/ai-tools", delivery_id="1")
    reconciler = V4Reconciler(store=state, authoritative_cases=lambda snapshot: [CASE], bridge=bridge(tmp_path, state, app))
    result = reconciler.reconcile()
    assert app.resumed == ["lifecycle-thread"]
    assert result["model_turns_started"] == 1
    assert len(app.started) == 1


def test_accepted_turn_completion_is_recorded_and_recovers_after_restart(tmp_path):
    state = store(tmp_path)
    app = FakeAppServer()
    state.mark_dirty(provider="github", resource_kind="repository", resource_id="marcogallotta/ai-tools", delivery_id="1")
    reconciler = V4Reconciler(store=state, authoritative_cases=lambda snapshot: [CASE], bridge=bridge(tmp_path, state, app))
    result = reconciler.reconcile()
    assert result["model_turns_started"] == 1
    receipt = next(iter(state.read()["receipts"].values()))
    wake_id = receipt["wake_id"]
    assert receipt["status"] == "ACCEPTED"

    # Turn still in flight: dispatch must not fabricate completion.
    state.mark_dirty(provider="github", resource_kind="repository", resource_id="marcogallotta/ai-tools", delivery_id="2")
    reconciler.reconcile()
    assert state.read()["receipts"][wake_id]["status"] == "ACCEPTED"

    # Terminal turn state now persisted in thread history; recovery must
    # reconstruct COMPLETED mechanically rather than requiring the process
    # that started the turn to still be alive.
    app.turn_status[wake_id] = "completed"
    state.mark_dirty(provider="github", resource_kind="repository", resource_id="marcogallotta/ai-tools", delivery_id="3")
    reconciler.reconcile()
    assert state.read()["receipts"][wake_id]["status"] == "COMPLETED"

    # A brand-new store/bridge over the same durable state (crash/restart)
    # must recover the terminal COMPLETED state, distinguishing it from
    # still-in-flight ACCEPTED, purely from persisted receipts/history.
    restarted = V4Reconciler(
        store=state, authoritative_cases=lambda snapshot: [CASE], bridge=bridge(tmp_path, state, app)
    )
    restarted.reconcile(force=True)
    assert state.read()["receipts"][wake_id]["status"] == "COMPLETED"


def test_wake_packet_includes_required_provenance_and_excludes_volatile_evidence():
    v3_case = dict(CASE)
    v3_case["evidence_fingerprint"] = "fp-" + "c" * 12
    v3_case["reviewed_head"] = "d" * 40
    v3_case["review_verdict"] = "BLOCK"
    version = actionable_version(v3_case)
    packet = wake_packet(owner="Integrator", cases=[v3_case], versions=[version], policy_generation=POLICY_GENERATION)
    case = packet["cases"][0]
    assert case["evidence_fingerprint"] == v3_case["evidence_fingerprint"]
    assert case["reviewed_head"] == v3_case["reviewed_head"]
    assert case["review_verdict"] == v3_case["review_verdict"]
    assert case["evidence"] == {"check": "Dish / exact-head certification"}


def test_webhook_signature_verification_is_fail_closed():
    body = b'{"x":1}'
    secret = "secret"
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_github_signature(body, f"sha256={digest}", secret)
    assert verify_asana_signature(body, digest, secret)
    assert not verify_github_signature(body + b"x", f"sha256={digest}", secret)
    assert not verify_asana_signature(body + b"x", digest, secret)
