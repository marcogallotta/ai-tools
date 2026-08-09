from __future__ import annotations

from sqlalchemy import inspect

from dish_pg.release import ALEMBIC_HEAD


def _table_names(database) -> set[str]:
    return database.read(lambda connection: set(inspect(connection).get_table_names()))


def test_frontend_security_migration_upgrades_0032_with_support_tables(
    sqlite_migration_database,
) -> None:
    sqlite_migration_database.initialize("0032_imported_operation_history")
    before = _table_names(sqlite_migration_database)
    assert not any(name.startswith("frontend_") for name in before)

    sqlite_migration_database.upgrade(ALEMBIC_HEAD)
    sqlite_migration_database.assert_revision(ALEMBIC_HEAD)

    assert {
        "frontend_security_state",
        "frontend_sessions",
        "frontend_login_events",
        "frontend_security_audit",
    } <= _table_names(sqlite_migration_database)
    columns = sqlite_migration_database.read(
        lambda connection: {column["name"] for column in inspect(connection).get_columns("frontend_login_events")}
    )
    assert {"peer_blocked_until", "global_blocked_until"} <= columns
