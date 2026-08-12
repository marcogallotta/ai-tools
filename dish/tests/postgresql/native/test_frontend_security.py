from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import uuid

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from dish_pg.database import session_factory
from dish_pg.frontend_security_models import FrontendSecurityState, FrontendSession
from dish_pg.release import ALEMBIC_HEAD
from dish_service.frontend_auth import FrontendAuthFailure, FrontendAuthService
from dish_service.frontend_private_runtime import FrontendPrivateRuntime
from dish_service.frontend_security import (
    Argon2Policy,
    FrontendSecurityConfigurationError,
    create_restore_fence,
)
from dish_service.frontend_settings import FrontendRuntimeSettings
from dish_service.frontend_password_admin import (
    FrontendPasswordAdminSettings,
    provision,
    rotate_password,
)
from tests.support.postgresql.core import _bootstrap_registry, _import_one, _next, _uuid_stream
from tests.support.postgresql.migrations import MigrationDatabase

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def _policy() -> Argon2Policy:
    return Argon2Policy(
        time_cost=1,
        memory_cost_kib=1024,
        parallelism=1,
        hash_len=16,
        salt_len=16,
        min_time_cost=1,
        max_time_cost=2,
        min_memory_cost_kib=1024,
        max_memory_cost_kib=2048,
        min_parallelism=1,
        max_parallelism=2,
    )


@contextmanager
def _sibling_database(database, *, label: str):
    base_url = make_url(database.sqlalchemy_url)
    suffix = uuid.uuid4().hex[:8]
    name = f"dish_{label}_{suffix}"[:63]

    admin_engine = database.create_engine()
    try:
        with admin_engine.connect() as connection:
            connection = connection.execution_options(isolation_level="AUTOCOMMIT")
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin_engine.dispose()

    url = base_url.set(database=name).render_as_string(hide_password=False)
    sibling = MigrationDatabase(
        sqlalchemy_url=url,
        expected_dialect="postgresql",
        certification_evidence=True,
        lane="native_postgresql_certification",
    )
    sibling.fresh_bootstrap(ALEMBIC_HEAD)
    try:
        yield sibling
    finally:
        cleanup = database.create_engine()
        try:
            with cleanup.connect() as connection:
                connection = connection.execution_options(isolation_level="AUTOCOMMIT")
                connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        finally:
            cleanup.dispose()


def _frontend_security_counts(engine) -> tuple[int, int, int, int]:
    tables = (
        "frontend_security_state",
        "frontend_sessions",
        "frontend_login_events",
        "frontend_security_audit",
    )
    with engine.connect() as connection:
        return tuple(
            int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
            for table in tables
        )


def test_native_frontend_security_migration_and_session_lifecycle(
    native_migration_database,
    tmp_path: Path,
) -> None:
    database = native_migration_database
    database.initialize("0032_imported_operation_history")
    database.upgrade(ALEMBIC_HEAD)
    database.assert_revision(ALEMBIC_HEAD)

    tables = database.read(lambda connection: set(inspect(connection).get_table_names()))
    assert {
        "frontend_security_state",
        "frontend_sessions",
        "frontend_login_events",
        "frontend_security_audit",
    } <= tables

    fence_path = tmp_path / "frontend-security.fence"
    create_restore_fence(fence_path)
    settings = FrontendPasswordAdminSettings(
        database_url=database.sqlalchemy_url,
        restore_fence_path=fence_path,
        argon2_policy=_policy(),
        forbidden_secrets=(),
    )
    provision(settings, "correct horse battery staple")

    engine = database.create_engine()
    try:
        from dish_pg.database import session_factory

        factory = session_factory(engine)
        auth = FrontendAuthService(
            factory,
            restore_fence_path=fence_path,
            session_secret=b"s" * 32,
            csrf_secret=b"c" * 32,
            peer_secret=b"p" * 32,
            argon2_policy=_policy(),
        )
        auth.startup_check()
        result = auth.login(password="correct horse battery staple", peer="127.0.0.1")
        bootstrap = auth.bootstrap(result.token)
        assert 0 < bootstrap.remaining_seconds <= 604800
        auth.logout(result.token, csrf=bootstrap.csrf_proof)

        for _ in range(5):
            with pytest.raises(FrontendAuthFailure) as failure:
                auth.login(password="wrong", peer="127.0.0.2")
            assert failure.value.code == "login_invalid"
        restarted = FrontendAuthService(
            factory,
            restore_fence_path=fence_path,
            session_secret=b"s" * 32,
            csrf_secret=b"c" * 32,
            peer_secret=b"p" * 32,
            argon2_policy=_policy(),
        )
        restarted.startup_check()
        with pytest.raises(FrontendAuthFailure) as failure:
            restarted.login(password="correct horse battery staple", peer="127.0.0.2")
        assert failure.value.code == "login_throttled"
        assert 1 <= failure.value.retry_after_seconds <= 900

        with Session(engine) as session:
            state = session.get(FrontendSecurityState, 1)
            assert state is not None and state.security_generation == 1
            row = session.scalar(select(FrontendSession))
            assert row is not None and row.revoked_at is not None
    finally:
        engine.dispose()

def test_native_out_of_policy_stored_argon2_lengths_fail_startup_and_login_closed(
    native_migration_database,
    tmp_path: Path,
) -> None:
    database = native_migration_database
    database.initialize("0032_imported_operation_history")
    database.upgrade(ALEMBIC_HEAD)

    fence_path = tmp_path / "frontend-security.fence"
    create_restore_fence(fence_path)
    settings = FrontendPasswordAdminSettings(
        database_url=database.sqlalchemy_url,
        restore_fence_path=fence_path,
        argon2_policy=_policy(),
        forbidden_secrets=(),
    )
    provision(settings, "correct horse battery staple")

    engine = database.create_engine()
    try:
        from dish_pg.database import session_factory

        auth = FrontendAuthService(
            session_factory(engine),
            restore_fence_path=fence_path,
            session_secret=b"s" * 32,
            csrf_secret=b"c" * 32,
            peer_secret=b"p" * 32,
            argon2_policy=_policy(),
        )
        for policy_field, stored_value in (("hash_len", 17), ("salt_len", 17)):
            stored_policy = replace(_policy(), **{policy_field: stored_value})
            with Session(engine) as session:
                state = session.get(FrontendSecurityState, 1)
                assert state is not None
                state.password_verifier = stored_policy.hasher().hash("correct horse battery staple")
                session.commit()

            with pytest.raises(FrontendSecurityConfigurationError, match=policy_field.replace("_", " ")):
                auth.startup_check()
            with pytest.raises(FrontendAuthFailure) as failure:
                auth.login(password="correct horse battery staple", peer="127.0.0.1")
            assert failure.value.code == "service_unavailable"
    finally:
        engine.dispose()


def test_native_frontend_runtime_physically_isolates_auth_writes_from_observation_reads(
    native_migration_database,
    tmp_path: Path,
) -> None:
    auth_database = native_migration_database
    auth_database.initialize("0032_imported_operation_history")
    auth_database.upgrade(ALEMBIC_HEAD)

    with _sibling_database(auth_database, label="frontend_observation") as observation_database:
        fence_path = tmp_path / "frontend-security.fence"
        create_restore_fence(fence_path)
        admin_settings = FrontendPasswordAdminSettings(
            database_url=auth_database.sqlalchemy_url,
            restore_fence_path=fence_path,
            argon2_policy=_policy(),
            forbidden_secrets=(),
        )
        provision(admin_settings, "correct horse battery staple")

        observation_seed_engine = observation_database.create_engine()
        try:
            ids = _uuid_stream()
            with session_factory(observation_seed_engine).begin() as session:
                context = _bootstrap_registry(
                    session,
                    ids,
                    generation_status="active",
                    schema_head=ALEMBIC_HEAD,
                )
                first_task_id = _next(ids)
                second_task_id = _next(ids)
                _import_one(
                    session, ids, context, task_id=first_task_id, asana_gid="123456789"
                )
                _import_one(
                    session, ids, context, task_id=second_task_id, asana_gid="123456790"
                )
        finally:
            observation_seed_engine.dispose()

        observation_url = make_url(observation_database.sqlalchemy_url).update_query_dict(
            {"options": "-c default_transaction_read_only=on"}
        ).render_as_string(hide_password=False)
        static_root = tmp_path / "dist"
        static_root.mkdir()
        (static_root / "index.html").write_text("<!doctype html>", encoding="utf-8")
        runtime = FrontendPrivateRuntime(
            FrontendRuntimeSettings(
                enabled=True,
                origin="https://dish.example.test",
                action_origin="https://action.example.test",
                database_url=auth_database.sqlalchemy_url,
                observation_database_url=observation_url,
                static_root=static_root,
                restore_fence_path=fence_path,
                token_secret=b"t" * 32,
                session_secret=b"s" * 32,
                csrf_secret=b"c" * 32,
                peer_secret=b"p" * 32,
                argon2_policy=_policy(),
                postgresql_reads_enabled=True,
                projection_delay_seconds=900,
            )
        )
        try:
            runtime.startup_check()
            assert runtime.board_config is not None
            runtime.board_config = replace(
                runtime.board_config, first_page_size=1, continuation_page_size=1
            )

            assert runtime.observation_engine is not None
            observation_counts_before = _frontend_security_counts(runtime.observation_engine)
            assert observation_counts_before == (0, 0, 0, 0)

            login = runtime.auth.login(
                password="correct horse battery staple", peer="127.0.0.1"
            )
            assert login.token
            board = runtime.board()
            section = board["sections"][0]
            assert len(section["cards"]) == 1
            assert section["next_cursor"] is not None
            task_route_id = section["cards"][0]["task_id"]
            continuation = runtime.continuation(
                section_route_id=section["section_id"], cursor=section["next_cursor"]
            )
            assert len(continuation["cards"]) == 1
            detail = runtime.detail(task_route_id=task_route_id)
            assert detail["task_id"] == task_route_id
            assert task_route_id == str(first_task_id)
            assert continuation["cards"][0]["task_id"] == str(second_task_id)

            with runtime.observation_engine.connect() as connection:
                transaction = connection.begin()
                assert connection.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
                with pytest.raises(DBAPIError):
                    connection.execute(
                        text(
                            "UPDATE frontend_security_state "
                            "SET security_generation = security_generation"
                        )
                    )
                transaction.rollback()

            assert _frontend_security_counts(runtime.observation_engine) == observation_counts_before
            auth_engine = auth_database.create_engine()
            try:
                auth_counts = _frontend_security_counts(auth_engine)
            finally:
                auth_engine.dispose()
            assert auth_counts[0] == 1
            assert auth_counts[1] >= 1
            assert auth_counts[3] >= 1

            assert rotate_password(admin_settings, "another correct battery staple") == 2
            assert _frontend_security_counts(runtime.observation_engine) == observation_counts_before
        finally:
            runtime.close()


def test_native_frontend_runtime_rejects_same_physical_database_for_both_urls(
    native_migration_database,
    tmp_path: Path,
) -> None:
    database = native_migration_database
    database.initialize("0032_imported_operation_history")
    database.upgrade(ALEMBIC_HEAD)

    fence_path = tmp_path / "frontend-security.fence"
    create_restore_fence(fence_path)
    provision(
        FrontendPasswordAdminSettings(
            database_url=database.sqlalchemy_url,
            restore_fence_path=fence_path,
            argon2_policy=_policy(),
            forbidden_secrets=(),
        ),
        "correct horse battery staple",
    )
    same_database_read_only_url = make_url(database.sqlalchemy_url).update_query_dict(
        {"options": "-c default_transaction_read_only=on"}
    ).render_as_string(hide_password=False)
    static_root = tmp_path / "dist"
    static_root.mkdir()
    (static_root / "index.html").write_text("<!doctype html>", encoding="utf-8")
    runtime = FrontendPrivateRuntime(
        FrontendRuntimeSettings(
            enabled=True,
            origin="https://dish.example.test",
            action_origin="https://action.example.test",
            database_url=database.sqlalchemy_url,
            observation_database_url=same_database_read_only_url,
            static_root=static_root,
            restore_fence_path=fence_path,
            token_secret=b"t" * 32,
            session_secret=b"s" * 32,
            csrf_secret=b"c" * 32,
            peer_secret=b"p" * 32,
            argon2_policy=_policy(),
            postgresql_reads_enabled=True,
            projection_delay_seconds=900,
        )
    )
    try:
        with pytest.raises(
            FrontendSecurityConfigurationError, match="different physical PostgreSQL databases"
        ):
            runtime.startup_check()
    finally:
        runtime.close()
