from __future__ import annotations

import json

import pytest

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import ServicePrincipal
from dish_tool.database import confirm_task_content, initialize_database
from dish_tool.task_document import parse_task_document, validate_task_document
from tests.support.service_foundation import _release_loader
from tests.support.request_restore import Backend
from tests.support.verification import TASK


INVALID_REASONS = [
    pytest.param("first line\nsecond line", id="lf"),
    pytest.param("first line\rsecond line", id="cr"),
    pytest.param("first line\r\nsecond line", id="crlf"),
    pytest.param("first line\u2028second line", id="unicode-line-separator"),
    pytest.param("first line\u2029second line", id="unicode-paragraph-separator"),
]

WORKFLOW_EVIDENCE_TABLES = (
    "operation_steps",
    "operation_executions",
    "write_attempts",
    "movement_attempts",
    "content_versions",
    "verification_cycles",
    "operation_actor_facts",
    "audit_events",
)


class CountingFactory:
    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.calls = 0

    def __call__(self) -> Backend:
        self.calls += 1
        return self.backend


def _principal(run_id: str) -> ServicePrincipal:
    return ServicePrincipal(owner_id="action", run_id=run_id)


def _service(tmp_path):
    backend = Backend()
    factory = CountingFactory(backend)
    honest = tmp_path / "honest"
    honest.mkdir()
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
        backend_factory=factory,
        release_loader=_release_loader(honest),
    )
    return service, backend, factory


def _awaiting_verification(tmp_path):
    service, backend, factory = _service(tmp_path)
    constructor = _principal("constructor-run")
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=constructor,
        request_id="10000000-0000-4000-8000-000000000001",
    )
    assert started["ok"]
    operation_id = started["submission_id"]
    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": operation_id,
            "file_text": TASK,
        },
        principal=constructor,
        request_id="10000000-0000-4000-8000-000000000002",
    )
    assert prepared["ok"]

    verifier = _principal("verifier-run")
    reviewed = service.execute_agent(
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
    inspected = service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": operation_id},
        principal=verifier,
    )
    assert inspected["ok"]
    return service, backend, factory, operation_id, verifier


def _workflow_snapshot(service: DishService, operation_id: str):
    conn = initialize_database(service.config.db_path)
    try:
        operation = conn.execute(
            "SELECT status, phase, expected_identity FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        lease = conn.execute(
            "SELECT owner_id, run_id, released_at FROM service_leases WHERE operation_id=? "
            "ORDER BY acquired_at DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in WORKFLOW_EVIDENCE_TABLES
        }
        return {
            "operation": None if operation is None else tuple(operation),
            "lease": None if lease is None else tuple(lease),
            "counts": counts,
        }
    finally:
        conn.close()


@pytest.mark.parametrize("reason", INVALID_REASONS)
def test_unsafe_reason_fails_after_journaling_but_before_backend_or_workflow_mutation(
    tmp_path, reason
):
    service, backend, factory, operation_id, verifier = _awaiting_verification(tmp_path)
    request_id = "20000000-0000-4000-8000-000000000001"
    corrected = TASK.replace("100 g test ingredient", "120 g test ingredient")
    task_before = (backend.title, backend.notes, backend.section, backend.writes, backend.moves)
    workflow_before = _workflow_snapshot(service, operation_id)
    factory_calls_before = factory.calls

    rejected = service.execute_agent(
        "reject",
        {
            "agent": "codex",
            "model": "gpt-5.6-sol",
            "submission_id": operation_id,
            "route": "large",
            "reason": reason,
            "file_text": corrected,
        },
        principal=verifier,
        request_id=request_id,
    )

    assert rejected["code"] == "INVALID_ARGUMENT"
    assert rejected["retryable"] is True
    assert rejected["errors"] == [
        {"rule": "rejection_reason_invalid_characters", "field": "reason"}
    ]
    assert rejected["data"]["request_id"] == request_id
    assert factory.calls == factory_calls_before
    assert (backend.title, backend.notes, backend.section, backend.writes, backend.moves) == task_before
    assert _workflow_snapshot(service, operation_id) == workflow_before

    conn = initialize_database(service.config.db_path)
    try:
        request = conn.execute(
            "SELECT status, result_json FROM service_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        assert request["status"] == "completed"
        durable = json.loads(request["result_json"])
        assert durable["ok"] is False
        assert durable["errors"] == rejected["errors"]
    finally:
        conn.close()


def test_invalid_request_replays_exact_failure_then_fresh_valid_long_reason_proceeds(tmp_path):
    service, backend, factory, operation_id, verifier = _awaiting_verification(tmp_path)
    corrected = TASK.replace("100 g test ingredient", "120 g test ingredient")
    invalid_request_id = "30000000-0000-4000-8000-000000000001"
    invalid_arguments = {
        "agent": "codex",
        "model": "gpt-5.6-sol",
        "submission_id": operation_id,
        "route": "large",
        "reason": "unsafe\nreason",
        "file_text": corrected,
    }

    first = service.execute_agent(
        "reject",
        invalid_arguments,
        principal=verifier,
        request_id=invalid_request_id,
    )
    task_after_first = (backend.title, backend.notes, backend.section, backend.writes, backend.moves)
    workflow_after_first = _workflow_snapshot(service, operation_id)
    factory_calls_after_first = factory.calls

    replay = service.execute_agent(
        "reject",
        invalid_arguments,
        principal=verifier,
        request_id=invalid_request_id,
    )
    assert replay["code"] == first["code"] == "INVALID_ARGUMENT"
    assert replay["errors"] == first["errors"]
    assert replay["retryable"] == first["retryable"] is True
    assert replay["data"]["request_replayed"] is True
    assert factory.calls == factory_calls_after_first
    assert (backend.title, backend.notes, backend.section, backend.writes, backend.moves) == task_after_first
    assert _workflow_snapshot(service, operation_id) == workflow_after_first

    long_reason = "single-line evidence " + ("x" * 4096)
    accepted = service.execute_agent(
        "reject",
        {
            **invalid_arguments,
            "reason": long_reason,
        },
        principal=verifier,
        request_id="30000000-0000-4000-8000-000000000002",
    )
    assert accepted["ok"]
    assert accepted["allowed_actions"] == ["start"]
    assert accepted["data"]["required_start_kind"] == "verification"

    document = parse_task_document(f"{backend.title}\n{backend.notes}")
    validation = validate_task_document(
        document, expected_schema_version="2", schema={}
    )
    assert validation.ok
    assert len(document.material_changes) == 1
    assert long_reason in document.material_changes[0]
    assert document.material_changes[0].split(" — ", 6)[4] == long_reason

    next_verifier = _principal("fresh-verifier-run")
    restarted = service.execute_agent(
        "start",
        {
            "agent": "gpt",
            "task_gid": "t",
            "kind": "verification",
            "independence_attestation": "independent",
        },
        principal=next_verifier,
        request_id="30000000-0000-4000-8000-000000000003",
    )
    assert restarted["ok"]
    assert restarted["submission_id"] == operation_id
    assert restarted["allowed_actions"] == ["inspect"]


def test_historical_malformed_material_change_is_not_rewritten_and_has_manual_guidance(tmp_path):
    service, backend, _factory, operation_id, verifier = _awaiting_verification(tmp_path)
    corrected = TASK.replace("100 g test ingredient", "120 g test ingredient")
    valid_reason = "historical valid reason"
    rejected = service.execute_agent(
        "reject",
        {
            "agent": "codex",
            "model": "gpt-5.6-sol",
            "submission_id": operation_id,
            "route": "large",
            "reason": valid_reason,
            "file_text": corrected,
        },
        principal=verifier,
        request_id="40000000-0000-4000-8000-000000000001",
    )
    assert rejected["ok"]

    malformed_notes = backend.notes.replace(
        f"{valid_reason} — Large — pending-verification",
        f"{valid_reason}\ncontinued historical text — Large — pending-verification",
    )
    assert malformed_notes != backend.notes
    backend.notes = malformed_notes
    conn = initialize_database(service.config.db_path)
    try:
        confirm_task_content(
            conn,
            task_gid="t",
            title=backend.title,
            notes=backend.notes,
            schema_version="2",
            operation_id=operation_id,
            boundary="historical_malformed_fixture",
        )
    finally:
        conn.close()

    task_before = (backend.title, backend.notes, backend.section, backend.writes, backend.moves)
    blocked = service.execute_agent(
        "start",
        {
            "agent": "gpt",
            "task_gid": "t",
            "kind": "verification",
            "independence_attestation": "independent",
        },
        principal=_principal("historical-recovery-run"),
        request_id="40000000-0000-4000-8000-000000000002",
    )

    assert blocked["code"] == "WRONG_STATE"
    assert blocked["errors"][0]["rule"] == "workflow_recovery_required"
    assert blocked["allowed_actions"] == []
    assert blocked["data"]["recovery_required"] is True
    assert "historical_material_change_malformed" in blocked["data"]["recovery_reasons"]
    assert blocked["data"]["required_admin_action"] == "manual-reconciliation"
    assert blocked["data"]["connected_action_available"] is False
    assert blocked["data"]["continuation_surface"] == "manual-reconciliation"
    assert blocked["data"]["admin_command"] is None
    assert "durable exact-content binding" in blocked["data"]["resolver"]
    assert blocked["data"]["historical_evidence"] == {
        "kind": "malformed-material-change",
        "validation_rules": [
            "material-changes.field-count",
            "material-changes.format",
        ],
        "automatic_rewrite": False,
        "required_scope": [
            "live-task-evidence",
            "durable-exact-content-binding",
        ],
    }
    assert (backend.title, backend.notes, backend.section, backend.writes, backend.moves) == task_before
