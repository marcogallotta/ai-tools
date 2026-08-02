"""PGlite runtime helpers for PostgreSQL-semantic development tests.

PGlite is a fast compatibility lane, not native PostgreSQL certification.
"""
from __future__ import annotations

import socket
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator

from py_pglite import PGliteConfig, PGliteManager

ROOT = Path(__file__).resolve().parents[3]
PGLITE_SUPPORT = ROOT / "tests" / "postgresql" / "pglite"
NODE_MODULES = PGLITE_SUPPORT / "node_modules"


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


@contextmanager
def pglite_runtime() -> Iterator[PGliteRuntime]:
    """Start an isolated PGlite instance over loopback TCP."""
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
        with PGliteManager(config) as manager:
            dsn = manager.get_dsn()
            yield PGliteRuntime(libpq_dsn=dsn, sqlalchemy_url=_sqlalchemy_url(dsn))
