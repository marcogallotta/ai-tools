"""SQLite connection, migration, and validation orchestration."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from pathlib import Path

from .constants import DEFAULT_DB_PATH
from .database_migrations import migrate_database
from .database_schema_validation import validate_current_database, validate_runtime_schema_state
from .errors import DishRuleError
from .transactions import read_transaction


def _backup_legacy_database(db_path: Path) -> None:
    """Keep one transactionally complete legacy snapshot before migration.

    Copying only the main SQLite file can omit committed pages still resident in
    a WAL file. Build the legacy backup through SQLite's online backup API and
    replace any earlier incomplete artifact while the live database is still on
    a pre-redesign schema.
    """

    if not db_path.exists() or str(db_path) == ":memory:":
        return
    backup = db_path.with_suffix(db_path.suffix + ".legacy-v2.bak")
    source = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    source.row_factory = sqlite3.Row
    temp_path: Path | None = None
    try:
        # Keep the schema-version observation and the online-backup source on
        # one SQLite snapshot. In WAL mode another initializer may migrate the
        # live file after this read; without the read transaction the backup
        # API can then copy the newer schema while ``version`` still describes
        # the legacy one.
        with read_transaction(source):
            version = int(source.execute("PRAGMA user_version").fetchone()[0])
            if version >= 3:
                return
            with tempfile.NamedTemporaryFile(
                dir=backup.parent,
                prefix=f".{backup.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
            target = sqlite3.connect(str(temp_path), timeout=30, isolation_level=None)
            try:
                source.backup(target)
                target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                target.close()
        check = sqlite3.connect(str(temp_path), timeout=30, isolation_level=None)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("legacy backup integrity check failed")
            if int(check.execute("PRAGMA user_version").fetchone()[0]) != version:
                raise sqlite3.DatabaseError("legacy backup schema version mismatch")
        finally:
            check.close()
        os.replace(temp_path, backup)
        temp_path = None
    finally:
        source.close()
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


WAL_BUSY_TIMEOUT_MS = 100
WAL_RETRY_ATTEMPTS = 20
WAL_RETRY_SLEEP_BASE_SECONDS = 0.01
WAL_RETRY_SLEEP_CAP_SECONDS = 0.1
MIGRATION_BUSY_TIMEOUT_MS = 2000
RUNTIME_BUSY_TIMEOUT_MS = 30000


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
