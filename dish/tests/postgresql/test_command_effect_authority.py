from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg.command_effects import CommandEffectSpec, effect_spec_for, expected_projection_count
from dish_pg.command_port import CommandEffectMismatch, PostgresCommandPort
from dish_pg.database import session_scope
from dish_pg.transition import ProjectionService
from tests.support.postgresql.workflow import _next, _register_run, workflow_db
from tests.support.postgresql.command import SECRET, _call, _port
from tests.support.postgresql.command import (
    _add_verification_queue,
    _inspect,
    _prepare_for_verification,
    _start_initial,
    _verification_ready,
)

pytestmark = pytest.mark.smoke


def test_effect_spec_is_the_exact_branch_sensitive_authority() -> None:
    assert effect_spec_for("prepare", {}).projection_event_types == (
        "update_task_document",
        "move_task",
    )
    assert effect_spec_for("approve", {"correction": "none"}) == CommandEffectSpec(
        (
            "activate_corrected_content_version",
            "record_verification_signoff",
            "advance_operation",
        ),
        ("update_task_document",),
        verify_mutation_effects=True,
    )
    assert effect_spec_for("approve", {"correction": "small"}).projection_event_types == (
        "update_task_document",
    )
    assert effect_spec_for("reject", {"route": "large"}).projection_event_types == (
        "update_task_document",
    )
    assert expected_projection_count("reject", {"route": "evidence"}) == 1
    assert expected_projection_count("reject", {"route": "human_review"}) == 1
    assert expected_projection_count("migrate", {}) == 2

    verified = {
        command
        for command, arguments in (
            ("create", {}),
            ("start", {"kind": "initial"}),
            ("prepare", {}),
            ("migrate", {}),
            ("approve", {"correction": "none"}),
            ("reject", {"route": "large"}),
            ("submit", {}),
        )
        if effect_spec_for(command, arguments).verify_mutation_effects
    }
    assert verified == {"prepare", "approve", "reject"}


class _DropMoveProjection:
    def __init__(self, delegate: ProjectionService) -> None:
        self.delegate = delegate

    def record(self, **kwargs):
        if kwargs["event_type"] == "move_task":
            return uuid.uuid4()
        return self.delegate.record(**kwargs)

    def recover(self, **kwargs):
        return self.delegate.recover(**kwargs)

    def unresolved_attempt_id(self, task_id):
        return self.delegate.unresolved_attempt_id(task_id)

    def task_freshness(self, task_id):
        return self.delegate.task_freshness(task_id)


def test_execution_rejects_and_rolls_back_missing_projection_intent(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    author_run = _next(ids)
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        _register_run(session, generation_id=context["generation_id"], run_id=author_run)
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=author_run)
        operation_id = started.data["operation_id"]
        baseline_activations = session.scalar(
            select(func.count()).select_from(models.ContentActivation).where(
                models.ContentActivation.task_id == task_id
            )
        )
        baseline_events = session.scalar(
            select(func.count()).select_from(tx.ProjectionOutboxEvent).where(
                tx.ProjectionOutboxEvent.task_id == task_id
            )
        )

    with pytest.raises(CommandEffectMismatch, match="projection effects mismatch"):
        with session_scope(factory) as session:
            projection = ProjectionService(session, uuid_factory=lambda: _next(ids))
            port = PostgresCommandPort(
                session,
                cursor_secret=SECRET,
                uuid_factory=lambda: _next(ids),
                projection_recorder=_DropMoveProjection(projection),
            )
            _prepare_for_verification(
                port,
                ids,
                task_id=task_id,
                operation_id=operation_id,
                run_id=author_run,
            )

    with session_scope(factory) as session:
        assert session.scalar(
            select(func.count()).select_from(models.ContentActivation).where(
                models.ContentActivation.task_id == task_id
            )
        ) == baseline_activations
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionOutboxEvent).where(
                tx.ProjectionOutboxEvent.task_id == task_id
            )
        ) == baseline_events
        operation = session.get(wf.WorkflowOperation, uuid.UUID(operation_id))
        assert operation.phase == "prepare_required"


def test_execution_rejects_and_rolls_back_mutation_spec_drift(workflow_db, monkeypatch) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _port_value, _author_run, verifier_run, started, prepared, _inspection = _verification_ready(
            session, ids, context, task_id
        )
        operation_id = started.data["operation_id"]
        reviewed_id = uuid.UUID(prepared.data["content_version_id"])

    monkeypatch.setattr(
        "dish_pg.command_port.effect_spec_for",
        lambda _command, _arguments, **_kwargs: CommandEffectSpec(
            ("advance_operation",),
            ("update_task_document",),
            verify_mutation_effects=True,
        ),
    )
    with pytest.raises(CommandEffectMismatch, match="authoritative effects mismatch"):
        with session_scope(factory) as session:
            reviewed = session.get(models.ContentVersion, reviewed_id)
            port = PostgresCommandPort(
                session,
                cursor_secret=SECRET,
                uuid_factory=lambda: _next(ids),
            )
            port.execute(
                _call(
                    "approve",
                    run_id=verifier_run,
                    request_id=_next(ids),
                    owner="verifier-owner",
                    principal="verification",
                    arguments={
                        "task_id": str(task_id),
                        "operation_id": operation_id,
                        "agent": "codex",
                        "model": "o3",
                        "correction": "none",
                        "reviewed_identity": reviewed.content_identity,
                        "semantic_review_complete": True,
                        "provenance_complete": True,
                    },
                )
            )

    with session_scope(factory) as session:
        operation = session.get(wf.WorkflowOperation, uuid.UUID(operation_id))
        assert operation.phase == "await_verification"
        assert session.scalar(select(func.count()).select_from(wf.VerificationSignoff)) == 0
