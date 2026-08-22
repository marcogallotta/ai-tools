from __future__ import annotations

import json
from datetime import datetime, timezone
import threading
from pathlib import Path
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ci_failure_fingerprint import causal_fingerprint
from pr_lifecycle_integrator import IntegratorAudit, consume_projection, model_outcome_for_wake
from pr_lifecycle_v3 import build_v3_projection
from pr_lifecycle_v4 import DirtySnapshot, V4StateStore, actionable_version
import pr_lifecycle_v4_service as v4_service


FINGERPRINT, IDENTITY = causal_fingerprint(
    owner_surface="python-control-plane",
    failure_surface="pytest",
    invariant="tests/test_policy.py::test_owner",
    signature="test_failure",
)


def projection(*, classification="AMBIGUOUS", active=True, gate_extra=None):
    gate = {
        "diagnosis": "FAILED_REQUIRED_CI",
        "raw_gate_outcome": "FAILED",
        "failure_ownership": classification,
        "failure_ownership_evidence": "exact workflow evidence",
        "candidate_disposition": "BLOCKING",
        "failure_causal_fingerprint": FINGERPRINT,
        "failure_causal_identity": IDENTITY,
        "required_check": "Dish / exact-head certification",
        "required_workflow_run_id": 700,
        "required_workflow_run_attempt": 1,
    }
    gate.update(gate_extra or {})
    case = {
        "case_key": "case-ci-31",
        "reason_class": "CI_OWNERSHIP_AMBIGUOUS",
        "repository": "marcogallotta/ai-tools",
        "pr": 31,
        "task": "1217735567499994",
        "head": "a" * 40,
        "evidence": {"failure_ownership": classification},
        "next_owner": "Integrator",
        "next_action": "diagnose CI ownership; no semantic mutation until ownership is proven",
    }
    return {
        "pull_requests": [{"number": 31, "gate": gate}],
        "v3": {"integrator": {"active_cases": [case] if active else []}},
    }


def test_adapter_consumes_canonical_ci_output_inside_the_existing_v4_identity():
    source = projection()
    original_case = source["v3"]["integrator"]["active_cases"][0]
    original_version = actionable_version(original_case)
    consumed = consume_projection(source)
    assert len(consumed.actionable_cases) == 1
    case = consumed.actionable_cases[0]
    assert case["evidence"]["canonical_ci"]["causal_fingerprint"] == FINGERPRINT
    canonical = consumed.report["decisions"][0]["canonical_ci"]
    assert canonical["classification"] == "AMBIGUOUS"
    assert canonical["causal_fingerprint"] == FINGERPRINT
    assert canonical["workflow_run_id"] == 700
    decision = consumed.report["decisions"][0]
    assert decision["actionable_version"] != original_version
    assert decision["actionable_version"] == actionable_version(case)
    assert consumed.report["wake_identity"] == "Lifecycle V4 actionable_version"


def test_missing_or_contradictory_canonical_ci_state_is_zero_turn_suppressed():
    missing = projection()
    missing["pull_requests"] = []
    result = consume_projection(missing)
    assert result.actionable_cases == ()
    assert result.report["counts"] == {"actionable": 0, "suppressed": 1}
    assert result.report["decisions"][0]["reason"] == "authoritative_ci_projection_unavailable"

    contradiction = projection()
    contradiction["v3"]["integrator"]["active_cases"][0]["evidence"]["failure_ownership"] = "INFRASTRUCTURE"
    result = consume_projection(contradiction)
    assert result.actionable_cases == ()
    assert result.report["decisions"][0]["reason"] == "canonical_ci_projection_contradiction"


def test_active_canonical_repair_owner_is_reported_as_zero_turn_suppression():
    value = projection(
        classification="LIKELY_NON_PR_OWNED",
        active=False,
        gate_extra={
            "candidate_disposition": "NON_BLOCKING_LIKELY_UNRELATED",
            "repair_owner_active": True,
            "repair_owner_task": "1217449623846547",
        },
    )
    result = consume_projection(value)
    assert result.actionable_cases == ()
    assert result.report["counts"] == {"actionable": 0, "suppressed": 1}
    decision = result.report["decisions"][0]
    assert decision["reason"] == "canonical_ci_owner_already_active"
    assert decision["causal_fingerprint"] == FINGERPRINT


def test_non_ci_integrator_case_is_zero_turn_suppressed():
    value = projection()
    value["v3"]["integrator"]["active_cases"][0]["reason_class"] = "MERGE_CONFLICT_OR_BASE_RECONCILIATION_REQUIRED"
    result = consume_projection(value)
    assert result.actionable_cases == ()
    assert result.report["decisions"][0]["reason"] == "outside_ci_reliability_scope"


def test_scheduled_ci_case_consumes_embedded_canonical_output_without_a_pr():
    value = projection()
    case = value["v3"]["integrator"]["active_cases"][0]
    case["pr"] = None
    gate = value["pull_requests"][0]["gate"]
    case["evidence"] = {
        "failure_ownership": "AMBIGUOUS",
        "canonical_ci": gate,
        "full_regression_run_id": "nightly-700",
    }
    value["pull_requests"] = []
    result = consume_projection(value)
    assert len(result.actionable_cases) == 1
    assert result.report["decisions"][0]["canonical_ci"]["causal_fingerprint"] == FINGERPRINT


def test_semantic_ci_changes_wake_but_occurrence_only_retry_reuses_v4_identity(tmp_path):
    first = consume_projection(projection()).actionable_cases[0]
    retry = consume_projection(projection(
        gate_extra={"required_workflow_run_id": 700, "required_workflow_run_attempt": 2}
    )).actionable_cases[0]
    changed_fingerprint = projection(
        gate_extra={"failure_causal_fingerprint": "ci-cause-v1:" + "e" * 32}
    )
    second = consume_projection(changed_fingerprint).actionable_cases[0]
    changed_owner = projection(
        gate_extra={"repair_owner_task": "1217449623846547", "repair_owner_active": True}
    )
    third = consume_projection(changed_owner).actionable_cases[0]
    assert actionable_version(first) != actionable_version(second)
    assert actionable_version(first) != actionable_version(third)
    assert actionable_version(first) == actionable_version(retry)
    assert actionable_version(first) == actionable_version(
        consume_projection(projection()).actionable_cases[0]
    )
    store = V4StateStore(tmp_path / "state.json")
    assert len(store.prepare_wakes([first])) == 1
    assert store.prepare_wakes([retry]) == []


def test_nightly_clean_failure_and_replay_use_the_existing_v4_ledger(tmp_path):
    now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    clean = {
        "evidence": {
            "schema": "dish-full-regression-v1",
            "overall_result": "passed",
            "run_id": "700",
            "run_attempt": 1,
            "main_sha": "b" * 40,
            "event": "schedule",
            "failures": [],
        }
    }
    clean_v3 = build_v3_projection(
        [], tasks=[], source_observation={}, repository="marcogallotta/ai-tools",
        controller={}, full_regression=clean, generated_at=now,
    )
    assert consume_projection({"pull_requests": [], "v3": clean_v3}).actionable_cases == ()

    failed = json.loads(json.dumps(clean))
    failed["evidence"]["overall_result"] = "failed"
    failed["evidence"]["failures"] = [{
        "failure_id": "lane:python-control-plane:ci-tests:fixture",
        "component": "python-control-plane",
        "invariant": "ci/tests fixture",
        "failure_kind": "test_failure",
        "causal_fingerprint": FINGERPRINT,
        "causal_identity": IDENTITY,
    }]
    failed_v3 = build_v3_projection(
        [], tasks=[], source_observation={}, repository="marcogallotta/ai-tools",
        controller={}, full_regression=failed, generated_at=now,
    )
    cases = consume_projection({"pull_requests": [], "v3": failed_v3}).actionable_cases
    assert len(cases) == 1
    assert cases[0]["reason_class"] == "NIGHTLY_CI_UNRESOLVED"
    store = V4StateStore(tmp_path / "state.json")
    assert len(store.prepare_wakes(cases)) == 1
    assert store.prepare_wakes(cases) == []


def test_audit_publishes_latest_report_and_rotates_ndjson(tmp_path):
    audit = IntegratorAudit(
        tmp_path / "audit.ndjson",
        report_path=tmp_path / "report.json",
        max_bytes=64_000,
    )
    report = consume_projection(projection()).report
    audit.publish_report(report)
    assert json.loads((tmp_path / "report.json").read_text())["schema"] == report["schema"]
    first = json.loads((tmp_path / "audit.ndjson").read_text().splitlines()[0])
    assert first["event"] == "projection_consumed"
    assert first["model_turns_started"] == 0

    for _ in range(80):
        audit.write("fixture", detail="x" * 1000)
    assert (tmp_path / "audit.ndjson.1").exists()


def test_completed_model_outcome_is_reconstructed_from_persisted_turn():
    value = model_outcome_for_wake(
        {
            "thread": {
                "turns": [{
                    "id": "turn-1",
                    "items": [
                        {"type": "userMessage", "clientId": "wake-1"},
                        {"type": "agentMessage", "phase": "final_answer", "text": '{"classification":"AMBIGUOUS"}'},
                    ],
                }]
            }
        },
        "wake-1",
    )
    assert value == {
        "valid": True,
        "proposal": {"classification": "AMBIGUOUS"},
        "turn_id": "turn-1",
    }


def test_service_bootstrap_and_dirty_task_are_forwarded_to_projection(monkeypatch):
    commands = []
    monkeypatch.setattr(v4_service.subprocess, "run", lambda command, **kwargs: (
        commands.append(command) or SimpleNamespace(returncode=0, stdout="", stderr="")
    ))
    monkeypatch.setattr(v4_service, "read_projection", lambda path: {})
    monkeypatch.setattr(v4_service, "consume_projection", lambda value: SimpleNamespace(
        report={}, actionable_cases=(),
    ))
    runtime = object.__new__(v4_service.Runtime)
    runtime.projection_ready = threading.Event()
    runtime.projection_bootstrap_pending = True
    runtime.projection_error = "pending"
    runtime.audit = SimpleNamespace(publish_report=lambda report: None)
    snapshot = DirtySnapshot(token=1, resources=({
        "provider": "asana", "resource_kind": "task", "resource_id": "1217762116932884",
    },))

    assert runtime.authoritative_cases(snapshot) == []
    command = commands[0]
    assert "--projection-bootstrap" in command
    assert command[command.index("--refresh-task-gid") + 1] == "1217762116932884"
    assert runtime.projection_ready.is_set()
    assert runtime.projection_bootstrap_pending is False


def test_completion_notification_records_outcome_without_another_reconcile(monkeypatch):
    calls = {"completed": 0, "outcome": 0}
    class Observer:
        def __init__(self, *args, **kwargs):
            self.reads = 0
        def thread_read(self, thread_id, *, include_turns):
            self.reads += 1
            status = "inProgress" if self.reads == 1 else "completed"
            return {"thread": {"turns": [{"id": "turn-1", "status": status}]}}
        def wait_for_turn_completed(self, turn_id, *, timeout_seconds):
            return {"id": turn_id, "status": "completed"}
        def close(self):
            pass
    class Store:
        def read(self):
            status = "COMPLETED" if calls["completed"] else "ACCEPTED"
            return {"receipts": {"wake-1": {"status": status, "turn_id": "turn-1"}}}
        def mark_completed(self, wake_id):
            calls["completed"] += 1
    class Audit:
        def __init__(self):
            self.values = []
        def write(self, event, **values):
            self.values.append((event, values))
    monkeypatch.setattr(v4_service, "CodexDaemonAppServer", Observer)
    runtime = object.__new__(v4_service.Runtime)
    runtime.thread_id = "thread-1"
    runtime.store = Store()
    runtime.audit = Audit()
    runtime.reconcile_lock = threading.Lock()
    runtime.completion_lock = threading.Lock()
    runtime.completion_observers = {"wake-1"}
    runtime.record_completed_model_outcomes = lambda **kwargs: calls.__setitem__("outcome", calls["outcome"] + 1)
    runtime._observe_completion("wake-1", "turn-1")
    assert calls == {"completed": 1, "outcome": 1}
    assert runtime.audit.values[-1][0] == "model_completion_observed"
    assert runtime.completion_observers == set()
