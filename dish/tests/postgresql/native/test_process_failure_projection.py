"""Native PostgreSQL projection-worker crash boundaries across real OS processes."""
from __future__ import annotations

import pytest

from tests.support.postgresql.certification import postgresql_dsn
from tests.support.postgresql.core import core_db
from tests.support.postgresql.process_failure import (
    BarrierServer,
    SettlementNotification,
    event_snapshot,
    expire_claim,
    read_ledger,
    start_projection_worker,
    write_scenario,
)
from tests.support.postgresql.projection_attempts import native_workflow_db, seed_events

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def _seed(core_db, *, count: int = 1):
    factory, ids, context, task_id = native_workflow_db(core_db)
    events = seed_events(factory, ids, context, task_id, count=count)
    return factory, events


def test_process_failure_before_claim(core_db, tmp_path) -> None:
    factory, events = _seed(core_db)
    ledger = tmp_path / "external-ledger.json"
    with BarrierServer() as barrier:
        child = start_projection_worker(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            ledger=ledger,
            worker_id="before-claim-old",
            scenario="before_claim",
            barrier=barrier,
            once=True,
        )
        reached = barrier.wait("before_claim")
        before = event_snapshot(factory, events)
        assert before["events"][0]["state"] == "pending"
        assert before["events"][0]["claim_owner"] is None
        assert before["attempts"] == []
        child.kill()
        reached.close()

    replacement = start_projection_worker(
        dsn=postgresql_dsn(),
        tmp_path=tmp_path,
        ledger=ledger,
        worker_id="before-claim-new",
    )
    replacement.wait()
    after = event_snapshot(factory, events)
    external = read_ledger(ledger)
    assert after["events"][0]["state"] == "applied"
    assert len(after["attempts"]) == 1
    assert external["dispatch_calls"] == 1
    write_scenario(
        "projection-before-claim",
        {"before": before, "after": after, "external": external},
        nodeid="tests/postgresql/native/test_process_failure_projection.py::test_process_failure_before_claim",
        tmp_path=tmp_path,
    )


def test_process_failure_after_claim_before_durable_intent(core_db, tmp_path) -> None:
    factory, events = _seed(core_db)
    ledger = tmp_path / "external-ledger.json"
    with BarrierServer() as barrier:
        child = start_projection_worker(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            ledger=ledger,
            worker_id="after-claim-old",
            scenario="after_claim",
            barrier=barrier,
        )
        reached = barrier.wait("after_claim_before_durable_intent")
        before = event_snapshot(factory, events)
        assert before["events"][0]["state"] == "claimed"
        assert before["events"][0]["claim_owner"] == "after-claim-old"
        assert before["attempts"] == []
        assert read_ledger(ledger)["dispatch_calls"] == 0
        child.kill()
        reached.close()

    expire_claim(factory, events[0])
    replacement = start_projection_worker(
        dsn=postgresql_dsn(),
        tmp_path=tmp_path,
        ledger=ledger,
        worker_id="after-claim-new",
    )
    replacement.wait()
    after = event_snapshot(factory, events)
    external = read_ledger(ledger)
    assert after["events"][0]["state"] == "applied"
    assert [row["kind"] for row in after["attempts"]] == ["dispatch"]
    assert external["dispatch_calls"] == 1
    write_scenario(
        "projection-after-claim-before-intent",
        {"before": before, "after": after, "external": external},
        nodeid="tests/postgresql/native/test_process_failure_projection.py::test_process_failure_after_claim_before_durable_intent",
        tmp_path=tmp_path,
    )


def test_process_failure_after_durable_intent_before_external_call(core_db, tmp_path) -> None:
    factory, events = _seed(core_db)
    ledger = tmp_path / "external-ledger.json"
    with BarrierServer() as barrier:
        child = start_projection_worker(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            ledger=ledger,
            worker_id="after-intent-old",
            scenario="after_intent",
            barrier=barrier,
        )
        reached = barrier.wait("after_durable_intent_before_external_call")
        before = event_snapshot(factory, events)
        assert before["events"][0]["state"] == "claimed"
        assert [row["state"] for row in before["attempts"]] == ["dispatched"]
        assert read_ledger(ledger)["dispatch_calls"] == 0
        child.kill()
        reached.close()

    expire_claim(factory, events[0])
    recovery = start_projection_worker(
        dsn=postgresql_dsn(),
        tmp_path=tmp_path,
        ledger=ledger,
        worker_id="after-intent-recovery",
    )
    recovery.wait()
    recovered = event_snapshot(factory, events)
    external_after_recovery = read_ledger(ledger)
    assert recovered["events"][0]["state"] == "pending"
    assert [row["kind"] for row in recovered["attempts"]] == ["dispatch", "recovery"]
    assert [row["state"] for row in recovered["attempts"]] == ["uncertain", "not_applied"]
    assert external_after_recovery["dispatch_calls"] == 0
    assert external_after_recovery["recovery_observations"] == 1

    safe_retry = start_projection_worker(
        dsn=postgresql_dsn(),
        tmp_path=tmp_path,
        ledger=ledger,
        worker_id="after-intent-safe-retry",
    )
    safe_retry.wait()
    after = event_snapshot(factory, events)
    external = read_ledger(ledger)
    assert after["events"][0]["state"] == "applied"
    assert external["dispatch_calls"] == 1
    write_scenario(
        "projection-after-intent-before-call",
        {
            "before": before,
            "after_recovery": recovered,
            "after_safe_retry": after,
            "external": external,
        },
        nodeid="tests/postgresql/native/test_process_failure_projection.py::test_process_failure_after_durable_intent_before_external_call",
        tmp_path=tmp_path,
    )


def test_process_failure_after_ambiguous_external_response(core_db, tmp_path) -> None:
    factory, events = _seed(core_db, count=2)
    ledger = tmp_path / "external-ledger.json"
    with BarrierServer() as barrier:
        child = start_projection_worker(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            ledger=ledger,
            worker_id="ambiguous-old",
            scenario="ambiguous_response",
            barrier=barrier,
        )
        reached = barrier.wait("after_ambiguous_external_response_before_settlement")
        before = event_snapshot(factory, events)
        assert [row["state"] for row in before["events"]] == ["claimed", "pending"]
        assert read_ledger(ledger)["dispatch_calls"] == 1

        ineligible = start_projection_worker(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            ledger=ledger,
            worker_id="ambiguous-too-early",
        )
        ineligible.wait()
        assert event_snapshot(factory, events) == before
        child.kill()
        reached.close()

    expire_claim(factory, events[0])
    recovery = start_projection_worker(
        dsn=postgresql_dsn(),
        tmp_path=tmp_path,
        ledger=ledger,
        worker_id="ambiguous-recovery",
        scenario="ambiguous_unresolved",
    )
    recovery.wait()
    recovered = event_snapshot(factory, events)
    assert [row["state"] for row in recovered["events"]] == ["uncertain", "pending"]
    assert [row["kind"] for row in recovered["attempts"]] == ["dispatch", "recovery"]
    assert [row["state"] for row in recovered["attempts"]] == ["uncertain", "uncertain"]
    assert read_ledger(ledger)["dispatch_calls"] == 1

    next_event = start_projection_worker(
        dsn=postgresql_dsn(),
        tmp_path=tmp_path,
        ledger=ledger,
        worker_id="ambiguous-next-event",
    )
    next_event.wait()
    after = event_snapshot(factory, events)
    external = read_ledger(ledger)
    assert after == recovered
    assert external["dispatch_calls"] == 1
    assert external["recovery_observations"] == 1
    write_scenario(
        "projection-after-ambiguous-response",
        {"before": before, "after_recovery": recovered, "after": after, "external": external},
        nodeid="tests/postgresql/native/test_process_failure_projection.py::test_process_failure_after_ambiguous_external_response",
        tmp_path=tmp_path,
    )


def test_process_failure_after_settlement_before_shutdown(core_db, tmp_path) -> None:
    factory, events = _seed(core_db)
    ledger = tmp_path / "external-ledger.json"
    notification = SettlementNotification(dsn=postgresql_dsn(), event_id=events[0])
    child = start_projection_worker(
        dsn=postgresql_dsn(),
        tmp_path=tmp_path,
        ledger=ledger,
        worker_id="after-settlement-old",
        once=False,
    )
    try:
        notification.wait()
        settled = event_snapshot(factory, events)
        assert settled["events"][0]["state"] == "applied"
        assert child.process.poll() is None
        child.kill()
    finally:
        notification.close()

    replacement = start_projection_worker(
        dsn=postgresql_dsn(),
        tmp_path=tmp_path,
        ledger=ledger,
        worker_id="after-settlement-new",
    )
    replacement.wait()
    after = event_snapshot(factory, events)
    external = read_ledger(ledger)
    assert after == settled
    assert external["dispatch_calls"] == 1
    write_scenario(
        "projection-after-settlement",
        {"settled_before_kill": settled, "after_restart": after, "external": external},
        nodeid="tests/postgresql/native/test_process_failure_projection.py::test_process_failure_after_settlement_before_shutdown",
        tmp_path=tmp_path,
    )
