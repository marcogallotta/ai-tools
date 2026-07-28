from __future__ import annotations

import json
import threading
import uuid
from http.client import HTTPConnection
from urllib.parse import urlsplit

import pytest

from dish_service.application import DishService
from dish_service.backup import BackupManager
from dish_service.config import ServiceConfig
from dish_service.http import build_server
from dish_service.leases import ServicePrincipal
from tests.test_dish_tool_r42_service_foundation import _release_loader
from tests.test_dish_tool_r52_request_restore_durability import Backend

RUN_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REQUEST_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OTHER_REQUEST_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OPERATION_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


def _service(tmp_path):
    backend = Backend()
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


def _running(tmp_path):
    service, backend = _service(tmp_path)
    server = build_server(service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return service, backend, server, thread, f"http://{host}:{port}"


def _post(url, path, *, token, payload):
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


AGENT_ARGUMENTS = {
    "create": {"agent": "gpt", "title": "Dish"},
    "start": {"agent": "gpt", "task_gid": "123456789", "kind": "initial"},
    "prepare": {
        "agent": "gpt", "model": "model", "submission_id": OPERATION_ID,
        "file_text": "candidate",
    },
    "approve": {
        "agent": "gpt", "model": "model", "submission_id": OPERATION_ID,
        "correction": "none", "reviewed_identity": "identity",
        "semantic_review_complete": True, "provenance_complete": True,
    },
    "reject": {
        "agent": "gpt", "submission_id": OPERATION_ID,
        "reason": "blocked", "route": "evidence",
    },
    "submit": {"submission_id": OPERATION_ID},
}


@pytest.mark.parametrize("command", sorted(AGENT_ARGUMENTS))
def test_every_agent_mutation_requires_request_id(tmp_path, command):
    _service_obj, _backend, server, thread, url = _running(tmp_path)
    try:
        status, result = _post(
            url,
            f"/v1/action/{command}",
            token="action-secret",
            payload={
                "client": {"run_id": RUN_ID},
                "arguments": AGENT_ARGUMENTS[command],
            },
        )
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert status == 200
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"] == [
        {"field": "client.request_id", "rule": "request_field_required"}
    ]


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/v1/admin/migrate", {"arguments": {}}),
        ("/v1/admin/recover", {"arguments": {"submission_id": OPERATION_ID}}),
        ("/v1/admin/repair-destination", {"arguments": {"submission_id": OPERATION_ID}}),
        ("/v1/admin/discard", {"arguments": {"submission_id": OPERATION_ID}}),
        ("/v1/admin/reopen", {"arguments": {"submission_id": OPERATION_ID}}),
        ("/v1/admin/supply-evidence", {"arguments": {"submission_id": OPERATION_ID}}),
        ("/v1/admin/record-human-decision", {"arguments": {"submission_id": OPERATION_ID}}),
        ("/v1/admin/authorize-governed-change", {"arguments": {"submission_id": OPERATION_ID}}),
        (f"/v1/admin/leases/{OPERATION_ID}/recover", {"reason": "operator recovery"}),
        ("/v1/admin/backups/create", {"label": "manual"}),
        ("/v1/admin/backups/restore", {"backup_id": "missing.sqlite3"}),
    ],
)
def test_every_admin_and_service_state_mutation_requires_request_id(tmp_path, path, payload):
    _service_obj, _backend, server, thread, url = _running(tmp_path)
    payload = {**payload, "client": {"run_id": RUN_ID}}
    try:
        status, result = _post(url, path, token="admin-secret", payload=payload)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert status == 400
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"] == [
        {"field": "client.request_id", "rule": "request_field_required"}
    ]


def test_renew_lease_requires_request_id(tmp_path):
    _service_obj, _backend, server, thread, url = _running(tmp_path)
    try:
        status, result = _post(
            url,
            f"/v1/leases/{OPERATION_ID}/renew",
            token="agent-secret",
            payload={"client": {"run_id": RUN_ID}},
        )
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert status == 400
    assert result["errors"] == [
        {"field": "client.request_id", "rule": "request_field_required"}
    ]


def test_malformed_request_id_is_not_recorded_and_identifies_field(tmp_path):
    service, _backend, server, thread, url = _running(tmp_path)
    try:
        _status, result = _post(
            url,
            "/v1/action/create",
            token="action-secret",
            payload={
                "client": {"run_id": RUN_ID, "request_id": "not-a-uuid"},
                "arguments": AGENT_ARGUMENTS["create"],
            },
        )
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert result["errors"] == [
        {
            "field": "client.request_id",
            "rule": "uuid_identifier_required",
            "expected_format": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        }
    ]
    from dish_tool.database import initialize_database
    conn = initialize_database(service.config.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM service_requests").fetchone()[0] == 0
    finally:
        conn.close()


def test_first_validation_failure_is_replayed_and_changed_reuse_conflicts(tmp_path):
    _service_obj, backend, server, thread, url = _running(tmp_path)
    payload = {
        "client": {"run_id": RUN_ID, "request_id": REQUEST_ID},
        "arguments": {"agent": "gpt", "task_gid": "bad-gid", "kind": "initial"},
    }
    try:
        first_status, first = _post(url, "/v1/action/start", token="action-secret", payload=payload)
        second_status, second = _post(url, "/v1/action/start", token="action-secret", payload=payload)
        changed = {
            **payload,
            "arguments": {"agent": "gpt", "task_gid": "different", "kind": "initial"},
        }
        conflict_status, conflict = _post(
            url, "/v1/action/start", token="action-secret", payload=changed
        )
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert first_status == second_status == conflict_status == 200
    assert first["code"] == second["code"] == "INVALID_ARGUMENT"
    assert second["data"]["request_replayed"] is True
    assert conflict["code"] == "CONFLICT"
    assert conflict["errors"][0]["rule"] == "service_request_identity_conflict"
    assert backend.writes == 0


def test_restore_request_replay_survives_database_replacement_and_restart(tmp_path, monkeypatch):
    service, backend = _service(tmp_path)
    principal = ServicePrincipal(owner_id="admin", run_id=RUN_ID)
    created = service.create_backup(
        label="restore-source", principal=principal, request_id=OTHER_REQUEST_ID
    )
    assert created["ok"]
    backup_id = created["data"]["backup"]["backup_id"]

    first = service.restore_backup(
        backup_id, principal=principal, request_id=REQUEST_ID
    )
    assert first["ok"]

    restarted = DishService(
        service.config,
        backend_factory=lambda: backend,
        release_loader=service.release_loader,
    )

    def forbidden_restore(self, _backup_id):
        raise AssertionError("restore executed twice")

    monkeypatch.setattr(BackupManager, "restore", forbidden_restore)
    replayed = restarted.restore_backup(
        backup_id, principal=principal, request_id=REQUEST_ID
    )
    assert replayed["ok"]
    assert replayed["data"]["request_replayed"] is True
    assert replayed["data"]["restored"] == first["data"]["restored"]


def test_pending_restore_request_is_not_blindly_repeated(tmp_path, monkeypatch):
    service, _backend = _service(tmp_path)
    principal = ServicePrincipal(owner_id="admin", run_id=RUN_ID)
    service._restore_requests.begin(
        request_id=REQUEST_ID,
        owner_id=principal.owner_id,
        run_id=principal.run_id,
        command="backup-restore",
        arguments={"backup_id": "candidate.sqlite3"},
    )

    def forbidden_restore(self, _backup_id):
        raise AssertionError("pending restore was repeated")

    monkeypatch.setattr(BackupManager, "restore", forbidden_restore)
    result = service.restore_backup(
        "candidate.sqlite3", principal=principal, request_id=REQUEST_ID
    )
    assert result["code"] == "BACKEND_UNCERTAIN"
    assert result["errors"][0]["rule"] == "service_request_pending"
    assert result["retryable"] is False

def _complete_service_submission(service, backend):
    constructor = ServicePrincipal(owner_id="action", run_id=RUN_ID)
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=constructor,
        request_id="10000000-0000-4000-8000-000000000001",
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
        request_id="10000000-0000-4000-8000-000000000002",
    )
    assert prepared["ok"]
    verifier = ServicePrincipal(
        owner_id="action", run_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    )
    reviewed = service.execute_agent(
        "start",
        {"agent": "codex", "task_gid": "t", "kind": "verification", "independence_attestation": "independent"},
        principal=verifier,
        request_id="10000000-0000-4000-8000-000000000003",
    )
    assert reviewed["ok"]
    inspected = service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": started["submission_id"]},
        principal=verifier,
    )
    assert inspected["ok"]
    approved = service.execute_agent(
        "approve",
        {
            "agent": "codex",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "correction": "none",
            "reviewed_identity": reviewed["data"]["reviewed_identity"],
            "semantic_review_complete": True,
            "provenance_complete": True,
            "independence_attestation": "independent",
        },
        principal=verifier,
        request_id="10000000-0000-4000-8000-000000000004",
    )
    assert approved["ok"]
    return verifier, started["submission_id"]


def test_completed_submit_exact_and_fresh_request_are_idempotent(tmp_path):
    service, backend = _service(tmp_path)
    verifier, operation_id = _complete_service_submission(service, backend)
    first_id = "10000000-0000-4000-8000-000000000005"
    first = service.execute_agent(
        "submit",
        {"submission_id": operation_id},
        principal=verifier,
        request_id=first_id,
    )
    assert first["ok"]
    moves = backend.moves

    exact = service.execute_agent(
        "submit",
        {"submission_id": operation_id},
        principal=verifier,
        request_id=first_id,
    )
    fresh = service.execute_agent(
        "submit",
        {"submission_id": operation_id},
        principal=verifier,
        request_id="10000000-0000-4000-8000-000000000006",
    )
    assert exact["ok"] and exact["data"]["request_replayed"] is True
    assert fresh["ok"] and fresh["data"]["completed_submission_reused"] is True
    assert fresh["data"]["signed_identity"] == first["data"]["signed_identity"]
    assert backend.moves == moves

def test_planning_rejects_nonexistent_destination_before_write(tmp_path):
    from tests.test_dish_tool_step6_prepare import Backend as PlanningBackend, PLANNING, app, write

    backend = PlanningBackend()
    application = app(tmp_path, backend)
    started = application.execute(
        "start", agent="gpt", task_gid="t", kind="planning"
    )
    candidate = PLANNING.replace("Sichuan — 12345", "Missing — 999999")
    result = application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=started["submission_id"],
        file_path=write(tmp_path, "planning.txt", candidate),
    )
    assert result["code"] == "VALIDATION_FAILED"
    assert result["errors"][0]["rule"] == "destination_unresolved"
    assert backend.writes == 0
    assert backend.moves == 0


def test_destination_deleted_after_approval_leaves_recoverable_open_state(tmp_path):
    from tests.test_dish_tool_step9_submit import _signed

    application, backend, operation_id = _signed(tmp_path)
    backend.sections = [row for row in backend.sections if row["gid"] != "12345"]
    result = application.execute("submit", submission_id=operation_id)
    assert result["code"] == "VALIDATION_FAILED"
    assert result["errors"][0]["rule"] == "destination_movement_unresolvable"
    assert result["errors"][0]["operation_state"] == "ready_move_failed"
    operation = application.conn.execute(
        "SELECT status, phase, signoff_completed_at FROM operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    assert operation["status"] == "open"
    assert operation["phase"] == "ready_move_failed"
    assert operation["signoff_completed_at"] is not None
    assert "Status: ready" in backend.notes
    assert backend.section == "vq"


def test_admin_destination_repair_changes_only_destination_and_preserves_signoff(tmp_path):
    from dish_tool.admin import DishAdminApplication
    from dish_tool.governed_diff import canonical_diff
    from dish_tool.task_document import parse_task_document
    from tests.test_dish_tool_step9_submit import _signed

    application, backend, operation_id = _signed(tmp_path)
    approved = parse_task_document(f"{backend.title}\n{backend.notes}")
    signed_cycle = application.conn.execute(
        "SELECT cycle_id, signed_identity FROM verification_cycles WHERE operation_id=? AND outcome='approved'",
        (operation_id,),
    ).fetchone()
    backend.sections = [row for row in backend.sections if row["gid"] != "12345"]
    failed = application.execute("submit", submission_id=operation_id)
    assert failed["errors"][0]["legal_next_action"] == "dish-admin repair-destination"
    inspected = application.execute("inspect", agent="gpt", submission_id=operation_id)
    assert inspected["ok"]
    assert inspected["allowed_actions"] == []
    assert inspected["data"]["required_admin_action"] == "repair-destination"
    assert inspected["data"]["authoritative_view"]["movement_failure"]["content_approved"] is True

    backend.sections.append({"gid": "67890", "name": "Hunan"})
    writes_before = backend.writes
    admin = DishAdminApplication(
        application.conn,
        backend=backend,
        release_loader=lambda: application.release_loader(None),
    )
    repaired = admin.execute(
        "repair-destination",
        submission_id=operation_id,
        destination_section_gid="67890",
        reason="approved destination was deleted",
        run_id="marco-repair-run",
    )
    assert repaired["ok"]
    assert repaired["allowed_actions"] == ["submit"]
    assert repaired["data"]["content_approved"] is True
    assert repaired["data"]["approved_identity"] == signed_cycle["signed_identity"]
    assert repaired["data"]["repaired_identity"] != signed_cycle["signed_identity"]
    assert backend.writes == writes_before + 1
    corrected = parse_task_document(f"{backend.title}\n{backend.notes}")
    assert set(canonical_diff(approved, corrected)) == {"planning.Destination section"}
    assert corrected.planning_brief.values["Destination section"] == "Hunan — 67890"
    unchanged_cycle = application.conn.execute(
        "SELECT cycle_id, signed_identity FROM verification_cycles WHERE operation_id=? AND outcome='approved'",
        (operation_id,),
    ).fetchone()
    assert dict(unchanged_cycle) == dict(signed_cycle)

    writes_after_repair = backend.writes
    submitted = application.execute("submit", submission_id=operation_id)
    assert submitted["ok"]
    assert submitted["data"]["destination"] == {"name": "Hunan", "gid": "67890"}
    assert submitted["data"]["signed_identity"] == signed_cycle["signed_identity"]
    assert submitted["data"]["effective_identity"] == repaired["data"]["repaired_identity"]
    assert submitted["data"]["destination_repair"]["actor_run_id"] == "marco-repair-run"
    assert backend.section == "67890"
    assert backend.writes == writes_after_repair


def test_destination_repair_rejects_queue_and_retry_safe_failure(tmp_path):
    from dish_tool.admin import DishAdminApplication
    from dish_tool.errors import BackendFailure
    from tests.test_dish_tool_step9_submit import _signed

    application, backend, operation_id = _signed(tmp_path / "unrecoverable")
    backend.sections = [row for row in backend.sections if row["gid"] != "12345"]
    assert not application.execute("submit", submission_id=operation_id)["ok"]
    admin = DishAdminApplication(
        application.conn,
        backend=backend,
        release_loader=lambda: application.release_loader(None),
    )
    illegal = admin.execute(
        "repair-destination",
        submission_id=operation_id,
        destination_section_gid="vq",
        reason="not a legal Cooking destination",
        run_id="marco-repair-run",
    )
    assert illegal["code"] == "VALIDATION_FAILED"
    assert illegal["errors"][0]["rule"] == "destination_is_queue"

    retry_app, retry_backend, retry_operation = _signed(tmp_path / "retry-safe")
    original_move = retry_backend.move_task_to_section

    def reject_once(*, task_gid, section_gid):
        retry_backend.move_task_to_section = original_move
        raise BackendFailure("BACKEND_REJECTED", "temporary movement rejection", retryable=True)

    retry_backend.move_task_to_section = reject_once
    failed = retry_app.execute("submit", submission_id=retry_operation)
    assert failed["errors"][0]["movement_retry_safe"] is True
    retry_admin = DishAdminApplication(
        retry_app.conn,
        backend=retry_backend,
        release_loader=lambda: retry_app.release_loader(None),
    )
    not_required = retry_admin.execute(
        "repair-destination",
        submission_id=retry_operation,
        destination_section_gid="12345",
        reason="should retry unchanged",
        run_id="marco-repair-run",
    )
    assert not_required["code"] == "WRONG_STATE"
    assert not_required["errors"][0]["rule"] == "operation_action_not_allowed"


def test_destination_repair_evidence_survives_restart(tmp_path):
    from dish_tool.admin import DishAdminApplication
    from dish_tool.commands import DishApplication
    from dish_tool.database import initialize_database
    from tests.test_dish_tool_step9_submit import _signed

    application, backend, operation_id = _signed(tmp_path)
    backend.sections = [row for row in backend.sections if row["gid"] != "12345"]
    assert not application.execute("submit", submission_id=operation_id)["ok"]
    backend.sections.append({"gid": "67890", "name": "Hunan"})
    admin = DishAdminApplication(
        application.conn,
        backend=backend,
        release_loader=lambda: application.release_loader(None),
    )
    repaired = admin.execute(
        "repair-destination",
        submission_id=operation_id,
        destination_section_gid="67890",
        reason="replacement after deletion",
        run_id="marco-repair-run",
    )
    assert repaired["ok"]
    repaired_identity = repaired["data"]["repaired_identity"]
    application.conn.close()

    restarted = DishApplication(
        initialize_database(tmp_path / "dish.db"),
        backend,
        release_loader=application.release_loader,
    )
    inspected = restarted.execute("inspect", agent="gpt", submission_id=operation_id)
    assert inspected["ok"]
    assert inspected["data"]["authoritative_view"]["required_identity"] == repaired_identity
    assert inspected["allowed_actions"] == ["submit"]
    submitted = restarted.execute("submit", submission_id=operation_id)
    assert submitted["ok"]
    assert submitted["data"]["effective_identity"] == repaired_identity
    assert backend.section == "67890"


def test_destination_repair_request_replays_without_second_write(tmp_path, monkeypatch):
    from dish_service.application import DishService
    from dish_service.config import ServiceConfig
    from dish_service.leases import ServicePrincipal
    from tests.test_dish_tool_step9_submit import _signed

    application, backend, operation_id = _signed(tmp_path)
    backend.sections = [row for row in backend.sections if row["gid"] != "12345"]
    assert not application.execute("submit", submission_id=operation_id)["ok"]
    backend.sections.append({"gid": "67890", "name": "Hunan"})
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "dish.db",
            honest_root=tmp_path / "honest",
            backup_dir=tmp_path / "backups",
            port=0,
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
        ),
        backend_factory=lambda: backend,
        release_loader=application.release_loader,
    )
    principal = ServicePrincipal(owner_id="marco", run_id=RUN_ID)
    arguments = {
        "submission_id": operation_id,
        "destination_section_gid": "67890",
        "reason": "approved destination was deleted",
    }
    request_id = "20000000-0000-4000-8000-000000000001"
    first = service.execute_admin(
        "repair-destination", arguments, principal=principal, request_id=request_id
    )
    assert first["ok"]
    writes = backend.writes

    def forbidden_write(*, task_gid, title, notes):
        raise AssertionError("destination repair content write repeated")

    monkeypatch.setattr(backend, "update_task_content", forbidden_write)
    replay = service.execute_admin(
        "repair-destination", arguments, principal=principal, request_id=request_id
    )
    assert replay["ok"]
    assert replay["data"]["request_replayed"] is True
    assert replay["data"]["repaired_identity"] == first["data"]["repaired_identity"]
    assert backend.writes == writes
    conflict = service.execute_admin(
        "repair-destination",
        {**arguments, "destination_section_gid": "12345"},
        principal=principal,
        request_id=request_id,
    )
    assert conflict["code"] == "CONFLICT"
    assert conflict["errors"][0]["rule"] == "service_request_identity_conflict"


def test_uncertain_destination_repair_recovers_from_live_evidence(tmp_path):
    from dish_tool.admin import DishAdminApplication
    from dish_tool.errors import BackendFailure
    from tests.test_dish_tool_step9_submit import _signed

    application, backend, operation_id = _signed(tmp_path)
    backend.sections = [row for row in backend.sections if row["gid"] != "12345"]
    assert not application.execute("submit", submission_id=operation_id)["ok"]
    backend.sections.append({"gid": "67890", "name": "Hunan"})
    original_read = backend.read_task
    original_write = backend.update_task_content
    fail_reread = False

    def write_then_lose_confirmation(*, task_gid, title, notes):
        nonlocal fail_reread
        original_write(task_gid=task_gid, title=title, notes=notes)
        fail_reread = True

    def read_with_one_lost_response(task_gid):
        nonlocal fail_reread
        if fail_reread:
            fail_reread = False
            raise BackendFailure(
                "BACKEND_UNCERTAIN", "simulated lost confirmation reread", retryable=False
            )
        return original_read(task_gid)

    backend.update_task_content = write_then_lose_confirmation
    backend.read_task = read_with_one_lost_response
    admin = DishAdminApplication(
        application.conn,
        backend=backend,
        release_loader=lambda: application.release_loader(None),
    )
    uncertain = admin.execute(
        "repair-destination",
        submission_id=operation_id,
        destination_section_gid="67890",
        reason="approved destination was deleted",
        run_id="marco-repair-run",
    )
    assert uncertain["code"] == "BACKEND_UNCERTAIN"
    assert uncertain["retryable"] is False
    assert application.conn.execute(
        "SELECT phase FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()[0] == "ready_move_failed"
    pending = application.conn.execute(
        "SELECT step_name FROM operation_steps WHERE operation_id=? AND completed_at IS NULL",
        (operation_id,),
    ).fetchall()
    assert [row["step_name"] for row in pending] == [
        row["step_name"] for row in pending if row["step_name"].startswith("destination_repair:")
    ]

    backend.update_task_content = original_write
    backend.read_task = original_read
    recovered = admin.execute(
        "recover",
        submission_id=operation_id,
        outcome="applied",
        reason="live task proves destination repair write applied",
    )
    assert recovered["ok"]
    assert any(
        action.get("step", "").startswith("destination_repair:")
        for action in recovered["data"]["actions"]
    )
    assert application.conn.execute(
        "SELECT phase FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()[0] == "await_submission"
    submitted = application.execute("submit", submission_id=operation_id)
    assert submitted["ok"]
    assert submitted["data"]["destination"]["gid"] == "67890"


def test_not_applied_destination_move_can_retry_without_content_write(tmp_path):
    from dish_tool.errors import BackendFailure
    from tests.test_dish_tool_step9_submit import _signed

    application, backend, operation_id = _signed(tmp_path)
    original_move = backend.move_task_to_section
    writes = backend.writes
    failed_once = False

    def reject_once(*, task_gid, section_gid):
        nonlocal failed_once
        if not failed_once and section_gid == "12345":
            failed_once = True
            raise BackendFailure(
                "BACKEND_REJECTED", "temporary movement rejection", retryable=True
            )
        return original_move(task_gid=task_gid, section_gid=section_gid)

    backend.move_task_to_section = reject_once
    failed = application.execute("submit", submission_id=operation_id)
    assert failed["code"] == "BACKEND_REJECTED"
    assert failed["errors"][0]["movement_retry_safe"] is True
    assert failed["errors"][0]["legal_next_action"] == "submit"
    assert application.conn.execute(
        "SELECT phase FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()[0] == "ready_move_failed"

    retried = application.execute("submit", submission_id=operation_id)
    assert retried["ok"]
    assert retried["data"]["handoff"] == "moved_to_destination"
    assert backend.section == "12345"
    assert backend.writes == writes

@pytest.mark.parametrize(
    ("route", "admin_command", "held_phase"),
    [
        ("evidence", "supply-evidence", "held_evidence"),
        ("human-review", "record-human-decision", "held_human"),
    ],
)
def test_initial_research_can_hold_before_prepare_and_resume_same_operation(
    tmp_path, route, admin_command, held_phase
):
    from dish_tool.admin import DishAdminApplication
    from tests.test_dish_tool_step6_prepare import Backend as PlanningBackend, TASK, app, release

    lines = TASK.splitlines()
    backend = PlanningBackend(lines[0], "\n".join(lines[1:]) + "\n")
    application = app(tmp_path, backend)
    started = application.execute(
        "start", agent="gpt", task_gid="t", kind="initial"
    )
    operation_id = started["submission_id"]
    writes = backend.writes
    held = application.execute(
        "reject",
        agent="gpt",
        submission_id=operation_id,
        route=route,
        reason="Need authoritative input before constructing a candidate",
        resume_status="pending-research",
    )
    assert held["ok"]
    assert held["data"]["description"] == "Research blocked before construction"
    assert held["data"]["candidate_content_existed"] is False
    assert held["state"] == "open"
    assert backend.writes == writes
    assert application.conn.execute(
        "SELECT COUNT(*) FROM verification_cycles WHERE operation_id=?", (operation_id,)
    ).fetchone()[0] == 0

    inspected = application.execute(
        "inspect", agent="gpt", submission_id=operation_id
    )
    view = inspected["data"]["authoritative_view"]
    assert view["phase"] == held_phase
    assert view["preconstruction_hold"] is True
    assert view["research_hold"]["candidate_content_existed"] is False
    assert view["research_hold"]["resume_status"] == "pending-research"

    admin = DishAdminApplication(
        application.conn,
        backend=backend,
        release_loader=lambda: release(tmp_path / "honest"),
    )
    resolved = admin.execute(
        admin_command,
        submission_id=operation_id,
        detail="Required input supplied",
        resume_status="pending-research",
    )
    assert resolved["ok"]
    assert resolved["data"]["candidate_content_existed"] is False
    assert resolved["data"]["phase"] == "prepare_required"
    assert backend.writes == writes

    resumed = application.execute(
        "inspect", agent="gpt", submission_id=operation_id
    )["data"]["authoritative_view"]
    assert resumed["phase"] == "prepare_required"
    assert "prepare" in resumed["legal_actions"]


def test_preconstruction_hold_rejects_wrong_resume_status_without_false_cycle(tmp_path):
    from tests.test_dish_tool_step6_prepare import Backend as PlanningBackend, TASK, app

    lines = TASK.splitlines()
    backend = PlanningBackend(lines[0], "\n".join(lines[1:]) + "\n")
    application = app(tmp_path, backend)
    started = application.execute(
        "start", agent="gpt", task_gid="t", kind="initial"
    )
    result = application.execute(
        "reject",
        agent="gpt",
        submission_id=started["submission_id"],
        route="evidence",
        reason="Need evidence",
        resume_status="pending-verification",
    )
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["rule"] == "preconstruction_resume_status_invalid"
    assert application.conn.execute(
        "SELECT COUNT(*) FROM verification_cycles WHERE operation_id=?",
        (started["submission_id"],),
    ).fetchone()[0] == 0

def test_service_preconstruction_hold_persists_request_identity_and_timestamp(tmp_path):
    service, _backend = _service(tmp_path)
    principal = ServicePrincipal(owner_id="action", run_id=RUN_ID)
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=principal,
        request_id="20000000-0000-4000-8000-000000000001",
    )
    assert started["ok"]
    hold_request_id = "20000000-0000-4000-8000-000000000002"
    held = service.execute_agent(
        "reject",
        {
            "agent": "gpt",
            "submission_id": started["submission_id"],
            "route": "evidence",
            "reason": "Need authoritative source before construction",
            "resume_status": "pending-research",
        },
        principal=principal,
        request_id=hold_request_id,
    )
    assert held["ok"]
    assert held["data"]["request_id"] == hold_request_id
    assert held["data"]["timestamp"].endswith("Z")

    inspected = service.execute_agent(
        "inspect",
        {"agent": "gpt", "submission_id": started["submission_id"]},
        principal=principal,
    )
    record = inspected["data"]["authoritative_view"]["research_hold"]
    assert record["request_id"] == hold_request_id
    assert record["timestamp"] == held["data"]["timestamp"]

def test_material_change_grammar_reports_all_detectable_subfields():
    from dish_tool.task_document import parse_task_document, validate_task_document
    from tests.test_dish_tool_step2_canonical import TASK as CANONICAL_TASK

    invalid = CANONICAL_TASK.replace(
        "2026-07-25 — ChatGPT — GPT-5 — tightened hydration — improve crispness — Large — pending-verification",
        "25/07/2026 — GPT —  —  —  — Medium — verified — GPT, bad, someday",
    )
    findings = validate_task_document(parse_task_document(invalid)).findings
    rules = {finding.rule for finding in findings}
    assert {
        "material-changes.format",
        "material-changes.date",
        "material-changes.agent",
        "material-changes.model",
        "material-changes.change",
        "material-changes.reason",
        "material-changes.materiality",
        "material-changes.verification",
    } <= rules
    format_finding = next(
        finding for finding in findings if finding.rule == "material-changes.format"
    )
    assert "exactly seven fields" not in format_finding.message
    assert "<YYYY-MM-DD>" in format_finding.message
    assert "<Small|Large>" in format_finding.message


def test_material_change_approval_finalizes_pending_entry_and_survives_restart(tmp_path):
    from dish_tool.database import initialize_database
    from dish_tool.task_document import parse_task_document
    from tests.test_dish_tool_r27_r29_readiness import _approve_and_submit
    from tests.test_dish_tool_r42_authority_matrix import _authorize_dish_candidate, _review
    from tests.test_dish_tool_step7_verification import make_app

    application, backend, initial_operation, _ = make_app(tmp_path)
    _approve_and_submit(application, initial_operation, run="initial-review")

    started = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="small",
        change_reason="rename candidate",
        run_id="change-editor",
    )
    assert started["ok"]
    operation_id = started["submission_id"]
    candidate = tmp_path / "material-change.txt"
    candidate.write_text(
        f"{backend.title}\n{backend.notes}".replace(
            "Dish candidate: Test dish", "Dish candidate: Different dish"
        )
    )
    _authorize_dish_candidate(application, backend, operation_id)
    prepared = application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        file_path=str(candidate),
        material_classification="material",
        run_id="change-editor",
    )
    assert prepared["ok"]
    assert "Small — pending-verification" in backend.notes

    review = _review(application, run="change-review", agent="codex")
    approved = application.execute(
        "approve",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True,
        provenance_complete=True,
        run_id="change-review",
        independence_attestation="independent",
    )
    assert approved["ok"]
    document = parse_task_document(f"{backend.title}\n{backend.notes}")
    assert "Small — verified — Codex, self-reported model: gpt-5.6-sol," in document.material_changes[-1]
    assert not document.material_changes[-1].endswith(" — pending-verification")

    submitted = application.execute("submit", submission_id=operation_id)
    assert submitted["ok"]
    signed_identity = approved["data"]["signed_identity"]
    application.conn.close()

    reopened = initialize_database(tmp_path / "dish.db")
    try:
        cycle = reopened.execute(
            """SELECT signed_identity,signed_content_version_id
                 FROM verification_cycles
                WHERE operation_id=? AND outcome='approved'""",
            (operation_id,),
        ).fetchone()
        assert cycle["signed_identity"] == signed_identity
        version = reopened.execute(
            "SELECT notes FROM content_versions WHERE content_version_id=?",
            (cycle["signed_content_version_id"],),
        ).fetchone()
        assert "Small — verified — Codex, self-reported model: gpt-5.6-sol," in version["notes"]
        assert "Small — pending-verification" not in version["notes"]
    finally:
        reopened.close()


def test_submit_refuses_ready_task_with_latest_material_change_pending(tmp_path, monkeypatch):
    import dataclasses

    from dish_tool.database import content_identity
    from dish_tool import step9
    from dish_tool.task_document import parse_task_document
    from tests.test_dish_tool_r27_r29_readiness import _approve_and_submit
    from tests.test_dish_tool_r42_authority_matrix import _authorize_dish_candidate, _review
    from tests.test_dish_tool_step7_verification import make_app

    application, backend, initial_operation, _ = make_app(tmp_path)
    _approve_and_submit(application, initial_operation, run="initial-review")
    started = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="small",
        change_reason="rename candidate",
        run_id="change-editor",
    )
    operation_id = started["submission_id"]
    candidate = tmp_path / "material-change-pending.txt"
    candidate.write_text(
        f"{backend.title}\n{backend.notes}".replace(
            "Dish candidate: Test dish", "Dish candidate: Different dish"
        )
    )
    _authorize_dish_candidate(application, backend, operation_id)
    assert application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        file_path=str(candidate),
        material_classification="material",
        run_id="change-editor",
    )["ok"]
    review = _review(application, run="change-review", agent="codex")
    approved = application.execute(
        "approve",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True,
        provenance_complete=True,
        run_id="change-review",
        independence_attestation="independent",
    )
    assert approved["ok"]

    signed = parse_task_document(f"{backend.title}\n{backend.notes}")
    latest = signed.material_changes[-1]
    pending = latest.split(" — verified — ", 1)[0] + " — pending-verification"
    corrupted = dataclasses.replace(
        signed,
        material_changes=signed.material_changes[:-1] + (pending,),
    )
    lines = corrupted.render().splitlines()
    backend.title = lines[0]
    backend.notes = "\n".join(lines[1:]) + "\n"
    identity = content_identity(backend.title, backend.notes).digest
    monkeypatch.setattr(step9, "_signed_identity", lambda conn, op_id: identity)

    with pytest.raises(Exception) as exc_info:
        step9.submit_live(application.conn, backend, operation_id=operation_id)
    error = exc_info.value
    assert getattr(error, "rule", None) == "material_change_verification_pending"
    row = application.conn.execute(
        "SELECT status,phase,terminal_outcome FROM operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    assert tuple(row) == ("open", "await_submission", None)

def test_post_signoff_change_cannot_rewrite_material_change_history(tmp_path):
    from tests.test_dish_tool_r27_r29_readiness import _approve_and_submit
    from tests.test_dish_tool_r42_authority_matrix import _authorize_dish_candidate, _review
    from tests.test_dish_tool_step7_verification import make_app

    application, backend, initial_operation, _ = make_app(tmp_path)
    _approve_and_submit(application, initial_operation, run="initial-review")

    first = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="small",
        change_reason="rename candidate",
        run_id="first-editor",
    )
    first_candidate = tmp_path / "first-material-change.txt"
    first_candidate.write_text(
        f"{backend.title}\n{backend.notes}".replace(
            "Dish candidate: Test dish", "Dish candidate: Different dish"
        )
    )
    _authorize_dish_candidate(application, backend, first["submission_id"])
    assert application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=first["submission_id"],
        file_path=str(first_candidate),
        material_classification="material",
        run_id="first-editor",
    )["ok"]
    review = _review(application, run="first-review", agent="codex")
    approved = application.execute(
        "approve",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=first["submission_id"],
        correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True,
        provenance_complete=True,
        run_id="first-review",
        independence_attestation="independent",
    )
    assert approved["ok"]
    assert application.execute("submit", submission_id=first["submission_id"])["ok"]

    second = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="small",
        change_reason="handling clarification",
        run_id="second-editor",
    )
    tampered = f"{backend.title}\n{backend.notes}".replace(
        "updated the candidate", "rewrote the historical description"
    ).replace("1. Cook it.", "1. Cook it gently.")
    second_candidate = tmp_path / "tampered-history.txt"
    second_candidate.write_text(tampered)
    result = application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=second["submission_id"],
        file_path=str(second_candidate),
        material_classification="non-material",
        run_id="second-editor",
    )
    assert result["code"] == "VALIDATION_FAILED"
    assert result["errors"][0]["rule"] == "material_change_history_modified"
    assert result["errors"][0]["authority"] == (
        "Dish appends and finalizes the current workflow entry"
    )

def test_material_classification_is_required_only_for_changed_post_signoff_body(tmp_path):
    from tests.test_dish_tool_r27_r29_readiness import _approve_and_submit
    from tests.test_dish_tool_step7_verification import make_app

    application, backend, initial_operation, _ = make_app(tmp_path)
    _approve_and_submit(application, initial_operation, run="initial-review")
    started = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="small",
        change_reason="clarify serving handling",
        run_id="later-editor",
    )
    candidate = tmp_path / "classification-required.txt"
    candidate.write_text(
        f"{backend.title}\n{backend.notes}".replace("1. Cook it.", "1. Cook it gently.")
    )
    missing = application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=started["submission_id"],
        file_path=str(candidate),
        run_id="later-editor",
    )
    assert missing["code"] == "INVALID_ARGUMENT"
    assert missing["errors"][0]["rule"] == "material_classification_required"
    assert missing["errors"][0]["classified_subject"] == (
        "canonical body diff from the signed baseline"
    )


def test_material_classification_reports_effective_route_and_forced_reasons(tmp_path):
    from tests.test_dish_tool_r27_r29_readiness import _approve_and_submit
    from tests.test_dish_tool_r42_authority_matrix import _authorize_dish_candidate
    from tests.test_dish_tool_step7_verification import make_app

    application, backend, initial_operation, _ = make_app(tmp_path)
    _approve_and_submit(application, initial_operation, run="initial-review")
    started = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="small",
        change_reason="rename candidate",
        run_id="later-editor",
    )
    candidate = tmp_path / "classification-forced.txt"
    candidate.write_text(
        f"{backend.title}\n{backend.notes}".replace(
            "Dish candidate: Test dish", "Dish candidate: Different dish"
        )
    )
    _authorize_dish_candidate(application, backend, started["submission_id"])
    prepared = application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=started["submission_id"],
        file_path=str(candidate),
        material_classification="non-material",
        run_id="later-editor",
    )
    classification = prepared["data"]["material_classification"]
    assert classification == {
        "classified_subject": "canonical body diff from the signed baseline",
        "requested": "non-material",
        "effective": "material",
        "forced_material_reasons": ["dish_candidate"],
        "route": "verification",
    }


def test_material_classification_is_rejected_when_no_body_diff_exists(tmp_path):
    from tests.test_dish_tool_r27_r29_readiness import _approve_and_submit
    from tests.test_dish_tool_step7_verification import make_app

    application, backend, initial_operation, _ = make_app(tmp_path)
    _approve_and_submit(application, initial_operation, run="initial-review")
    started = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="small",
        change_reason="no-op probe",
        run_id="later-editor",
    )
    candidate = tmp_path / "classification-no-diff.txt"
    candidate.write_text(f"{backend.title}\n{backend.notes}")
    result = application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=started["submission_id"],
        file_path=str(candidate),
        material_classification="non-material",
        run_id="later-editor",
    )
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["rule"] == "material_classification_not_applicable"
