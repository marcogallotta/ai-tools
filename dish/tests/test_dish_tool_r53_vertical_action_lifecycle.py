from __future__ import annotations

import threading

import asana

from dish_service.application import DishService
from dish_service.client import DishActionClient
from dish_service.config import ServiceConfig
from dish_service.http import build_action_server
from dish_tool.backend import AsanaBackend
from dish_tool.database import initialize_database
from tests.test_dish_tool_r40_placement_gate import StatefulAsanaTransport, _release
from tests.test_dish_tool_step6_prepare import PLANNING, TASK


def test_production_action_topology_drives_real_sdk_full_lifecycle(tmp_path):
    config = asana.Configuration()
    config.return_page_iterator = False
    api_client = asana.ApiClient(config)
    transport = StatefulAsanaTransport()
    api_client.call_api = transport.call_api
    backend = AsanaBackend(api_client=api_client)

    honest = tmp_path / "honest"
    honest.mkdir()
    (honest / "dish-verification-protocol.md").write_text(
        "# Verification protocol\n", encoding="utf-8"
    )
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.sqlite3",
            honest_root=honest,
            backup_dir=tmp_path / "backups",
            port=0,
            action_port=0,
            agent_token="agent-secret-123",
            admin_token="admin-secret-123",
            action_token="action-secret-123",
        ),
        backend_factory=lambda: backend,
        release_loader=lambda role=None: _release(honest, role),
    )
    server = build_action_server(service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    url = f"http://{host}:{port}"

    try:
        planner = DishActionClient(url, token="action-secret-123", run_id="planning-run")
        created = planner.execute(
            "create",
            agent="gpt",
            title="Bare",
            request_id="11111111-1111-4111-8111-111111111111",
        )
        task_gid = created["task_gid"]
        planning = planner.execute(
            "start",
            agent="gpt",
            task_gid=task_gid,
            kind="planning",
            request_id="22222222-2222-4222-8222-222222222222",
        )
        planned = planner.execute(
            "prepare",
            agent="gpt",
            model="gpt-5.6-sol",
            submission_id=planning["submission_id"],
            file_text=PLANNING.replace("Sichuan — 12345", "Planned — 333"),
        )

        researcher = DishActionClient(
            url, token="action-secret-123", run_id="research-run"
        )
        research = researcher.execute(
            "start",
            agent="gpt",
            task_gid=task_gid,
            kind="initial",
            request_id="33333333-3333-4333-8333-333333333333",
        )
        prepared = researcher.execute(
            "prepare",
            agent="gpt",
            model="gpt-5.6-sol",
            submission_id=research["submission_id"],
            file_text=TASK.replace("Sichuan — 12345", "Planned — 333"),
        )

        verifier = DishActionClient(
            url, token="action-secret-123", run_id="verification-run"
        )
        review = verifier.execute(
            "start",
            agent="codex",
            task_gid=task_gid,
            kind="verification",
            request_id="44444444-4444-4444-8444-444444444444",
        )
        approved = verifier.execute(
            "approve",
            agent="codex",
            model="gpt-5.6-sol",
            submission_id=research["submission_id"],
            correction="none",
            reviewed_identity=review["data"]["reviewed_identity"],
            semantic_review_complete=True,
            provenance_complete=True,
        )
        submitted = verifier.execute(
            "submit", submission_id=research["submission_id"]
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert created["ok"] and planning["ok"] and planned["ok"]
    assert research["ok"] and prepared["ok"] and review["ok"]
    assert approved["ok"] and submitted["ok"]
    assert transport.tasks[task_gid]["section"] == "333"

    placement_calls = [
        call for call in transport.calls
        if call[0] == "/sections/{section_gid}/addTask"
    ]
    assert [call[2]["section_gid"] for call in placement_calls] == ["rq", "vq", "333"]
    assert all(call[3] == {"data": {"task": task_gid}} for call in placement_calls)

    conn = initialize_database(service.config.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM service_requests WHERE status='completed'"
        ).fetchone()[0] == 4
        assert conn.execute(
            "SELECT COUNT(*) FROM service_leases WHERE released_at IS NULL"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT terminal_outcome FROM operations WHERE operation_id=?",
            (research["submission_id"],),
        ).fetchone()[0] == "destination_handled"
    finally:
        conn.close()
