"""Native durable reconciliation-progress loss and fresh-process resumption evidence."""
from __future__ import annotations

import uuid

import pytest

from dish_pg.database import session_scope
from dish_pg.transition import ProjectionService
from tests.support.postgresql.certification import postgresql_dsn
from tests.support.postgresql.process_failure import (
    BarrierServer,
    read_reconciliation_child_result,
    reconciliation_snapshot,
    start_reconciliation_checkpoint_process,
    write_scenario,
)
from tests.support.postgresql.projection_attempts import native_workflow_db
from tests.support.postgresql.workflow import NOW, _next

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def _seed_reconciliation_authority(core_db):
    factory, ids, context, _task_id = native_workflow_db(core_db)
    with session_scope(factory) as session:
        ProjectionService(session, uuid_factory=lambda: _next(ids)).activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="section1 reconciliation process-loss rehearsal",
            created_at=NOW,
            external_effects_enabled=True,
        )
    return factory, context["generation_id"]


def _expected_item_identities(corpus_identity: str, item_count: int) -> list[str]:
    return [f"corpus:{corpus_identity}:item-{index}" for index in range(1, item_count + 1)]


def _resume_and_assert_exact_corpus(
    *,
    factory,
    generation_id: uuid.UUID,
    corpus_identity: str,
    item_count: int,
    reconciliation_run_id: uuid.UUID,
    tmp_path,
    ledger,
    output,
) -> tuple[dict, dict]:
    replacement = start_reconciliation_checkpoint_process(
        dsn=postgresql_dsn(),
        tmp_path=tmp_path,
        ledger=ledger,
        generation_id=generation_id,
        corpus_identity=corpus_identity,
        output=output,
        item_count=item_count,
        mode="resume",
        reconciliation_run_id=reconciliation_run_id,
        now=NOW,
    )
    replacement.wait()
    result = read_reconciliation_child_result(output)
    snapshot = reconciliation_snapshot(
        factory,
        generation_id=generation_id,
        corpus_identity=corpus_identity,
    )
    assert result["reconciliation_run_id"] == str(reconciliation_run_id)
    assert result["run_status"] == "complete"
    assert result["processed_items"] == item_count
    assert snapshot["run_count"] == 1
    assert snapshot["item_count"] == item_count
    assert snapshot["runs"] == [
        {
            "reconciliation_run_id": str(reconciliation_run_id),
            "status": "complete",
            "expected_items": item_count,
            "processed_items": item_count,
            "completed": True,
            "item_identities": _expected_item_identities(corpus_identity, item_count),
            "item_outcomes": ["matched"] * item_count,
        }
    ]
    return result, snapshot


def test_reconciliation_process_loss_after_durable_run_creation_resumes_exact_run(
    core_db, tmp_path
) -> None:
    factory, generation_id = _seed_reconciliation_authority(core_db)
    corpus_identity = "section1-durable-run-loss"
    item_count = 3
    ledger = tmp_path / "external-ledger.json"
    lost_output = tmp_path / "lost-durable-run-output.json"

    with BarrierServer() as barrier:
        first = start_reconciliation_checkpoint_process(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            ledger=ledger,
            generation_id=generation_id,
            corpus_identity=corpus_identity,
            output=lost_output,
            item_count=item_count,
            mode="start",
            scenario="after_durable_run_creation",
            barrier=barrier,
            now=NOW,
        )
        reached = barrier.wait("after_durable_reconciliation_run_creation")
        reconciliation_run_id = uuid.UUID(reached.payload["reconciliation_run_id"])
        before_loss = reconciliation_snapshot(
            factory,
            generation_id=generation_id,
            corpus_identity=corpus_identity,
        )
        assert before_loss["run_count"] == 1
        assert before_loss["item_count"] == 0
        assert before_loss["runs"] == [
            {
                "reconciliation_run_id": str(reconciliation_run_id),
                "status": "running",
                "expected_items": item_count,
                "processed_items": 0,
                "completed": False,
                "item_identities": [],
                "item_outcomes": [],
            }
        ]
        first_exit = first.kill()
        reached.close()

    assert first_exit != 0
    assert not lost_output.exists()
    after_loss = reconciliation_snapshot(
        factory,
        generation_id=generation_id,
        corpus_identity=corpus_identity,
    )
    assert after_loss == before_loss
    recovered_result, recovered = _resume_and_assert_exact_corpus(
        factory=factory,
        generation_id=generation_id,
        corpus_identity=corpus_identity,
        item_count=item_count,
        reconciliation_run_id=reconciliation_run_id,
        tmp_path=tmp_path,
        ledger=ledger,
        output=tmp_path / "recovered-durable-run-output.json",
    )
    write_scenario(
        "reconciliation-loss-after-durable-run",
        {
            "terminated_process_exit_code": first_exit,
            "lost_output_exists": lost_output.exists(),
            "before_loss": before_loss,
            "after_loss": after_loss,
            "recovered_result": recovered_result,
            "recovered": recovered,
        },
        nodeid=(
            "tests/postgresql/native/test_process_failure_reconciliation.py::"
            "test_reconciliation_process_loss_after_durable_run_creation_resumes_exact_run"
        ),
        tmp_path=tmp_path,
    )


def test_reconciliation_process_loss_after_partial_corpus_resumes_without_duplicate_items(
    core_db, tmp_path
) -> None:
    factory, generation_id = _seed_reconciliation_authority(core_db)
    corpus_identity = "section1-partial-corpus-loss"
    item_count = 3
    ledger = tmp_path / "external-ledger.json"
    lost_output = tmp_path / "lost-partial-corpus-output.json"

    with BarrierServer() as barrier:
        first = start_reconciliation_checkpoint_process(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            ledger=ledger,
            generation_id=generation_id,
            corpus_identity=corpus_identity,
            output=lost_output,
            item_count=item_count,
            mode="start",
            scenario="after_partial_corpus",
            barrier=barrier,
            now=NOW,
        )
        reached = barrier.wait("after_partially_recorded_reconciliation_corpus")
        reconciliation_run_id = uuid.UUID(reached.payload["reconciliation_run_id"])
        before_loss = reconciliation_snapshot(
            factory,
            generation_id=generation_id,
            corpus_identity=corpus_identity,
        )
        assert before_loss["run_count"] == 1
        assert before_loss["item_count"] == 1
        assert before_loss["runs"] == [
            {
                "reconciliation_run_id": str(reconciliation_run_id),
                "status": "running",
                "expected_items": item_count,
                "processed_items": 1,
                "completed": False,
                "item_identities": [_expected_item_identities(corpus_identity, item_count)[0]],
                "item_outcomes": ["matched"],
            }
        ]
        first_exit = first.kill()
        reached.close()

    assert first_exit != 0
    assert not lost_output.exists()
    after_loss = reconciliation_snapshot(
        factory,
        generation_id=generation_id,
        corpus_identity=corpus_identity,
    )
    assert after_loss == before_loss
    recovered_result, recovered = _resume_and_assert_exact_corpus(
        factory=factory,
        generation_id=generation_id,
        corpus_identity=corpus_identity,
        item_count=item_count,
        reconciliation_run_id=reconciliation_run_id,
        tmp_path=tmp_path,
        ledger=ledger,
        output=tmp_path / "recovered-partial-corpus-output.json",
    )
    write_scenario(
        "reconciliation-loss-after-partial-corpus",
        {
            "terminated_process_exit_code": first_exit,
            "lost_output_exists": lost_output.exists(),
            "before_loss": before_loss,
            "after_loss": after_loss,
            "recovered_result": recovered_result,
            "recovered": recovered,
        },
        nodeid=(
            "tests/postgresql/native/test_process_failure_reconciliation.py::"
            "test_reconciliation_process_loss_after_partial_corpus_resumes_without_duplicate_items"
        ),
        tmp_path=tmp_path,
    )
