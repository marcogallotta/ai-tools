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
    reconciliation_report,
)
from dish_pg.transition import ProjectionService, TransitionAuthorityError
from tests.support.postgresql.core import NOW
from tests.support.postgresql.release import _prepare_candidate
from tests.support.postgresql.workflow import workflow_db


def _activate_epoch(factory, ids, generation_id: uuid.UUID) -> None:
    with session_scope(factory) as session:
        ProjectionService(session, uuid_factory=lambda: next(ids)).activate_epoch(
            generation_id=generation_id,
            activation_reason="reconciliation worker contract",
            created_at=NOW,
            external_effects_enabled=True,
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
    report = reconciliation_report(factory, run)
    assert report["ok"] is True
    assert report["outcome_counts"] == {"matched": 2}
    assert report["report_sha256"]
    with session_scope(factory) as session:
        stored = session.get(tx.ProjectionReconciliationRun, run.reconciliation_run_id)
        assert stored.status == "complete"
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionReconciliationItem).where(
                tx.ProjectionReconciliationItem.reconciliation_run_id == run.reconciliation_run_id
            )
        ) == 2


def test_completed_worker_replay_returns_same_run_without_duplicate_work(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    _activate_epoch(factory, ids, context["generation_id"])
    compare_calls: list[str] = []

    corpus = (
        ExternalCorpusItem("task:1", "task", {"gid": "1"}),
        ExternalCorpusItem("task:2", "task", {"gid": "2"}),
    )

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
        fetch_corpus=lambda _identity: corpus,
        compare_item=compare,
        generation_id=context["generation_id"],
        corpus_identity="corpus-replayed",
        clock=lambda: NOW,
    )

    first = worker.run_once()
    repeated = worker.run_once()

    assert repeated.reconciliation_run_id == first.reconciliation_run_id
    assert repeated.status == "complete"
    assert compare_calls == ["task:1", "task:2"]
    with session_scope(factory) as session:
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionReconciliationRun)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionReconciliationItem)
        ) == 2


def test_worker_restart_resumes_only_missing_reconciliation_items(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    _activate_epoch(factory, ids, context["generation_id"])
    corpus = (
        ExternalCorpusItem("task:1", "task", {"gid": "1"}),
        ExternalCorpusItem("task:2", "task", {"gid": "2"}),
    )
    with session_scope(factory) as session:
        service = ProjectionService(session, uuid_factory=lambda: next(ids))
        started = service.start_reconciliation(
            generation_id=context["generation_id"],
            corpus_identity="corpus-resume",
            expected_items=2,
            started_at=NOW,
        )
        service.record_reconciliation_item(
            reconciliation_run_id=started.reconciliation_run_id,
            item_identity="task:1",
            entity_kind="task",
            mapping_id=None,
            outcome="matched",
            evidence={"gid": "1"},
            recorded_at=NOW,
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

    resumed = ReconciliationWorker(
        session_maker=factory,
        fetch_corpus=lambda _identity: corpus,
        compare_item=compare,
        generation_id=context["generation_id"],
        corpus_identity="corpus-resume",
        clock=lambda: NOW,
    ).run_once()

    assert resumed.reconciliation_run_id == started.reconciliation_run_id
    assert resumed.status == "complete"
    assert compare_calls == ["task:2"]
    with session_scope(factory) as session:
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionReconciliationRun)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionReconciliationItem)
        ) == 2


def test_repeated_start_fails_closed_on_immutable_count_conflict(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    _activate_epoch(factory, ids, context["generation_id"])
    with session_scope(factory) as session:
        first = ProjectionService(session, uuid_factory=lambda: next(ids)).start_reconciliation(
            generation_id=context["generation_id"],
            corpus_identity="corpus-count-conflict",
            expected_items=1,
            started_at=NOW,
        )

    with pytest.raises(TransitionAuthorityError, match="immutable inputs conflict"):
        with session_scope(factory) as session:
            ProjectionService(session).start_reconciliation(
                generation_id=context["generation_id"],
                corpus_identity="corpus-count-conflict",
                expected_items=2,
                started_at=NOW,
            )

    with session_scope(factory) as session:
        stored = session.get(tx.ProjectionReconciliationRun, first.reconciliation_run_id)
        assert stored.expected_items == 1
        assert stored.status == "running"
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionReconciliationRun)
        ) == 1


def test_repeated_start_fails_closed_on_candidate_bound_authority(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _prepare_candidate(session, ids, context, task_id)

    with pytest.raises(TransitionAuthorityError, match="authority conflict"):
        with session_scope(factory) as session:
            ProjectionService(session).start_reconciliation(
                generation_id=context["generation_id"],
                corpus_identity="candidate-release-corpus@42619b9",
                expected_items=3,
                started_at=NOW,
            )

    with session_scope(factory) as session:
        candidate_run = session.scalar(
            select(tx.ProjectionReconciliationRun).where(
                tx.ProjectionReconciliationRun.corpus_identity
                == "candidate-release-corpus@42619b9"
            )
        )
        assert candidate_run.candidate_id is not None
        assert candidate_run.status == "complete"
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionReconciliationRun).where(
                tx.ProjectionReconciliationRun.corpus_identity
                == "candidate-release-corpus@42619b9"
            )
        ) == 1


def test_completed_replay_fails_closed_on_changed_corpus_membership(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    _activate_epoch(factory, ids, context["generation_id"])

    def compare(_session, _generation_id, item: ExternalCorpusItem):
        return ReconciliationRecord(
            item_identity=item.item_identity,
            entity_kind=item.entity_kind,
            mapping_id=None,
            outcome="matched",
            evidence=dict(item.payload),
        )

    first_worker = ReconciliationWorker(
        session_maker=factory,
        fetch_corpus=lambda _identity: (
            ExternalCorpusItem("task:1", "task", {"gid": "1"}),
            ExternalCorpusItem("task:2", "task", {"gid": "2"}),
        ),
        compare_item=compare,
        generation_id=context["generation_id"],
        corpus_identity="corpus-membership-conflict",
        clock=lambda: NOW,
    )
    first = first_worker.run_once()
    unexpected_compare_calls: list[str] = []

    conflicting_worker = ReconciliationWorker(
        session_maker=factory,
        fetch_corpus=lambda _identity: (
            ExternalCorpusItem("task:1", "task", {"gid": "1"}),
            ExternalCorpusItem("task:3", "task", {"gid": "3"}),
        ),
        compare_item=lambda _session, _generation_id, item: (
            unexpected_compare_calls.append(item.item_identity)
            or compare(_session, _generation_id, item)
        ),
        generation_id=context["generation_id"],
        corpus_identity="corpus-membership-conflict",
        clock=lambda: NOW,
    )

    with pytest.raises(TransitionAuthorityError, match="immutable inputs conflict"):
        conflicting_worker.run_once()
    assert unexpected_compare_calls == []
    with session_scope(factory) as session:
        stored = session.get(tx.ProjectionReconciliationRun, first.reconciliation_run_id)
        assert stored.status == "complete"
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionReconciliationRun)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionReconciliationItem)
        ) == 2


def test_completed_run_fences_stale_reconciliation_writer(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    _activate_epoch(factory, ids, context["generation_id"])
    with session_scope(factory) as session:
        service = ProjectionService(session, uuid_factory=lambda: next(ids))
        run = service.start_reconciliation(
            generation_id=context["generation_id"],
            corpus_identity="corpus-stale-writer",
            expected_items=0,
            started_at=NOW,
        )
        service.complete_reconciliation(
            reconciliation_run_id=run.reconciliation_run_id,
            completed_at=NOW,
        )

    with pytest.raises(TransitionAuthorityError, match="not active"):
        with session_scope(factory) as session:
            ProjectionService(session).record_reconciliation_item(
                reconciliation_run_id=run.reconciliation_run_id,
                item_identity="task:late",
                entity_kind="task",
                mapping_id=None,
                outcome="matched",
                evidence={"gid": "late"},
                recorded_at=NOW,
            )

    with session_scope(factory) as session:
        stored = session.get(tx.ProjectionReconciliationRun, run.reconciliation_run_id)
        assert stored.status == "complete"
        assert stored.processed_items == 0
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionReconciliationItem)
        ) == 0


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

    blocked = worker.run_once()
    assert blocked.status == "blocked"
    report = reconciliation_report(factory, blocked)
    assert report["ok"] is False
    assert report["outcome_counts"] == {"matched": 1, "unknown_external": 1}


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
