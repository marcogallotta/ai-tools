from __future__ import annotations

import threading

import asana
import pytest

from dish_service.application import DishService
from dish_service.client import DishActionClient
from dish_service.config import ServiceConfig
from dish_service.http import build_action_server
from dish_tool.backend import AsanaBackend, close_asana_sdk_client
from dish_tool.database_initialization import initialize_database
from tests.support.thread_teardown import join_thread, start_server_thread, stop_server
from tests.support.placement import StatefulAsanaTransport, _release
from tests.support.planning import PLANNING, TASK


def _running_action_topology(tmp_path):
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
    thread = start_server_thread(server, daemon=True, name="thread")
    host, port = server.server_address
    return service, transport, api_client, server, thread, f"http://{host}:{port}"


def _run_planning(url):
    planner = DishActionClient(
        url,
        token="action-secret-123",
        run_id="11111111-1111-4111-8111-111111111111",
    )
    created = planner.execute(
        "create",
        agent="gpt",
        title="Bare",
        request_id="11111111-1111-4111-8111-111111111111",
    )
    task_gid = created["task_gid"]
    assert created["allowed_actions"] == ["start"]
    assert created["data"]["required_start_kind"] == "planning"
    resting = planner.execute("read", agent="gpt", task_gid=task_gid)
    assert resting["allowed_actions"] == ["start"]
    assert resting["data"]["required_start_kind"] == "planning"
    challenge = planner.execute(
        "start",
        agent="gpt",
        task_gid=task_gid,
        kind="planning",
        request_id="22222222-2222-4222-8222-222222222222",
    )
    assert challenge["code"] == "CONFIRMATION_REQUIRED"
    planning = planner.execute(
        "start",
        agent="gpt",
        task_gid=task_gid,
        kind="planning",
        intent_challenge_id=challenge["data"]["intent_challenge_id"],
        intent_basis="user_requested",
        request_id="25252525-2525-4525-8525-252525252525",
    )
    assert planning["allowed_actions"] == ["prepare"]
    planned = planner.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=planning["submission_id"],
        file_text=PLANNING.replace("Sichuan — 12345", "Planned — 333"),
    )
    assert planned["allowed_actions"] == ["start"]
    assert planned["data"]["required_start_kind"] == "initial"
    resting = planner.execute("read", agent="gpt", task_gid=task_gid)
    assert resting["allowed_actions"] == ["start"]
    assert resting["data"]["required_start_kind"] == "initial"
    return created, planning, planned, task_gid


def _run_research(url, task_gid):
    researcher = DishActionClient(
        url,
        token="action-secret-123",
        run_id="22222222-2222-4222-8222-222222222222",
    )
    research = researcher.execute(
        "start",
        agent="gpt",
        task_gid=task_gid,
        kind="initial",
        request_id="33333333-3333-4333-8333-333333333333",
    )
    assert research["allowed_actions"] == ["prepare"]
    prepared = researcher.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=research["submission_id"],
        file_text=TASK.replace("Sichuan — 12345", "Planned — 333"),
    )
    assert prepared["allowed_actions"] == ["start"]
    assert prepared["data"]["required_start_kind"] == "verification"
    resting = researcher.execute("read", agent="gpt", task_gid=task_gid)
    assert resting["allowed_actions"] == ["start"]
    assert resting["data"]["required_start_kind"] == "verification"
    return research, prepared


def _run_verification(url, task_gid, operation_id):
    verifier = DishActionClient(
        url,
        token="action-secret-123",
        run_id="33333333-3333-4333-8333-333333333333",
    )
    review = verifier.execute(
        "start",
        agent="codex",
        task_gid=task_gid,
        kind="verification",
        request_id="44444444-4444-4444-8444-444444444444",
        independence_attestation="independent",
    )
    assert review["allowed_actions"] == ["inspect"]
    inspected = verifier.execute(
        "inspect", agent="codex", submission_id=operation_id
    )
    assert inspected["ok"], inspected
    assert inspected["allowed_actions"] == ["approve", "reject"]
    assert inspected["data"].get("dish_inspect_fact"), inspected
    approved = verifier.execute(
        "approve",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True,
        provenance_complete=True,
    )
    assert approved["allowed_actions"] == ["submit"]
    submitted = verifier.execute("submit", submission_id=operation_id)
    assert submitted["allowed_actions"] == []
    resting = verifier.execute("read", agent="gpt", task_gid=task_gid)
    assert resting["allowed_actions"] == ["start"]
    assert resting["data"]["required_start_kind"] == "change"
    return review, approved, submitted


def _assert_full_lifecycle_persistence(service, transport, task_gid, operation_id):
    assert transport.tasks[task_gid]["section"] == "333"
    placement_calls = [
        call
        for call in transport.calls
        if call[0] == "/sections/{section_gid}/addTask"
    ]
    assert [call[2]["section_gid"] for call in placement_calls] == ["rq", "vq", "333"]
    assert all(call[3] == {"data": {"task": task_gid}} for call in placement_calls)

    conn = initialize_database(service.config.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM service_requests WHERE status='completed'"
        ).fetchone()[0] == 10
        assert conn.execute(
            "SELECT COUNT(*) FROM service_requests "
            "WHERE command='inspect' AND status='completed'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM planning_intent_challenges WHERE status='consumed'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM service_leases WHERE released_at IS NULL"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT terminal_outcome FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()[0] == "destination_handled"
    finally:
        conn.close()


@pytest.mark.smoke
def test_production_action_topology_drives_real_sdk_full_lifecycle(tmp_path):
    service, transport, api_client, server, thread, url = _running_action_topology(
        tmp_path
    )
    try:
        created, planning, planned, task_gid = _run_planning(url)
        research, prepared = _run_research(url, task_gid)
        review, approved, submitted = _run_verification(
            url, task_gid, research["submission_id"]
        )
    finally:
        stop_server(server, thread)
        close_asana_sdk_client(api_client)

    assert created["ok"] and planning["ok"] and planned["ok"]
    assert research["ok"] and prepared["ok"] and review["ok"]
    assert approved["ok"], approved
    assert submitted["ok"], submitted
    _assert_full_lifecycle_persistence(
        service, transport, task_gid, research["submission_id"]
    )
