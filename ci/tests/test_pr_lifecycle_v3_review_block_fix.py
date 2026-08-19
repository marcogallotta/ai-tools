from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from pr_lifecycle_owner import TaskReferences
from pr_lifecycle_projection import build_projection
from pr_lifecycle_support import LifecycleState, PRLifecycle, STATE_LABELS
from pr_lifecycle_task_state import execution_truth


NOW = datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc)
OWNER = "1217611794618560"
RELATED = "1217624821583998"
HEAD = "a" * 40
REPOSITORY = "marcogallotta/ai-tools"


def lifecycle(*, owner: str | None = OWNER, owner_error: str | None = None) -> PRLifecycle:
    task_ids = TaskReferences(
        [OWNER, RELATED],
        owning_task_id=owner,
        owning_task_error=owner_error,
    )
    return PRLifecycle(
        number=182,
        url="https://github.com/marcogallotta/ai-tools/pull/182",
        title="Lifecycle V3 review fix",
        head=HEAD,
        branch="agent/lifecycle-v3-completion",
        base="agent/lifecycle-v3",
        draft=False,
        state=LifecycleState.INTEGRATION_READY,
        state_label=STATE_LABELS[LifecycleState.INTEGRATION_READY],
        task_ids=task_ids,
        review_verdict="MERGE",
        reviewed_head=HEAD,
        gate={"diagnosis": "READY"},
    )


def task(gid: str, hold_state: str) -> dict[str, object]:
    return {
        "gid": gid,
        "completed": False,
        "execution": {
            "state": "NO DURABLE EXECUTION EVIDENCE",
            "stale": False,
            "stale_kind": None,
            "timestamp": None,
            "source_landing_hold": {
                "state": hold_state,
                "reason": f"fixture {hold_state.lower()}",
                "active_hold_id": "hold-00000001" if hold_state == "HELD" else None,
            },
        },
    }


def source() -> dict[str, object]:
    return {
        "status": "COMPLETE",
        "pull_requests": {
            "182": {
                "state": "NOT_LANDED",
                "ultimate_target": "main",
                "publication_state": "open",
                "provenance": "fixture",
            }
        },
        "workstreams": [],
    }


def projection(owner_hold: str, related_hold: str, *, owner: str | None = OWNER, owner_error: str | None = None):
    return build_projection(
        [lifecycle(owner=owner, owner_error=owner_error)],
        repository=REPOSITORY,
        tasks=[task(OWNER, owner_hold), task(RELATED, related_hold)],
        source_observation=source(),
        generated_at=NOW,
    )


def story(text: str, *, age: timedelta, gid: str) -> dict[str, str]:
    return {
        "gid": gid,
        "created_at": (NOW - age).isoformat(),
        "text": text,
    }


def test_unrelated_task_hold_or_contradiction_cannot_veto_owner_clear_landing():
    held = projection("CLEAR", "HELD")
    contradicted = projection("CLEAR", "CONTRADICTION")

    for payload in (held, contradicted):
        assert payload["pull_requests"][0]["owning_task_id"] == OWNER
        decision = payload["v3"]["executor"]["decisions"][0]
        assert decision["decision"] == "WOULD_EXECUTE_EXISTING_INTEGRATION"
        assert decision["human_hold"]["tasks"] == [OWNER]


def test_owning_task_hold_and_contradiction_remain_authoritative():
    held = projection("HELD", "CLEAR")["v3"]["executor"]["decisions"][0]
    contradicted = projection("CONTRADICTION", "CLEAR")["v3"]["executor"]["decisions"][0]

    assert held["decision"] == "BLOCK_HUMAN_HOLD"
    assert held["human_hold"]["tasks"] == [OWNER]
    assert contradicted["decision"] == "BLOCK_HOLD_AUTHORITY"
    assert contradicted["human_hold"]["tasks"] == [OWNER]


def test_missing_or_ambiguous_owner_identity_fails_closed():
    payload = projection(
        "CLEAR",
        "CLEAR",
        owner=None,
        owner_error="multiple conflicting explicit owning-task declarations",
    )
    pr = payload["pull_requests"][0]
    decision = payload["v3"]["executor"]["decisions"][0]

    assert pr["owning_task_id"] is None
    assert "conflicting" in pr["owning_task_error"]
    assert decision["decision"] == "BLOCK_HOLD_AUTHORITY"
    assert decision["human_hold"]["state"] == "UNKNOWN"


def test_p0_accepted_worker_without_fresh_attempt_evidence_is_stale_after_six_hours():
    value = execution_truth(
        {"gid": OWNER, "name": "P0 — worker", "notes": "PRIORITY: P0"},
        [story("DISPATCH ACCEPTED attempt_id=wa-1234", age=timedelta(hours=6, minutes=1), gid="100")],
        now=NOW,
    )

    assert value["priority"] == "P0"
    assert value["attention_threshold_seconds"] == 6 * 60 * 60
    assert value["stale"] is True
    assert value["stale_kind"] == "WORKER_EXECUTION_STALE"
    assert value["attempt_id"] == "wa-1234"
    assert value["stale_is_dead"] is False
    assert value["takeover_authorized"] is False
    assert value["recovery_requires_fresh_attempt_generation"] is True


def test_fresh_attempt_bound_producer_evidence_resets_execution_freshness():
    value = execution_truth(
        {"gid": OWNER, "name": "P0 — worker", "notes": "PRIORITY: P0"},
        [
            story("DISPATCH ACCEPTED attempt_id=wa-1234", age=timedelta(hours=8), gid="100"),
            story("RUNNING-SOURCE attempt_id=wa-1234 branch/head movement", age=timedelta(hours=1), gid="101"),
        ],
        now=NOW,
    )

    assert value["state"] == "RUNNING-SOURCE"
    assert value["stale"] is False
    assert value["stale_kind"] is None
    assert value["attempt_id"] == "wa-1234"
    assert value["freshness_age_seconds"] == 60 * 60


def test_unbound_or_different_attempt_producer_evidence_cannot_refresh_accepted_worker():
    value = execution_truth(
        {"gid": OWNER, "name": "P0 — worker", "notes": "PRIORITY: P0"},
        [
            story("DISPATCH ACCEPTED attempt_id=wa-1234", age=timedelta(hours=8), gid="100"),
            story("RUNNING-SOURCE attempt_id=wa-9999 unrelated producer", age=timedelta(minutes=5), gid="101"),
        ],
        now=NOW,
    )

    assert value["stale"] is True
    assert value["stale_kind"] == "WORKER_EXECUTION_STALE"
    assert value["attempt_id"] == "wa-1234"
    assert value["takeover_authorized"] is False
