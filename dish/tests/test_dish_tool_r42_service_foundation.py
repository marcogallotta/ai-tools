import threading
from dataclasses import replace
from pathlib import Path

import pytest

from dish_service.application import DishService
from dish_service.client import DishServiceClient
from dish_service.config import ServiceConfig
from dish_service.http import build_server
from dish_tool.commands import DishApplication
from dish_tool.database import initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.models import ResolvedRelease
from tests.test_dish_tool_step7_verification import Backend, TASK


def _release_loader(root: Path):
    verification = "# frozen verification\n"
    (root / "dish-verification-protocol.md").write_text(verification)

    def load(role=None, include_migrations=False):
        return ResolvedRelease(
            version="1.0.10",
            commit="",
            root=root,
            protocols={} if role is None else {role: verification if role == "verification" else f"{role} protocol"},
            manifests={},
            manifest_texts={},
            schema_version="2",
            schema={},
            schema_text="{}",
            migration_metadata={},
            requested_protocol_role=role,
        )

    return load


def _service(tmp_path, backend, *, loader=None):
    honest = tmp_path / "honest"
    honest.mkdir(exist_ok=True)
    return DishService(
        ServiceConfig(db_path=tmp_path / "shared.db", honest_root=honest, port=0),
        backend_factory=lambda: backend,
        release_loader=loader or _release_loader(honest),
    )


def test_service_and_direct_application_share_canonical_result_contract(tmp_path):
    backend = Backend()
    honest = tmp_path / "honest"
    honest.mkdir()
    loader = _release_loader(honest)
    direct = DishApplication(
        initialize_database(tmp_path / "direct.db"), backend, release_loader=loader
    )
    service = DishService(
        ServiceConfig(db_path=tmp_path / "service.db", honest_root=honest),
        backend_factory=lambda: backend,
        release_loader=loader,
    )
    try:
        expected = direct.execute("read", agent="gpt", task_gid="t")
        actual = service.execute_agent("read", {"agent": "gpt", "task_gid": "t"})
    finally:
        direct.conn.close()
    assert actual == expected
    assert list(actual) == [
        "ok", "command", "code", "task_gid", "submission_id", "state",
        "retryable", "allowed_actions", "data", "errors",
    ]


def test_service_restart_preserves_open_operation(tmp_path):
    backend = Backend()
    service = _service(tmp_path, backend)
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial", "run_id": "constructor"},
    )
    assert started["ok"]

    restarted = DishService(
        service.config,
        backend_factory=lambda: backend,
        release_loader=service.release_loader,
    )
    inspected = restarted.execute_agent(
        "inspect",
        {"agent": "gpt", "submission_id": started["submission_id"]},
    )
    assert inspected["ok"]
    assert inspected["submission_id"] == started["submission_id"]
    assert inspected["allowed_actions"] == ["prepare"]


def test_compatibility_failure_blocks_mutation_before_backend_write(tmp_path):
    backend = Backend()

    def incompatible(*_args, **_kwargs):
        raise DishRuleError(
            "PROTOCOL_INCOMPATIBLE",
            "service and Honest versions do not match",
            rule="protocol_version_unsupported",
        )

    service = _service(tmp_path, backend, loader=incompatible)
    result = service.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial"}
    )
    assert result["code"] == "PROTOCOL_INCOMPATIBLE"
    assert backend.writes == 0
    assert backend.moves == 0


def test_loopback_http_transport_returns_same_envelope(tmp_path):
    backend = Backend()
    service = _service(tmp_path, backend)
    server = build_server(service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        client = DishServiceClient(f"http://{host}:{port}")
        health = client.health()
        result = client.execute("sections", {"agent": "gpt"})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert health["ok"]
    assert result["ok"]
    assert result["command"] == "sections"
    assert result["data"]["sections"][0] == {"gid": "rq", "name": "Research Queue"}


def test_http_request_size_limit_fails_before_command(tmp_path):
    backend = Backend()
    service = _service(tmp_path, backend)
    service = DishService(
        replace(service.config, max_body_bytes=20),
        backend_factory=lambda: backend,
        release_loader=service.release_loader,
    )
    server = build_server(service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        client = DishServiceClient(f"http://{host}:{port}")
        result = client.execute("create", {"agent": "gpt", "title": "x" * 100})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["rule"] == "request_too_large"
