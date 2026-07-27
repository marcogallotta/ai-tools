from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from dish_service.application import DishService
from dish_service.client import DishActionClient, DishAdminServiceClient, DishServiceClient
from dish_service.config import ServiceConfig
from dish_service.http import build_action_server, build_private_server
from dish_service.process_lock import ServiceProcessLock
from dish_tool import admin_cli
from tests.test_dish_tool_r42_service_foundation import _release_loader
from tests.test_dish_tool_step7_verification import Backend


ROOT = Path(__file__).resolve().parent.parent


def _split_servers(tmp_path):
    backend = Backend()
    honest = tmp_path / "honest"
    honest.mkdir()
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            port=0,
            action_port=0,
            agent_token="cli-secret",
            admin_token="admin-secret",
            action_token="action-secret",
        ),
        backend_factory=lambda: backend,
        release_loader=_release_loader(honest),
    )
    private = build_private_server(service)
    action = build_action_server(service)
    private_thread = threading.Thread(target=private.serve_forever, daemon=True)
    action_thread = threading.Thread(target=action.serve_forever, daemon=True)
    private_thread.start()
    action_thread.start()
    private_host, private_port = private.server_address
    action_host, action_port = action.server_address
    return (
        backend,
        private,
        action,
        private_thread,
        action_thread,
        f"http://{private_host}:{private_port}",
        f"http://{action_host}:{action_port}",
    )


def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_private_and_public_listeners_have_disjoint_route_surfaces(tmp_path):
    backend, private, action, private_thread, action_thread, private_url, action_url = _split_servers(tmp_path)
    try:
        cli = DishServiceClient(private_url, token="cli-secret", run_id="9940d276-a582-5787-b6d9-b4fba846e271")
        action_client = DishActionClient(action_url, token="action-secret", run_id="7b87f6d2-db66-5199-882f-07841e94589c")
        wrong_action = DishActionClient(private_url, token="action-secret", run_id="7b87f6d2-db66-5199-882f-07841e94589c")
        wrong_admin = DishAdminServiceClient(action_url, token="admin-secret", run_id="49aa30ee-8c28-59f4-96c5-acedac34764b")

        private_result = cli.execute("sections", agent="gpt")
        public_result = action_client.execute("sections", agent="gpt")
        hidden_action = wrong_action.execute("sections", agent="gpt")
        hidden_admin = wrong_admin.execute("recover", submission_id="x", outcome="inspect", reason="x")
        public_health = action_client.health()
    finally:
        _stop(private, private_thread)
        _stop(action, action_thread)

    assert private_result == public_result
    assert private_result["ok"]
    assert hidden_action == {"error": "not_found", "ok": False}
    assert hidden_admin == {"error": "not_found", "ok": False}
    assert public_health == {"error": "not_found", "ok": False}
    assert backend.writes == 0
    assert backend.moves == 0


def test_hidden_post_route_closes_without_reinterpreting_its_body(tmp_path):
    backend, private, action, private_thread, action_thread, private_url, _action_url = _split_servers(tmp_path)
    parsed = urlsplit(private_url)
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    try:
        connection.request(
            "POST",
            "/v1/action/sections",
            body=json.dumps(
                {"client": {"run_id": "7b87f6d2-db66-5199-882f-07841e94589c"}, "arguments": {"agent": "gpt"}}
            ),
            headers={
                "Authorization": "Bearer action-secret",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
    finally:
        connection.close()
        _stop(private, private_thread)
        _stop(action, action_thread)

    assert response.status == 404
    assert response.getheader("Connection") == "close"
    assert response.will_close
    assert payload == {"error": "not_found", "ok": False}
    assert backend.writes == 0
    assert backend.moves == 0


def test_service_process_lock_rejects_second_process_owner(tmp_path):
    path = tmp_path / "shared.db.service.lock"
    first = ServiceProcessLock(path).acquire()
    try:
        with pytest.raises(Exception) as exc:
            ServiceProcessLock(path).acquire()
        assert getattr(exc.value, "rule", None) == "service_process_lock_held"
    finally:
        first.release()

    with ServiceProcessLock(path):
        pass


class FakeRemoteAdmin:
    def __init__(self):
        self.calls = []

    def create_backup(self, *, label):
        self.calls.append(("create", label))
        return {"ok": True, "command": "backup-create", "code": "OK", "task_gid": None,
                "submission_id": None, "state": None, "retryable": False,
                "allowed_actions": [], "data": {}, "errors": []}

    def restore_backup(self, backup_id):
        self.calls.append(("restore", backup_id))
        return {"ok": True, "command": "backup-restore", "code": "OK", "task_gid": None,
                "submission_id": None, "state": None, "retryable": False,
                "allowed_actions": [], "data": {}, "errors": []}

    def recover_lease(self, operation_id, *, reason):
        self.calls.append(("recover-lease", operation_id, reason))
        return {"ok": True, "command": "recover-lease", "code": "OK", "task_gid": None,
                "submission_id": operation_id, "state": None, "retryable": False,
                "allowed_actions": [], "data": {}, "errors": []}


def test_admin_cli_exposes_backup_restore_and_stale_lease_recovery(capsys):
    app = FakeRemoteAdmin()
    assert admin_cli.main(["backup-create", "--label", "before-step12"], application=app) == 0
    json.loads(capsys.readouterr().out)
    assert admin_cli.main(["backup-restore", "dish-test.sqlite3"], application=app) == 0
    json.loads(capsys.readouterr().out)
    assert admin_cli.main(["recover-lease", "op-1", "--reason", "run ended"], application=app) == 0
    json.loads(capsys.readouterr().out)
    assert app.calls == [
        ("create", "before-step12"),
        ("restore", "dish-test.sqlite3"),
        ("recover-lease", "op-1", "run ended"),
    ]


def test_checked_in_contract_documents_final_step11_access_paths():
    runtime = (ROOT / "docs" / "runtime-contract.md").read_text()
    readme = (ROOT / "README.md").read_text()
    tailscale = (ROOT / "deploy" / "tailscale" / "README.md").read_text()
    action_guide = (ROOT / "deploy" / "gpt-action.md").read_text()
    smoke = (ROOT / "deploy" / "live-test-project-smoke.md").read_text()

    assert "one laptop-hosted `dish-service` process" in runtime
    assert "Planning's read-only lookup" in runtime
    assert "There is intentionally no general-purpose `unblock`" in runtime
    assert "DISH_LIVE_MODE=1" in runtime and "DISH_MODE=service" in runtime
    assert "Action-only listener" in runtime
    assert "Step 12" in readme and "does not itself authorize production activation" in readme
    assert "--https=8444" in tailscale and "--https=443" in tailscale
    assert "port 443 is free" in tailscale and "do not overwrite" in tailscale
    assert "127.0.0.1:8765" in tailscale and "127.0.0.1:8766" in tailscale
    assert "1216693403164366" in smoke
    assert "Do not run this against production Cooking" in smoke
    assert "https://laptop.tail46f0b9.ts.net/openapi/action.json" in action_guide
    assert "Authorization: Bearer <DISH_SERVICE_ACTION_TOKEN>" in action_guide
    assert "client.run_id" in action_guide and "allowed_actions" in action_guide
    assert "canonical lowercase UUID" in action_guide
    assert "After Verification" in action_guide and "call `inspect`" in action_guide
    assert "All agent mutations are replay-bound" in runtime
    assert "prepare`, `approve`, `reject`, and `submit`" in runtime
    assert "BACKEND_UNCERTAIN" in action_guide and "recover-lease" in action_guide


def test_deployment_assets_keep_secrets_host_side_and_action_schema_trimmed():
    env_example = (ROOT / "deploy" / "systemd" / "service.env.example").read_text()
    unit = (ROOT / "deploy" / "systemd" / "dish-service.service").read_text()
    openapi = json.loads((ROOT / "openapi" / "dish-action.openapi.json").read_text())
    rendered = json.dumps(openapi).lower()

    assert "ASANA_ENV=" in env_example
    assert "DISH_SERVICE_AGENT_TOKEN=" in env_example
    assert "DISH_SERVICE_ADMIN_TOKEN=" in env_example
    assert "DISH_SERVICE_ACTION_TOKEN=" in env_example
    assert "DISH_COOKING_PROJECT_GID=" in env_example
    assert "EnvironmentFile=" in unit and "UMask=0077" in unit
    assert all(path.startswith("/v1/action/") for path in openapi["paths"])
    assert "/admin" not in rendered
    assert "backup" not in rendered
    assert "recover" not in rendered
    assert "asana" not in rendered
