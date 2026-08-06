"""Native PostgreSQL disconnect/recovery behavior for deployable worker processes."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from tests.support.postgresql.certification import postgresql_dsn
from tests.support.postgresql.core import core_db
from tests.support.postgresql.process_failure import (
    BarrierServer,
    compose_control,
    event_snapshot,
    read_ledger,
    start_projection_worker,
    start_reconciliation_worker,
    write_scenario,
)
from tests.support.postgresql.projection_attempts import native_workflow_db, seed_events

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def test_projection_worker_fails_clearly_across_postgresql_disconnect(core_db, tmp_path) -> None:
    factory, ids, context, task_id = native_workflow_db(core_db)
    events = seed_events(factory, ids, context, task_id)
    ledger = tmp_path / "external-ledger.json"
    with BarrierServer() as barrier:
        child = start_projection_worker(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            ledger=ledger,
            worker_id="disconnect-projection",
            scenario="before_claim",
            barrier=barrier,
        )
        reached = barrier.wait("before_claim")
        compose_control("stop")
        reached.release()
        failure_code = child.wait(expected=None)
        assert failure_code != 0

    compose_control("start")
    factory.kw["bind"].dispose()
    after_disconnect = event_snapshot(factory, events)
    assert after_disconnect["events"][0]["state"] == "pending"
    assert after_disconnect["events"][0]["claim_owner"] is None
    assert after_disconnect["attempts"] == []
    assert read_ledger(ledger)["dispatch_calls"] == 0

    replacement = start_projection_worker(
        dsn=postgresql_dsn(),
        tmp_path=tmp_path,
        ledger=ledger,
        worker_id="disconnect-projection-restart",
    )
    replacement.wait()
    after_recovery = event_snapshot(factory, events)
    external = read_ledger(ledger)
    assert after_recovery["events"][0]["state"] == "applied"
    assert external["dispatch_calls"] == 1
    write_scenario(
        "postgresql-disconnect-projection-worker",
        {
            "worker_exit_code": failure_code,
            "after_disconnect": after_disconnect,
            "after_restart": after_recovery,
            "external": external,
        },
        nodeid="tests/postgresql/native/test_process_failure_disconnect.py::test_projection_worker_fails_clearly_across_postgresql_disconnect",
        tmp_path=tmp_path,
    )


def test_reconciliation_worker_writes_nothing_while_postgresql_is_down(core_db, tmp_path) -> None:
    factory, ids, context, task_id = native_workflow_db(core_db)
    seed_events(factory, ids, context, task_id)
    ledger = tmp_path / "external-ledger.json"
    failed_output = tmp_path / "failed-reconciliation.json"
    with BarrierServer() as barrier:
        child = start_reconciliation_worker(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            ledger=ledger,
            generation_id=context["generation_id"],
            corpus_identity="section1-disconnect-corpus",
            output=failed_output,
            scenario="reconciliation_before_transaction",
            barrier=barrier,
        )
        reached = barrier.wait("after_corpus_fetch_before_reconciliation_transaction")
        compose_control("stop")
        reached.release()
        failure_code = child.wait(expected=None)
        assert failure_code != 0
        assert not failed_output.exists()

    compose_control("start")
    factory.kw["bind"].dispose()
    with session_scope(factory) as session:
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionReconciliationRun)
        ) == 0

    recovered_output = tmp_path / "recovered-reconciliation.json"
    replacement = start_reconciliation_worker(
        dsn=postgresql_dsn(),
        tmp_path=tmp_path,
        ledger=ledger,
        generation_id=context["generation_id"],
        corpus_identity="section1-disconnect-corpus",
        output=recovered_output,
    )
    replacement.wait()
    report = json.loads(recovered_output.read_text(encoding="utf-8"))
    with session_scope(factory) as session:
        runs = int(session.scalar(select(func.count()).select_from(tx.ProjectionReconciliationRun)))
        items = int(session.scalar(select(func.count()).select_from(tx.ProjectionReconciliationItem)))
    assert report["ok"] is True
    assert report["run_status"] == "complete"
    assert (runs, items) == (1, 1)
    write_scenario(
        "postgresql-disconnect-reconciliation-worker",
        {
            "worker_exit_code": failure_code,
            "failed_output_exists": failed_output.exists(),
            "recovered_report": report,
            "run_count": runs,
            "item_count": items,
        },
        nodeid="tests/postgresql/native/test_process_failure_disconnect.py::test_reconciliation_worker_writes_nothing_while_postgresql_is_down",
        tmp_path=tmp_path,
    )
