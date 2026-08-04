"""Deterministic helpers for native PostgreSQL concurrency certification tests."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Barrier, BrokenBarrierError, Event
from typing import Callable, Generic, Iterator, TypeVar

from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from dish_tool.errors import DishRuleError

T = TypeVar("T")

DEFAULT_RACE_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class TransactionOutcome(Generic[T]):
    """Observable commit/rollback result for one test-owned transaction."""

    value: T | None
    error: BaseException | None
    committed: bool
    rolled_back: bool


class TransactionGate:
    """Two-phase deterministic gate for one blocked transaction."""

    def __init__(self, *, label: str) -> None:
        self.label = label
        self._entered = Event()
        self._release = Event()

    def block(self, *, timeout: float = DEFAULT_RACE_TIMEOUT_SECONDS) -> None:
        self._entered.set()
        if not self._release.wait(timeout=timeout):
            raise AssertionError(f"transaction gate timed out waiting for release: {self.label}")

    def wait_until_blocked(self, *, timeout: float = DEFAULT_RACE_TIMEOUT_SECONDS) -> None:
        if not self._entered.wait(timeout=timeout):
            raise AssertionError(f"transaction did not reach gate: {self.label}")

    def release(self) -> None:
        self._release.set()


@contextmanager
def independent_connections(engine: Engine, count: int = 2) -> Iterator[tuple[Connection, ...]]:
    """Open independent DBAPI transactions and close them in reverse order."""

    if count < 2:
        raise ValueError("concurrency tests require at least two independent connections")
    connections = tuple(engine.connect() for _ in range(count))
    try:
        yield connections
    finally:
        for connection in reversed(connections):
            connection.close()


@contextmanager
def managed_session(connection: Connection) -> Iterator[Session]:
    """Commit one bound session on success and roll it back on any failure."""

    session = Session(bind=connection, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def execute_transaction(connection: Connection, operation: Callable[[Session], T]) -> TransactionOutcome[T]:
    """Execute one operation and retain whether commit or rollback occurred."""

    session = Session(bind=connection, expire_on_commit=False)
    try:
        value = operation(session)
        session.commit()
        return TransactionOutcome(value=value, error=None, committed=True, rolled_back=False)
    except BaseException as exc:
        session.rollback()
        return TransactionOutcome(value=None, error=exc, committed=False, rolled_back=True)
    finally:
        session.close()


def assert_transaction_blocked(future: Future[object]) -> None:
    """Prove a transaction is held at an explicit synchronization gate."""

    if future.done():
        # Surface the worker's actual result or exception before explaining the
        # interleaving failure.  Callers invoke this only after the gate's
        # entered event has been observed and before releasing that gate.
        future.result()
        raise AssertionError("transaction completed before the expected release point")


def assert_transaction_committed(outcome: TransactionOutcome[T]) -> T:
    if not outcome.committed or outcome.rolled_back or outcome.error is not None:
        raise AssertionError(f"expected committed transaction, got {outcome!r}")
    return outcome.value  # type: ignore[return-value]


def assert_transaction_aborted(
    outcome: TransactionOutcome[object], *, error_type: type[BaseException] | None = None
) -> BaseException:
    if not outcome.rolled_back or outcome.committed or outcome.error is None:
        raise AssertionError(f"expected aborted transaction, got {outcome!r}")
    if error_type is not None and not isinstance(outcome.error, error_type):
        raise AssertionError(
            f"expected aborted transaction error {error_type.__name__}, "
            f"got {type(outcome.error).__name__}"
        )
    return outcome.error


def assert_stale_writer_rejected(
    outcome: TransactionOutcome[object], *, expected_rule: str
) -> DishRuleError:
    error = assert_transaction_aborted(outcome, error_type=DishRuleError)
    assert isinstance(error, DishRuleError)
    if error.rule != expected_rule:
        raise AssertionError(
            f"expected stale-writer rule {expected_rule!r}, got {error.rule!r}"
        )
    return error


def assert_lease_takeover(
    before,
    after,
    *,
    expected_owner_id: str,
    expected_run_id: str,
) -> None:
    """Assert that a new lease row replaced, rather than mutated, the old owner."""

    if before is None or after is None:
        raise AssertionError("lease takeover requires before and after rows")
    if before["lease_id"] == after["lease_id"]:
        raise AssertionError("lease takeover reused the prior lease identity")
    if after["owner_id"] != expected_owner_id or after["run_id"] != expected_run_id:
        raise AssertionError(
            "lease takeover did not assign the expected owner/run: "
            f"{after['owner_id']!r}/{after['run_id']!r}"
        )
    if before["released_at"] is None:
        raise AssertionError("prior lease was not durably released before takeover")


def assert_conditional_update_contention_lost(rowcount: int) -> None:
    if rowcount != 0:
        raise AssertionError(
            f"expected conditional-update contention loss (rowcount 0), got {rowcount}"
        )


def wait_at_barrier(
    barrier: Barrier,
    *,
    checkpoint: str,
    timeout: float = DEFAULT_RACE_TIMEOUT_SECONDS,
) -> int:
    """Wait at a deterministic race checkpoint and fail with useful context."""

    try:
        return barrier.wait(timeout=timeout)
    except BrokenBarrierError as exc:
        raise AssertionError(f"concurrency barrier broke at {checkpoint}") from exc


def run_concurrent_workers(
    worker_count: int,
    worker: Callable[[int, Barrier], T],
    *,
    timeout: float = DEFAULT_RACE_TIMEOUT_SECONDS,
) -> list[T]:
    """Run workers in distinct threads and release them through one shared barrier."""

    barrier = Barrier(worker_count)
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures: list[Future[T]] = [
            pool.submit(worker, index, barrier) for index in range(worker_count)
        ]
        return [future.result(timeout=timeout) for future in futures]
