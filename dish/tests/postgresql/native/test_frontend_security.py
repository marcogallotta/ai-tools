from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from dish_pg.frontend_security_models import FrontendSecurityState, FrontendSession
from dish_pg.release import ALEMBIC_HEAD
from dish_service.frontend_auth import FrontendAuthFailure, FrontendAuthService
from dish_service.frontend_security import (
    Argon2Policy,
    FrontendSecurityConfigurationError,
    create_restore_fence,
)
from dish_tool.frontend_password_admin import FrontendPasswordAdminSettings, provision

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
