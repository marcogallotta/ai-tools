from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from pr_lifecycle_v4 import (
    V4Reconciler,
    V4StateStore,
    WakeBridge,
    actionable_version,
    ingest_event,
    verify_asana_signature,
    verify_github_signature,
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

    def thread_read(self, thread_id, *, include_turns):
        turns = []
        if include_turns:
            turns = [
                {"items": [{"type": "userMessage", "clientId": value}]}
                for value in self.history
            ]
        return {"thread": {"id": thread_id, "status": {"type": self.status}, "turns": turns}}

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
        authoritative_cases=lambda: calls.append(True) or [CASE],
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
        authoritative_cases=lambda: calls.append(True) or [CASE],
        bridge=bridge(tmp_path, state, app),
    )
    result = reconciler.reconcile()
    assert result["model_turns_started"] == 1
    assert len(app.started) == 1
    assert len(calls) == 1
    assert state.snapshot_dirty().resources == ()
    ingest_event(state, provider="github", payload=payload, delivery_id="dup-after")
    result = reconciler.reconcile()
    assert result["model_turns_started"] == 0
    assert len(app.started) == 1


def test_actionable_version_ignores_volatile_timing_but_changes_on_semantic_action():
    first = dict(CASE)
    second = dict(CASE)
    second["evidence"] = {"check": "Dish / exact-head certification", "last_changed": "volatile-b", "retry_after": 90}
    assert actionable_version(first) == actionable_version(second)
    second["next_action"] = "different material next action"
    assert actionable_version(first) != actionable_version(second)


def test_new_material_case_for_same_owner_creates_second_wake(tmp_path):
    state = store(tmp_path)
    app = FakeAppServer()
    state.mark_dirty(provider="github", resource_kind="repository", resource_id="marcogallotta/ai-tools", delivery_id="1")
    current = [dict(CASE)]
    reconciler = V4Reconciler(store=state, authoritative_cases=lambda: current, bridge=bridge(tmp_path, state, app))
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
    reconciler = V4Reconciler(store=state, authoritative_cases=lambda: [CASE, other], bridge=bridge(tmp_path, state, app))
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
    reconciler = V4Reconciler(store=state, authoritative_cases=lambda: [coordinator], bridge=bridge(tmp_path, state, app))
    result = reconciler.reconcile()
    assert result["prepared"] == 0
    assert result["model_turns_started"] == 0
    assert app.started == []


def test_human_active_thread_fence_starts_zero_turns_until_idle(tmp_path):
    state = store(tmp_path)
    app = FakeAppServer()
    app.status = "active"
    state.mark_dirty(provider="github", resource_kind="repository", resource_id="marcogallotta/ai-tools", delivery_id="1")
    reconciler = V4Reconciler(store=state, authoritative_cases=lambda: [CASE], bridge=bridge(tmp_path, state, app))
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
    reconciler = V4Reconciler(store=state, authoritative_cases=lambda: [CASE], bridge=wake)
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
    reconciler = V4Reconciler(store=state, authoritative_cases=lambda: [CASE], bridge=bridge(tmp_path, state, app))
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


def test_webhook_signature_verification_is_fail_closed():
    body = b'{"x":1}'
    secret = "secret"
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_github_signature(body, f"sha256={digest}", secret)
    assert verify_asana_signature(body, digest, secret)
    assert not verify_github_signature(body + b"x", f"sha256={digest}", secret)
    assert not verify_asana_signature(body + b"x", digest, secret)
