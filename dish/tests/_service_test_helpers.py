from __future__ import annotations

from tests.support.thread_teardown import start_server_thread

import json
import threading
from http.client import HTTPConnection
from urllib.parse import urlsplit

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.http import build_server
from dish_service.leases import ServicePrincipal
from tests._partial_recovery_helpers import ServiceBackend, release_loader

RUN_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REQUEST_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OTHER_REQUEST_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OPERATION_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


def service(tmp_path):
    backend = ServiceBackend()
    honest = tmp_path / "honest"
    honest.mkdir(exist_ok=True)
    instance = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            backup_dir=tmp_path / "backups",
            port=0,
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
        ),
        backend_factory=lambda: backend,
        release_loader=release_loader(honest),
    )
    return instance, backend


def running(tmp_path):
    instance, backend = service(tmp_path)
    server = build_server(instance)
    thread = start_server_thread(server, daemon=True, name="thread")
    host, port = server.server_address
    return instance, backend, server, thread, f"http://{host}:{port}"


def post(url, path, *, token, payload):
    parsed = urlsplit(url)
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=3)
    try:
        body = json.dumps(payload)
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Content-Length": str(len(body.encode("utf-8"))),
            },
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def complete_service_submission(instance, backend):
    constructor = ServicePrincipal(owner_id="action", run_id=RUN_ID)
    started = instance.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=constructor,
        request_id="10000000-0000-4000-8000-000000000001",
    )
    assert started["ok"]
    prepared = instance.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "file_text": backend.title + "\n" + backend.notes,
        },
        principal=constructor,
        request_id="10000000-0000-4000-8000-000000000002",
    )
    assert prepared["ok"]
    verifier = ServicePrincipal(
        owner_id="action", run_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    )
    reviewed = instance.execute_agent(
        "start",
        {
            "agent": "codex",
            "task_gid": "t",
            "kind": "verification",
            "independence_attestation": "independent",
        },
        principal=verifier,
        request_id="10000000-0000-4000-8000-000000000003",
    )
    assert reviewed["ok"]
    inspected = instance.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": started["submission_id"]},
        principal=verifier,
    )
    assert inspected["ok"]
    approved = instance.execute_agent(
        "approve",
        {
            "agent": "codex",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "correction": "none",
            "reviewed_identity": reviewed["data"]["reviewed_identity"],
            "semantic_review_complete": True,
            "provenance_complete": True,
        },
        principal=verifier,
        request_id="10000000-0000-4000-8000-000000000004",
    )
    assert approved["ok"]
    return verifier, started["submission_id"]
