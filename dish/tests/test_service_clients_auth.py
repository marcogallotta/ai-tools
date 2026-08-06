import json
import threading
import uuid
from pathlib import Path

import pytest

from dish_service.application import DishService
from dish_service.client import (
    DishActionClient,
    DishAdminServiceClient,
    DishServiceClient,
)
from dish_service.config import ServiceConfig
from dish_service.http import build_server
from dish_tool import admin_cli, cli
from dish_tool.errors import DishRuleError
from tests.support.thread_teardown import join_thread, start_server_thread, stop_server
from tests.support.service_foundation import _release_loader
from tests.support.verification import Backend, TASK


def _running_service(tmp_path):
    backend = Backend(task_gid="123456789")
    backend.add_task(
        task_gid="t",
        title=backend.title,
        notes=backend.notes,
        section_gid=backend.section,
    )
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
    thread = start_server_thread(server, daemon=True, name="thread")
    host, port = server.server_address
    return service, backend, server, thread, f"http://{host}:{port}"


def _stop(server, thread):
    stop_server(server, thread)


def test_unauthorized_clients_cannot_read_or_mutate(tmp_path):
    _service, backend, server, thread, url = _running_service(tmp_path)
    try:
        wrong = DishServiceClient(url, token="wrong", run_id="11111111-1111-4111-8111-111111111111")
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
        agent = DishServiceClient(url, token="agent-secret", run_id="11111111-1111-4111-8111-111111111111")
        admin_with_agent_token = DishAdminServiceClient(url, token="agent-secret", run_id="11111111-1111-4111-8111-111111111111")
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
    monkeypatch.setenv("DISH_CLIENT_RUN_ID", "22222222-2222-4222-8222-222222222222")
    try:
        assert cli.main(["start", "123456789", "--agent", "gpt", "--kind", "initial", "--run-id", "22222222-2222-4222-8222-222222222222"]) == 0
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


def test_sections_result_omits_agent_and_admin_tokens(tmp_path):
    _service, _backend, server, thread, url = _running_service(tmp_path)
    try:
        client = DishServiceClient(url, token="agent-secret", run_id="11111111-1111-4111-8111-111111111111")
        result = client.execute("sections", {"agent": "gpt"})
    finally:
        _stop(server, thread)
    assert "agent-secret" not in json.dumps(result)
    assert "admin-secret" not in json.dumps(result)


@pytest.mark.parametrize(
    ("client_type", "expected_path"),
    [
        (DishServiceClient, "/v1/commands/apply-proposal"),
        (DishActionClient, "/v1/action/apply-proposal"),
    ],
)
def test_apply_proposal_clients_generate_and_preserve_request_identity(
    client_type, expected_path
):
    payloads = []

    class CapturingClient(client_type):
        def _result_request(
            self, path, *, method, payload, ambiguous_after_dispatch=False
        ):
            assert path == expected_path
            assert method == "POST"
            assert ambiguous_after_dispatch is True
            payloads.append(payload)
            return {"ok": True}

    client = CapturingClient(
        "http://dish.invalid",
        token="agent-secret",
        run_id="11111111-1111-4111-8111-111111111111",
    )
    arguments = {
        "proposal_id": "22222222-2222-4222-8222-222222222222",
        "agent": "gpt",
        "model": "gpt-5.6-sol",
    }
    client.execute("apply-proposal", arguments)
    explicit = "33333333-3333-4333-8333-333333333333"
    client.execute("apply-proposal", arguments, request_id=explicit)

    generated = payloads[0]["client"]["request_id"]
    assert str(uuid.UUID(generated)) == generated
    assert generated != "00000000-0000-0000-0000-000000000000"
    assert payloads[1]["client"]["request_id"] == explicit


def test_apply_proposal_cli_accepts_explicit_request_identity():
    request_id = "33333333-3333-4333-8333-333333333333"
    parsed = cli.build_parser().parse_args(
        [
            "apply-proposal",
            "22222222-2222-4222-8222-222222222222",
            "--agent",
            "gpt",
            "--model",
            "gpt-5.6-sol",
            "--request-id",
            request_id,
        ]
    )
    assert parsed.request_id == request_id


def test_admin_cli_builds_remote_admin_client(monkeypatch):
    monkeypatch.setenv("DISH_MODE", "service")
    monkeypatch.setenv("DISH_SERVICE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("DISH_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("DISH_CLIENT_RUN_ID", "admin-run")
    monkeypatch.setattr(admin_cli, "initialize_database", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("local database opened")))
    monkeypatch.setattr(admin_cli, "AsanaBackend", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Asana backend created")))
    app = admin_cli.build_application()
    assert isinstance(app, DishAdminServiceClient)


def test_named_profiles_select_matching_url_and_agent_token(monkeypatch):
    monkeypatch.setenv("DISH_MODE", "service")
    monkeypatch.setenv("DISH_CLIENT_RUN_ID", "profile-run")
    monkeypatch.setenv("DISH_SERVICE_URL", "http://legacy.invalid")
    monkeypatch.setenv("DISH_SERVICE_TOKEN", "legacy-token")
    monkeypatch.setenv("DISH_SERVICE_URL_PROD", "http://prod.invalid")
    monkeypatch.setenv("DISH_SERVICE_TOKEN_PROD", "prod-token")
    monkeypatch.setenv("DISH_SERVICE_URL_TEST", "http://test.invalid")
    monkeypatch.setenv("DISH_SERVICE_TOKEN_TEST", "test-token")

    default_client = cli.build_application()
    test_client = cli.build_application("test")

    assert default_client.base_url == "http://prod.invalid"
    assert default_client.token == "prod-token"
    assert test_client.base_url == "http://test.invalid"
    assert test_client.token == "test-token"


def test_admin_profiles_use_environment_specific_admin_tokens(monkeypatch):
    monkeypatch.setenv("DISH_MODE", "service")
    monkeypatch.setenv("DISH_CLIENT_RUN_ID", "profile-run")
    monkeypatch.setenv("DISH_SERVICE_URL_PROD", "http://prod.invalid")
    monkeypatch.setenv("DISH_SERVICE_URL_TEST", "http://test.invalid")
    monkeypatch.setenv("DISH_ADMIN_TOKEN_TEST", "test-admin-token")
    monkeypatch.delenv("DISH_ADMIN_TOKEN_PROD", raising=False)
    monkeypatch.delenv("DISH_ADMIN_TOKEN", raising=False)

    test_client = admin_cli.build_application("test")

    assert test_client.base_url == "http://test.invalid"
    assert test_client.token == "test-admin-token"
    with pytest.raises(DishRuleError) as exc:
        admin_cli.build_application("prod")
    assert exc.value.rule == "service_token_required"


def test_mode_is_required_for_direct_local_operation(monkeypatch):
    monkeypatch.delenv("DISH_MODE", raising=False)
    monkeypatch.delenv("DISH_SERVICE_URL", raising=False)
    monkeypatch.delenv("DISH_LIVE_MODE", raising=False)
    with pytest.raises(DishRuleError) as exc:
        cli.build_application()
    assert exc.value.rule == "dish_mode_required"


def test_service_owned_database_rejects_direct_agent_and_admin_mode(tmp_path, monkeypatch):
    from dish_service.database_ownership import ServiceDatabaseOwnership

    db_path = tmp_path / "shared.sqlite3"
    ServiceDatabaseOwnership(db_path).mark()
    monkeypatch.setenv("DISH_MODE", "local")
    monkeypatch.delenv("DISH_LIVE_MODE", raising=False)
    monkeypatch.setattr(cli, "DB_PATH", db_path)
    monkeypatch.setattr(admin_cli, "DB_PATH", db_path)
    monkeypatch.setattr(
        cli, "initialize_database",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("database opened")),
    )
    monkeypatch.setattr(
        admin_cli, "initialize_database",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("database opened")),
    )

    with pytest.raises(DishRuleError) as agent_exc:
        cli.build_application()
    with pytest.raises(DishRuleError) as admin_exc:
        admin_cli.build_application()
    assert agent_exc.value.rule == "service_owned_database"
    assert admin_exc.value.rule == "service_owned_database"


def test_service_database_ownership_marker_survives_reinstantiation(tmp_path):
    from dish_service.database_ownership import ServiceDatabaseOwnership

    db_path = tmp_path / "shared.sqlite3"
    marker = ServiceDatabaseOwnership(db_path)
    marker.mark()
    assert marker.path.exists()
    with pytest.raises(DishRuleError) as exc:
        ServiceDatabaseOwnership(db_path).assert_local_access_allowed()
    assert exc.value.rule == "service_owned_database"


def test_private_admin_http_and_cli_cover_authorization_recovery_and_migration(tmp_path, capsys):
    from dish_service.leases import ServicePrincipal
    from dish_tool.database import initialize_database

    service, _backend, server, thread, url = _running_service(tmp_path)
    owner = ServicePrincipal("agent", "33333333-3333-4333-8333-333333333333")
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=owner,
    )
    assert started["ok"]
    operation_id = started["submission_id"]
    admin = DishAdminServiceClient(
        url,
        token="admin-secret",
        run_id="44444444-4444-4444-8444-444444444444",
    )
    try:
        status = admin_cli.main(
            [
                "authorize-governed-change",
                operation_id,
                "--field", "Locks",
                "--before", '"Keep crisp"',
                "--after", '"Keep very crisp"',
                "--reason", "Marco approved the exact change",
            ],
            application=admin,
        )
        authorized = json.loads(capsys.readouterr().out)
        assert status == 0 and authorized["ok"]

        direct_recover = admin.execute(
            "recover",
            submission_id=operation_id,
            outcome="not-applied",
            reason="private parity check",
        )
        cli_status = admin_cli.main(
            ["recover", operation_id, "--outcome", "not-applied", "--reason", "private parity check"],
            application=admin,
        )
        cli_recover = json.loads(capsys.readouterr().out)
        assert cli_status != 0
        assert (cli_recover["code"], cli_recover["errors"][0]["rule"]) == (
            direct_recover["code"], direct_recover["errors"][0]["rule"]
        )

        direct_migrate = admin.execute("migrate", task_gid="t")
        cli_status = admin_cli.main(["migrate", "t"], application=admin)
        cli_migrate = json.loads(capsys.readouterr().out)
        assert cli_status != 0
        assert (cli_migrate["code"], cli_migrate["errors"][0]["rule"]) == (
            direct_migrate["code"], direct_migrate["errors"][0]["rule"]
        )
    finally:
        _stop(server, thread)

    conn = initialize_database(service.config.db_path)
    try:
        row = conn.execute(
            "SELECT field_name, before_json, after_json FROM marco_authorizations "
            "WHERE operation_id=? ORDER BY created_at DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["field_name"] == "Locks"
    assert json.loads(row["before_json"]) == "Keep crisp"
    assert json.loads(row["after_json"]) == "Keep very crisp"
