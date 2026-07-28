from __future__ import annotations

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import ServicePrincipal
from dish_service.request_replay import begin_request
from dish_tool.commands import DishApplication
from dish_tool.database import initialize_database
from tests.test_dish_tool_r42_service_foundation import _release_loader
from tests.test_dish_tool_step7_verification import Backend as WorkflowBackend


class Backend(WorkflowBackend):
    def create_bare_task(self, *, title, project_gid, section_gid):
        self.writes += 1
        self.title = title
        self.notes = ""
        self.section = section_gid
        return {"gid": "1000000000000001", "name": title, "notes": ""}


CREATE_REQUEST = "11111111-1111-4111-8111-111111111111"
START_REQUEST = "22222222-2222-4222-8222-222222222222"
VERIFY_REQUEST = "33333333-3333-4333-8333-333333333333"


def _service(tmp_path, backend=None):
    backend = backend or Backend()
    honest = tmp_path / "honest"
    honest.mkdir(exist_ok=True)
    service = DishService(
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
        release_loader=_release_loader(honest),
    )
    return service, backend


def _principal(run="run"):
    return ServicePrincipal(owner_id="action", run_id=run)


def test_completed_create_request_replays_without_duplicate_task(tmp_path):
    service, backend = _service(tmp_path)
    first = service.execute_agent(
        "create",
        {"agent": "gpt", "title": "Replay dish"},
        principal=_principal(),
        request_id=CREATE_REQUEST,
    )
    second = service.execute_agent(
        "create",
        {"agent": "gpt", "title": "Replay dish"},
        principal=_principal(),
        request_id=CREATE_REQUEST,
    )
    assert first["ok"] and second["ok"]
    assert second["task_gid"] == first["task_gid"]
    assert second["data"]["request_replayed"] is True
    assert backend.writes == 1



def test_completed_start_request_replays_full_stored_result(tmp_path):
    service, _backend = _service(tmp_path)
    first = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=_principal(),
        request_id=START_REQUEST,
    )
    second = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=_principal(),
        request_id=START_REQUEST,
    )
    assert first["ok"] and second["ok"]
    assert second["submission_id"] == first["submission_id"]
    assert second["data"]["request_replayed"] is True
    assert second["data"]["protocol"] == first["data"]["protocol"]
    assert second["data"]["runtime_context"] == first["data"]["runtime_context"]
    conn = initialize_database(service.config.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
    finally:
        conn.close()

def test_request_id_cannot_be_reused_for_different_work(tmp_path):
    service, backend = _service(tmp_path)
    first = service.execute_agent(
        "create", {"agent": "gpt", "title": "Dish A"},
        principal=_principal(), request_id=CREATE_REQUEST,
    )
    assert first["ok"]
    conflict = service.execute_agent(
        "create", {"agent": "gpt", "title": "Dish B"},
        principal=_principal(), request_id=CREATE_REQUEST,
    )
    assert conflict["code"] == "CONFLICT"
    assert conflict["errors"][0]["rule"] == "service_request_identity_conflict"
    assert backend.writes == 1


def test_pending_create_fails_uncertain_instead_of_creating_duplicate(tmp_path):
    service, backend = _service(tmp_path)
    conn = initialize_database(service.config.db_path)
    try:
        begin_request(
            conn,
            request_id=CREATE_REQUEST,
            owner_id="action",
            run_id="run",
            command="create",
            arguments={"agent": "gpt", "title": "Maybe created"},
        )
    finally:
        conn.close()
    result = service.execute_agent(
        "create", {"agent": "gpt", "title": "Maybe created"},
        principal=_principal(), request_id=CREATE_REQUEST,
    )
    assert result["code"] == "BACKEND_UNCERTAIN"
    assert result["errors"][0]["rule"] == "service_request_pending"
    assert backend.writes == 0


def test_pending_start_reconciles_exact_existing_operation(tmp_path):
    service, backend = _service(tmp_path)
    conn = initialize_database(service.config.db_path)
    try:
        prepared = {
            "agent": "gpt", "task_gid": "t", "kind": "initial", "run_id": "run"
        }
        begin_request(
            conn,
            request_id=START_REQUEST,
            owner_id="action",
            run_id="run",
            command="start",
            arguments=prepared,
        )
        app = DishApplication(
            conn,
            backend,
            release_loader=lambda role=None: service._release(role),
            invocation_run_id="run",
        )
        started = app.execute("start", **prepared)
        assert started["ok"]
        operation_id = started["submission_id"]
    finally:
        conn.close()

    replayed = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=_principal(),
        request_id=START_REQUEST,
    )
    assert replayed["ok"]
    assert replayed["submission_id"] == operation_id
    assert replayed["data"]["request_replayed"] is True
    assert replayed["data"]["protocol"]["role"] == "research"
    assert replayed["data"]["protocol"]["text"] == "research protocol"
    assert replayed["data"]["runtime_context"]["verification_queue"]["gid"] == "vq"
    assert replayed["data"]["service_lease"]["run_id"] == "run"



def test_pending_verification_start_reconstructs_exact_review_context(tmp_path):
    service, backend = _service(tmp_path)
    constructor = _principal("constructor-run")
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=constructor,
        request_id=START_REQUEST,
    )
    assert started["ok"]
    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "file_text": backend.title + "\n" + backend.notes,
        },
        principal=constructor,
    )
    assert prepared["ok"]

    verifier = _principal("verifier-run")
    request_arguments = {
        "agent": "codex",
        "task_gid": "t",
        "kind": "verification",
        "run_id": "verifier-run",
    }
    conn = initialize_database(service.config.db_path)
    try:
        begin_request(
            conn,
            request_id=VERIFY_REQUEST,
            owner_id="action",
            run_id="verifier-run",
            command="start",
            arguments=request_arguments,
        )
        app = DishApplication(
            conn,
            backend,
            release_loader=lambda role=None: service._release(role),
            invocation_run_id="verifier-run",
        )
        original = app.execute("start", **request_arguments)
        assert original["ok"]
    finally:
        conn.close()

    replayed = service.execute_agent(
        "start",
        {"agent": "codex", "task_gid": "t", "kind": "verification"},
        principal=verifier,
        request_id=VERIFY_REQUEST,
    )
    assert replayed["ok"]
    assert replayed["submission_id"] == started["submission_id"]
    assert replayed["allowed_actions"] == ["inspect", "approve", "reject"]
    assert replayed["data"]["request_replayed"] is True
    assert replayed["data"]["reviewed_identity"] == original["data"]["reviewed_identity"]
    assert replayed["data"]["verification_protocol"] == original["data"]["verification_protocol"]
    assert replayed["data"]["task"]["title"] == original["data"]["task"]["title"]
    assert replayed["data"]["service_lease"]["run_id"] == "verifier-run"

    conn = initialize_database(service.config.db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE operation_id=? "
            "AND event_type='verification.review_started'",
            (started["submission_id"],),
        ).fetchone()[0] == 1
    finally:
        conn.close()

def test_restore_fault_marker_survives_service_restart(tmp_path):
    service, backend = _service(tmp_path)
    service._restore_fault.set({"kind": "simulated_unproven_restore"})
    restarted = DishService(
        service.config,
        backend_factory=lambda: backend,
        release_loader=service.release_loader,
    )
    health = restarted.health()
    assert not health["ok"]
    assert health["maintenance"]["restore_recovery_required"] is True
    assert health["maintenance"]["restore_fault"]["kind"] == "simulated_unproven_restore"
    blocked = restarted.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=_principal(),
        request_id=START_REQUEST,
    )
    assert blocked["code"] == "INTERNAL_ERROR"
    assert blocked["errors"][0]["rule"] == "service_restore_recovery_required"
    assert backend.writes == 0


def test_action_http_replays_completed_create_with_same_request_id(tmp_path):
    import threading

    from dish_service.client import DishActionClient
    from dish_service.http import build_server

    service, backend = _service(tmp_path)
    server = build_server(service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        action = DishActionClient(
            f"http://{host}:{port}", token="action-secret", run_id="11111111-2222-4333-8444-555555555555"
        )
        first = action.execute(
            "create",
            {"agent": "gpt", "title": "Replay through HTTP"},
            request_id=CREATE_REQUEST,
        )
        second = action.execute(
            "create",
            {"agent": "gpt", "title": "Replay through HTTP"},
            request_id=CREATE_REQUEST,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert first["ok"] and second["ok"]
    assert second["task_gid"] == first["task_gid"]
    assert second["data"]["request_replayed"] is True
    assert backend.writes == 1


def test_schema_20_upgrades_with_empty_request_ledger(tmp_path):
    import sqlite3

    from dish_tool.constants import SCHEMA_VERSION

    db_path = tmp_path / "v20.sqlite3"
    conn = initialize_database(db_path)
    conn.execute("DROP TABLE service_requests")
    conn.execute("DROP TABLE operation_execution_claims")
    conn.execute("DROP INDEX write_attempts_one_unresolved_operation")
    conn.execute("DROP INDEX movement_attempts_one_unresolved_operation")
    conn.execute("DELETE FROM schema_migrations WHERE version>=21")
    conn.execute("PRAGMA user_version=20")
    conn.close()

    upgraded = initialize_database(db_path)
    try:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert upgraded.execute(
            "SELECT COUNT(*) FROM service_requests"
        ).fetchone()[0] == 0
        assert upgraded.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        upgraded.close()
