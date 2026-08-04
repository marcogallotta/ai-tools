"""PGlite runtime helpers for PostgreSQL-semantic development tests.

PGlite is a fast compatibility lane, not native PostgreSQL certification.
"""
from __future__ import annotations

import os
import selectors
import socket
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator, Protocol

import psycopg
from py_pglite import PGliteConfig, PGliteManager
from py_pglite.utils import find_pglite_modules

ROOT = Path(__file__).resolve().parents[3]
PGLITE_SUPPORT = ROOT / "tests" / "postgresql" / "pglite"
NODE_MODULES = PGLITE_SUPPORT / "node_modules"
PGLITE_STARTUP_ATTEMPTS = 3
PGLITE_READINESS_PROBES = 2
PGLITE_MAX_CONNECTIONS = 16
_RETRYABLE_STARTUP_SIGNATURES = (
    "pglite process died during startup",
    "pglite server failed to start within",
    "server closed the connection unexpectedly",
    "connection refused",
    "connection reset",
    "broken pipe",
)


@dataclass(frozen=True)
class PGliteRuntime:
    libpq_dsn: str
    sqlalchemy_url: str


class PGliteLifecycleError(RuntimeError):
    """The embedded server failed before a stable SQL session existed."""


class DishPGliteManager(PGliteManager):
    """Start PGlite without py-pglite's destructive raw-TCP probe."""

    def _generate_tcp_js_content(
        self, ext_requires_str: str, extensions_obj_str: str
    ) -> str:
        source = super()._generate_tcp_js_content(
            ext_requires_str, extensions_obj_str
        )
        server_database = "            db,\n"
        if source.count(server_database) != 1:
            raise RuntimeError(
                "py-pglite socket launcher shape changed; cannot set connection capacity"
            )
        return source.replace(
            server_database,
            server_database
            + f"            maxConnections: {PGLITE_MAX_CONNECTIONS},\n",
            1,
        )

    def start(self) -> None:
        """Wait for the launcher's ready line instead of opening a raw socket."""

        if self.process is not None:
            raise RuntimeError("PGlite process already running")

        self.work_dir = self._setup_work_dir()
        self._kill_existing_processes()
        self._cleanup_socket()
        self._original_cwd = os.getcwd()
        os.chdir(self.work_dir)
        try:
            self._install_dependencies(self.work_dir)
            env = os.environ.copy()
            if self.config.node_options:
                env["NODE_OPTIONS"] = self.config.node_options
            node_modules_path = find_pglite_modules(self.work_dir)
            if node_modules_path:
                env["NODE_PATH"] = str(node_modules_path)

            self.process = subprocess.Popen(
                ["node", "pglite_manager.js"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )
            self._wait_for_launcher_ready()
        finally:
            os.chdir(self._original_cwd)

    def _wait_for_launcher_ready(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            raise RuntimeError("PGlite launcher process was not created")

        ready_line = (
            f"Server started on TCP {self.config.tcp_host}:{self.config.tcp_port}"
        )
        deadline = time.monotonic() + self.config.timeout
        output: list[str] = []
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while True:
                exit_code = process.poll()
                if exit_code is not None:
                    remainder = process.stdout.read()
                    if remainder:
                        output.append(remainder)
                    raise RuntimeError(
                        "PGlite process died during startup with code "
                        f"{exit_code}. Output: {''.join(output)[-2000:]}"
                    )

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        "PGlite server failed to start within "
                        f"{self.config.timeout} seconds. Output: "
                        f"{''.join(output)[-2000:]}"
                    )

                events = selector.select(timeout=min(remaining, 0.25))
                if not events:
                    continue
                line = process.stdout.readline()
                if not line:
                    continue
                output.append(line)
                if ready_line in line:
                    return
        finally:
            selector.close()


class _Process(Protocol):
    def poll(self) -> int | None: ...


class _Manager(Protocol):
    process: _Process | None


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _sqlalchemy_url(libpq_dsn: str) -> str:
    values = dict(part.split("=", 1) for part in libpq_dsn.split())
    return (
        "postgresql+psycopg://"
        f"{values['user']}:{values['password']}@{values['host']}:{values['port']}/"
        f"{values['dbname']}?sslmode={values.get('sslmode', 'disable')}"
    )


def _process_exit_code(manager: _Manager) -> int | None:
    process = manager.process
    if process is None:
        return -1
    return process.poll()


def _verify_sql_readiness(manager: _Manager, dsn: str) -> None:
    """Prove readiness with independent PostgreSQL protocol connections."""

    for probe_number in range(1, PGLITE_READINESS_PROBES + 1):
        exit_code = _process_exit_code(manager)
        if exit_code is not None:
            raise PGliteLifecycleError(
                "PGlite process exited before SQL readiness "
                f"probe {probe_number} with code {exit_code}"
            )
        try:
            with psycopg.connect(
                dsn,
                autocommit=True,
                connect_timeout=3,
                prepare_threshold=None,
            ) as connection:
                row = connection.execute("SELECT 1, version()").fetchone()
        except psycopg.OperationalError as exc:
            raise PGliteLifecycleError(
                f"PGlite SQL readiness probe {probe_number} failed: {exc}"
            ) from exc
        if row is None or row[0] != 1 or "PostgreSQL" not in str(row[1]):
            raise RuntimeError(
                f"PGlite SQL readiness probe {probe_number} returned {row!r}"
            )

    exit_code = _process_exit_code(manager)
    if exit_code is not None:
        raise PGliteLifecycleError(
            "PGlite process exited immediately after SQL readiness "
            f"with code {exit_code}"
        )


def _is_retryable_startup_error(exc: BaseException) -> bool:
    if isinstance(exc, PGliteLifecycleError):
        return True
    detail = str(exc).lower()
    return any(signature in detail for signature in _RETRYABLE_STARTUP_SIGNATURES)


@contextmanager
def pglite_runtime() -> Iterator[PGliteRuntime]:
    """Start an isolated, SQL-verified PGlite instance over loopback TCP.

    Only startup may be retried. Once the fixture yields, test-body exceptions are
    never caught or rerun.
    """

    if not NODE_MODULES.is_dir():
        raise RuntimeError(
            "PGlite node_modules are unavailable; restore tests/postgresql/pglite/node_modules"
        )

    last_error: BaseException | None = None
    for attempt in range(1, PGLITE_STARTUP_ATTEMPTS + 1):
        with TemporaryDirectory(prefix="dish-pglite-") as temp:
            work_dir = Path(temp)
            (work_dir / "node_modules").symlink_to(
                NODE_MODULES, target_is_directory=True
            )
            config = PGliteConfig(
                work_dir=work_dir,
                use_tcp=True,
                tcp_host="127.0.0.1",
                tcp_port=_free_tcp_port(),
            )
            manager = DishPGliteManager(config)
            try:
                manager.start()
                dsn = manager.get_dsn()
                _verify_sql_readiness(manager, dsn)
            except Exception as exc:
                manager.stop()
                if not _is_retryable_startup_error(exc):
                    raise
                last_error = exc
                if attempt == PGLITE_STARTUP_ATTEMPTS:
                    raise RuntimeError(
                        "PGlite failed SQL-verified startup after "
                        f"{PGLITE_STARTUP_ATTEMPTS} fresh attempts"
                    ) from exc
                continue

            try:
                yield PGliteRuntime(
                    libpq_dsn=dsn,
                    sqlalchemy_url=_sqlalchemy_url(dsn),
                )
            finally:
                manager.stop()
            return

    raise RuntimeError("PGlite startup attempts exhausted") from last_error
