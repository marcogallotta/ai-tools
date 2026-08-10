from __future__ import annotations

from pathlib import Path
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from dish_pg.production_reset import (
    recreate_database,
    restore_database_access,
    snapshot_database_state,
)

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def _q(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def test_native_full_reset_terminates_blocker_and_restores_non_owner_access(
    native_migration_database,
) -> None:
    base = native_migration_database
    base_url = make_url(base.sqlalchemy_url)
    suffix = uuid.uuid4().hex[:8]
    database_name = f"dish_reset_{suffix}"
    observer = f"dish_observer_{suffix}"
    writer = f"dish_writer_{suffix}"

    admin_engine = base.create_engine()
    target_engine = None
    blocker_engine = None
    blocker_connection = None
    try:
        with admin_engine.connect() as raw_connection:
            connection = raw_connection.execution_options(isolation_level="AUTOCOMMIT")
            owner = str(connection.execute(text("SELECT current_user")).scalar_one())
            connection.exec_driver_sql(f"CREATE ROLE {_q(observer)} NOLOGIN")
            connection.exec_driver_sql(f"CREATE ROLE {_q(writer)} NOLOGIN")
            connection.exec_driver_sql(
                f"CREATE DATABASE {_q(database_name)} OWNER {_q(owner)} TEMPLATE template0"
            )

        database_url = base_url.set(database=database_name).render_as_string(
            hide_password=False
        )
        target_engine = create_engine(database_url)
        with target_engine.begin() as connection:
            connection.exec_driver_sql("CREATE SCHEMA app")
            connection.exec_driver_sql(
                "CREATE TABLE app.items (id bigint PRIMARY KEY, payload text, private_note text)"
            )
            connection.exec_driver_sql("CREATE SEQUENCE app.item_seq")
            connection.exec_driver_sql(
                f"REVOKE ALL PRIVILEGES ON DATABASE {_q(database_name)} FROM PUBLIC"
            )
            connection.exec_driver_sql(
                f"GRANT CONNECT ON DATABASE {_q(database_name)} TO {_q(observer)}"
            )
            connection.exec_driver_sql(
                f"GRANT CONNECT ON DATABASE {_q(database_name)} TO {_q(writer)}"
            )
            connection.exec_driver_sql(
                f"GRANT USAGE ON SCHEMA app TO {_q(observer)}, {_q(writer)}"
            )
            connection.exec_driver_sql(
                f"GRANT SELECT ON TABLE app.items TO {_q(observer)}"
            )
            connection.exec_driver_sql(
                f"GRANT UPDATE ON TABLE app.items TO {_q(writer)}"
            )
            connection.exec_driver_sql(
                f"GRANT SELECT (private_note) ON TABLE app.items TO {_q(writer)}"
            )
            connection.exec_driver_sql(
                f"GRANT USAGE, SELECT ON SEQUENCE app.item_seq TO {_q(observer)}"
            )
            connection.exec_driver_sql(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {_q(owner)} IN SCHEMA app "
                f"GRANT SELECT ON TABLES TO {_q(observer)}"
            )
            connection.exec_driver_sql(
                f"ALTER ROLE {_q(observer)} IN DATABASE {_q(database_name)} "
                "SET default_transaction_read_only TO 'on'"
            )

        snapshot = snapshot_database_state(database_url, database_name)
        assert any(
            grant.grantee == observer
            and grant.object_type == "TABLE"
            and grant.privilege == "SELECT"
            for grant in snapshot.object_grants
        )
        assert any(
            setting.role_name == observer
            and setting.name == "default_transaction_read_only"
            and setting.value == "on"
            for setting in snapshot.settings
        )

        blocker_engine = create_engine(database_url)
        blocker_connection = blocker_engine.connect()
        blocker_pid = int(
            blocker_connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
        )
        messages: list[str] = []
        recreate_database(database_url, snapshot, log=messages.append)
        assert any(f"pid={blocker_pid}" in message for message in messages)
        assert any("all blocking sessions are gone" in message for message in messages)

        target_engine.dispose()
        target_engine = create_engine(database_url)
        with target_engine.begin() as connection:
            connection.exec_driver_sql("CREATE SCHEMA app")
            connection.exec_driver_sql(
                "CREATE TABLE app.items (id bigint PRIMARY KEY, payload text, private_note text)"
            )
            connection.exec_driver_sql("CREATE SEQUENCE app.item_seq")

        restore_database_access(database_url, snapshot)

        with target_engine.begin() as connection:
            assert connection.execute(
                text(
                    "SELECT has_database_privilege(:role, :database, 'CONNECT')"
                ),
                {"role": observer, "database": database_name},
            ).scalar_one()
            assert connection.execute(
                text("SELECT has_schema_privilege(:role, 'app', 'USAGE')"),
                {"role": observer},
            ).scalar_one()
            assert connection.execute(
                text("SELECT has_table_privilege(:role, 'app.items', 'SELECT')"),
                {"role": observer},
            ).scalar_one()
            assert connection.execute(
                text("SELECT has_table_privilege(:role, 'app.items', 'UPDATE')"),
                {"role": writer},
            ).scalar_one()
            assert connection.execute(
                text(
                    "SELECT has_column_privilege(:role, 'app.items', 'private_note', 'SELECT')"
                ),
                {"role": writer},
            ).scalar_one()
            assert connection.execute(
                text("SELECT has_sequence_privilege(:role, 'app.item_seq', 'USAGE')"),
                {"role": observer},
            ).scalar_one()
            setting = connection.execute(
                text(
                    """
                    SELECT setting
                    FROM pg_db_role_setting AS role_setting
                    JOIN pg_roles AS role ON role.oid = role_setting.setrole
                    CROSS JOIN LATERAL unnest(role_setting.setconfig) AS setting
                    WHERE role.rolname = :role
                      AND role_setting.setdatabase = (
                          SELECT oid FROM pg_database WHERE datname = :database
                      )
                    """
                ),
                {"role": observer, "database": database_name},
            ).scalar_one()
            assert setting == "default_transaction_read_only=on"

            connection.exec_driver_sql(
                "CREATE TABLE app.future_items (id bigint PRIMARY KEY)"
            )
            assert connection.execute(
                text(
                    "SELECT has_table_privilege(:role, 'app.future_items', 'SELECT')"
                ),
                {"role": observer},
            ).scalar_one()
    finally:
        if blocker_connection is not None:
            try:
                blocker_connection.close()
            except DBAPIError:
                pass
        if blocker_engine is not None:
            blocker_engine.dispose()
        if target_engine is not None:
            target_engine.dispose()
        try:
            with admin_engine.connect() as raw_connection:
                connection = raw_connection.execution_options(isolation_level="AUTOCOMMIT")
                connection.exec_driver_sql(
                    f"DROP DATABASE IF EXISTS {_q(database_name)} WITH (FORCE)"
                )
                connection.exec_driver_sql(f"DROP ROLE IF EXISTS {_q(observer)}")
                connection.exec_driver_sql(f"DROP ROLE IF EXISTS {_q(writer)}")
        finally:
            admin_engine.dispose()
