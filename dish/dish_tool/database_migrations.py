"""SQLite schema migration execution and version-ledger checks."""

from __future__ import annotations

import sqlite3

from .database_schema import MIGRATIONS
from .errors import DishRuleError
from .models import utc_now
from .transactions import immediate_transaction


def _execute_script_statements(conn: sqlite3.Connection, script: str) -> None:
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            if sql:
                conn.execute(sql)
            statement = ""
    if statement.strip():
        raise sqlite3.OperationalError("incomplete migration SQL statement")


def _schema_version_state(conn: sqlite3.Connection) -> tuple[int, int | None]:
    try:
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        has_ledger = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone() is not None
        ledger_version = None
        if has_ledger:
            ledger_version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            ledger_version = None if ledger_version is None else int(ledger_version)
        return user_version, ledger_version
    except (sqlite3.DatabaseError, TypeError, ValueError, IndexError) as exc:
        raise DishRuleError(
            "VALIDATION_FAILED", "database migration ledger is malformed",
            rule="database_ledger_malformed",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc


def _validate_version_claims(conn: sqlite3.Connection, *, allow_empty: bool = False) -> None:
    current = max(MIGRATIONS)
    user_version, ledger_version = _schema_version_state(conn)
    if user_version > current:
        raise DishRuleError("VALIDATION_FAILED", "database user_version is newer than this release", rule="database_future_user_version", details={"user_version": user_version, "current": current})
    if ledger_version is not None and ledger_version > current:
        raise DishRuleError("VALIDATION_FAILED", "database migration ledger is newer than this release", rule="database_future_ledger", details={"ledger_version": ledger_version, "current": current})
    if ledger_version is not None and ledger_version != user_version:
        raise DishRuleError("VALIDATION_FAILED", "database migration ledger and user_version disagree", rule="database_version_disagreement", details={"user_version": user_version, "ledger_version": ledger_version})
    if ledger_version is not None:
        ledger_rows = [int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        expected_rows = list(range(1, ledger_version + 1))
        if ledger_rows != expected_rows:
            raise DishRuleError(
                "VALIDATION_FAILED",
                "database migration ledger is not contiguous",
                rule="database_ledger_gap",
                details={"versions": ledger_rows, "expected": expected_rows},
            )
    if not allow_empty and user_version > 0 and ledger_version is None:
        raise DishRuleError("VALIDATION_FAILED", "versioned database is missing its migration ledger", rule="database_ledger_missing", details={"user_version": user_version})


def migrate_database(conn: sqlite3.Connection) -> None:
    # Hold one SQLite write lock across discovery and every migration. This makes
    # concurrent initializers serialize instead of racing on CREATE/ALTER steps.
    with immediate_transaction(conn, "migrate_database"):
        # Validate existing claims before creating or applying anything. A truly
        # empty database is the only permitted ledger-less state.
        existing_tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()[0]
        _validate_version_claims(conn, allow_empty=not bool(existing_tables))
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version INTEGER PRIMARY KEY,
                   applied_at TEXT NOT NULL
               )"""
        )
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        for version in sorted(MIGRATIONS):
            if version in applied:
                continue
            _execute_script_statements(conn, MIGRATIONS[version])
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, utc_now()),
            )
            conn.execute(f"PRAGMA user_version = {version}")
