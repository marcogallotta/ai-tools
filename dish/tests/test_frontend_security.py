from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dish_pg.frontend_security_models import FrontendLoginEvent, FrontendSecurityState, FrontendSession
from dish_pg.models import Base
from dish_service import frontend_auth as auth_module
from dish_service import frontend_private_runtime as private_runtime_module
from dish_service.frontend_admission import MAX_LOGIN_BODY_BYTES
from dish_service.frontend_auth import FrontendAuthFailure, FrontendAuthService
from dish_service.frontend_private_runtime import FrontendPrivateRuntime
from dish_service.frontend_security import (
    Argon2Policy,
    FrontendSecurityConfigurationError,
    create_restore_fence,
    read_restore_fence,
    restore_fence_digest,
)
from dish_service.frontend_settings import FrontendRuntimeSettings

pytestmark = pytest.mark.smoke


def argon2_test_policy() -> Argon2Policy:
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


@pytest.fixture
def auth_state(tmp_path: Path):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    fence = tmp_path / "frontend-restore-fence"
    fence_value = create_restore_fence(fence)
    policy = argon2_test_policy()
    now = datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc)
    with factory.begin() as session:
        session.add(FrontendSecurityState(
            state_id=1,
            security_generation=1,
            password_verifier=policy.hasher().hash("correct horse battery staple"),
            restore_fence_sha256=restore_fence_digest(fence_value),
            updated_at=now,
        ))
    service = FrontendAuthService(
        factory,
        restore_fence_path=fence,
        session_secret=b"s" * 32,
        csrf_secret=b"c" * 32,
        peer_secret=b"p" * 32,
        argon2_policy=policy,
        now=lambda: now,
    )
    try:
        yield service, factory, fence
    finally:
        engine.dispose()


def test_login_bootstrap_and_idempotent_logout(auth_state) -> None:
    service, factory, _ = auth_state
    service.startup_check()
    result = service.login(password="correct horse battery staple", peer="127.0.0.1")
    bootstrap = service.bootstrap(result.token)
    assert bootstrap.remaining_seconds == 604800
    assert bootstrap.principal.session_id == result.principal.session_id
    assert bootstrap.csrf_proof

    service.logout(result.token, csrf=bootstrap.csrf_proof)
    service.logout(result.token, csrf=bootstrap.csrf_proof)
    with pytest.raises(FrontendAuthFailure, match="no longer valid") as failure:
        service.validate(result.token)
    assert failure.value.code == "session_revoked"
    with factory.begin() as session:
        row = session.execute(select(FrontendSession)).scalar_one()
        assert row.revoked_at is not None


def test_failed_logins_commit_and_sixth_attempt_is_throttled_before_argon2(auth_state, monkeypatch) -> None:
    service, factory, _ = auth_state
    for _ in range(5):
        with pytest.raises(FrontendAuthFailure) as failure:
            service.login(password="wrong", peer="127.0.0.1")
        assert failure.value.code == "login_invalid"
    with factory.begin() as session:
        assert len(session.execute(select(FrontendLoginEvent)).scalars().all()) == 5

    monkeypatch.setattr(auth_module, "verify_password", lambda *_args, **_kwargs: pytest.fail("Argon2 must not run"))
    with pytest.raises(FrontendAuthFailure) as failure:
        service.login(password="anything", peer="127.0.0.1")
    assert failure.value.code == "login_throttled"
    assert 1 <= failure.value.retry_after_seconds <= 900


def test_restore_fence_change_invalidates_existing_session(auth_state) -> None:
    service, _, fence = auth_state
    result = service.login(password="correct horse battery staple", peer="127.0.0.1")
    create_restore_fence(fence, replace=True)
    with pytest.raises(FrontendAuthFailure) as failure:
        service.validate(result.token)
    assert failure.value.code == "session_revoked"
    with pytest.raises(FrontendSecurityConfigurationError, match="restore fence"):
        service.startup_check()


@pytest.mark.parametrize(("policy_field", "stored_value"), [("hash_len", 17), ("salt_len", 17)])
def test_out_of_policy_stored_argon2_lengths_fail_startup_and_login_closed(
    auth_state, policy_field: str, stored_value: int
) -> None:
    service, factory, _ = auth_state
    stored_policy = replace(argon2_test_policy(), **{policy_field: stored_value})
    with factory.begin() as session:
        state = session.get(FrontendSecurityState, 1)
        assert state is not None
        state.password_verifier = stored_policy.hasher().hash("correct horse battery staple")

    with pytest.raises(FrontendSecurityConfigurationError, match=policy_field.replace("_", " ")):
        service.startup_check()
    with pytest.raises(FrontendAuthFailure) as failure:
        service.login(password="correct horse battery staple", peer="127.0.0.1")
    assert failure.value.code == "service_unavailable"


def test_restore_fence_requires_owner_only_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "fence"
    create_restore_fence(path)
    path.chmod(0o644)
    with pytest.raises(FrontendSecurityConfigurationError, match="permissions"):
        read_restore_fence(path)


def encoded(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).rstrip(b"=").decode()


def enabled_env() -> dict[str, str]:
    return {
        "DISH_FRONTEND_ENABLED": "1",
        "DISH_FRONTEND_ORIGIN": "https://dish.example.test",
        "DISH_ACTION_PUBLIC_ORIGIN": "https://action.example.test",
        "DISH_FRONTEND_DATABASE_URL": "postgresql+psycopg://dish:secret@127.0.0.1/dish",
        "DISH_FRONTEND_REFRESH_INTERVAL_SECONDS": "25",
        "DISH_FRONTEND_RESTORE_FENCE_PATH": "/tmp/dish-frontend-fence",
        "DISH_FRONTEND_TOKEN_SECRET": encoded(1),
        "DISH_FRONTEND_SESSION_SECRET": encoded(2),
        "DISH_FRONTEND_CSRF_SECRET": encoded(3),
        "DISH_FRONTEND_PEER_SECRET": encoded(4),
        "DISH_FRONTEND_ARGON2_TIME_COST": "2",
        "DISH_FRONTEND_ARGON2_MEMORY_KIB": "65536",
        "DISH_FRONTEND_ARGON2_PARALLELISM": "2",
        "DISH_FRONTEND_ARGON2_HASH_LEN": "32",
        "DISH_FRONTEND_ARGON2_SALT_LEN": "16",
        "DISH_FRONTEND_ARGON2_MIN_TIME_COST": "2",
        "DISH_FRONTEND_ARGON2_MAX_TIME_COST": "3",
        "DISH_FRONTEND_ARGON2_MIN_MEMORY_KIB": "65536",
        "DISH_FRONTEND_ARGON2_MAX_MEMORY_KIB": "131072",
        "DISH_FRONTEND_ARGON2_MIN_PARALLELISM": "1",
        "DISH_FRONTEND_ARGON2_MAX_PARALLELISM": "4",
    }


def test_frontend_settings_fail_closed_and_keep_postgresql_reads_explicit(tmp_path: Path) -> None:
    assert not FrontendRuntimeSettings.from_mapping({}, dish_root=tmp_path).enabled
    env = enabled_env()
    settings = FrontendRuntimeSettings.from_mapping(env, dish_root=tmp_path)
    assert settings.enabled
    assert not settings.postgresql_reads_enabled
    assert settings.observation_database_url is None
    assert settings.projection_delay_seconds is None
    assert settings.refresh_interval_seconds == 25

    enabled_reads = {**env, "DISH_FRONTEND_POSTGRESQL_READS_ENABLED": "1"}
    with pytest.raises(FrontendSecurityConfigurationError, match="OBSERVATION_DATABASE_URL"):
        FrontendRuntimeSettings.from_mapping(enabled_reads, dish_root=tmp_path)
    enabled_reads["DISH_FRONTEND_OBSERVATION_DATABASE_URL"] = (
        "postgresql+psycopg://dish_observer:observation-secret@127.0.0.1/dish_observation"
    )
    with pytest.raises(FrontendSecurityConfigurationError, match="PROJECTION_DELAY"):
        FrontendRuntimeSettings.from_mapping(enabled_reads, dish_root=tmp_path)
    enabled_reads["DISH_FRONTEND_PROJECTION_DELAY_SECONDS"] = "900"
    settings = FrontendRuntimeSettings.from_mapping(enabled_reads, dish_root=tmp_path)
    assert settings.postgresql_reads_enabled
    assert settings.projection_delay_seconds == 900
    assert settings.observation_database_url == enabled_reads["DISH_FRONTEND_OBSERVATION_DATABASE_URL"]



def test_frontend_settings_bound_active_refresh_interval(tmp_path: Path) -> None:
    env = enabled_env()
    env["DISH_FRONTEND_REFRESH_INTERVAL_SECONDS"] = "30"
    assert FrontendRuntimeSettings.from_mapping(env, dish_root=tmp_path).refresh_interval_seconds == 30
    env["DISH_FRONTEND_REFRESH_INTERVAL_SECONDS"] = "31"
    with pytest.raises(FrontendSecurityConfigurationError, match="at most 30"):
        FrontendRuntimeSettings.from_mapping(env, dish_root=tmp_path)


def test_frontend_settings_require_explicit_database_url(tmp_path: Path) -> None:
    env = enabled_env()
    del env["DISH_FRONTEND_DATABASE_URL"]
    with pytest.raises(FrontendSecurityConfigurationError, match="DATABASE_URL"):
        FrontendRuntimeSettings.from_mapping(env, dish_root=tmp_path)


def test_login_body_bound_accepts_maximum_password_under_json_escaping() -> None:
    import json

    # JSON permits non-ASCII and control characters to be escaped. The transport
    # bound must not make a provisionable 1024-code-point password unsendable.
    worst_case = "\U0001f600" * 1024
    payload = json.dumps({"password": worst_case}, ensure_ascii=True).encode("utf-8")
    assert len(payload) <= MAX_LOGIN_BODY_BYTES


def test_frontend_settings_reject_service_token_reuse(tmp_path: Path) -> None:
    env = enabled_env()
    env["DISH_SERVICE_AGENT_TOKEN"] = env["DISH_FRONTEND_SESSION_SECRET"]
    with pytest.raises(FrontendSecurityConfigurationError, match="existing service/database secrets"):
        FrontendRuntimeSettings.from_mapping(env, dish_root=tmp_path)


def test_frontend_settings_reject_database_password_reuse(tmp_path: Path) -> None:
    env = enabled_env()
    # Keep all frontend material syntactically valid while making the database
    # password exactly equal to one configured frontend security secret.
    env["DISH_FRONTEND_DATABASE_URL"] = f"postgresql+psycopg://dish:{env['DISH_FRONTEND_TOKEN_SECRET']}@127.0.0.1/dish"
    with pytest.raises(FrontendSecurityConfigurationError, match="existing service/database secrets"):
        FrontendRuntimeSettings.from_mapping(env, dish_root=tmp_path)


def test_frontend_settings_reject_observation_database_password_reuse(tmp_path: Path) -> None:
    env = enabled_env()
    env["DISH_FRONTEND_OBSERVATION_DATABASE_URL"] = (
        f"postgresql+psycopg://dish_observer:{env['DISH_FRONTEND_TOKEN_SECRET']}@127.0.0.1/dish_observation"
    )
    with pytest.raises(FrontendSecurityConfigurationError, match="existing service/database secrets"):
        FrontendRuntimeSettings.from_mapping(env, dish_root=tmp_path)


def test_private_runtime_routes_auth_and_observation_to_distinct_factories(
    monkeypatch, tmp_path: Path
) -> None:
    static_root = tmp_path / "dist"
    static_root.mkdir()
    (static_root / "index.html").write_text("<!doctype html>", encoding="utf-8")
    settings = FrontendRuntimeSettings(
        enabled=True,
        database_url="postgresql+psycopg://auth:secret@127.0.0.1/auth",
        observation_database_url="postgresql+psycopg://observer:secret@127.0.0.1/observation",
        static_root=static_root,
        restore_fence_path=tmp_path / "fence",
        token_secret=b"t" * 32,
        session_secret=b"s" * 32,
        csrf_secret=b"c" * 32,
        peer_secret=b"p" * 32,
        postgresql_reads_enabled=True,
        projection_delay_seconds=900,
    )
    auth_engine, observation_engine = MagicMock(), MagicMock()
    auth_factory, observation_factory = MagicMock(), MagicMock()
    monkeypatch.setattr(
        private_runtime_module,
        "create_database_engine",
        MagicMock(side_effect=[auth_engine, observation_engine]),
    )
    monkeypatch.setattr(
        private_runtime_module,
        "session_factory",
        MagicMock(side_effect=[auth_factory, observation_factory]),
    )
    auth = MagicMock()
    monkeypatch.setattr(private_runtime_module, "FrontendAuthService", MagicMock(return_value=auth))

    auth_startup, observation_startup = MagicMock(), MagicMock()
    auth_startup.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    observation_startup.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    auth_identity = MagicMock()
    auth_identity.one.return_value = ("frontend_auth", 101)
    observation_identity = MagicMock()
    observation_identity.one.return_value = ("dark_launch", 202)
    auth_startup.execute.return_value = auth_identity
    observation_startup.execute.side_effect = [MagicMock(), observation_identity]
    observation_factory.begin.return_value.__enter__.return_value = observation_startup
    auth_factory.begin.return_value.__enter__.return_value = auth_startup
    board_session, detail_session = MagicMock(), MagicMock()
    board_session.begin.return_value.__enter__.return_value = board_session
    detail_session.begin.return_value.__enter__.return_value = detail_session
    observation_factory.side_effect = [board_session, detail_session]
    board_service, detail_service = MagicMock(), MagicMock()
    board_service.bootstrap.return_value = {"source": "observation"}
    detail_service.capture.return_value = {"facts": True}
    detail_service.present.return_value = {"source": "observation-detail"}
    board_query = MagicMock()
    detail_query = MagicMock()
    monkeypatch.setattr(private_runtime_module, "FrontendBoardQuery", board_query)
    monkeypatch.setattr(private_runtime_module, "FrontendBoardService", MagicMock(return_value=board_service))
    monkeypatch.setattr(private_runtime_module, "FrontendDetailQuery", detail_query)
    monkeypatch.setattr(private_runtime_module, "FrontendDetailService", MagicMock(return_value=detail_service))

    runtime = FrontendPrivateRuntime(settings)
    runtime.startup_check()
    assert runtime.auth_factory is auth_factory
    assert runtime.observation_factory is observation_factory
    assert runtime.board() == {"source": "observation"}
    assert runtime.detail(task_route_id="r1t-task") == {"source": "observation-detail"}
    board_query.assert_called_once_with(board_session)
    detail_query.assert_called_once_with(detail_session)
    assert any(
        "SET TRANSACTION READ ONLY" in str(call.args[0])
        for call in observation_startup.execute.call_args_list
    )
    auth_factory.assert_not_called()
    runtime.close()
    auth_engine.dispose.assert_called_once_with()
    observation_engine.dispose.assert_called_once_with()



def test_private_runtime_rejects_same_physical_database_despite_distinct_urls(
    monkeypatch, tmp_path: Path
) -> None:
    static_root = tmp_path / "dist"
    static_root.mkdir()
    (static_root / "index.html").write_text("<!doctype html>", encoding="utf-8")
    settings = FrontendRuntimeSettings(
        enabled=True,
        database_url="postgresql+psycopg://auth:auth-secret@localhost:5432/shared",
        observation_database_url=(
            "postgresql+psycopg://observer:observation-secret@127.0.0.1:5432/shared"
            "?options=-c%20default_transaction_read_only%3Don"
        ),
        static_root=static_root,
        restore_fence_path=tmp_path / "fence",
        token_secret=b"t" * 32,
        session_secret=b"s" * 32,
        csrf_secret=b"c" * 32,
        peer_secret=b"p" * 32,
        postgresql_reads_enabled=True,
        projection_delay_seconds=900,
    )
    auth_engine, observation_engine = MagicMock(), MagicMock()
    auth_factory, observation_factory = MagicMock(), MagicMock()
    monkeypatch.setattr(
        private_runtime_module,
        "create_database_engine",
        MagicMock(side_effect=[auth_engine, observation_engine]),
    )
    monkeypatch.setattr(
        private_runtime_module,
        "session_factory",
        MagicMock(side_effect=[auth_factory, observation_factory]),
    )
    monkeypatch.setattr(
        private_runtime_module, "FrontendAuthService", MagicMock(return_value=MagicMock())
    )

    auth_startup, observation_startup = MagicMock(), MagicMock()
    for startup in (auth_startup, observation_startup):
        startup.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    auth_identity = MagicMock()
    auth_identity.one.return_value = ("shared", 777)
    observation_identity = MagicMock()
    observation_identity.one.return_value = ("shared", 777)
    auth_startup.execute.return_value = auth_identity
    observation_startup.execute.side_effect = [MagicMock(), observation_identity]
    auth_factory.begin.return_value.__enter__.return_value = auth_startup
    observation_factory.begin.return_value.__enter__.return_value = observation_startup

    runtime = FrontendPrivateRuntime(settings)
    try:
        with pytest.raises(FrontendSecurityConfigurationError, match="different physical PostgreSQL databases"):
            runtime.startup_check()
        assert any(
            "SET TRANSACTION READ ONLY" in str(call.args[0])
            for call in observation_startup.execute.call_args_list
        )
    finally:
        runtime.close()


def test_peer_throttle_remains_blocked_for_full_interval_after_threshold(auth_state) -> None:
    original, factory, fence = auth_state
    base = datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc)
    clock = [base]
    service = FrontendAuthService(
        factory,
        restore_fence_path=fence,
        session_secret=original.session_secret,
        csrf_secret=original.csrf_secret,
        peer_secret=original.peer_secret,
        argon2_policy=original.argon2_policy,
        now=lambda: clock[0],
    )
    for minutes in (0, 1, 2, 3, 14):
        clock[0] = base + timedelta(minutes=minutes)
        with pytest.raises(FrontendAuthFailure) as failure:
            service.login(password="wrong", peer="127.0.0.1")
        assert failure.value.code == "login_invalid"

    with pytest.raises(FrontendAuthFailure) as failure:
        service.login(password="correct horse battery staple", peer="127.0.0.1")
    assert failure.value.code == "login_throttled"
    assert failure.value.retry_after_seconds == 900

    # The first four failures have fallen outside the 15-minute counting window,
    # but the durable block created by the fifth failure remains in force.
    clock[0] = base + timedelta(minutes=20)
    with pytest.raises(FrontendAuthFailure) as failure:
        service.login(password="correct horse battery staple", peer="127.0.0.1")
    assert failure.value.code == "login_throttled"
    assert failure.value.retry_after_seconds == 540

    clock[0] = base + timedelta(minutes=29, seconds=1)
    result = service.login(password="correct horse battery staple", peer="127.0.0.1")
    assert result.token


def test_global_throttle_blocks_after_thirtieth_failure(auth_state) -> None:
    service, _, _ = auth_state
    for index in range(30):
        with pytest.raises(FrontendAuthFailure) as failure:
            service.login(password="wrong", peer=f"127.0.1.{index}")
        assert failure.value.code == "login_invalid"

    with pytest.raises(FrontendAuthFailure) as failure:
        service.login(password="correct horse battery staple", peer="127.0.2.1")
    assert failure.value.code == "login_throttled"
    assert failure.value.retry_after_seconds == 900


def test_restore_fence_rotation_replaces_symlink_without_following_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("do-not-touch\n", encoding="ascii")
    fence = tmp_path / "fence"
    fence.symlink_to(target)

    value = create_restore_fence(fence, replace=True)

    assert target.read_text(encoding="ascii") == "do-not-touch\n"
    assert not fence.is_symlink()
    assert read_restore_fence(fence) == value
