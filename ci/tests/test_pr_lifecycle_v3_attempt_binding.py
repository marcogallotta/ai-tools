from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from pr_lifecycle_task_state import execution_truth

NOW = datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc)
OWNER = "1217611794618560"


def story(text: str, *, age: timedelta, gid: str) -> dict[str, str]:
    return {
        "gid": gid,
        "created_at": (NOW - age).isoformat(),
        "text": text,
    }


def task() -> dict[str, str]:
    return {"gid": OWNER, "name": "P0 — worker", "notes": "PRIORITY: P0"}


def assert_unbound(value: dict[str, object]) -> None:
    assert value["state"] == "ACCEPTANCE UNBOUND — ATTEMPT IDENTITY REQUIRED"
    assert value["stale"] is True
    assert value["stale_kind"] == "WORKER_EXECUTION_STALE"
    assert value["attempt_id"] is None
    assert value["freshness_timestamp"] is None
    assert value["freshness_age_seconds"] is None
    assert value["stale_is_dead"] is False
    assert value["takeover_authorized"] is False
    assert value["recovery_requires_fresh_attempt_generation"] is True


def test_unbound_dispatch_acceptance_cannot_anchor_execution_freshness():
    value = execution_truth(
        task(),
        [story("DISPATCH ACCEPTED", age=timedelta(minutes=5), gid="100")],
        now=NOW,
    )
    assert_unbound(value)


def test_unbound_acceptance_followed_by_arbitrary_producer_cannot_refresh_execution():
    value = execution_truth(
        task(),
        [
            story("DISPATCH ACCEPTED", age=timedelta(hours=8), gid="100"),
            story(
                "RUNNING-SOURCE attempt_id=wa-9999 arbitrary producer",
                age=timedelta(minutes=5),
                gid="101",
            ),
        ],
        now=NOW,
    )
    assert_unbound(value)
