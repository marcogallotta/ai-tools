"""SQLite schema inspection and current-database validation."""

from __future__ import annotations

import sqlite3

from .database_migrations import (
    _execute_script_statements,
    _schema_version_state,
    _validate_version_claims,
)
from .database_schema import MIGRATIONS, _validate_semantic_evidence
from .errors import DishRuleError
from .transactions import immediate_transaction, read_transaction


def validate_runtime_schema_state(conn: sqlite3.Connection) -> None:
    """Check only the bounded schema facts required at request admission."""

    with read_transaction(conn):
        current = max(MIGRATIONS)
        _validate_version_claims(conn)
        user_version, ledger_version = _schema_version_state(conn)
        if user_version != current or ledger_version != current:
            raise DishRuleError(
                "VALIDATION_FAILED",
                "database did not converge to the current schema",
                rule="database_schema_not_current",
                details={
                    "user_version": user_version,
                    "ledger_version": ledger_version,
                    "current": current,
                },
            )


def validate_current_schema(conn: sqlite3.Connection) -> None:
    current = max(MIGRATIONS)
    _validate_version_claims(conn)
    user_version, ledger_version = _schema_version_state(conn)
    if user_version != current or ledger_version != current:
        raise DishRuleError("VALIDATION_FAILED", "database did not converge to the current schema", rule="database_schema_not_current", details={"user_version": user_version, "ledger_version": ledger_version, "current": current})
    required = {"operations", "operation_steps", "operation_actor_facts", "verification_cycles", "write_attempts", "movement_attempts", "task_content_state", "content_versions", "audit_events", "marco_authorizations", "service_leases", "service_requests", "operation_execution_claims", "operation_executions", "dish_inspect_facts", "planning_reopen_attempts", "backup_creations", "abandonment_attempts", "operation_successions", "safe_reclaims", "planning_intent_challenges", "kill_request_bindings"}
    actual = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = sorted(required - actual)
    if missing:
        raise DishRuleError("VALIDATION_FAILED", "current-version database is missing required tables", rule="database_schema_incomplete", details={"missing_tables": missing})
    expected = _canonical_schema_manifest()
    actual_manifest = _schema_manifest(conn)
    if actual_manifest != expected:
        missing_objects = sorted(set(expected) - set(actual_manifest))
        extra_objects = sorted(set(actual_manifest) - set(expected))
        altered_objects = sorted(name for name in set(expected) & set(actual_manifest) if expected[name] != actual_manifest[name])
        raise DishRuleError(
            "VALIDATION_FAILED",
            "current-version database schema does not match the canonical release schema",
            rule="database_schema_signature_mismatch",
            details={"missing_objects": missing_objects, "extra_objects": extra_objects, "altered_objects": altered_objects},
        )



def validate_current_database(conn: sqlite3.Connection) -> None:
    """Validate the canonical schema and all durable historical relationships."""

    validate_current_schema(conn)
    _validate_semantic_evidence(conn)


def _normalized_schema_sql(sql: str | None) -> str:
    import re
    text = " ".join((sql or "").split())
    text = re.sub(r"\s*([(),])\s*", r"\1", text)
    return text


def _schema_manifest(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        """SELECT type, name, sql FROM sqlite_master
           WHERE type IN ('table','index','trigger')
             AND name NOT LIKE 'sqlite_%'
           ORDER BY type, name"""
    )
    return {f"{row[0]}:{row[1]}": _normalized_schema_sql(row[2]) for row in rows}


_CANONICAL_SCHEMA_MANIFEST: dict[str, str] | None = None


def _canonical_schema_manifest() -> dict[str, str]:
    global _CANONICAL_SCHEMA_MANIFEST
    if _CANONICAL_SCHEMA_MANIFEST is None:
        probe = sqlite3.connect(":memory:", isolation_level=None)
        try:
            probe.execute("PRAGMA foreign_keys = ON")
            with immediate_transaction(probe, "build_canonical_schema"):
                probe.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
                for version in sorted(MIGRATIONS):
                    _execute_script_statements(probe, MIGRATIONS[version])
                    probe.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'canonical')", (version,))
                    probe.execute(f"PRAGMA user_version = {version}")
            _CANONICAL_SCHEMA_MANIFEST = _schema_manifest(probe)
        finally:
            probe.close()
    return dict(_CANONICAL_SCHEMA_MANIFEST)
