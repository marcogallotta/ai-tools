from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from pr_lifecycle_coordinator import DUTIES, audit_record, consume_projection
from pr_lifecycle_v4 import actionable_version


REPOSITORY = "marcogallotta/ai-tools"


def projection(*, tasks=None, cases=None, scope_status="COMPLETE", noon=None):
    value = {
        "repository": REPOSITORY,
        "task_scope": {"status": scope_status, "projects": ["project-1"]},
        "tasks": list(tasks or []),
        "v3": {"attention": {"cases": list(cases or [])}},
    }
    if noon is not None:
        value["coordinator_duty_evidence"] = {"coordinator.noon-hygiene": noon}
    return value


def task(*, gid="121", priority="UNKNOWN", section=None, residuals=None):
    memberships = []
    if section:
        memberships = [{
            "project": {"gid": "project-1"},
            "section": {"gid": "section-1", "name": section},
        }]
    return {
        "gid": gid,
        "name": "Exact task",
        "completed": False,
        "modified_at": "2026-08-22T10:00:00+00:00",
        "memberships": memberships,
        "execution": {"priority": priority},
        "residuals": list(residuals or []),
    }


def test_registry_is_declarative_observe_only_and_role_scoped():
    assert set(DUTIES) == {"coordinator.hourly-frontier", "coordinator.noon-hygiene"}
    assert all(value.role == "Coordinator" for value in DUTIES.values())
    assert all(value.observe_only for value in DUTIES.values())


def test_clean_or_unavailable_hourly_scan_starts_zero_model_turns():
    clean = consume_projection(projection(), duty_id="coordinator.hourly-frontier")
    assert clean.actionable_cases == ()
    assert clean.report["counts"] == {"actionable": 0, "suppressed": 0, "model_turns_started": 0}

    unavailable = consume_projection(
        projection(tasks=[task(priority="P0")], scope_status="INCOMPLETE"),
        duty_id="coordinator.hourly-frontier",
    )
    assert unavailable.actionable_cases == ()
    assert unavailable.report["counts"]["model_turns_started"] == 0
    assert unavailable.report["decisions"][0]["reason"] == "authoritative_task_scope_unavailable"


def test_critical_task_produces_one_existing_v4_identity_stable_across_timing_noise():
    first = consume_projection(
        projection(tasks=[task(priority="P0")]),
        duty_id="coordinator.hourly-frontier",
    )
    changed = task(priority="P0")
    changed["modified_at"] = "2026-08-22T11:00:00+00:00"
    second = consume_projection(
        projection(tasks=[changed]),
        duty_id="coordinator.hourly-frontier",
    )
    assert len(first.actionable_cases) == 1
    assert first.report["counts"]["model_turns_started"] == 0
    first_version = first.report["decisions"][0]["actionable_version"]
    second_version = second.report["decisions"][0]["actionable_version"]
    assert first_version == second_version
    assert first_version == actionable_version(first.actionable_cases[0])
    assert first.report["frontier_digest_is_wake_identity"] is False


def test_existing_coordinator_case_is_consumed_without_reinventing_owner_or_action():
    case = {
        "case_key": "existing",
        "reason_class": "POST_MERGE_ACTION_REQUIRED",
        "repository": REPOSITORY,
        "pr": 236,
        "task": "task-236",
        "head": "a" * 40,
        "evidence": {"post_merge_gates": ["natural commissioning"]},
        "next_owner": "Coordinator",
        "next_action": "schedule/track residual post-merge acceptance work",
    }
    result = consume_projection(
        projection(cases=[case]), duty_id="coordinator.hourly-frontier"
    )
    assert result.actionable_cases == (case,)
    assert result.report["decisions"][0]["actionable_version"] == actionable_version(case)


def test_residual_requires_authoritative_provenance_owner_and_wake():
    inferred = {
        "id": "r-inferred",
        "state": "active",
        "owner": "Coordinator",
        "wake_condition": "later",
        "provenance": {"kind": "agent_inference"},
    }
    authoritative = {
        "id": "r-real",
        "state": "deferred",
        "owner": "Deployment",
        "wake_condition": "source lands",
        "provenance": {"kind": "accepted_task_design_obligation", "source": "task-1"},
    }
    cancelled = {
        **authoritative,
        "id": "r-cancelled",
        "state": "cancelled",
    }
    result = consume_projection(
        projection(tasks=[task(residuals=[inferred, authoritative, cancelled])]),
        duty_id="coordinator.hourly-frontier",
    )
    assert [case["evidence"]["residual_id"] for case in result.actionable_cases] == ["r-real"]
    assert any(
        item.get("reason") == "residual_lacks_authoritative_provenance"
        for item in result.report["decisions"]
    )


def test_noon_clean_scan_is_zero_turn_and_due_hygiene_routes_without_triaging():
    clean = consume_projection(
        projection(noon={"status": "COMPLETE", "items": []}),
        duty_id="coordinator.noon-hygiene",
    )
    assert clean.actionable_cases == ()
    assert clean.report["counts"]["model_turns_started"] == 0

    due = consume_projection(
        projection(noon={
            "status": "COMPLETE",
            "items": [{
                "kind": "development_workflow_hygiene_due",
                "source_id": "code-smells-project",
                "queue": "Code Smells / Engineering Debt",
                "due": True,
            }],
        }),
        duty_id="coordinator.noon-hygiene",
    )
    assert len(due.actionable_cases) == 1
    assert due.actionable_cases[0]["next_owner"] == "Coordinator"
    assert due.actionable_cases[0]["next_action"].startswith("route the due semantic triage")
    assert due.report["counts"]["model_turns_started"] == 0


def test_noon_missing_authority_is_silent_and_unknown_duty_fails_closed():
    missing = consume_projection(projection(), duty_id="coordinator.noon-hygiene")
    assert missing.actionable_cases == ()
    assert missing.report["decisions"][0]["reason"] == "authoritative_noon_evidence_unavailable"

    try:
        consume_projection(projection(), duty_id="coordinator.never")
    except ValueError as exc:
        assert "unknown Coordinator duty" in str(exc)
    else:
        raise AssertionError("unknown duty must fail closed")


def test_report_replays_into_bounded_existing_v4_audit_record():
    result = consume_projection(
        projection(tasks=[task(priority="P0")]),
        duty_id="coordinator.hourly-frontier",
    )
    record = audit_record(result.report, source_id="asana:project-1", correlation_id="delivery-1")
    assert record["schema"] == "dish-coordinator-audit-v1"
    assert record["duty_id"] == "coordinator.hourly-frontier"
    assert record["counts"]["actionable"] == 1
    assert record["model_turns_started"] == 0
