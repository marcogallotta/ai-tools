"""Database initialization separates bounded request setup from full audits."""

from __future__ import annotations

from dish_tool import database_initialization


def test_runtime_open_uses_schema_validation_without_full_history_scan(monkeypatch, tmp_path):
    calls: list[str] = []
    original_schema = database_initialization.validate_runtime_schema_state

    def schema(conn):
        calls.append("schema")
        return original_schema(conn)

    def full(_conn):
        calls.append("full")
        raise AssertionError("runtime request must not scan full durable history")

    db_path = tmp_path / "dish.db"
    initialized = database_initialization.initialize_database(db_path)
    initialized.close()
    calls.clear()
    monkeypatch.setattr(database_initialization, "validate_runtime_schema_state", schema)
    monkeypatch.setattr(database_initialization, "validate_current_database", full)
    conn = database_initialization.open_runtime_database(db_path)
    conn.close()
    assert calls == ["schema"]


def test_startup_initialization_runs_the_full_semantic_audit(monkeypatch, tmp_path):
    calls: list[str] = []
    original_full = database_initialization.validate_current_database

    def full(conn):
        calls.append("full")
        return original_full(conn)

    monkeypatch.setattr(database_initialization, "validate_current_database", full)
    conn = database_initialization.initialize_database(tmp_path / "dish.db")
    conn.close()
    assert calls == ["full"]


def test_first_runtime_open_bootstraps_then_later_opens_skip_full_audit(monkeypatch, tmp_path):
    db_path = tmp_path / "dish.db"
    calls: list[str] = []
    original_full = database_initialization.validate_current_database

    def full(conn):
        calls.append("full")
        return original_full(conn)

    monkeypatch.setattr(database_initialization, "validate_current_database", full)
    first = database_initialization.open_runtime_database(db_path)
    first.close()
    second = database_initialization.open_runtime_database(db_path)
    second.close()
    assert calls == ["full"]

