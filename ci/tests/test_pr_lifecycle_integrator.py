from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ci_failure_fingerprint import causal_fingerprint
from pr_lifecycle_integrator import IntegratorAudit, consume_projection, model_outcome_for_wake
from pr_lifecycle_v4 import actionable_version


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
    assert case == original_case
    canonical = consumed.report["decisions"][0]["canonical_ci"]
    assert canonical["classification"] == "AMBIGUOUS"
    assert canonical["causal_fingerprint"] == FINGERPRINT
    assert canonical["workflow_run_id"] == 700
    decision = consumed.report["decisions"][0]
    assert decision["actionable_version"] == original_version
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
