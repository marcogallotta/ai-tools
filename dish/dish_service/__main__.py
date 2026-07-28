"""Run the laptop-hosted dish service."""
from __future__ import annotations

import argparse
import logging
import queue
import signal
import threading
from collections.abc import Sequence
from types import FrameType
from typing import Any

from .application import DishService
from .config import ServiceConfig
from .database_ownership import ServiceDatabaseOwnership, service_process_lock_path
from .http import DishHTTPServer, build_action_server, build_private_server
from .process_lock import ServiceProcessLock
from dish_tool.errors import DishRuleError

LOG = logging.getLogger("dish.service")


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="dish-service",
        description=(
            "Run the single-process Dish HTTP service. Runtime configuration is read "
            "from the service environment; see deploy/systemd/service.env.example."
        ),
    )


def _serve(
    server: DishHTTPServer,
    *,
    name: str,
    stop_event: threading.Event,
    failures: "queue.SimpleQueue[tuple[str, BaseException]]",
) -> None:
    try:
        server.serve_forever()
        if not stop_event.is_set():
            failures.put((name, RuntimeError(f"{name} listener stopped unexpectedly")))
            stop_event.set()
    except BaseException as exc:
        failures.put((name, exc))
        stop_event.set()


def _shutdown_servers(
    servers: Sequence[DishHTTPServer],
    threads: Sequence[threading.Thread],
    *,
    started_count: int | None = None,
) -> None:
    count = len(threads) if started_count is None else started_count
    # Close admission on both surfaces before waiting for either serve loop.
    # Existing handlers that already crossed admission still drain below.
    for server in servers:
        stop_accepting = getattr(server, "stop_accepting", None)
        if stop_accepting is not None:
            stop_accepting()
    # shutdown deadlocks when serve_forever never started, so call it only for
    # listeners whose threads were successfully launched.
    for server in servers[:count]:
        server.shutdown()
    # Every bound listener still owns a socket, including one whose thread did
    # not start. Close all of them, then join only launched threads.
    for server in servers:
        server.server_close()
    for thread in threads[:count]:
        thread.join()


def _run_servers(
    private_server: DishHTTPServer,
    action_server: DishHTTPServer,
    *,
    stop_event: threading.Event | None = None,
) -> int:
    stop = stop_event or threading.Event()
    failures: "queue.SimpleQueue[tuple[str, BaseException]]" = queue.SimpleQueue()
    servers = (private_server, action_server)
    for server in servers:
        attach_stop_event = getattr(server, "attach_stop_event", None)
        if attach_stop_event is not None:
            attach_stop_event(stop)
    threads = tuple(
        threading.Thread(
            target=_serve,
            kwargs={
                "server": server,
                "name": name,
                "stop_event": stop,
                "failures": failures,
            },
            name=f"dish-{name}-http",
            daemon=False,
        )
        for name, server in (("private", private_server), ("action", action_server))
    )
    started_count = 0
    try:
        for thread in threads:
            thread.start()
            started_count += 1
    except BaseException as exc:
        failures.put(("startup", exc))
        stop.set()
    try:
        if started_count == len(threads):
            stop.wait()
    except KeyboardInterrupt:
        stop.set()
    finally:
        _shutdown_servers(servers, threads, started_count=started_count)

    if failures.empty():
        return 0
    name, failure = failures.get()
    LOG.error(
        "listener_stopped name=%s error_type=%s detail=%s",
        name,
        type(failure).__name__,
        failure,
    )
    return 1


def _signal_handler(stop_event: threading.Event):
    def handle(signum: int, _frame: FrameType | None) -> None:
        # Admission closes before logging so a slow handler cannot extend the
        # window in which a request is allowed to start.
        stop_event.set()
        LOG.info("shutdown_requested signal=%s", signal.Signals(signum).name)

    return handle


def _build_servers(service: DishService) -> tuple[DishHTTPServer, DishHTTPServer]:
    private_server = build_private_server(service)
    try:
        action_server = build_action_server(service)
    except Exception:
        private_server.server_close()
        raise
    return private_server, action_server


def _run_configured_service(config: ServiceConfig) -> int:
    lock_path = service_process_lock_path(config.db_path)
    with ServiceProcessLock(lock_path):
        ServiceDatabaseOwnership(config.db_path).mark()
        service = DishService(config)
        startup = service.startup_check()
        if not startup.get("startup_ready", startup["ok"]):
            raise RuntimeError("dish service startup validation failed")
        if not startup["asana"]["ok"]:
            LOG.warning(
                "Asana health check failed; mutation endpoints will remain fail-closed"
            )

        private_server, action_server = _build_servers(service)

        stop_event = threading.Event()
        handler = _signal_handler(stop_event)
        previous: dict[int, Any] = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, handler)
        try:
            return _run_servers(
                private_server,
                action_server,
                stop_event=stop_event,
            )
        finally:
            for signum, prior in previous.items():
                signal.signal(signum, prior)


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = ServiceConfig.from_env()
        config.validate_runtime(require_action=True)
        return _run_configured_service(config)
    except DishRuleError as exc:
        LOG.error(
            "startup_failed rule=%s detail=%s",
            exc.rule or "dish_rule_error",
            exc,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
