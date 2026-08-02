from __future__ import annotations

import json
import stat
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.http import build_server
from dish_service.legacy_writer_fence import (
    engage_legacy_writer_fence,
    read_legacy_writer_fence,
    release_legacy_writer_fence,
)
from dish_tool.errors import DishRuleError
from tests.support.service_foundation import _release_loader
from tests.support.thread_teardown import start_server_thread, stop_server
from tests.support.verification import Backend

NOW = datetime(2026, 8, 1, 22, 0, tzinfo=timezone.utc)


def test_legacy_writer_fence_is_atomic_fail_closed_and_digest_bound(tmp_path: Path) -> None:
    path = tmp_path / "state" / "legacy-writer-fence.json"
    manifest, digest = engage_legacy_writer_fence(
        path,
        fence_id="fence-1",
        candidate_id="candidate-1",
        source_release="dish-42619b9",
        source_commit="42619b9",
        engaged_at=NOW,
        operator="Marco",
    )
    assert read_legacy_writer_fence(path) == (manifest, digest)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert engage_legacy_writer_fence(
        path,
        fence_id="fence-1",
        candidate_id="candidate-1",
        source_release="dish-42619b9",
        source_commit="42619b9",
        engaged_at=NOW,
        operator="Marco",
    )[1] == digest
    with pytest.raises(DishRuleError) as conflict:
        engage_legacy_writer_fence(
            path,
            fence_id="fence-2",
            candidate_id="candidate-1",
            source_release="dish-42619b9",
            source_commit="42619b9",
            engaged_at=NOW,
            operator="Marco",
        )
    assert conflict.value.rule == "legacy_writer_fence_conflict"
    with pytest.raises(DishRuleError) as release_conflict:
        release_legacy_writer_fence(path, expected_sha256="0" * 64)
    assert release_conflict.value.rule == "legacy_writer_fence_release_conflict"
    release_legacy_writer_fence(path, expected_sha256=digest)
    assert not path.exists()

    path.write_text("not-json", encoding="utf-8")
    unreadable, unreadable_digest = read_legacy_writer_fence(path)
    assert unreadable["format"] == "dish-legacy-writer-fence-unreadable-v1"
    assert len(unreadable_digest) == 64


def test_http_fence_runs_after_authentication_and_before_body_parsing(tmp_path: Path) -> None:
    honest = tmp_path / "honest"
    honest.mkdir()
    fence_path = tmp_path / "legacy-writer-fence.json"
    engage_legacy_writer_fence(
        fence_path,
        fence_id="fence-1",
        candidate_id="candidate-1",
        source_release="dish-42619b9",
        source_commit="42619b9",
        engaged_at=NOW,
        operator="Marco",
    )
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            port=0,
            max_body_bytes=16,
            agent_token="cli-secret-1",
            admin_token="admin-secret-1",
            action_token="action-secret-1",
            legacy_writer_fence_path=fence_path,
        ),
        backend_factory=lambda: Backend(task_gid="123456789"),
        release_loader=_release_loader(honest),
    )
    server = build_server(service)
    thread = start_server_thread(server, daemon=True, name="stage6-fence-http")
    host, port = server.server_address
    parsed = urlsplit(f"http://{host}:{port}")
    try:
        connection = HTTPConnection(parsed.hostname, parsed.port, timeout=2)
        connection.request(
            "POST",
            "/v1/commands/start",
            body=b"{" + b"x" * 1000,
            headers={"Authorization": "Bearer wrong-token", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        unauthorized = json.loads(response.read())
        connection.close()
        assert response.status == 401
        assert unauthorized["errors"][0]["rule"] == "service_auth_invalid"

        connection = HTTPConnection(parsed.hostname, parsed.port, timeout=2)
        connection.request(
            "POST",
            "/v1/commands/start",
            body=b"{" + b"x" * 1000,
            headers={"Authorization": "Bearer cli-secret-1", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        fenced = json.loads(response.read())
        connection.close()
        assert response.status == 409
        assert fenced["errors"][0]["rule"] == "legacy_writer_fenced"
    finally:
        stop_server(server, thread)
