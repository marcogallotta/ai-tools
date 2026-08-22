from __future__ import annotations

import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dish_pg.first_admission_request import submit_first_admission
from tests.support.thread_teardown import start_server_thread, stop_server

REQUEST_ID = "20000000-0000-4000-8000-000000000001"
RUN_ID = "20000000-0000-4000-8000-000000000002"
TASK_ID = "20000000-0000-4000-8000-000000000003"


class _Target(BaseHTTPRequestHandler):
    received: dict[str, object] = {}

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        type(self).received = {
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "json": json.loads(body),
        }
        response = json.dumps(
            {
                "ok": True,
                "command": "start",
                "code": "OK",
                "data": {"request_id": REQUEST_ID, "operation_id": "fixture-operation"},
            },
            separators=(",", ":"),
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def test_first_admission_helper_submits_exact_plan_once(tmp_path: Path) -> None:
    plan = tmp_path / "first-admission-plan.json"
    plan.write_text(
        json.dumps(
            {
                "request_id": REQUEST_ID,
                "command_name": "start",
                "command_arguments": {
                    "task_id": TASK_ID,
                    "agent": "codex",
                    "kind": "initial",
                },
                "task_id": TASK_ID,
                "owner_id": "owner-1",
                "principal_class": "agent",
                "run_id": RUN_ID,
                "payload": {"probe": "first production mutation"},
                "recorded_at": "2026-08-05T07:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    plan.chmod(0o600)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Target)
    thread = start_server_thread(server, poll_interval=0.005)
    try:
        report = submit_first_admission(
            plan_path=plan.resolve(),
            service_url=f"http://127.0.0.1:{server.server_port}",
            token="fixture-token",
            connect_timeout=2,
            response_timeout=2,
        )
    finally:
        stop_server(server, thread, timeout=5)

    assert report["ok"] is True
    assert report["delivery_state"] == "response_received"
    assert report["retried"] is False
    assert report["request"]["request_id"] == REQUEST_ID
    assert _Target.received == {
        "path": "/v1/commands/start",
        "authorization": "Bearer fixture-token",
        "json": {
            "arguments": {"task_id": TASK_ID, "agent": "codex", "kind": "initial"},
            "client": {"run_id": RUN_ID, "request_id": REQUEST_ID},
        },
    }


def test_first_admission_transport_failure_retains_exact_request_evidence(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "first-admission-plan.json"
    plan.write_text(
        json.dumps(
            {
                "request_id": REQUEST_ID,
                "command_name": "start",
                "command_arguments": {"task_id": TASK_ID, "agent": "codex"},
                "task_id": TASK_ID,
                "owner_id": "owner-1",
                "principal_class": "agent",
                "run_id": RUN_ID,
                "recorded_at": "2026-08-05T07:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    plan.chmod(0o600)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        unused_port = probe.getsockname()[1]

    report = submit_first_admission(
        plan_path=plan.resolve(),
        service_url=f"http://127.0.0.1:{unused_port}",
        token="fixture-token",
        connect_timeout=0.2,
        response_timeout=0.2,
    )

    assert report["ok"] is False
    assert report["delivery_state"] == "not_sent"
    assert report["request"]["request_id"] == REQUEST_ID
    assert report["retried"] is False
