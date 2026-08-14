from __future__ import annotations

from pathlib import Path
import runpy
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import dish_pg.production_reset as production_reset
from dish_pg.production_reset import (
    RECOVERY_STATE_COMPLETED,
    RECOVERY_STATE_PREPARE_FAILED,
    ProductionResetError,
    ResetRecoveryStore,
    inspect_reset_guard,
    install_reset_guard,
    recreate_database,
    snapshot_database_state,
)

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]
ROOT = Path(__file__).resolve().parents[3]
RESET = ROOT / "scripts/dish-pg-production-reset"


def _q(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _create_database_and_roles(base, *, observer: str, writer: str, database_name: str):
    admin_engine = base.create_engine()
    with admin_engine.connect() as raw:
        connection = raw.execution_options(isolation_level="AUTOCOMMIT")
        owner = str(connection.execute(text("SELECT current_user")).scalar_one())
        connection.exec_driver_sql(f"CREATE ROLE {_q(observer)} NOLOGIN")
        connection.exec_driver_sql(f"CREATE ROLE {_q(writer)} NOLOGIN")
        connection.exec_driver_sql(
            f"CREATE DATABASE {_q(database_name)} OWNER {_q(owner)} TEMPLATE template0"
        )
    url = make_url(base.sqlalchemy_url).set(database=database_name).render_as_string(hide_password=False)
    return admin_engine, owner, url


def _seed_original_access(database_url: str, database_name: str, owner: str, observer: str, writer: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE SCHEMA app")
            connection.exec_driver_sql(
                "CREATE TABLE app.items (id bigint PRIMARY KEY, payload text, private_note text)"
            )
            connection.exec_driver_sql("CREATE SEQUENCE app.item_seq")
            connection.exec_driver_sql(
                f"REVOKE ALL PRIVILEGES ON DATABASE {_q(database_name)} FROM PUBLIC"
            )
            connection.exec_driver_sql(
                f"GRANT CONNECT ON DATABASE {_q(database_name)} TO {_q(observer)}, {_q(writer)}"
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
    finally:
        engine.dispose()


def _rebuild_schema(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE SCHEMA app")
            connection.exec_driver_sql(
                "CREATE TABLE app.items (id bigint PRIMARY KEY, payload text, private_note text)"
            )
            connection.exec_driver_sql("CREATE SEQUENCE app.item_seq")
    finally:
        engine.dispose()


def _cleanup(admin_engine, database_name: str, observer: str, writer: str) -> None:
    try:
        with admin_engine.connect() as raw:
            connection = raw.execution_options(isolation_level="AUTOCOMMIT")
            connection.exec_driver_sql(f"DROP DATABASE IF EXISTS {_q(database_name)} WITH (FORCE)")
            connection.exec_driver_sql(f"DROP ROLE IF EXISTS {_q(observer)}")
            connection.exec_driver_sql(f"DROP ROLE IF EXISTS {_q(writer)}")
    finally:
        admin_engine.dispose()


def test_native_retry_refuses_grant_lost_baseline_and_resume_restores_original_authority(
    native_migration_database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = native_migration_database
    suffix = uuid.uuid4().hex[:8]
    database_name = f"dish_reset_{suffix}"
    observer = f"dish_observer_{suffix}"
    writer = f"dish_writer_{suffix}"
    admin_engine, owner, database_url = _create_database_and_roles(
        base, observer=observer, writer=writer, database_name=database_name
    )
    try:
        _seed_original_access(database_url, database_name, owner, observer, writer)
        namespace = runpy.run_path(str(RESET))
        globals_ = namespace["_ordinary_reset"].__globals__
        globals_["log"] = lambda _message: None
        recovery_dir = tmp_path / "reset"
        recovery_dir.mkdir(mode=0o700)
        store = ResetRecoveryStore(recovery_dir / "recovery.json")

        prepare_calls = 0

        def failing_prepare(*, preflight_only: bool) -> None:
            nonlocal prepare_calls
            if preflight_only:
                return
            prepare_calls += 1
            _rebuild_schema(database_url)
            raise ProductionResetError("injected prepare failure after baseline rebuild")

        monkeypatch.setitem(globals_, "_run_prepare", failing_prepare)
        with pytest.raises(ProductionResetError, match="retained original snapshot"):
            globals_["_ordinary_reset"](
                database_url=database_url,
                expected_database_name=database_name,
                store=store,
            )
        assert prepare_calls == 1
        retained = store.load()
        assert retained.state == RECOVERY_STATE_PREPARE_FAILED
        assert retained.snapshot.database.name == database_name
        assert any(g.grantee == observer and g.object_type == "TABLE" for g in retained.snapshot.object_grants)

        # The fresh failed baseline has deliberately lost the original non-owner
        # grants.  An ordinary retry must refuse before a new snapshot can bless it.
        target_engine = create_engine(database_url)
        try:
            with target_engine.connect() as connection:
                assert not bool(
                    connection.execute(
                        text("SELECT has_table_privilege(:role, 'app.items', 'SELECT')"),
                        {"role": observer},
                    ).scalar_one()
                )
        finally:
            target_engine.dispose()
        monkeypatch.setitem(
            globals_,
            "snapshot_database_state",
            lambda *_args: pytest.fail("ordinary retry must not take a live snapshot"),
        )
        with pytest.raises(ProductionResetError, match="ordinary retry is forbidden"):
            globals_["_ordinary_reset"](
                database_url=database_url,
                expected_database_name=database_name,
                store=store,
            )

        def successful_prepare(*, preflight_only: bool) -> None:
            if not preflight_only:
                _rebuild_schema(database_url)

        monkeypatch.setitem(globals_, "_run_prepare", successful_prepare)
        globals_["_resume_reset"](
            database_url=database_url,
            expected_database_name=database_name,
            store=store,
        )
        assert store.load().state == RECOVERY_STATE_COMPLETED
        guard = inspect_reset_guard(database_url, database_name)
        assert guard.reset_id is None and guard.allow_connections is True

        target_engine = create_engine(database_url)
        try:
            with target_engine.begin() as connection:
                assert bool(connection.execute(text("SELECT has_database_privilege(:role, :db, 'CONNECT')"), {"role": observer, "db": database_name}).scalar_one())
                assert bool(connection.execute(text("SELECT has_schema_privilege(:role, 'app', 'USAGE')"), {"role": observer}).scalar_one())
                assert bool(connection.execute(text("SELECT has_table_privilege(:role, 'app.items', 'SELECT')"), {"role": observer}).scalar_one())
                assert bool(connection.execute(text("SELECT has_table_privilege(:role, 'app.items', 'UPDATE')"), {"role": writer}).scalar_one())
                assert bool(connection.execute(text("SELECT has_column_privilege(:role, 'app.items', 'private_note', 'SELECT')"), {"role": writer}).scalar_one())
                assert bool(connection.execute(text("SELECT has_sequence_privilege(:role, 'app.item_seq', 'USAGE')"), {"role": observer}).scalar_one())
                setting = connection.execute(
                    text(
                        """
                        SELECT setting FROM pg_db_role_setting AS s
                        JOIN pg_roles AS r ON r.oid = s.setrole
                        CROSS JOIN LATERAL unnest(s.setconfig) AS setting
                        WHERE r.rolname = :role
                          AND s.setdatabase = (SELECT oid FROM pg_database WHERE datname = :db)
                        """
                    ),
                    {"role": observer, "db": database_name},
                ).scalar_one()
                assert setting == "default_transaction_read_only=on"
                connection.exec_driver_sql("CREATE TABLE app.future_items (id bigint PRIMARY KEY)")
                assert bool(connection.execute(text("SELECT has_table_privilege(:role, 'app.future_items', 'SELECT')"), {"role": observer}).scalar_one())
        finally:
            target_engine.dispose()
    finally:
        _cleanup(admin_engine, database_name, observer, writer)


def test_native_create_before_guard_crash_leaves_database_connection_fenced(
    native_migration_database, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = native_migration_database
    suffix = uuid.uuid4().hex[:8]
    database_name = f"dish_reset_crash_{suffix}"
    observer = f"dish_observer_{suffix}"
    writer = f"dish_writer_{suffix}"
    admin_engine, owner, database_url = _create_database_and_roles(
        base, observer=observer, writer=writer, database_name=database_name
    )
    try:
        snapshot = snapshot_database_state(database_url, database_name)
        reset_id = str(uuid.uuid4())
        install_reset_guard(database_url, database_name, reset_id, expected_owner=owner)

        class InjectedCrash(RuntimeError):
            pass

        monkeypatch.setattr(
            production_reset,
            "_install_reset_guard_on_connection",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(InjectedCrash("after CREATE before guard")),
        )
        with pytest.raises(InjectedCrash):
            recreate_database(database_url, snapshot, reset_id=reset_id)
        state = inspect_reset_guard(database_url, database_name)
        assert state.exists
        assert state.allow_connections is False
        assert state.reset_id is None
    finally:
        _cleanup(admin_engine, database_name, observer, writer)


def test_native_guard_reset_id_mismatch_refuses_before_database_mutation(
    native_migration_database,
) -> None:
    base = native_migration_database
    suffix = uuid.uuid4().hex[:8]
    database_name = f"dish_reset_guard_{suffix}"
    observer = f"dish_observer_{suffix}"
    writer = f"dish_writer_{suffix}"
    admin_engine, owner, database_url = _create_database_and_roles(
        base, observer=observer, writer=writer, database_name=database_name
    )
    try:
        target_engine = create_engine(database_url)
        with target_engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE sentinel (id integer PRIMARY KEY)")
        target_engine.dispose()
        snapshot = snapshot_database_state(database_url, database_name)
        original_reset_id = str(uuid.uuid4())
        install_reset_guard(database_url, database_name, original_reset_id, expected_owner=owner)

        with pytest.raises(ProductionResetError, match="guard mismatch"):
            recreate_database(database_url, snapshot, reset_id=str(uuid.uuid4()))

        state = inspect_reset_guard(database_url, database_name)
        assert state.reset_id == original_reset_id
        assert state.allow_connections is True
        target_engine = create_engine(database_url)
        try:
            with target_engine.connect() as connection:
                assert connection.execute(text("SELECT count(*) FROM sentinel")).scalar_one() == 0
        finally:
            target_engine.dispose()
    finally:
        _cleanup(admin_engine, database_name, observer, writer)
