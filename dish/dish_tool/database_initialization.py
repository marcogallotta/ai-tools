"""SQLite connection, migration, and validation orchestration."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from .database_schema import (
    DEFAULT_DB_PATH,
    MIGRATION_BUSY_TIMEOUT_MS,
    RUNTIME_BUSY_TIMEOUT_MS,
    WAL_BUSY_TIMEOUT_MS,
    WAL_RETRY_ATTEMPTS,
    WAL_RETRY_SLEEP_BASE_SECONDS,
    WAL_RETRY_SLEEP_CAP_SECONDS,
    _backup_legacy_database,
    migrate_database,
)
from .database_schema_validation import validate_current_database, validate_runtime_schema_state
from .errors import DishRuleError


def _open_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {RUNTIME_BUSY_TIMEOUT_MS}")
    return conn


def _open_migrated_database(path: str | os.PathLike[str]) -> sqlite3.Connection:
    db_path = Path(path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _backup_legacy_database(db_path)
    conn = _open_connection(db_path)
    conn.execute(f"PRAGMA busy_timeout = {WAL_BUSY_TIMEOUT_MS}")
    journal_exc: sqlite3.OperationalError | None = None
    for attempt in range(WAL_RETRY_ATTEMPTS):
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            journal_exc = None
            break
        except sqlite3.OperationalError as exc:
            text = str(exc).lower()
            if "locked" not in text and "busy" not in text:
                conn.close()
                raise
            journal_exc = exc
            time.sleep(
                min(
                    WAL_RETRY_SLEEP_BASE_SECONDS * (attempt + 1),
                    WAL_RETRY_SLEEP_CAP_SECONDS,
                )
            )
    if journal_exc is not None:
        conn.close()
        raise DishRuleError(
            "BACKEND_REJECTED",
            "database journal mode could not be established while another reader holds the file",
            rule="database_reader_lock",
            retryable=True,
        ) from journal_exc
    conn.execute(f"PRAGMA busy_timeout = {MIGRATION_BUSY_TIMEOUT_MS}")
    try:
        migrate_database(conn)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            conn.close()
            raise DishRuleError(
                "BACKEND_REJECTED",
                "database initialization is blocked by another writer",
                rule="database_writer_lock",
                retryable=True,
                details={"timeout_ms": MIGRATION_BUSY_TIMEOUT_MS},
            ) from exc
        conn.close()
        raise
    conn.execute(f"PRAGMA busy_timeout = {RUNTIME_BUSY_TIMEOUT_MS}")
    return conn


def initialize_database(
    path: str | os.PathLike[str] = DEFAULT_DB_PATH,
) -> sqlite3.Connection:
    """Open, migrate, and perform the complete historical semantic audit."""

    conn = _open_migrated_database(path)
    try:
        validate_current_database(conn)
    except Exception:
        conn.close()
        raise
    return conn


def open_runtime_database(
    path: str | os.PathLike[str] = DEFAULT_DB_PATH,
) -> sqlite3.Connection:
    """Open and migrate a request connection with bounded schema validation.

    Request handlers still perform exact workflow authority checks for the rows
    they consume. The full append-history semantic audit remains a startup,
    health, administrative, and explicit diagnostic responsibility.
    """

    db_path = Path(path).expanduser()
    if not db_path.is_file():
        return initialize_database(db_path)
    conn = _open_connection(db_path)
    try:
        validate_runtime_schema_state(conn)
    except DishRuleError as exc:
        conn.close()
        if exc.rule == "database_schema_not_current":
            return initialize_database(db_path)
        raise
    except Exception:
        conn.close()
        raise
    return conn
