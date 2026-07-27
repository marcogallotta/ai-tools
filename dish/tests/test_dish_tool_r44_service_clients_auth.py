import json
import threading
from pathlib import Path

import pytest

from dish_service.application import DishService
from dish_service.client import DishAdminServiceClient, DishServiceClient
from dish_service.config import ServiceConfig
from dish_service.http import build_server
from dish_tool import admin_cli, cli
from dish_tool.errors import DishRuleError
from tests.test_dish_tool_r42_service_foundation import _release_loader
from tests.test_dish_tool_step7_verification import Backend, TASK


def _running_service(tmp_path):
    backend = Backend()
    honest = tmp_path / "honest"
    honest.mkdir()
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            port=0,
            agent_token="agent-secret",
            admin_token="admin-secret",
        ),
        backend_factory=lambda: backend,
        release_loader=_release_loader(honest),
    )
    server = build_server(service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return service, backend, server, thread, f"http://{host}:{port}"


def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_unauthorized_clients_cannot_read_or_mutate(tmp_path):
    _service, backend, server, thread, url = _running_service(tmp_path)
    try:
        wrong = DishServiceClient(url, token="wrong", run_id="run")
        read = wrong.execute("read", {"agent": "gpt", "task_gid": "t"})
        start = wrong.execute("start", {"agent": "gpt", "task_gid": "t", "kind": "initial"})
    finally:
        _stop(server, thread)
    assert read["code"] == "AGENT_MISMATCH"
    assert start["code"] == "AGENT_MISMATCH"
    assert backend.writes == 0
    assert backend.moves == 0


def test_agent_and_admin_credentials_are_separate(tmp_path):
    _service, _backend, server, thread, url = _running_service(tmp_path)
    try:
        agent = DishServiceClient(url, token="agent-secret", run_id="run")
        admin_with_agent_token = DishAdminServiceClient(url, token="agent-secret", run_id="run")
        agent_result = agent.execute("sections", {"agent": "gpt"})
        admin_result = admin_with_agent_token.execute("discard", {"submission_id": "missing", "reason": "x"})
    finally:
        _stop(server, thread)
    assert agent_result["ok"]
    assert admin_result["code"] == "AGENT_MISMATCH"
    assert admin_result["errors"][0]["rule"] == "service_scope_forbidden"


def test_service_mode_cli_does_not_open_local_database_or_asana_backend(monkeypatch):
    monkeypatch.setenv("DISH_MODE", "service")
    monkeypatch.setenv("DISH_SERVICE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("DISH_SERVICE_TOKEN", "agent-secret")
    monkeypatch.setenv("DISH_CLIENT_RUN_ID", "run")
    monkeypatch.setattr(cli, "initialize_database", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("local database opened")))
    monkeypatch.setattr(cli, "AsanaBackend", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Asana backend created")))
    app = cli.build_application()
    assert isinstance(app, DishServiceClient)
    assert not hasattr(app, "conn")


def test_live_mode_fails_closed_without_service(monkeypatch):
    monkeypatch.setenv("DISH_LIVE_MODE", "1")
    monkeypatch.delenv("DISH_MODE", raising=False)
    monkeypatch.delenv("DISH_SERVICE_URL", raising=False)
    with pytest.raises(DishRuleError) as exc:
        cli.build_application()
    assert exc.value.rule == "shared_service_required"


def test_remote_cli_transports_candidate_text_not_client_path(tmp_path, monkeypatch, capsys):
    _service, backend, server, thread, url = _running_service(tmp_path)
    candidate = tmp_path / "candidate.txt"
    candidate.write_text(TASK)
    monkeypatch.setenv("DISH_MODE", "service")
    monkeypatch.setenv("DISH_SERVICE_URL", url)
    monkeypatch.setenv("DISH_SERVICE_TOKEN", "agent-secret")
    monkeypatch.setenv("DISH_CLIENT_RUN_ID", "constructor-run")
    try:
        assert cli.main(["start", "123456789", "--agent", "gpt", "--kind", "initial", "--run-id", "constructor-run"]) == 0
        started = json.loads(capsys.readouterr().out)
        assert cli.main([
            "prepare", started["submission_id"], "--agent", "gpt", "--model", "gpt-5.6-sol",
            "--file", str(candidate),
        ]) == 0
        prepared = json.loads(capsys.readouterr().out)
    finally:
        _stop(server, thread)
    assert prepared["ok"]
    assert backend.writes == 1
    assert backend.moves == 1


def test_service_token_never_appears_in_results(tmp_path):
    _service, _backend, server, thread, url = _running_service(tmp_path)
    try:
        client = DishServiceClient(url, token="agent-secret", run_id="run")
        result = client.execute("sections", {"agent": "gpt"})
    finally:
        _stop(server, thread)
    assert "agent-secret" not in json.dumps(result)
    assert "admin-secret" not in json.dumps(result)


def test_admin_cli_builds_remote_admin_client(monkeypatch):
    monkeypatch.setenv("DISH_MODE", "service")
    monkeypatch.setenv("DISH_SERVICE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("DISH_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("DISH_CLIENT_RUN_ID", "admin-run")
    monkeypatch.setattr(admin_cli, "initialize_database", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("local database opened")))
    monkeypatch.setattr(admin_cli, "AsanaBackend", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Asana backend created")))
    app = admin_cli.build_application()
    assert isinstance(app, DishAdminServiceClient)
