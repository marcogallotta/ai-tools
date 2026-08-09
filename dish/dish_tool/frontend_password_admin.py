"""Guarded provisioning and rotation for the private frontend shared password."""
from __future__ import annotations

import getpass
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from urllib.parse import unquote, urlsplit

from sqlalchemy.exc import SQLAlchemyError

from dish_pg.database import DatabaseSettings, create_database_engine, session_factory
from dish_pg.frontend_security_models import FrontendSecurityState
from dish_pg.frontend_security_repository import FrontendSecurityRepository
from dish_service.frontend_security import (
    Argon2Policy,
    FrontendSecurityConfigurationError,
    create_restore_fence,
    read_restore_fence,
    require_provisionable_password,
    restore_fence_digest,
)


@dataclass(frozen=True, slots=True)
class FrontendPasswordAdminSettings:
    database_url: str
    restore_fence_path: Path
    argon2_policy: Argon2Policy
    forbidden_secrets: tuple[str, ...]

    @classmethod
    def from_mapping(cls, env: Mapping[str, str]) -> "FrontendPasswordAdminSettings":
        def required(name: str) -> str:
            value = str(env.get(name, ""))
            if not value or value != value.strip():
                raise FrontendSecurityConfigurationError(f"{name} is required without surrounding whitespace")
            return value

        def integer(name: str) -> int:
            try:
                value = int(required(name))
            except ValueError as exc:
                raise FrontendSecurityConfigurationError(f"{name} must be an integer") from exc
            if value <= 0:
                raise FrontendSecurityConfigurationError(f"{name} must be positive")
            return value

        policy = Argon2Policy(
            time_cost=integer("DISH_FRONTEND_ARGON2_TIME_COST"),
            memory_cost_kib=integer("DISH_FRONTEND_ARGON2_MEMORY_KIB"),
            parallelism=integer("DISH_FRONTEND_ARGON2_PARALLELISM"),
            hash_len=integer("DISH_FRONTEND_ARGON2_HASH_LEN"),
            salt_len=integer("DISH_FRONTEND_ARGON2_SALT_LEN"),
            min_time_cost=integer("DISH_FRONTEND_ARGON2_MIN_TIME_COST"),
            max_time_cost=integer("DISH_FRONTEND_ARGON2_MAX_TIME_COST"),
            min_memory_cost_kib=integer("DISH_FRONTEND_ARGON2_MIN_MEMORY_KIB"),
            max_memory_cost_kib=integer("DISH_FRONTEND_ARGON2_MAX_MEMORY_KIB"),
            min_parallelism=integer("DISH_FRONTEND_ARGON2_MIN_PARALLELISM"),
            max_parallelism=integer("DISH_FRONTEND_ARGON2_MAX_PARALLELISM"),
        )
        database_url = required("DISH_FRONTEND_DATABASE_URL")
        forbidden = [
            str(value) for name, value in env.items()
            if value and any(marker in name.upper() for marker in ("TOKEN", "SECRET", "PASSWORD"))
            and not name.startswith("DISH_FRONTEND_ARGON2_")
        ]
        database_password = unquote(urlsplit(database_url).password or "")
        if database_password:
            forbidden.append(database_password)
        return cls(
            database_url=database_url,
            restore_fence_path=Path(required("DISH_FRONTEND_RESTORE_FENCE_PATH")).expanduser(),
            argon2_policy=policy,
            forbidden_secrets=tuple(forbidden),
        )


def provision(settings: FrontendPasswordAdminSettings, password: str) -> None:
    password = require_provisionable_password(password, forbidden_secrets=settings.forbidden_secrets)
    fence_sha = restore_fence_digest(read_restore_fence(settings.restore_fence_path))
    verifier = settings.argon2_policy.hasher().hash(password)
    engine = create_database_engine(DatabaseSettings(url=settings.database_url))
    factory = session_factory(engine)
    now = datetime.now(timezone.utc)
    try:
        with factory.begin() as session:
            repo = FrontendSecurityRepository(session)
            if repo.state(for_update=True) is not None:
                raise FrontendSecurityConfigurationError("frontend password is already provisioned; use rotate")
            session.add(FrontendSecurityState(
                state_id=1, security_generation=1, password_verifier=verifier,
                restore_fence_sha256=fence_sha, updated_at=now,
            ))
            repo.add_audit(event_type="password_provisioned", now=now, generation=1)
    except SQLAlchemyError as exc:
        raise FrontendSecurityConfigurationError("frontend password provisioning persistence failed") from exc
    finally:
        engine.dispose()


def rotate_password(settings: FrontendPasswordAdminSettings, password: str) -> int:
    password = require_provisionable_password(password, forbidden_secrets=settings.forbidden_secrets)
    fence_sha = restore_fence_digest(read_restore_fence(settings.restore_fence_path))
    verifier = settings.argon2_policy.hasher().hash(password)
    return _rotate_state(settings, fence_sha=fence_sha, password_verifier=verifier, event_type="password_rotated")


def rotate_restore_fence(settings: FrontendPasswordAdminSettings) -> int:
    value = create_restore_fence(settings.restore_fence_path, replace=True)
    fence_sha = restore_fence_digest(value)
    return _rotate_state(settings, fence_sha=fence_sha, password_verifier=None, event_type="restore_fence_rotated")


def _rotate_state(
    settings: FrontendPasswordAdminSettings,
    *,
    fence_sha: str,
    password_verifier: str | None,
    event_type: str,
) -> int:
    engine = create_database_engine(DatabaseSettings(url=settings.database_url))
    factory = session_factory(engine)
    now = datetime.now(timezone.utc)
    try:
        with factory.begin() as session:
            repo = FrontendSecurityRepository(session)
            state = repo.state(for_update=True)
            if state is None:
                raise FrontendSecurityConfigurationError("frontend password has not been provisioned")
            if event_type == "password_rotated" and state.restore_fence_sha256 != fence_sha:
                raise FrontendSecurityConfigurationError("frontend restore fence does not match PostgreSQL security state")
            generation = state.security_generation + 1
            state.security_generation = generation
            state.restore_fence_sha256 = fence_sha
            if password_verifier is not None:
                state.password_verifier = password_verifier
            state.updated_at = now
            repo.revoke_all(now=now)
            repo.add_audit(event_type=event_type, now=now, generation=generation)
            repo.add_audit(event_type="global_invalidation", now=now, generation=generation, detail_code=event_type)
            return generation
    except SQLAlchemyError as exc:
        raise FrontendSecurityConfigurationError("frontend security rotation persistence failed") from exc
    finally:
        engine.dispose()


def prompt_password() -> str:
    first = getpass.getpass("New Dish frontend password: ")
    second = getpass.getpass("Confirm Dish frontend password: ")
    if first != second:
        raise FrontendSecurityConfigurationError("password confirmation does not match")
    return first
