"""Runtime wiring rehearsal: exercise the reconciliation worker against real PostgreSQL.

This does not repeat the corpus-reconciliation correctness already covered by
tests/postgresql/test_reconciliation_worker.py (SQLite-backed, source
contract). It proves the *process*: dish_pg.reconciliation_worker driving the
real ProjectionService across a real connection pool and real row locks,
matching how the deployed worker will run.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.reconciliation_worker import (
    ExternalCorpusItem,
    ReconciliationRecord,
    ReconciliationWorker,
)
from dish_pg.transition import ProjectionService
from tests.support.postgresql.concurrency import TransactionGate, assert_transaction_blocked
from tests.support.postgresql.core import NOW, _bootstrap_registry, core_db

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def test_reconciliation_worker_completes_one_corpus_against_real_postgresql(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        ProjectionService(session, uuid_factory=lambda: next(ids)).activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="reconciliation worker rehearsal",
            created_at=NOW,
            external_effects_enabled=True,
        )
    generation_id = context["generation_id"]

    def fetch(corpus_identity: str):
        assert corpus_identity == "corpus-rehearsal"
        return (
            ExternalCorpusItem("task:1", "task", {"gid": "1"}),
            ExternalCorpusItem("task:2", "task", {"gid": "2"}),
        )

    compare_calls: list[str] = []

    def compare(_session, _generation_id, item: ExternalCorpusItem):
        compare_calls.append(item.item_identity)
        return ReconciliationRecord(
            item_identity=item.item_identity,
            entity_kind=item.entity_kind,
            mapping_id=None,
            outcome="matched",
            evidence=dict(item.payload),
        )

    worker = ReconciliationWorker(
        session_maker=factory,
        fetch_corpus=fetch,
        compare_item=compare,
        generation_id=generation_id,
        corpus_identity="corpus-rehearsal",
        clock=lambda: NOW,
    )

    run = worker.run_once()
    replayed = worker.run_once()

    assert run.status == "complete"
    assert replayed.reconciliation_run_id == run.reconciliation_run_id
    assert replayed.status == "complete"
    assert run.expected_items == 2
    assert run.processed_items == 2
    assert compare_calls == ["task:1", "task:2"]

    with session_scope(factory) as session:
        stored = session.get(tx.ProjectionReconciliationRun, run.reconciliation_run_id)
        assert stored.status == "complete"
        item_count = session.scalar(
            select(func.count())
            .select_from(tx.ProjectionReconciliationItem)
            .where(tx.ProjectionReconciliationItem.reconciliation_run_id == run.reconciliation_run_id)
        )
        assert item_count == 2
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionReconciliationRun)
        ) == 1

    gate = TransactionGate(label="first reconciliation start holds unique key")

    def start_reconciliation(*, hold_before_commit: bool):
        with session_scope(factory) as session:
            started = ProjectionService(session).start_reconciliation(
                generation_id=generation_id,
                corpus_identity="corpus-concurrent-start",
                expected_items=0,
                started_at=NOW,
            )
            if hold_before_commit:
                gate.block()
            return started.reconciliation_run_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(start_reconciliation, hold_before_commit=True)
        gate.wait_until_blocked()
        second = pool.submit(start_reconciliation, hold_before_commit=False)
        assert_transaction_blocked(second)
        gate.release()
        first_run_id = first.result()
        second_run_id = second.result()

    assert second_run_id == first_run_id
    with session_scope(factory) as session:
        concurrent = session.get(tx.ProjectionReconciliationRun, first_run_id)
        assert concurrent.status == "running"
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionReconciliationRun).where(
                tx.ProjectionReconciliationRun.corpus_identity == "corpus-concurrent-start"
            )
        ) == 1
        ProjectionService(session).complete_reconciliation(
            reconciliation_run_id=first_run_id,
            completed_at=NOW,
        )
