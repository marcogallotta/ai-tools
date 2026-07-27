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
    max_body_bytes: int = 2 * 1024 * 1024
    request_timeout_seconds: float = 60.0

    @classmethod
    def from_env(cls) -> "ServiceConfig":
        return cls(
            db_path=Path(os.environ.get("DISH_DB_PATH", str(DEFAULT_DB_PATH))).expanduser(),
            honest_root=configured_honest_path(),
            bind_host=os.environ.get("DISH_SERVICE_BIND", "127.0.0.1").strip() or "127.0.0.1",
            port=int(os.environ.get("DISH_SERVICE_PORT", "8765")),
            max_body_bytes=int(os.environ.get("DISH_SERVICE_MAX_BODY_BYTES", str(2 * 1024 * 1024))),
            request_timeout_seconds=float(os.environ.get("DISH_SERVICE_REQUEST_TIMEOUT", "60")),
        )
