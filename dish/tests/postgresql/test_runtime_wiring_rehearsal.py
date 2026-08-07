from __future__ import annotations

import hashlib
import json
import os
import socket
import socketserver
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.support.postgresql.runtime_wiring_rehearsal import _free_port

from dish_pg import runtime_wiring_rehearsal as rehearsal
from dish_service import __main__ as service_main
from dish_service.config import ServiceConfig
from dish_service.http import DishHTTPServer
from dish_tool.errors import DishRuleError
from tests.support.postgresql import process_failure as process_support
from tests.support.postgresql.runtime_wiring_evidence import valid_scenario_evidence

DISH_ROOT = Path(__file__).resolve().parents[2]


def _report_hash(payload: dict) -> str:
    copy = dict(payload)
    expected = copy.pop("report_sha256")
    assert hashlib.sha256(rehearsal._canonical_bytes(copy)).hexdigest() == expected
    return expected


@pytest.mark.parametrize("name", ["dish_section3_runtime_wiring_test", "dish_local_7_test"])
def test_runtime_wiring_rehearsal_accepts_disposable_database_names(name: str) -> None:
    assert rehearsal._safe_database_name(name) == name


@pytest.mark.parametrize("name", ["dish", "dish_prod_test", "dish_production_test", "section3_test"])
def test_runtime_wiring_rehearsal_rejects_unsafe_database_names(name: str) -> None:
    with pytest.raises(rehearsal.RehearsalConfigurationError):
        rehearsal._safe_database_name(name)


def test_runtime_wiring_rehearsal_rejects_unsafe_compose_project() -> None:
    with pytest.raises(rehearsal.RehearsalConfigurationError):
        rehearsal._safe_project("dish-section1-wrong")


def test_runtime_wiring_rehearsal_emits_bounded_blocked_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "report.json"
    evidence = tmp_path / "evidence"
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("ASANA_ACCESS_TOKEN", "must-not-propagate")
    monkeypatch.setenv("DISH_SERVICE_URL", "http://127.0.0.1:8775")

    result = rehearsal.main(
        [
            "--output",
            str(output),
            "--evidence-dir",
            str(evidence),
            "--compose-project",
            "dish-section3-source-test",
            "--received-archive-name",
            "ai-tools-venv(20260806-132400).tgz",
            "--received-archive-sha256",
            "e9dbbd6cc5c62f1f22b818060ac8edea1dcc7d9ad783b3d9e571a9bdb2385b96",
            "--base-identity",
            "f27f1c5ca38efe1e3f084c6b8aadfe6e83b231cc",
        ]
    )

    assert result == 3
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["format"] == "dish-postgresql-runtime-wiring-rehearsal-v2"
    assert report["status"] == "blocked"
    assert report["ok"] is False
    assert report["repository_commit"] is None
    assert report["received_source"]["base_identity"] == (
        "f27f1c5ca38efe1e3f084c6b8aadfe6e83b231cc"
    )
    assert report["received_source"]["archive_sha256"] == (
        "e9dbbd6cc5c62f1f22b818060ac8edea1dcc7d9ad783b3d9e571a9bdb2385b96"
    )
    assert report["first_attempt"]["status"] == "blocked"
    assert report["processes"] == []
    assert report["scenario_evidence"] is None
    assert set(report["required_scenarios"]) == set(rehearsal.REQUIRED_SCENARIOS)
    for scenario in report["required_scenarios"].values():
        assert scenario["status"] == "blocked"
        assert scenario["evidence"] is None
        assert "Docker Compose is unavailable" in scenario["blocker"]
    assert report["production_profile_reachable"] is False
    assert report["production_credentials_reachable"] is False
    assert report["production_routes_reachable"] is False
    assert report["asana_resources_reachable"] is False
    assert report["scrubbed_asana_environment_keys"] == ["ASANA_ACCESS_TOKEN"]
    assert report["scrubbed_dish_environment_keys"] == ["DISH_SERVICE_URL"]
    assert "SQLite/PGlite were not substituted" in report["unavailable_native_evidence"]
    _report_hash(report)


def test_runtime_wiring_evidence_validation_requires_distinct_real_processes() -> None:
    identity = {
        "database": "dish_section3_runtime_wiring_test",
        "schema_head": "0027_typed_worker_readiness_evidence",
        "dish_release": "test-release",
        "generation_id": "00000000-0000-4000-8000-000000000001",
        "generation_status": "active",
    }
    processes = [
        {
            "path": f"{label}.json",
            "payload": {
                "label": label,
                "pid": index,
                "completion_state": "terminated" if label == "postgresql-service" else "completed",
                "command": ["python", "-m", label],
            },
        }
        for index, label in enumerate(sorted(rehearsal.REQUIRED_PROCESS_LABELS), start=100)
    ]
    identities = [
        {
            "path": f"identity-{index}.json",
            "payload": {"ok": True, "role": role, "identity": identity},
        }
        for index, role in enumerate(
            ("projection_worker", "projection_worker", "reconciliation_worker")
        )
    ]
    scenario = {"evidence": valid_scenario_evidence(identity)}
    result = rehearsal._validate_evidence(
        cases={rehearsal.TEST_NODE: {"status": "passed"}},
        junit_errors=[],
        scenario=scenario,
        process_records=processes,
        identity_records=identities,
    )
    assert result["ok"] is True, result["errors"]
    assert result["distinct_pid_count"] == len(rehearsal.REQUIRED_PROCESS_LABELS)


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data = self.request.recv(4096)
        self.request.sendall(data)


def test_postgresql_proxy_is_a_restartable_separate_process(tmp_path: Path) -> None:
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), _EchoHandler) as target:
        thread = threading.Thread(target=target.serve_forever, daemon=True)
        thread.start()
        proxy_port = _free_port()
        dsn = (
            "postgresql+psycopg://dish:dish@127.0.0.1:"
            f"{target.server_address[1]}/dish_proxy_test"
        )

        first, proxied = process_support.start_postgresql_proxy(
            dsn=dsn,
            tmp_path=tmp_path,
            listen_port=proxy_port,
            label="postgresql-tcp-proxy-before-loss",
        )
        assert f"127.0.0.1:{proxy_port}" in proxied
        with socket.create_connection(("127.0.0.1", proxy_port), timeout=3.0) as client:
            client.sendall(b"first")
            assert client.recv(4096) == b"first"
        first.terminate()
        assert first.manifest["completion_state"] == "terminated"

        second, restarted = process_support.start_postgresql_proxy(
            dsn=dsn,
            tmp_path=tmp_path,
            listen_port=proxy_port,
            label="postgresql-tcp-proxy-after-restart",
        )
        try:
            assert restarted == proxied
            assert second.process.pid != first.process.pid
            with socket.create_connection(("127.0.0.1", proxy_port), timeout=3.0) as client:
                client.sendall(b"second")
                assert client.recv(4096) == b"second"
        finally:
            second.terminate()
            target.shutdown()
            thread.join(timeout=3.0)


def _service_args(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(state_dir=tmp_path)


def _set_service_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISH_PROFILE", "test")
    monkeypatch.setenv("DISH_SERVICE_BIND", "127.0.0.1")
    monkeypatch.setenv("DISH_ACTION_BIND", "127.0.0.1")
    monkeypatch.setenv("DISH_SERVICE_PORT", "19001")
    monkeypatch.setenv("DISH_ACTION_PORT", "19002")
    monkeypatch.setenv("DISH_SERVICE_AGENT_TOKEN", "section3-agent-token")
    monkeypatch.setenv("DISH_SERVICE_ADMIN_TOKEN", "section3-admin-token")
    monkeypatch.setenv("DISH_SERVICE_ACTION_TOKEN", "section3-action-token")


def test_postgresql_service_mode_requires_test_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_service_env(monkeypatch)
    monkeypatch.setenv("DISH_PROFILE", "production")
    with pytest.raises(DishRuleError) as caught:
        service_main._postgresql_runtime_config(_service_args(tmp_path))
    assert caught.value.code == "INVALID_ARGUMENT"
    assert caught.value.rule == "postgresql_runtime_profile_not_test"


def test_postgresql_service_mode_rejects_reachable_asana_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_service_env(monkeypatch)
    monkeypatch.setenv("ASANA_ACCESS_TOKEN", "reachable")
    with pytest.raises(DishRuleError) as caught:
        service_main._postgresql_runtime_config(_service_args(tmp_path))
    assert caught.value.code == "INVALID_ARGUMENT"
    assert caught.value.rule == "postgresql_runtime_asana_environment_reachable"


def test_postgresql_service_mode_reuses_loopback_service_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_service_env(monkeypatch)
    for key in list(os.environ):
        if "ASANA" in key.upper():
            monkeypatch.delenv(key, raising=False)
    config = service_main._postgresql_runtime_config(_service_args(tmp_path))
    assert config.bind_host == "127.0.0.1"
    assert config.action_bind_host == "127.0.0.1"
    assert config.legacy_writer_fence_path is None



class _AgentOnlyHTTPService:
    def __init__(self, tmp_path: Path) -> None:
        self.config = ServiceConfig(
            db_path=tmp_path / "unused.sqlite3",
            honest_root=tmp_path,
            port=0,
            action_port=0,
            agent_token="section3-agent-token",
            admin_token="section3-admin-token",
            action_token="section3-action-token",
            legacy_writer_fence_path=None,
        )
        self.unsupported_method_calls = 0

    @staticmethod
    def supports_http_route(surface: str, command: str) -> bool:
        del command
        return surface == "agent"

    def renew_lease(self, *args, **kwargs):
        self.unsupported_method_calls += 1
        raise AssertionError("unsupported route reached renew_lease")

    def recover_lease(self, *args, **kwargs):
        self.unsupported_method_calls += 1
        raise AssertionError("unsupported route reached recover_lease")

    def execute_admin(self, *args, **kwargs):
        self.unsupported_method_calls += 1
        raise AssertionError("unsupported route reached execute_admin")

    def execute_action(self, *args, **kwargs):
        self.unsupported_method_calls += 1
        raise AssertionError("unsupported route reached execute_action")


def _post_json(url: str, *, token: str) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        response = urllib.request.urlopen(request, timeout=3.0)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))
    with response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_postgresql_test_service_hides_unsupported_routes_before_dispatch(
    tmp_path: Path,
) -> None:
    service = _AgentOnlyHTTPService(tmp_path)
    with DishHTTPServer(("127.0.0.1", 0), service, surface_mode="combined") as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        operation_id = "00000000-0000-4000-8000-000000000111"
        cases = (
            (f"{base}/v1/leases/{operation_id}/renew", "section3-agent-token"),
            (
                f"{base}/v1/admin/leases/{operation_id}/recover",
                "section3-admin-token",
            ),
            (f"{base}/v1/admin/inspect", "section3-admin-token"),
            (f"{base}/v1/action/renew-lease", "section3-action-token"),
        )
        try:
            for url, token in cases:
                assert _post_json(url, token=token) == (
                    404,
                    {"ok": False, "error": "not_found"},
                )
        finally:
            server.shutdown()
            thread.join(timeout=3.0)
    assert service.unsupported_method_calls == 0

def test_runtime_wiring_script_is_the_committed_entry_point() -> None:
    script = DISH_ROOT / "scripts/dish-pg-runtime-wiring-rehearsal"
    assert script.is_file()
    assert os.access(script, os.X_OK)
    text = script.read_text(encoding="utf-8")
    assert "dish_pg.runtime_wiring_rehearsal import main" in text
