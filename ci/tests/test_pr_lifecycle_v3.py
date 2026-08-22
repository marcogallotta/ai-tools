from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pr_lifecycle_controller as controller
from pr_lifecycle_projection import build_projection
from pr_lifecycle_support import LifecycleState, PRLifecycle, STATE_LABELS
from pr_lifecycle_task_state import (
    SOURCE_LANDING_HOLD_MARKER,
    execution_truth,
    source_landing_hold,
    structured_story,
)
from pr_lifecycle_v4 import actionable_version


NOW = datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc)
HEAD = "a" * 40
TASK = "1217611794618560"
REPOSITORY = "marcogallotta/ai-tools"


def story(text: str, *, minutes_ago: int = 0, gid: str = "100") -> dict[str, str]:
    return {
        "gid": gid,
        "created_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "text": text,
    }


def hold_marker(action: str, *, hold_id: str = "hold-00000001", decision: str) -> str:
    return structured_story(
        SOURCE_LANDING_HOLD_MARKER,
        {
            "action": action,
            "hold_id": hold_id,
            "decision": decision,
            "authority": "marco",
        },
    )


def lifecycle(
    *,
    state: LifecycleState = LifecycleState.INTEGRATION_READY,
    gate: dict[str, object] | None = None,
    post_merge_gates: list[str] | None = None,
) -> PRLifecycle:
    return PRLifecycle(
        number=179,
        url="https://github.com/marcogallotta/ai-tools/pull/179",
        title="Lifecycle V3",
        head=HEAD,
        branch="agent/lifecycle-v3",
        base="main",
        draft=False,
        state=state,
        state_label=STATE_LABELS[state],
        task_ids=[TASK],
        review_verdict="MERGE",
        reviewed_head=HEAD,
        gate=gate or {"diagnosis": "READY"},
        post_merge_gates=list(post_merge_gates or []),
    )


def source() -> dict[str, object]:
    return {
        "status": "COMPLETE",
        "pull_requests": {
            "179": {
                "state": "NOT_LANDED",
                "ultimate_target": "main",
                "publication_state": "open",
                "provenance": "fixture",
            }
        },
        "workstreams": [],
    }


def task_with_hold(hold: dict[str, object], *, completed: bool = False) -> dict[str, object]:
    return {
        "gid": TASK,
        "completed": completed,
        "execution": {
            "state": "NO DURABLE EXECUTION EVIDENCE",
            "stale": False,
            "stale_kind": None,
            "timestamp": None,
            "source_landing_hold": hold,
        },
    }


def clear_hold() -> dict[str, object]:
    return {
        "state": "CLEAR",
        "reason": "no explicit durable human source-landing hold exists",
        "active_hold_id": None,
    }


def test_explicit_human_hold_and_release_are_append_only_and_exact():
    held = source_landing_hold([
        story(hold_marker("hold", decision="decision-hold-001")),
    ])
    assert held["state"] == "HELD"
    assert held["active_hold_id"] == "hold-00000001"

    released = source_landing_hold([
        story(hold_marker("hold", decision="decision-hold-001"), minutes_ago=2, gid="100"),
        story(hold_marker("release", decision="decision-release-001"), minutes_ago=1, gid="101"),
    ])
    assert released["state"] == "CLEAR"
    assert released["release_decision"] == "decision-release-001"


def test_same_timestamp_hold_lineage_uses_numeric_story_order():
    same = NOW.isoformat()
    held = hold_marker("hold", decision="decision-hold-001")
    released = hold_marker("release", decision="decision-release-001")
    value = source_landing_hold([
        {"gid": "10", "created_at": same, "text": released},
        {"gid": "9", "created_at": same, "text": held},
    ])
    assert value["state"] == "CLEAR"
    assert value["release_decision"] == "decision-release-001"


def test_agent_footer_or_mismatched_release_cannot_become_human_hold_authority():
    agent_like = (
        hold_marker("hold", decision="decision-hold-001")
        + "\n— Dish Agent: Coordinator | repository control plane"
    )
    assert source_landing_hold([story(agent_like)])["state"] == "CONTRADICTION"

    mismatch = source_landing_hold([
        story(hold_marker("hold", decision="decision-hold-001"), minutes_ago=2, gid="100"),
        story(
            hold_marker(
                "release",
                hold_id="different-hold",
                decision="decision-release-001",
            ),
            minutes_ago=1,
            gid="101",
        ),
    ])
    assert mismatch["state"] == "CONTRADICTION"


def test_dispatch_invoked_without_acceptance_becomes_typed_attention_after_60_minutes():
    value = execution_truth(
        {"gid": TASK},
        [story("DISPATCH INVOKED — exact attempt requested", minutes_ago=61)],
        now=NOW,
    )
    assert value["state"] == "DISPATCH STALE — ACCEPTANCE NOT PROVEN"
    assert value["stale"] is True
    assert value["stale_kind"] == "WORKER_ACCEPTANCE_STALE"
    assert value["source_landing_hold"]["state"] == "CLEAR"


def test_inactive_executor_reuses_existing_integration_boundary_without_writer_authority():
    payload = build_projection(
        [lifecycle()],
        repository=REPOSITORY,
        tasks=[task_with_hold(clear_hold())],
        source_observation=source(),
        controller={"status": "running", "integrator_provider": "claude"},
        generated_at=NOW,
    )
    v3 = payload["v3"]
    decision = v3["executor"]["decisions"][0]

    assert v3["mode"] == "SHADOW_INACTIVE"
    assert v3["activation_authorized"] is False
    assert v3["write_authority"] is False
    assert v3["writer"] == {
        "active": "integration-v1a-local-fenced",
        "candidate": "v3-deterministic",
        "candidate_enabled": False,
        "single_writer": True,
        "cutover_authorized": False,
        "rollback": "explicit-reconcile-then-switch-writer",
    }
    assert decision["decision"] == "WOULD_EXECUTE_EXISTING_INTEGRATION"
    assert decision["admission_basis"] == "existing-integration-ready-state"
    assert decision["execution_adapter"] == "integration-v1a-local-fenced"
    assert decision["mutation_permitted"] is False


def test_active_human_hold_is_the_only_asana_side_v3_landing_veto():
    active = {
        "state": "HELD",
        "reason": "explicit durable human source-landing hold is active",
        "active_hold_id": "hold-00000001",
    }
    payload = build_projection(
        [lifecycle()],
        repository=REPOSITORY,
        tasks=[task_with_hold(active, completed=True)],
        source_observation=source(),
        generated_at=NOW,
    )

    # Generic Asana completion contradiction still exists in the normalized
    # projection, but V3's landing decision is controlled only by explicit hold.
    assert payload["resolved_lifecycle"][0]["truth"] == "CONTRADICTION"
    decision = payload["v3"]["executor"]["decisions"][0]
    assert decision["decision"] == "BLOCK_HUMAN_HOLD"
    assert decision["human_hold"]["state"] == "HELD"
    assert decision["mutation_permitted"] is False


def test_generic_asana_completion_contradiction_does_not_become_v3_landing_veto():
    payload = build_projection(
        [lifecycle()],
        repository=REPOSITORY,
        tasks=[task_with_hold(clear_hold(), completed=True)],
        source_observation=source(),
        generated_at=NOW,
    )
    assert payload["resolved_lifecycle"][0]["truth"] == "CONTRADICTION"
    assert payload["v3"]["executor"]["decisions"][0]["decision"] == "WOULD_EXECUTE_EXISTING_INTEGRATION"


def test_slow_ci_emits_one_deduped_integrator_case_without_changing_authority():
    gate = {
        "diagnosis": "PENDING",
        "required_workflow_run_started_at": (NOW - timedelta(minutes=31)).isoformat(),
        "required_status_context": "Dish / exact-head certification",
    }
    payload = build_projection(
        [lifecycle(state=LifecycleState.WAITING_CI, gate=gate)],
        repository=REPOSITORY,
        tasks=[task_with_hold(clear_hold())],
        source_observation=source(),
        controller={"status": "running", "integrator_provider": "codex"},
        generated_at=NOW,
    )
    cases = payload["v3"]["attention"]["cases"]
    assert [case["reason_class"] for case in cases] == ["CI_SLOW"]
    assert payload["v3"]["integrator"]["active_cases"][0]["case_key"] == cases[0]["case_key"]
    assert payload["v3"]["integrator"]["provider"] == "codex"
    assert payload["v3"]["integrator"]["integration_authority"] is False


def test_completed_task_stale_execution_history_is_not_active_attention():
    completed = task_with_hold(clear_hold(), completed=True)
    completed["execution"].update({
        "state": "DISPATCH STALE — ACCEPTANCE NOT PROVEN",
        "stale": True,
        "stale_kind": "WORKER_ACCEPTANCE_STALE",
        "timestamp": (NOW - timedelta(hours=2)).isoformat(),
    })

    payload = build_projection(
        [],
        repository=REPOSITORY,
        tasks=[completed],
        source_observation={"status": "COMPLETE", "pull_requests": {}, "workstreams": []},
        generated_at=NOW,
    )

    assert payload["v3"]["attention"]["cases"] == []


def test_new_accepted_attempt_gets_a_new_stale_wake_identity():
    def stale_task(attempt_id):
        value = task_with_hold(clear_hold())
        value["execution"].update({
            "state": "EXECUTION STALE — FRESH ATTEMPT-BOUND EVIDENCE REQUIRED",
            "stale": True,
            "stale_kind": "WORKER_EXECUTION_STALE",
            "timestamp": (NOW - timedelta(days=2)).isoformat(),
            "attempt_id": attempt_id,
        })
        return value

    def case(attempt_id):
        payload = build_projection(
            [], repository=REPOSITORY, tasks=[stale_task(attempt_id)],
            source_observation={"status": "COMPLETE", "pull_requests": {}, "workstreams": []},
            generated_at=NOW,
        )
        return payload["v3"]["attention"]["cases"][0]

    first = case("attempt-a")
    second = case("attempt-b")
    assert first["evidence"]["accepted_attempt_id"] == "attempt-a"
    assert actionable_version(first) != actionable_version(second)


def test_repeated_ci_pattern_emits_one_systemic_coordinator_finding():
    gate = {
        "diagnosis": "PENDING",
        "required_workflow_run_started_at": (NOW - timedelta(minutes=31)).isoformat(),
        "required_status_context": "Dish / exact-head certification",
    }
    first = lifecycle(state=LifecycleState.WAITING_CI, gate=gate)
    second = lifecycle(state=LifecycleState.WAITING_CI, gate=gate)
    second.number = 180
    second.url = "https://github.com/marcogallotta/ai-tools/pull/180"
    second.head = "b" * 40
    second.reviewed_head = second.head
    payload = build_projection(
        [first, second],
        repository=REPOSITORY,
        tasks=[task_with_hold(clear_hold())],
        source_observation={
            "status": "COMPLETE",
            "pull_requests": {
                "179": source()["pull_requests"]["179"],
                "180": {
                    "state": "NOT_LANDED",
                    "ultimate_target": "main",
                    "publication_state": "open",
                    "provenance": "fixture",
                },
            },
            "workstreams": [],
        },
        generated_at=NOW,
    )
    systemic = [
        case for case in payload["v3"]["attention"]["cases"]
        if case["reason_class"] == "RECURRING_BUILD_HEALTH_PATTERN"
    ]
    assert len(systemic) == 1
    assert systemic[0]["next_owner"] == "Coordinator"
    assert systemic[0]["evidence"]["affected_prs"] == [179, 180]
    assert systemic[0] in payload["v3"]["integrator"]["active_cases"]


def test_ci_ownership_routes_without_giving_integrator_scheduler_or_implementation_authority():
    pr_owned_gate = {
        "diagnosis": "FAILED_REQUIRED_CI",
        "failure_ownership": "PR_OWNED",
        "failure_ownership_evidence": "exact fixture",
    }
    ambiguous_gate = {
        "diagnosis": "FAILED_REQUIRED_CI",
        "failure_ownership": "AMBIGUOUS",
        "failure_ownership_evidence": "no exact owner",
    }
    second = lifecycle(state=LifecycleState.REVIEW_PASSED, gate=ambiguous_gate)
    second.number = 180
    second.url = "https://github.com/marcogallotta/ai-tools/pull/180"
    second.head = "b" * 40
    payload = build_projection(
        [
            lifecycle(state=LifecycleState.CHANGES_REQUESTED, gate=pr_owned_gate),
            second,
        ],
        repository=REPOSITORY,
        tasks=[task_with_hold(clear_hold())],
        source_observation={
            "status": "COMPLETE",
            "pull_requests": {
                "179": source()["pull_requests"]["179"],
                "180": {
                    "state": "NOT_LANDED",
                    "ultimate_target": "main",
                    "publication_state": "open",
                    "provenance": "fixture",
                },
            },
            "workstreams": [],
        },
        generated_at=NOW,
    )
    cases = {case["reason_class"]: case for case in payload["v3"]["attention"]["cases"]}
    assert cases["CI_RED_PR_OWNED"]["next_owner"] == "Implementation"
    assert cases["CI_OWNERSHIP_AMBIGUOUS"]["next_owner"] == "Integrator"
    integrator = payload["v3"]["integrator"]
    assert integrator["scheduler_authority"] is False
    assert integrator["semantic_implementation_authority"] is False
    assert integrator["review_authority"] is False


def test_settled_nonblocking_failure_with_active_repair_owner_creates_no_attention_case():
    gate = {
        "diagnosis": "FAILED_REQUIRED_CI",
        "raw_gate_outcome": "FAILED",
        "failure_ownership": "LIKELY_NON_PR_OWNED",
        "candidate_disposition": "NON_BLOCKING_LIKELY_UNRELATED",
        "repair_owner_active": True,
        "repair_owner_task": "1217449623846547",
    }
    payload = build_projection(
        [lifecycle(state=LifecycleState.REVIEW_PASSED, gate=gate)],
        repository=REPOSITORY,
        tasks=[task_with_hold(clear_hold())],
        source_observation=source(),
        generated_at=NOW,
    )
    assert not any(
        case["pr_number"] == 179 and case["reason_class"].startswith("CI_")
        for case in payload["v3"]["attention"]["cases"]
    )


def test_handoff_repair_capability_blocker_is_owned_attention_not_marco_relay():
    value = lifecycle(state=LifecycleState.CHANGES_REQUESTED)
    value.residual_reason = (
        "handoff repair capability blocker: WorkspaceAgentDispatcher.dispatch_worker unavailable; "
        "owner: Development Workflow / orchestration; recovery proof: exact packet accepted"
    )
    payload = build_projection(
        [value],
        repository=REPOSITORY,
        tasks=[task_with_hold(clear_hold())],
        source_observation=source(),
        generated_at=NOW,
    )
    cases = [
        case for case in payload["v3"]["attention"]["cases"]
        if case["reason_class"] == "HANDOFF_REPAIR_CAPABILITY_BLOCKED"
    ]
    assert len(cases) == 1
    assert cases[0]["next_owner"] == "Development Workflow"
    assert "do not ask Marco" in cases[0]["next_action"]
    assert cases[0] not in payload["v3"]["integrator"]["active_cases"]


def test_unfinished_implementation_readback_text_does_not_route_to_integrator():
    value = lifecycle(state=LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED)
    value.draft = True
    value.review_verdict = None
    value.reviewed_head = None
    value.residual_reason = (
        "draft PR has unfinished task-scoped authoring evidence: exact-tree materialization/branch attach "
        "and authoritative final readback; no active implementation lease"
    )
    payload = build_projection(
        [value],
        repository=REPOSITORY,
        tasks=[task_with_hold(clear_hold())],
        source_observation=source(),
        generated_at=NOW,
    )
    assert all(
        case["reason_class"] != "INTEGRATION_READBACK_UNCERTAIN"
        for case in payload["v3"]["attention"]["cases"]
    )


@pytest.mark.parametrize(
    "state",
    [LifecycleState.INTEGRATION_READY, LifecycleState.MERGING, LifecycleState.WAITING_INFRASTRUCTURE],
)
def test_exact_reviewed_integration_readback_uncertainty_routes_to_integrator(state):
    value = lifecycle(state=state)
    value.residual_reason = "local Integration returned without authoritative GitHub readback"
    payload = build_projection(
        [value],
        repository=REPOSITORY,
        tasks=[task_with_hold(clear_hold())],
        source_observation=source(),
        generated_at=NOW,
    )
    cases = [
        case for case in payload["v3"]["attention"]["cases"]
        if case["reason_class"] == "INTEGRATION_READBACK_UNCERTAIN"
    ]
    assert len(cases) == 1
    assert cases[0]["next_owner"] == "Integrator"


def test_provider_switch_reconstructs_same_case_identity_from_live_evidence():
    gate = {
        "diagnosis": "INFRASTRUCTURE_ERROR",
        "reason": "network unavailable",
        "required_workflow_run_started_at": (NOW - timedelta(minutes=5)).isoformat(),
    }
    common = dict(
        values=[lifecycle(state=LifecycleState.REVIEW_PASSED, gate=gate)],
        repository=REPOSITORY,
        tasks=[task_with_hold(clear_hold())],
        source_observation=source(),
        generated_at=NOW,
    )
    claude = build_projection(**common, controller={"status": "running", "integrator_provider": "claude"})
    codex = build_projection(**common, controller={"status": "running", "integrator_provider": "codex"})

    first = claude["v3"]["integrator"]["active_cases"][0]
    second = codex["v3"]["integrator"]["active_cases"][0]
    assert first["case_key"] == second["case_key"]
    assert claude["v3"]["integrator"]["provider"] == "claude"
    assert codex["v3"]["integrator"]["provider"] == "codex"


def test_retry_jitter_is_bounded_and_first_retry_stays_deterministic():
    assert controller._retry_delay(1, sample=0.0) == 1
    assert controller._retry_delay(2, sample=0.0) == pytest.approx(1.6)
    assert controller._retry_delay(2, sample=1.0) == pytest.approx(2.4)
    assert 0.5 <= controller._retry_delay(20, sample=1.0) <= controller.MAX_BACKOFF
