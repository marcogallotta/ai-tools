"""Configuration for the laptop-hosted dish service."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dish_tool.constants import DEFAULT_DB_PATH
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
    request_timeout_seconds: float = 60.0
    lease_ttl_seconds: int = 1800
    agent_token: str | None = None
    admin_token: str | None = None
    action_token: str | None = None
    backup_dir: Path | None = None

    @classmethod
    def from_env(cls) -> "ServiceConfig":
        return cls(
            db_path=Path(os.environ.get("DISH_DB_PATH", str(DEFAULT_DB_PATH))).expanduser(),
            honest_root=configured_honest_path(),
            bind_host=os.environ.get("DISH_SERVICE_BIND", "127.0.0.1").strip() or "127.0.0.1",
            port=int(os.environ.get("DISH_SERVICE_PORT", "8765")),
            action_bind_host=os.environ.get("DISH_ACTION_BIND", "127.0.0.1").strip() or "127.0.0.1",
            action_port=int(os.environ.get("DISH_ACTION_PORT", "8766")),
            max_body_bytes=int(os.environ.get("DISH_SERVICE_MAX_BODY_BYTES", str(2 * 1024 * 1024))),
            request_timeout_seconds=float(os.environ.get("DISH_SERVICE_REQUEST_TIMEOUT", "60")),
            lease_ttl_seconds=int(os.environ.get("DISH_SERVICE_LEASE_TTL_SECONDS", "1800")),
            agent_token=os.environ.get("DISH_SERVICE_AGENT_TOKEN") or None,
            admin_token=os.environ.get("DISH_SERVICE_ADMIN_TOKEN") or None,
            action_token=os.environ.get("DISH_SERVICE_ACTION_TOKEN") or None,
            backup_dir=(
                Path(os.environ["DISH_SERVICE_BACKUP_DIR"]).expanduser()
                if os.environ.get("DISH_SERVICE_BACKUP_DIR")
                else None
            ),
        )
