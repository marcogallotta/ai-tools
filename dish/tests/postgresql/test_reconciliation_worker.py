from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.reconciliation_worker import (
    ExternalCorpusItem,
    ReconciliationRecord,
    ReconciliationWorker,
)
from dish_pg.transition import ProjectionService, TransitionAuthorityError
from tests.support.postgresql.core import NOW
from tests.support.postgresql.workflow import workflow_db


def _activate_epoch(factory, ids, generation_id: uuid.UUID) -> None:
    with session_scope(factory) as session:
        ProjectionService(session, uuid_factory=lambda: next(ids)).activate_epoch(
            generation_id=generation_id,
            activation_reason="reconciliation worker contract",
            created_at=NOW,
        )


def test_worker_records_complete_corpus_through_projection_service(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    _activate_epoch(factory, ids, context["generation_id"])
    fetch_calls: list[str] = []

    def fetch(corpus_identity: str):
        fetch_calls.append(corpus_identity)
        return (
            ExternalCorpusItem("task:1", "task", {"gid": "1"}),
            ExternalCorpusItem("task:2", "task", {"gid": "2"}),
        )

    def compare(_session, _generation_id, item: ExternalCorpusItem):
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
        generation_id=context["generation_id"],
        corpus_identity="corpus-1",
        clock=lambda: NOW,
    )
    run = worker.run_once()

    assert fetch_calls == ["corpus-1"]
    assert run.status == "complete"
    assert run.expected_items == 2
    assert run.processed_items == 2
    with session_scope(factory) as session:
        stored = session.get(tx.ProjectionReconciliationRun, run.reconciliation_run_id)
        assert stored.status == "complete"
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionReconciliationItem).where(
                tx.ProjectionReconciliationItem.reconciliation_run_id == run.reconciliation_run_id
            )
        ) == 2


def test_worker_preserves_blocked_authority_result(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    _activate_epoch(factory, ids, context["generation_id"])

    def compare(_session, _generation_id, item: ExternalCorpusItem):
        return ReconciliationRecord(
            item_identity=item.item_identity,
            entity_kind=item.entity_kind,
            mapping_id=None,
            outcome=str(item.payload["outcome"]),
            evidence={"source": item.item_identity},
        )

    worker = ReconciliationWorker(
        session_maker=factory,
        fetch_corpus=lambda _identity: (
            ExternalCorpusItem("task:known", "task", {"outcome": "matched"}),
            ExternalCorpusItem("task:unknown", "task", {"outcome": "unknown_external"}),
        ),
        compare_item=compare,
        generation_id=context["generation_id"],
        corpus_identity="corpus-blocked",
        clock=lambda: NOW,
    )

    assert worker.run_once().status == "blocked"


def test_fetch_failure_opens_no_reconciliation_transaction(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    _activate_epoch(factory, ids, context["generation_id"])

    def fail_fetch(_identity: str):
        raise RuntimeError("external corpus unavailable")

    worker = ReconciliationWorker(
        session_maker=factory,
        fetch_corpus=fail_fetch,
        compare_item=lambda *_args: pytest.fail("comparator must not run"),
        generation_id=context["generation_id"],
        corpus_identity="corpus-unavailable",
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="external corpus unavailable"):
        worker.run_once()
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(tx.ProjectionReconciliationRun)) == 0


def test_conflicting_duplicate_item_is_not_prechecked_or_suppressed(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    _activate_epoch(factory, ids, context["generation_id"])
    comparisons = iter(("matched", "blocked"))

    def compare(_session, _generation_id, item: ExternalCorpusItem):
        return ReconciliationRecord(
            item_identity=item.item_identity,
            entity_kind=item.entity_kind,
            mapping_id=None,
            outcome=next(comparisons),
            evidence={"gid": "1"},
        )

    worker = ReconciliationWorker(
        session_maker=factory,
        fetch_corpus=lambda _identity: (
            ExternalCorpusItem("task:duplicate", "task", {}),
            ExternalCorpusItem("task:duplicate", "task", {}),
        ),
        compare_item=compare,
        generation_id=context["generation_id"],
        corpus_identity="corpus-conflict",
        clock=lambda: NOW,
    )

    with pytest.raises(TransitionAuthorityError, match="identity conflict"):
        worker.run_once()
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(tx.ProjectionReconciliationRun)) == 0
        assert session.scalar(select(func.count()).select_from(tx.ProjectionReconciliationItem)) == 0


def test_shutdown_during_fetch_opens_no_authority_transaction(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    _activate_epoch(factory, ids, context["generation_id"])
    worker: ReconciliationWorker

    def fetch(_identity: str):
        worker.request_shutdown()
        return (ExternalCorpusItem("project:1", "project", {}),)

    worker = ReconciliationWorker(
        session_maker=factory,
        fetch_corpus=fetch,
        compare_item=lambda *_args: pytest.fail("comparator must not run"),
        generation_id=context["generation_id"],
        corpus_identity="corpus-shutdown",
        clock=lambda: NOW,
    )

    assert worker.run_once() is None
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(tx.ProjectionReconciliationRun)) == 0
