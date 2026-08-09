"""Validated configuration for the private frontend security/runtime surface."""
from __future__ import annotations

import base64
import hmac
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import unquote, urlsplit

from .frontend_security import Argon2Policy, FrontendSecurityConfigurationError


def _required(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name, ""))
    if not value or value != value.strip():
        raise FrontendSecurityConfigurationError(f"{name} is required without surrounding whitespace")
    return value


def _flag(env: Mapping[str, str], name: str, *, default: bool = False) -> bool:
    raw = str(env.get(name, "1" if default else "0")).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise FrontendSecurityConfigurationError(f"{name} must be an explicit boolean")


def _positive_int(env: Mapping[str, str], name: str) -> int:
    raw = _required(env, name)
    try:
        value = int(raw)
    except ValueError as exc:
        raise FrontendSecurityConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise FrontendSecurityConfigurationError(f"{name} must be positive")
    return value


def _secret(env: Mapping[str, str], name: str) -> bytes:
    raw = _required(env, name)
    try:
        value = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception as exc:
        raise FrontendSecurityConfigurationError(f"{name} must be base64url security material") from exc
    if len(value) < 32:
        raise FrontendSecurityConfigurationError(f"{name} must decode to at least 32 bytes")
    return value


def _origin(value: str, *, field: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise FrontendSecurityConfigurationError(f"{field} must be one absolute HTTPS origin")
    if parsed.path == "/":
        value = value[:-1]
    return value


@dataclass(frozen=True, slots=True)
class FrontendRuntimeSettings:
    enabled: bool
    origin: str | None = None
    action_origin: str | None = None
    database_url: str | None = None
    observation_database_url: str | None = None
    static_root: Path | None = None
    restore_fence_path: Path | None = None
    token_secret: bytes | None = None
    session_secret: bytes | None = None
    csrf_secret: bytes | None = None
    peer_secret: bytes | None = None
    argon2_policy: Argon2Policy | None = None
    postgresql_reads_enabled: bool = False
    projection_delay_seconds: int | None = None
    refresh_interval_seconds: int | None = None

    @classmethod
    def from_mapping(cls, env: Mapping[str, str], *, dish_root: Path) -> "FrontendRuntimeSettings":
        enabled = _flag(env, "DISH_FRONTEND_ENABLED")
        if not enabled:
            return cls(enabled=False)
        origin = _origin(_required(env, "DISH_FRONTEND_ORIGIN"), field="DISH_FRONTEND_ORIGIN")
        action_origin = _origin(_required(env, "DISH_ACTION_PUBLIC_ORIGIN"), field="DISH_ACTION_PUBLIC_ORIGIN")
        if urlsplit(origin).hostname == urlsplit(action_origin).hostname:
            raise FrontendSecurityConfigurationError("frontend and Action origins must use different hostnames")
        secrets = {
            "token": _secret(env, "DISH_FRONTEND_TOKEN_SECRET"),
            "session": _secret(env, "DISH_FRONTEND_SESSION_SECRET"),
            "csrf": _secret(env, "DISH_FRONTEND_CSRF_SECRET"),
            "peer": _secret(env, "DISH_FRONTEND_PEER_SECRET"),
        }
        names = tuple(secrets)
        for index, name in enumerate(names):
            for other in names[index + 1:]:
                if hmac.compare_digest(secrets[name], secrets[other]):
                    raise FrontendSecurityConfigurationError("frontend security secrets must be pairwise distinct")
        frontend_secret_text = tuple(_required(env, f"DISH_FRONTEND_{name.upper()}_SECRET") for name in names)
        database_url = _required(env, "DISH_FRONTEND_DATABASE_URL")
        reads_enabled = _flag(env, "DISH_FRONTEND_POSTGRESQL_READS_ENABLED")
        observation_database_url = None
        if reads_enabled or env.get("DISH_FRONTEND_OBSERVATION_DATABASE_URL"):
            observation_database_url = _required(env, "DISH_FRONTEND_OBSERVATION_DATABASE_URL")
        database_passwords = [unquote(urlsplit(database_url).password or "")]
        if observation_database_url is not None:
            database_passwords.append(unquote(urlsplit(observation_database_url).password or ""))
        existing_secrets = [
            str(env[field])
            for field in ("DISH_SERVICE_AGENT_TOKEN", "DISH_SERVICE_ADMIN_TOKEN", "DISH_SERVICE_ACTION_TOKEN")
            if env.get(field)
        ]
        existing_secrets.extend(password for password in database_passwords if password)
        if any(hmac.compare_digest(frontend, existing) for frontend in frontend_secret_text for existing in existing_secrets):
            raise FrontendSecurityConfigurationError("frontend security secrets must differ from existing service/database secrets")
        policy = Argon2Policy(
            time_cost=_positive_int(env, "DISH_FRONTEND_ARGON2_TIME_COST"),
            memory_cost_kib=_positive_int(env, "DISH_FRONTEND_ARGON2_MEMORY_KIB"),
            parallelism=_positive_int(env, "DISH_FRONTEND_ARGON2_PARALLELISM"),
            hash_len=_positive_int(env, "DISH_FRONTEND_ARGON2_HASH_LEN"),
            salt_len=_positive_int(env, "DISH_FRONTEND_ARGON2_SALT_LEN"),
            min_time_cost=_positive_int(env, "DISH_FRONTEND_ARGON2_MIN_TIME_COST"),
            max_time_cost=_positive_int(env, "DISH_FRONTEND_ARGON2_MAX_TIME_COST"),
            min_memory_cost_kib=_positive_int(env, "DISH_FRONTEND_ARGON2_MIN_MEMORY_KIB"),
            max_memory_cost_kib=_positive_int(env, "DISH_FRONTEND_ARGON2_MAX_MEMORY_KIB"),
            min_parallelism=_positive_int(env, "DISH_FRONTEND_ARGON2_MIN_PARALLELISM"),
            max_parallelism=_positive_int(env, "DISH_FRONTEND_ARGON2_MAX_PARALLELISM"),
        )
        projection_delay = (
            _positive_int(env, "DISH_FRONTEND_PROJECTION_DELAY_SECONDS")
            if reads_enabled else None
        )
        refresh_interval = _positive_int(env, "DISH_FRONTEND_REFRESH_INTERVAL_SECONDS")
        if refresh_interval > 30:
            raise FrontendSecurityConfigurationError(
                "DISH_FRONTEND_REFRESH_INTERVAL_SECONDS must be at most 30"
            )
        static_root = Path(env.get("DISH_FRONTEND_STATIC_ROOT") or dish_root / "frontend" / "dist").expanduser()
        restore_path = Path(_required(env, "DISH_FRONTEND_RESTORE_FENCE_PATH")).expanduser()
        return cls(
            enabled=True,
            origin=origin,
            action_origin=action_origin,
            database_url=database_url,
            observation_database_url=observation_database_url,
            static_root=static_root,
            restore_fence_path=restore_path,
            token_secret=secrets["token"],
            session_secret=secrets["session"],
            csrf_secret=secrets["csrf"],
            peer_secret=secrets["peer"],
            argon2_policy=policy,
            postgresql_reads_enabled=reads_enabled,
            projection_delay_seconds=projection_delay,
            refresh_interval_seconds=refresh_interval,
        )
