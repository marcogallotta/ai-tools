"""Native worker-process supervision and deterministic restart evidence for §1."""
from __future__ import annotations

import json

import pytest

from dish_pg.database import session_scope
from dish_pg.transition import ProjectionService
from tests.support.postgresql.certification import postgresql_dsn
from tests.support.postgresql.process_failure import (
    BarrierServer,
    event_snapshot,
    expire_claim,
    read_ledger,
    reconciliation_snapshot,
    start_projection_worker,
    start_reconciliation_worker,
    write_scenario,
)
from tests.support.postgresql.projection_attempts import native_workflow_db, seed_events
from tests.support.postgresql.workflow import NOW, _next

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def test_long_running_projection_worker_is_supervised_and_restarted(core_db, tmp_path) -> None:
    factory, ids, context, task_id = native_workflow_db(core_db)
    events = seed_events(factory, ids, context, task_id, count=2)
    ledger = tmp_path / "external-ledger.json"

    with BarrierServer() as barrier:
        first = start_projection_worker(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            ledger=ledger,
            worker_id="supervised-projection-old",
            scenario="long_running_projection_restart",
            barrier=barrier,
            once=False,
        )
        reached = barrier.wait("long_running_projection_before_second_attempt")
        before_restart = event_snapshot(factory, events)
        before_external = read_ledger(ledger)
        assert [row["state"] for row in before_restart["events"]] == ["applied", "claimed"]
        assert before_restart["events"][1]["claim_owner"] == "supervised-projection-old"
        assert len(before_restart["attempts"]) == 1
        assert before_external["dispatch_calls"] == 1
        assert before_external["prepare_calls"] == 2
        first_exit = first.kill()
        reached.close()

    assert first_exit != 0
    expire_claim(factory, events[1])
    replacement = start_projection_worker(
        dsn=postgresql_dsn(),
        tmp_path=tmp_path,
        ledger=ledger,
        worker_id="supervised-projection-new",
    )
    replacement.wait()

    after_restart = event_snapshot(factory, events)
    external = read_ledger(ledger)
    assert [row["state"] for row in after_restart["events"]] == ["applied", "applied"]
    assert len(after_restart["attempts"]) == 2
    assert [row["worker_id"] for row in after_restart["attempts"]] == [
        "supervised-projection-old",
        "supervised-projection-new",
    ]
    assert external["dispatch_calls"] == 2
    write_scenario(
        "long-running-projection-supervision-restart",
        {
            "terminated_process_exit_code": first_exit,
            "before_restart": before_restart,
            "after_restart": after_restart,
            "external": external,
        },
        nodeid=(
            "tests/postgresql/native/test_process_failure_supervision.py::"
            "test_long_running_projection_worker_is_supervised_and_restarted"
        ),
        tmp_path=tmp_path,
    )


def test_reconciliation_worker_is_supervised_and_restarted(core_db, tmp_path) -> None:
    factory, ids, context, _task_id = native_workflow_db(core_db)
    with session_scope(factory) as session:
        ProjectionService(session, uuid_factory=lambda: _next(ids)).activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="section1 supervised reconciliation restart",
            created_at=NOW,
            external_effects_enabled=True,
        )

    corpus_identity = "section1-supervised-reconciliation"
    ledger = tmp_path / "external-ledger.json"
    lost_output = tmp_path / "lost-reconciliation-output.json"
    with BarrierServer() as barrier:
        first = start_reconciliation_worker(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            ledger=ledger,
            generation_id=context["generation_id"],
            corpus_identity=corpus_identity,
            output=lost_output,
            scenario="reconciliation_before_transaction",
            barrier=barrier,
            item_count=2,
        )
        reached = barrier.wait("after_corpus_fetch_before_reconciliation_transaction")
        before_restart = reconciliation_snapshot(
            factory,
            generation_id=context["generation_id"],
            corpus_identity=corpus_identity,
        )
        assert before_restart == {"run_count": 0, "item_count": 0, "runs": []}
        first_exit = first.kill()
        reached.close()

    assert first_exit != 0
    assert not lost_output.exists()
    recovered_output = tmp_path / "recovered-reconciliation-output.json"
    replacement = start_reconciliation_worker(
        dsn=postgresql_dsn(),
        tmp_path=tmp_path,
        ledger=ledger,
        generation_id=context["generation_id"],
        corpus_identity=corpus_identity,
        output=recovered_output,
        item_count=2,
    )
    replacement.wait()
    recovered_report = json.loads(recovered_output.read_text(encoding="utf-8"))
    after_restart = reconciliation_snapshot(
        factory,
        generation_id=context["generation_id"],
        corpus_identity=corpus_identity,
    )

    assert recovered_report["ok"] is True
    assert recovered_report["run_status"] == "complete"
    assert recovered_report["processed_items"] == 2
    assert after_restart["run_count"] == 1
    assert after_restart["item_count"] == 2
    assert after_restart["runs"][0]["status"] == "complete"
    assert after_restart["runs"][0]["processed_items"] == 2
    write_scenario(
        "reconciliation-worker-supervision-restart",
        {
            "terminated_process_exit_code": first_exit,
            "lost_output_exists": lost_output.exists(),
            "before_restart": before_restart,
            "recovered_report": recovered_report,
            "after_restart": after_restart,
        },
        nodeid=(
            "tests/postgresql/native/test_process_failure_supervision.py::"
            "test_reconciliation_worker_is_supervised_and_restarted"
        ),
        tmp_path=tmp_path,
    )
