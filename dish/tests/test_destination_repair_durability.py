from __future__ import annotations

import json

import pytest

from dish_service.application import DishService
from dish_service.backup import BackupManager
from dish_service.config import ServiceConfig
from dish_service.leases import ServicePrincipal
from tests.support.service_scenarios import (
    OTHER_REQUEST_ID,
    REQUEST_ID,
    RUN_ID,
    complete_service_submission as _complete_service_submission,
    service as _service,
)
from tests.support.service_foundation import _release_loader
from tests.support.request_restore import Backend
from tests.support.planning import Backend as PlanningBackend, PLANNING, app, write
from tests.support.submission import _signed
from tests.support.planning import Backend as PlanningBackend, TASK, app, release
from tests.support.planning import Backend as PlanningBackend, TASK, app

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
    # A legacy pending record has no exact-effect checkpoint and must remain
    # fail-closed rather than being re-executed from inference.
    journal_path = service._restore_requests._record_path(REQUEST_ID)
    legacy = json.loads(journal_path.read_text(encoding="utf-8"))
    legacy.pop("checkpoints", None)
    service._restore_requests._write(journal_path, legacy)

    def forbidden_restore(self, _backup_id):
        raise AssertionError("pending restore was repeated")

    monkeypatch.setattr(BackupManager, "restore", forbidden_restore)
    result = service.restore_backup(
        "candidate.sqlite3", principal=principal, request_id=REQUEST_ID
    )
    assert result["code"] == "BACKEND_UNCERTAIN"
    assert result["errors"][0]["rule"] == "service_request_pending"
    assert result["retryable"] is False
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
    from dish_tool.database_initialization import initialize_database

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
