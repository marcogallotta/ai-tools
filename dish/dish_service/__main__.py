"""Run the laptop-hosted dish service."""
from __future__ import annotations

import argparse
import logging
import os
import queue
import signal
import threading
import uuid
from collections.abc import Sequence
from pathlib import Path
from types import FrameType
from typing import Any

from .application import DishService
from .config import ServiceConfig
from .database_ownership import ServiceDatabaseOwnership, database_process_lock_path
from .http import DishHTTPServer, build_action_server, build_private_server
from .frontend_private_runtime import FrontendPrivateRuntime
from .frontend_settings import FrontendRuntimeSettings
from .process_lock import DatabaseProcessLock
from .sd_notify import notify as sd_notify
from dish_tool.errors import DishRuleError

LOG = logging.getLogger("dish.service")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dish-service",
        description=(
            "Run the single-process Dish HTTP service. Runtime configuration is read "
            "from the service environment; see deploy/systemd/service.env.example."
        ),
    )
    parser.add_argument(
        "--postgresql-test-runtime",
        action="store_true",
        help="run the existing HTTP service against an explicitly bound TEST PostgreSQL authority",
    )
    parser.add_argument("--database-url")
    parser.add_argument("--expected-database")
    parser.add_argument("--expected-schema-head")
    parser.add_argument("--expected-release")
    parser.add_argument("--expected-generation-id", type=uuid.UUID)
    parser.add_argument("--cursor-secret")
    parser.add_argument("--state-dir", type=Path)
    return parser


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
            sd_notify("READY=1")
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


def _build_servers(service: DishService, *, frontend_runtime=None) -> tuple[DishHTTPServer, DishHTTPServer]:
    private_server = (
        build_private_server(service)
        if frontend_runtime is None
        else build_private_server(service, frontend_runtime=frontend_runtime)
    )
    try:
        action_server = build_action_server(service)
    except Exception:
        private_server.server_close()
        raise
    return private_server, action_server


def _run_configured_service(config: ServiceConfig) -> int:
    lock_path = database_process_lock_path(config.db_path)
    with DatabaseProcessLock(
        lock_path, role="service", rule="service_process_lock_held"
    ):
        ServiceDatabaseOwnership(config.db_path).mark()
        service = DishService(config)
        startup = service.startup_check()
        if not startup.get("startup_ready", startup["ok"]):
            raise RuntimeError("dish service startup validation failed")
        if not startup["asana"]["ok"]:
            LOG.warning(
                "Asana health check failed; mutation endpoints will remain fail-closed"
            )

        frontend_runtime = None
        try:
            frontend_settings = FrontendRuntimeSettings.from_mapping(
                os.environ, dish_root=Path(__file__).resolve().parents[1]
            )
            if frontend_settings.enabled:
                frontend_runtime = FrontendPrivateRuntime(frontend_settings)
                frontend_runtime.startup_check()

            private_server, action_server = _build_servers(
                service, frontend_runtime=frontend_runtime
            )
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
        finally:
            if frontend_runtime is not None:
                frontend_runtime.close()


def _required_postgresql_runtime_argument(args, name: str):
    value = getattr(args, name)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise DishRuleError(
            "INVALID_ARGUMENT",
            f"--{name.replace('_', '-')} is required for the PostgreSQL TEST runtime",
            rule="postgresql_runtime_argument_required",
            details={"argument": name},
        )
    return value


def _postgresql_runtime_config(args) -> ServiceConfig:
    if os.environ.get("DISH_PROFILE", "").strip().lower() != "test":
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "the PostgreSQL rehearsal service requires DISH_PROFILE=test",
            rule="postgresql_runtime_profile_not_test",
        )
    populated_asana = sorted(
        key for key, value in os.environ.items() if "ASANA" in key.upper() and value
    )
    if populated_asana:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "Asana credentials or configuration are reachable in the PostgreSQL rehearsal process",
            rule="postgresql_runtime_asana_environment_reachable",
            details={"environment_keys": populated_asana},
        )
    state_dir = Path(_required_postgresql_runtime_argument(args, "state_dir")).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    config = ServiceConfig(
        db_path=state_dir / "unused-legacy-authority.sqlite3",
        honest_root=state_dir,
        bind_host=os.environ.get("DISH_SERVICE_BIND", "127.0.0.1"),
        port=int(os.environ.get("DISH_SERVICE_PORT", "0")),
        action_bind_host=os.environ.get("DISH_ACTION_BIND", "127.0.0.1"),
        action_port=int(os.environ.get("DISH_ACTION_PORT", "0")),
        agent_token=os.environ.get("DISH_SERVICE_AGENT_TOKEN"),
        admin_token=os.environ.get("DISH_SERVICE_ADMIN_TOKEN"),
        action_token=os.environ.get("DISH_SERVICE_ACTION_TOKEN"),
        legacy_writer_fence_path=None,
    )
    config.validate_runtime(require_action=True)
    return config


def _run_postgresql_test_runtime(args) -> int:
    from dish_pg.postgres_service import PostgresRuntimeService

    config = _postgresql_runtime_config(args)
    expected_database = str(_required_postgresql_runtime_argument(args, "expected_database"))
    if (
        not expected_database.startswith("dish_")
        or not expected_database.endswith("_test")
        or "prod" in expected_database.lower()
        or "production" in expected_database.lower()
    ):
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "expected PostgreSQL database must be a disposable dish_*_test database",
            rule="postgresql_runtime_database_not_disposable",
        )
    cursor_secret = str(_required_postgresql_runtime_argument(args, "cursor_secret")).encode()
    if len(cursor_secret) < 24:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "PostgreSQL cursor secret must contain at least 24 bytes",
            rule="postgresql_runtime_cursor_secret_weak",
        )
    service = PostgresRuntimeService(
        config,
        database_url=str(_required_postgresql_runtime_argument(args, "database_url")),
        cursor_secret=cursor_secret,
        expected_database=expected_database,
        expected_schema_head=str(
            _required_postgresql_runtime_argument(args, "expected_schema_head")
        ),
        expected_release=str(_required_postgresql_runtime_argument(args, "expected_release")),
        expected_generation_id=_required_postgresql_runtime_argument(
            args, "expected_generation_id"
        ),
    )
    try:
        startup = service.startup_check()
        if not startup["ok"] or startup["isolation"]["asana_environment_keys"]:
            raise RuntimeError("PostgreSQL rehearsal service startup validation failed")
        private_server, action_server = _build_servers(service)
        stop_event = threading.Event()
        handler = _signal_handler(stop_event)
        previous: dict[int, Any] = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, handler)
        try:
            return _run_servers(private_server, action_server, stop_event=stop_event)
        finally:
            for signum, prior in previous.items():
                signal.signal(signum, prior)
    finally:
        service.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        if args.postgresql_test_runtime:
            return _run_postgresql_test_runtime(args)
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
