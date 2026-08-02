"""Deterministic helpers for native PostgreSQL concurrency certification tests."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError
from typing import Callable, TypeVar

T = TypeVar("T")

DEFAULT_RACE_TIMEOUT_SECONDS = 30.0


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
