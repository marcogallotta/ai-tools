"""Configuration for the laptop-hosted dish service."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

from dish_tool.constants import (
    DB_PATH,
    MAX_REQUEST_LIFETIME_SECONDS,
    RECOVERY_SAFETY_MARGIN_SECONDS,
)
from dish_tool.errors import DishRuleError
from dish_tool.releases import configured_honest_path


@dataclass(frozen=True)
class ServiceConfig:
    db_path: Path
    honest_root: Path
    bind_host: str = "127.0.0.1"
    port: int = 8765
    action_bind_host: str = "127.0.0.1"
    action_port: int = 8766
    max_body_bytes: int = 2 * 1024 * 1024
    request_timeout_seconds: float = 30.0
    lease_ttl_seconds: int = 1800
    agent_token: str | None = None
    admin_token: str | None = None
    action_token: str | None = None
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
            if self.dark_launch_spool_path is None or self.dark_launch_emergency_dir is None:
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    "dark-launch spool and emergency paths are required when enabled",
                    rule="dark_launch_paths_required",
                )
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

    @classmethod
    def from_env(cls) -> "ServiceConfig":
        return cls(
            db_path=DB_PATH,
            honest_root=configured_honest_path(),
            bind_host=os.environ.get("DISH_SERVICE_BIND", "127.0.0.1").strip() or "127.0.0.1",
            port=int(os.environ.get("DISH_SERVICE_PORT", "8765")),
            action_bind_host=os.environ.get("DISH_ACTION_BIND", "127.0.0.1").strip() or "127.0.0.1",
            action_port=int(os.environ.get("DISH_ACTION_PORT", "8766")),
            max_body_bytes=int(os.environ.get("DISH_SERVICE_MAX_BODY_BYTES", str(2 * 1024 * 1024))),
            request_timeout_seconds=float(os.environ.get("DISH_SERVICE_REQUEST_TIMEOUT", "30")),
            lease_ttl_seconds=int(os.environ.get("DISH_SERVICE_LEASE_TTL_SECONDS", "1800")),
            agent_token=os.environ.get("DISH_SERVICE_AGENT_TOKEN") or None,
            admin_token=os.environ.get("DISH_SERVICE_ADMIN_TOKEN") or None,
            action_token=os.environ.get("DISH_SERVICE_ACTION_TOKEN") or None,
            backup_dir=(
                Path(os.environ["DISH_SERVICE_BACKUP_DIR"]).expanduser()
                if os.environ.get("DISH_SERVICE_BACKUP_DIR")
                else None
            ),
            legacy_writer_fence_path=(
                Path(os.environ["DISH_LEGACY_WRITER_FENCE"]).expanduser()
                if os.environ.get("DISH_LEGACY_WRITER_FENCE")
                else DB_PATH.parent / "legacy-writer-fence.json"
            ),
            dark_launch_mode=os.environ.get("DISH_DARK_LAUNCH_MODE", "off").strip().lower(),
            dark_launch_spool_path=(
                Path(os.environ["DISH_DARK_LAUNCH_SPOOL_PATH"]).expanduser()
                if os.environ.get("DISH_DARK_LAUNCH_SPOOL_PATH")
                else DB_PATH.parent / "dark-launch-spool.sqlite3"
            ),
            dark_launch_emergency_dir=(
                Path(os.environ["DISH_DARK_LAUNCH_EMERGENCY_DIR"]).expanduser()
                if os.environ.get("DISH_DARK_LAUNCH_EMERGENCY_DIR")
                else DB_PATH.parent / "dark-launch-emergency"
            ),
            dark_launch_source_generation=os.environ.get(
                "DISH_DARK_LAUNCH_SOURCE_GENERATION", "legacy-sqlite"
            ).strip(),
            dark_launch_kill_switch_path=(
                Path(os.environ["DISH_DARK_LAUNCH_KILL_SWITCH"]).expanduser()
                if os.environ.get("DISH_DARK_LAUNCH_KILL_SWITCH")
                else DB_PATH.parent / "dark-launch.disabled"
            ),
            dark_launch_busy_timeout_ms=int(
                os.environ.get("DISH_DARK_LAUNCH_BUSY_TIMEOUT_MS", "50")
            ),
            dark_launch_max_spool_bytes=int(
                os.environ.get("DISH_DARK_LAUNCH_MAX_SPOOL_BYTES", str(512 * 1024 * 1024))
            ),
            dark_launch_max_spool_records=int(
                os.environ.get("DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS", "100000")
            ),
            dark_launch_min_free_bytes=int(
                os.environ.get("DISH_DARK_LAUNCH_MIN_FREE_BYTES", str(1024 * 1024 * 1024))
            ),
        )
