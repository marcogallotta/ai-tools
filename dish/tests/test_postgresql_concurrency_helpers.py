from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import create_engine, text

from dish_tool.errors import DishRuleError
from tests.support.postgresql.concurrency import (
    TransactionGate,
    TransactionOutcome,
    assert_conditional_update_contention_lost,
    assert_lease_takeover,
    assert_stale_writer_rejected,
    assert_transaction_aborted,
    assert_transaction_blocked,
    assert_transaction_committed,
    execute_transaction,
    independent_connections,
)


def test_transaction_gate_proves_blocked_until_explicit_release() -> None:
    gate = TransactionGate(label="unit gate")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(gate.block)
        gate.wait_until_blocked()
        assert_transaction_blocked(future)
        gate.release()
        assert future.result(timeout=1) is None


def test_transaction_outcome_assertions_distinguish_commit_and_abort() -> None:
    committed = TransactionOutcome(value="ok", error=None, committed=True, rolled_back=False)
    assert assert_transaction_committed(committed) == "ok"

    error = ValueError("bad")
    aborted = TransactionOutcome(value=None, error=error, committed=False, rolled_back=True)
    assert assert_transaction_aborted(aborted, error_type=ValueError) is error


def test_stale_writer_assertion_requires_exact_rule() -> None:
    error = DishRuleError("CONFLICT", "stale", rule="stale_writer")
    outcome = TransactionOutcome(value=None, error=error, committed=False, rolled_back=True)
    assert assert_stale_writer_rejected(outcome, expected_rule="stale_writer") is error
    with pytest.raises(AssertionError, match="expected stale-writer rule"):
        assert_stale_writer_rejected(outcome, expected_rule="other")


def test_lease_takeover_and_conditional_update_assertions() -> None:
    before = {
        "lease_id": "old",
        "owner_id": "owner-a",
        "run_id": "run-a",
        "released_at": "2026-08-04T18:00:00+00:00",
    }
    after = {
        "lease_id": "new",
        "owner_id": "owner-b",
        "run_id": "run-b",
        "released_at": None,
    }
    assert_lease_takeover(
        before,
        after,
        expected_owner_id="owner-b",
        expected_run_id="run-b",
    )
    assert_conditional_update_contention_lost(0)
    with pytest.raises(AssertionError, match="rowcount 0"):
        assert_conditional_update_contention_lost(1)

def test_independent_transactions_report_commit_and_rollback(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'transactions.sqlite3'}", future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE events(value TEXT NOT NULL)"))
        with independent_connections(engine) as (first, second):
            committed = execute_transaction(
                first, lambda session: session.execute(text("INSERT INTO events VALUES ('ok')"))
            )

            def reject(session):
                session.execute(text("INSERT INTO events VALUES ('rolled-back')"))
                raise ValueError("reject transaction")

            aborted = execute_transaction(second, reject)
        assert assert_transaction_committed(committed).rowcount == 1
        assert isinstance(assert_transaction_aborted(aborted, error_type=ValueError), ValueError)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT value FROM events")).scalars().all() == ["ok"]
    finally:
        engine.dispose()

