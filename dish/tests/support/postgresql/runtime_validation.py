from __future__ import annotations

import copy
from pathlib import Path

from dish_pg.postgres_service import PostgresRuntimeService
from dish_service.config import ServiceConfig

_CURSOR_SECRET = b"postgres-validation-replay-secret"


def runtime_service(factory, tmp_path: Path) -> PostgresRuntimeService:
    service = PostgresRuntimeService.__new__(PostgresRuntimeService)
    service.config = ServiceConfig(
        db_path=tmp_path / "unused.sqlite3",
        honest_root=tmp_path,
        port=0,
        action_port=0,
        agent_token="postgres-agent-token",
        admin_token="postgres-admin-token",
        action_token=None,
        legacy_writer_fence_path=None,
    )
    service._session_maker = factory
    service._cursor_secret = _CURSOR_SECRET
    return service


def without_replay_metadata(payload: dict[str, object]) -> dict[str, object]:
    normalized = copy.deepcopy(payload)
    data = normalized.get("data")
    assert isinstance(data, dict)
    data.pop("request_replayed", None)
    return normalized
