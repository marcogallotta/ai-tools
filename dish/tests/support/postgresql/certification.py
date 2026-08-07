"""Native PostgreSQL certification diagnostics shared by pytest and lane scripts."""
from __future__ import annotations

import ast
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

DEFAULT_POSTGRESQL_DSN = (
    "postgresql+psycopg://dish:dish@127.0.0.1:55432/dish_stage_a_test"
)
NATIVE_POSTGRESQL_UNAVAILABLE = (
    "native PostgreSQL unavailable: rerun with --postgresql and an isolated "
    "DISH_TEST_POSTGRESQL_DSN; SQLite and PGlite are not certification substitutes"
)
_NON_NATIVE_SERVER_TOKENS = ("pglite", "emscripten", "webassembly", "wasm32")


class NativePostgreSQLUnavailable(RuntimeError):
    """The configured target is unreachable or is not a native PostgreSQL server."""


@dataclass(frozen=True)
class NativePostgreSQLIdentity:
    dialect: str
    driver: str
    database: str
    server_version: str
    server_version_full: str
    server_address: str | None
    server_port: int | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def postgresql_dsn() -> str:
    return os.environ.get("DISH_TEST_POSTGRESQL_DSN", DEFAULT_POSTGRESQL_DSN)


def redacted_dsn(dsn: str) -> str:
    """Return a report-safe DSN without credentials or query secrets."""

    url = make_url(dsn)
    return url.render_as_string(hide_password=True)


def probe_native_postgresql(dsn: str | None = None) -> NativePostgreSQLIdentity:
    """Connect once and prove the target is native PostgreSQL, not SQLite/PGlite."""

    target = dsn or postgresql_dsn()
    engine = create_engine(target, future=True, pool_pre_ping=True)
    try:
        if engine.dialect.name != "postgresql":
            raise NativePostgreSQLUnavailable(
                f"configured dialect is {engine.dialect.name!r}, expected 'postgresql'"
            )
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT current_database() AS database,
                           current_setting('server_version') AS server_version,
                           version() AS server_version_full,
                           inet_server_addr()::text AS server_address,
                           inet_server_port() AS server_port
                    """
                )
            ).mappings().one()
        full = str(row["server_version_full"])
        lowered = full.lower()
        token = next((value for value in _NON_NATIVE_SERVER_TOKENS if value in lowered), None)
        if token is not None:
            raise NativePostgreSQLUnavailable(
                f"server identity contains non-native token {token!r}: {full}"
            )
        return NativePostgreSQLIdentity(
            dialect=engine.dialect.name,
            driver=engine.dialect.driver,
            database=str(row["database"]),
            server_version=str(row["server_version"]),
            server_version_full=full,
            server_address=(
                None if row["server_address"] is None else str(row["server_address"])
            ),
            server_port=(None if row["server_port"] is None else int(row["server_port"])),
        )
    except NativePostgreSQLUnavailable:
        raise
    except Exception as exc:  # connection/driver failures are environment diagnostics
        raise NativePostgreSQLUnavailable(
            f"could not establish native PostgreSQL identity: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        engine.dispose()


def discover_native_postgresql_inventory(root: Path | None = None) -> tuple[str, ...]:
    """Derive the governed native inventory from native test modules.

    Pytest marker selection remains independent: the certification runner compares
    its selected node IDs with this file-derived inventory and fails on drift.
    """

    repo_root = (root or Path(__file__).resolve().parents[3]).resolve()
    native_root = repo_root / "tests" / "postgresql" / "native"
    nodeids: list[str] = []
    for path in sorted(native_root.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(repo_root).as_posix()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "test_"
            ):
                nodeids.append(f"{relative}::{node.name}")
    return tuple(sorted(nodeids))
