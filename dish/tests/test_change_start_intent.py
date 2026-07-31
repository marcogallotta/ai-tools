from __future__ import annotations

import sqlite3
import uuid

import pytest

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import ServicePrincipal
from dish_tool import database as database_module
from dish_tool.database import confirm_task_content, create_operation
from dish_tool.database_schema import initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.models import OperationActors


def _baseline(conn, task_gid="task-change"):
    identity = confirm_task_content(
        conn,
        task_gid=task_gid,
        title="Dish",
        notes="Canonical notes",
        schema_version="2",
        boundary="test",
    )
    return identity.digest


def test_change_operation_and_required_intent_commit_atomically(monkeypatch, tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    identity = _baseline(conn)

    def interrupt(*_args, **_kwargs):
        raise RuntimeError("interrupted while recording change intent")

    monkeypatch.setattr(database_module, "declare_operation_step", interrupt)
    with pytest.raises(RuntimeError, match="interrupted"):
        create_operation(
            conn,
            task_gid="task-change",
            operation_kind="change",
            expected_identity=identity,
            schema_version="2",
            expected_section_gid="section",
            actors=OperationActors(
                editor_agent="gpt",
                run_id="11111111-1111-4111-8111-111111111111",
            ),
            initial_steps={
                "change_intent": {"level": "small", "reason": "Correct salt"}
            },
        )

    assert conn.execute(
        "SELECT 1 FROM operations WHERE task_gid='task-change'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM operation_steps"
    ).fetchone() is None
    conn.close()


def test_semantic_validation_rejects_change_without_completed_intent(tmp_path):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    identity = _baseline(conn)
    operation = create_operation(
        conn,
        task_gid="task-change",
        operation_kind="change",
        expected_identity=identity,
        schema_version="2",
        expected_section_gid="section",
        actors=OperationActors(
            editor_agent="gpt",
            run_id="11111111-1111-4111-8111-111111111111",
        ),
    )
    conn.close()

    with pytest.raises(DishRuleError) as raised:
        initialize_database(db_path)

    assert raised.value.rule == "database_semantic_evidence_invalid"
    problem = next(
        item for item in raised.value.details["problems"]
        if item["invariant"] == "change_operation_intent_binding"
    )
    assert problem["record_id"] == operation["operation_id"]
    assert problem["mutation_provenance"]["task_gid"] == "task-change"
    assert problem["broken_relationship"]["targets"][0]["record_type"] == "operation_steps"
    assert "Correct salt" not in repr(raised.value.details)


def test_pending_change_start_cannot_replay_without_exact_intent(tmp_path):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    identity = _baseline(conn)
    operation = create_operation(
        conn,
        task_gid="task-change",
        operation_kind="change",
        expected_identity=identity,
        schema_version="2",
        expected_section_gid="section",
        actors=OperationActors(
            editor_agent="gpt",
            run_id="11111111-1111-4111-8111-111111111111",
        ),
    )
    service = DishService(
        ServiceConfig(db_path=db_path, honest_root=tmp_path),
        backend_factory=lambda: object(),
    )

    with pytest.raises(DishRuleError) as raised:
        service._reconcile_pending_start(
            conn=conn,
            backend=None,
            app=None,
            leases=None,
            principal=ServicePrincipal(
                owner_id="owner",
                run_id="11111111-1111-4111-8111-111111111111",
            ),
            arguments={
                "agent": "gpt",
                "task_gid": "task-change",
                "kind": "change",
                "change_level": "small",
                "change_reason": "Correct salt",
            },
            request_id=str(uuid.uuid4()),
        )

    assert raised.value.rule == "service_request_pending"
    assert raised.value.details["operation_id"] == operation["operation_id"]
    conn.close()
