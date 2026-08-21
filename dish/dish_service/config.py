"""Configuration for the laptop-hosted dish service."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from dish_tool.constants import (
    DB_PATH,
    MAX_REQUEST_LIFETIME_SECONDS,
    RECOVERY_SAFETY_MARGIN_SECONDS,
)
from dish_tool.errors import DishRuleError
from dish_tool.releases import configured_honest_path

from .path_safety import PathIdentityError, require_distinct_paths


@dataclass(frozen=True)
class ServiceConfig:
    db_path: Path
    honest_root: Path
    bind_host: str = "127.0.0.1"
    port: int = 8765
    action_bind_host: str = "127.0.0.1"
    action_port: int = 8766
    action_public_base_url: str | None = None
    max_body_bytes: int = 2 * 1024 * 1024
    request_timeout_seconds: float = 30.0
    lease_ttl_seconds: int = 1800
    agent_token: str | None = None
    admin_token: str | None = None
    action_token: str | None = None
    action_client_id: str = "gpt-action"
    backup_dir: Path | None = None
    legacy_writer_fence_path: Path | None = None
    dark_launch_mode: str = "off"
    dark_launch_spool_path: Path | None = None
    dark_launch_emergency_dir: Path | None = None
    dark_launch_source_generation: str = "legacy-sqlite"
    dark_launch_kill_switch_path: Path | None = None
    dark_launch_busy_timeout_ms: int = 50
    dark_launch_max_spool_bytes: int = 512 * 1024 * 1024
    dark_launch_max_spool_records: int = 100_000
    dark_launch_min_free_bytes: int = 1024 * 1024 * 1024

    def validate_runtime(self, *, require_action: bool = True) -> None:
        """Fail closed before listeners bind or startup reports healthy."""
        tokens = {
            "agent": self.agent_token,
            "admin": self.admin_token,
            "action": self.action_token,
        }
        required = ("agent", "admin", "action") if require_action else ("agent", "admin")
        for name in required:
            raw_value = str(tokens[name] or "")
            value = raw_value.strip()
            if raw_value != value:
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    f"{name} service token must not contain surrounding whitespace",
                    rule="service_token_whitespace",
                    details={"token": name},
                )
            if not value:
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    f"{name} service token is required",
                    rule="service_token_required",
                    details={"token": name},
                )
            if len(value) < 10 or value.lower() in {
                "changeme", "change-me", "placeholder", "secret", "token",
            }:
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    f"{name} service token is too weak",
                    rule="service_token_weak",
                    details={"token": name},
                )
        configured = [(name, str(value or "").strip()) for name, value in tokens.items() if value]
        seen: dict[str, str] = {}
        for name, value in configured:
            other = seen.get(value)
            if other is not None:
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    "service tokens must be pairwise distinct",
                    rule="service_tokens_duplicate",
                    details={"tokens": sorted([other, name])},
                )
            seen[value] = name
        if self.dark_launch_mode not in {"off", "capture", "execute"}:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "dark_launch_mode must be off, capture, or execute",
                rule="dark_launch_mode_invalid",
            )
        if self.dark_launch_mode != "off":
            if (
                self.dark_launch_spool_path is None
                or self.dark_launch_emergency_dir is None
                or self.dark_launch_kill_switch_path is None
            ):
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    "dark-launch spool, emergency, and kill-switch paths are required when enabled",
                    rule="dark_launch_paths_required",
                )
            try:
                require_distinct_paths({
                    "authority database": self.db_path,
                    "dark-launch spool": self.dark_launch_spool_path,
                    "dark-launch emergency directory": self.dark_launch_emergency_dir,
                    "dark-launch kill switch": self.dark_launch_kill_switch_path,
                })
            except PathIdentityError as exc:
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    str(exc),
                    rule="dark_launch_paths_alias",
                ) from exc
            if not self.dark_launch_source_generation.strip():
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    "dark-launch source generation is required",
                    rule="dark_launch_generation_required",
                )
        for field, value in (
            ("max_body_bytes", self.max_body_bytes),
            ("request_timeout_seconds", self.request_timeout_seconds),
            ("lease_ttl_seconds", self.lease_ttl_seconds),
            ("dark_launch_busy_timeout_ms", self.dark_launch_busy_timeout_ms),
            ("dark_launch_max_spool_bytes", self.dark_launch_max_spool_bytes),
            ("dark_launch_max_spool_records", self.dark_launch_max_spool_records),
            ("dark_launch_min_free_bytes", self.dark_launch_min_free_bytes),
        ):
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    f"{field} must be positive",
                    rule="service_config_nonpositive",
                    details={"field": field},
                )
        minimum_lease_ttl = (
            MAX_REQUEST_LIFETIME_SECONDS + RECOVERY_SAFETY_MARGIN_SECONDS
        )
        if self.lease_ttl_seconds <= minimum_lease_ttl:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "lease_ttl_seconds must exceed the longest legitimate request plus the recovery safety margin",
                rule="service_lease_ttl_too_short",
                details={
                    "field": "lease_ttl_seconds",
                    "minimum_exclusive": minimum_lease_ttl,
                },
            )
        for field, value in (("port", self.port), ("action_port", self.action_port)):
            if not 0 <= value <= 65535:
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    f"{field} is outside the valid TCP range",
                    rule="service_port_invalid",
                    details={"field": field},
                )
        loopback = {"127.0.0.1", "::1", "localhost"}
        for field, value in (
            ("bind_host", self.bind_host),
            ("action_bind_host", self.action_bind_host),
        ):
            if value not in loopback:
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    f"{field} must be loopback",
                    rule="service_bind_not_loopback",
                    details={"field": field},
                )
        if self.port and self.action_port and self.port == self.action_port:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "private and Action listeners must use distinct ports",
                rule="service_ports_duplicate",
            )
        if self.action_public_base_url is not None:
            value = self.action_public_base_url
            parsed = urlsplit(value)
            if (
                value != value.strip()
                or parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or value.endswith("/")
            ):
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    "action_public_base_url must be an HTTPS URL without credentials, query, fragment, or trailing slash",
                    rule="service_action_public_base_url_invalid",
                )

    @classmethod
    def from_mapping(
        cls,
        environment: Mapping[str, str],
        *,
        db_path: Path | None = None,
    ) -> "ServiceConfig":
        """Build the runtime configuration from an explicit environment mapping."""
        env = environment
        raw_db_path = str(env.get("DISH_DB_PATH", "")).strip()
        effective_db_path = (
            db_path
            if db_path is not None
            else (Path(raw_db_path).expanduser() if raw_db_path else DB_PATH)
        )
        return cls(
            db_path=effective_db_path,
            honest_root=configured_honest_path(env),
            bind_host=str(env.get("DISH_SERVICE_BIND", "127.0.0.1")).strip()
            or "127.0.0.1",
            port=int(env.get("DISH_SERVICE_PORT", "8765")),
            action_bind_host=str(env.get("DISH_ACTION_BIND", "127.0.0.1")).strip()
            or "127.0.0.1",
            action_port=int(env.get("DISH_ACTION_PORT", "8766")),
            action_public_base_url=(
                str(env["DISH_ACTION_PUBLIC_BASE_URL"])
                if env.get("DISH_ACTION_PUBLIC_BASE_URL")
                else None
            ),
            max_body_bytes=int(
                env.get("DISH_SERVICE_MAX_BODY_BYTES", str(2 * 1024 * 1024))
            ),
            request_timeout_seconds=float(
                env.get("DISH_SERVICE_REQUEST_TIMEOUT", "30")
            ),
            lease_ttl_seconds=int(
                env.get("DISH_SERVICE_LEASE_TTL_SECONDS", "1800")
            ),
            agent_token=env.get("DISH_SERVICE_AGENT_TOKEN") or None,
            admin_token=env.get("DISH_SERVICE_ADMIN_TOKEN") or None,
            action_token=env.get("DISH_SERVICE_ACTION_TOKEN") or None,
            action_client_id=env.get("DISH_ACTION_CLIENT_ID") or "gpt-action",
            backup_dir=(
                Path(env["DISH_SERVICE_BACKUP_DIR"]).expanduser()
                if env.get("DISH_SERVICE_BACKUP_DIR")
                else None
            ),
            legacy_writer_fence_path=(
                Path(env["DISH_LEGACY_WRITER_FENCE"]).expanduser()
                if env.get("DISH_LEGACY_WRITER_FENCE")
                else effective_db_path.parent / "legacy-writer-fence.json"
            ),
            dark_launch_mode=str(env.get("DISH_DARK_LAUNCH_MODE", "off"))
            .strip()
            .lower(),
            dark_launch_spool_path=(
                Path(env["DISH_DARK_LAUNCH_SPOOL_PATH"]).expanduser()
                if env.get("DISH_DARK_LAUNCH_SPOOL_PATH")
                else effective_db_path.parent / "dark-launch-spool.sqlite3"
            ),
            dark_launch_emergency_dir=(
                Path(env["DISH_DARK_LAUNCH_EMERGENCY_DIR"]).expanduser()
                if env.get("DISH_DARK_LAUNCH_EMERGENCY_DIR")
                else effective_db_path.parent / "dark-launch-emergency"
            ),
            dark_launch_source_generation=str(
                env.get("DISH_DARK_LAUNCH_SOURCE_GENERATION", "legacy-sqlite")
            ).strip(),
            dark_launch_kill_switch_path=(
                Path(env["DISH_DARK_LAUNCH_KILL_SWITCH"]).expanduser()
                if env.get("DISH_DARK_LAUNCH_KILL_SWITCH")
                else effective_db_path.parent / "dark-launch.disabled"
            ),
            dark_launch_busy_timeout_ms=int(
                env.get("DISH_DARK_LAUNCH_BUSY_TIMEOUT_MS", "50")
            ),
            dark_launch_max_spool_bytes=int(
                env.get(
                    "DISH_DARK_LAUNCH_MAX_SPOOL_BYTES",
                    str(512 * 1024 * 1024),
                )
            ),
            dark_launch_max_spool_records=int(
                env.get("DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS", "100000")
            ),
            dark_launch_min_free_bytes=int(
                env.get(
                    "DISH_DARK_LAUNCH_MIN_FREE_BYTES",
                    str(1024 * 1024 * 1024),
                )
            ),
        )

    @classmethod
    def from_env(cls) -> "ServiceConfig":
        return cls.from_mapping(os.environ, db_path=DB_PATH)
