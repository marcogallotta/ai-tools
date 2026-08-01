"""Deterministic thread and HTTP-server lifecycle helpers for tests."""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable
from typing import Any


_FAILURES_ATTRIBUTE = "_dish_test_thread_failures"


def managed_thread(
    target: Callable[..., Any],
    *,
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
    daemon: bool = False,
    name: str | None = None,
) -> threading.Thread:
    """Build an unstarted thread whose uncaught exception is retained."""
    failures: list[tuple[BaseException, str]] = []

    def guarded_target() -> None:
        try:
            target(*args, **(kwargs or {}))
        except BaseException as exc:  # test infrastructure must retain all failures
            failures.append((exc, traceback.format_exc()))

    thread = threading.Thread(target=guarded_target, daemon=daemon, name=name)
    setattr(thread, _FAILURES_ATTRIBUTE, failures)
    return thread


def start_thread(
    target: Callable[..., Any],
    *,
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
    daemon: bool = False,
    name: str | None = None,
) -> threading.Thread:
    """Start a managed thread immediately."""
    thread = managed_thread(
        target, args=args, kwargs=kwargs, daemon=daemon, name=name
    )
    thread.start()
    return thread


def start_server_thread(
    server,
    *,
    daemon: bool = True,
    name: str | None = None,
) -> threading.Thread:
    """Start ``serve_forever`` with uncaught-exception capture."""
    return start_thread(server.serve_forever, daemon=daemon, name=name)


def join_thread(thread: threading.Thread, *, timeout: float) -> None:
    """Join a thread, prove termination, and surface a captured worker failure."""
    thread.join(timeout=timeout)
    assert not thread.is_alive(), (
        f"thread {thread.name!r} did not terminate within {timeout} seconds"
    )
    failures = getattr(thread, _FAILURES_ATTRIBUTE, ())
    if failures:
        exc, rendered = failures[0]
        raise AssertionError(
            f"thread {thread.name!r} raised {type(exc).__name__}: {exc}\n{rendered}"
        ) from exc


def stop_server(server, thread: threading.Thread, *, timeout: float = 2) -> None:
    """Stop an HTTP server, close its socket, and prove its thread terminated."""
    server.shutdown()
    server.server_close()
    join_thread(thread, timeout=timeout)
