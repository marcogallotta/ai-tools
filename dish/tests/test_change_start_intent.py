from __future__ import annotations

import sqlite3
import uuid

import pytest

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import ServicePrincipal
from dish_tool import database as database_module
from dish_tool.database import confirm_task_content, create_operation
from dish_tool.commands import DishApplication
from dish_tool.database_initialization import initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.models import OperationActors
from tests.support.readiness import _approve_and_submit
from tests.support.verification import Backend, make_app


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


def _signed_ready(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    app, backend, operation_id, _ = make_app(tmp_path)
    _approve_and_submit(app, operation_id)
    return app, backend


def test_change_operation_and_required_intent_commit_atomically(monkeypatch, tmp_path):
    app, _backend = _signed_ready(tmp_path)
    conn = app.conn
    identity = conn.execute(
        "SELECT last_confirmed_identity FROM task_content_state WHERE task_gid='t'"
    ).fetchone()[0]
    operation_count = conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
    step_count = conn.execute("SELECT COUNT(*) FROM operation_steps").fetchone()[0]

    def interrupt(*_args, **_kwargs):
        raise RuntimeError("interrupted while recording change intent")

    monkeypatch.setattr(database_module, "declare_operation_step", interrupt)
    with pytest.raises(RuntimeError, match="interrupted"):
        create_operation(
            conn,
            task_gid="t",
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

    assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == operation_count
    assert conn.execute("SELECT COUNT(*) FROM operation_steps").fetchone()[0] == step_count
    conn.close()


def test_semantic_validation_rejects_change_without_completed_intent(tmp_path):
    db_path = tmp_path / "dish.db"
    app, _backend = _signed_ready(tmp_path)
    conn = app.conn
    identity = conn.execute(
        "SELECT last_confirmed_identity FROM task_content_state WHERE task_gid='t'"
    ).fetchone()[0]
    operation = create_operation(
        conn,
        task_gid="t",
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
    assert problem["mutation_provenance"]["task_gid"] == "t"
    assert problem["broken_relationship"]["targets"][0]["record_type"] == "operation_steps"
    assert "Correct salt" not in repr(raised.value.details)


def test_pending_change_start_cannot_replay_without_exact_intent(tmp_path):
    db_path = tmp_path / "dish.db"
    app, _backend = _signed_ready(tmp_path)
    conn = app.conn
    identity = conn.execute(
        "SELECT last_confirmed_identity FROM task_content_state WHERE task_gid='t'"
    ).fetchone()[0]
    operation = create_operation(
        conn,
        task_gid="t",
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
                "task_gid": "t",
                "kind": "change",
                "change_level": "small",
                "change_reason": "Correct salt",
            },
            request_id=str(uuid.uuid4()),
        )

    assert raised.value.rule == "service_request_pending"
    assert raised.value.details["operation_id"] == operation["operation_id"]
    conn.close()


def test_direct_create_change_requires_signed_baseline_before_insert(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    identity = _baseline(conn)

    with pytest.raises(DishRuleError) as raised:
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

    assert raised.value.rule == "post_signoff_change_signed_baseline_required"
    assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
    conn.close()


def test_read_exposes_change_only_for_exact_signed_ready_resting_task(tmp_path):
    app, backend = _signed_ready(tmp_path / "signed")

    read = app.execute("read", agent="gpt", task_gid="t")

    assert read["ok"]
    assert read["allowed_actions"] == ["start"]
    assert read["data"]["required_start_kind"] == "change"

    unsigned_backend = Backend()
    unsigned_backend.title = backend.title
    unsigned_backend.notes = backend.notes
    unsigned_backend.section = backend.section
    unsigned_backend.completed = backend.completed
    unsigned_app = DishApplication(
        initialize_database(tmp_path / "unsigned.db"),
        unsigned_backend,
        release_loader=app.release_loader,
    )

    unsigned = unsigned_app.execute("read", agent="gpt", task_gid="t")

    assert unsigned["ok"]
    assert unsigned["allowed_actions"] == []
    assert "required_start_kind" not in unsigned["data"]


def test_read_suppresses_change_when_signed_ready_task_is_in_excluded_section(tmp_path):
    app, backend = _signed_ready(tmp_path)
    backend.section = "src"

    read = app.execute("read", agent="gpt", task_gid="t")

    assert read["ok"]
    assert read["allowed_actions"] == []
    assert "required_start_kind" not in read["data"]

    started = app.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="small",
        change_reason="Correct one exact detail",
        run_id="excluded-change-run",
    )

    assert not started["ok"]
    assert started["code"] == "UNMANAGED_TASK"
    assert started["errors"] == [{"rule": "task_in_excluded_section"}]
    assert app.conn.execute(
        "SELECT COUNT(*) FROM operations WHERE status IN ('open','uncertain')"
    ).fetchone()[0] == 0


def test_start_uses_same_resting_authority_and_wrong_kind_cannot_strand_operation(tmp_path):
    app, _backend = _signed_ready(tmp_path)

    wrong = app.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="initial",
        change_level=None,
        change_reason=None,
        run_id="wrong-kind",
    )

    assert not wrong["ok"]
    assert wrong["errors"][0]["rule"] == "resting_task_start_kind_mismatch"
    assert wrong["data"]["required_start_kind"] == "change"
    assert app.conn.execute(
        "SELECT COUNT(*) FROM operations WHERE status IN ('open','uncertain')"
    ).fetchone()[0] == 0

    started = app.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="small",
        change_reason="Correct one exact detail",
        run_id="change-run",
    )

    assert started["ok"]
    assert started["allowed_actions"] == ["prepare"]


def test_change_start_rejects_ready_text_without_durable_signoff(tmp_path):
    signed_app, signed_backend = _signed_ready(tmp_path / "source")
    backend = Backend()
    backend.title = signed_backend.title
    backend.notes = signed_backend.notes
    backend.section = signed_backend.section
    app = DishApplication(
        initialize_database(tmp_path / "unsigned.db"),
        backend,
        release_loader=signed_app.release_loader,
    )

    result = app.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="small",
        change_reason="Correct one exact detail",
        run_id="change-run",
    )

    assert not result["ok"]
    assert result["errors"][0]["rule"] == "post_signoff_change_signed_baseline_required"
    assert app.conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
