"""PGlite runtime helpers for PostgreSQL-semantic development tests.

PGlite is a fast compatibility lane, not native PostgreSQL certification.
"""
from __future__ import annotations

import socket
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator

import psycopg
from py_pglite import PGliteConfig, PGliteManager

ROOT = Path(__file__).resolve().parents[3]
PGLITE_SUPPORT = ROOT / "tests" / "postgresql" / "pglite"
NODE_MODULES = PGLITE_SUPPORT / "node_modules"
_SQL_READINESS_PROBES = 2
_SQL_READINESS_TIMEOUT_SECONDS = 10.0
_PGLITE_MAX_CONNECTIONS = 16


class _DishPGliteManager(PGliteManager):
    """Raise the socket wrapper's one-client default for rapid test reconnects."""

    def _generate_tcp_js_content(
        self, ext_requires_str: str, extensions_obj_str: str
    ) -> str:
        content = super()._generate_tcp_js_content(
            ext_requires_str, extensions_obj_str
        )
        needle = f"port: {self.config.tcp_port}\n"
        replacement = (
            f"port: {self.config.tcp_port},\n"
            f"                        maxConnections: {_PGLITE_MAX_CONNECTIONS}\n"
        )
        if needle not in content:
            raise RuntimeError("py-pglite TCP server template changed unexpectedly")
        return content.replace(needle, replacement, 1)


@dataclass(frozen=True)
class PGliteRuntime:
    libpq_dsn: str
    sqlalchemy_url: str


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


def _prove_sql_readiness(libpq_dsn: str) -> None:
    """Require two independent PostgreSQL handshakes after raw TCP readiness."""

    for probe in range(_SQL_READINESS_PROBES):
        deadline = time.monotonic() + _SQL_READINESS_TIMEOUT_SECONDS
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(libpq_dsn, connect_timeout=2) as connection:
                    row = connection.execute("SELECT 1, version()").fetchone()
                if row is None or int(row[0]) != 1 or "postgresql" not in str(row[1]).lower():
                    raise RuntimeError(f"unexpected PGlite readiness row: {row!r}")
                break
            except (psycopg.OperationalError, RuntimeError) as exc:
                last_error = exc
                time.sleep(0.1)
        else:
            raise RuntimeError(
                f"PGlite SQL readiness probe {probe + 1} failed: "
                f"{type(last_error).__name__}: {last_error}"
            ) from last_error


@contextmanager
def pglite_runtime() -> Iterator[PGliteRuntime]:
    """Start one isolated PGlite instance and prove PostgreSQL query readiness."""
    if not NODE_MODULES.is_dir():
        raise RuntimeError(
            "PGlite node_modules are unavailable; restore tests/postgresql/pglite/node_modules"
        )
    with TemporaryDirectory(prefix="dish-pglite-") as temp:
        work_dir = Path(temp)
        (work_dir / "node_modules").symlink_to(NODE_MODULES, target_is_directory=True)
        config = PGliteConfig(
            work_dir=work_dir,
            use_tcp=True,
            tcp_host="127.0.0.1",
            tcp_port=_free_tcp_port(),
        )
        with _DishPGliteManager(config) as manager:
            dsn = manager.get_dsn()
            _prove_sql_readiness(dsn)
            yield PGliteRuntime(libpq_dsn=dsn, sqlalchemy_url=_sqlalchemy_url(dsn))
