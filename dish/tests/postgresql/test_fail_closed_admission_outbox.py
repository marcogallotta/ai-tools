from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg import stage6_models as rel
from dish_pg.command_port import CommandCall, PostgresCommandPort
from dish_pg.database import session_scope
from dish_pg.transition import ProjectionService
from dish_pg.workflow import MutationAdmissionClosed, WorkflowAuthorityService
from tests.support.postgresql.workflow import (
    NOW,
    _admit,
    _next,
    _register_run,
    workflow_db,
)
from tests.support.postgresql.release import _prepare_candidate

pytestmark = pytest.mark.smoke

SECRET = b"fail-closed-command-port-secret!!"


def _activate_projection(session, ids, generation_id: uuid.UUID) -> None:
    ProjectionService(session, uuid_factory=lambda: _next(ids)).activate_epoch(
        generation_id=generation_id,
        activation_reason="fail-closed command-port test",
        created_at=NOW,
    )


def test_pre_candidate_development_admission_remains_open(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        admission = _admit(
            WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids)),
            request_id=_next(ids),
            generation_id=context["generation_id"],
            run_id=run_id,
        )
        assert admission.replayed is False


@pytest.mark.parametrize("control_state", [None, "closed"])
def test_candidate_admission_rejects_missing_or_closed_control(
    workflow_db, control_state: str | None
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _service, _candidate_id = _prepare_candidate(session, ids, context, task_id)
        control = session.get(rel.MutationAdmissionControl, context["generation_id"])
        if control_state is None:
            session.delete(control)
            session.flush()
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        with pytest.raises(MutationAdmissionClosed, match="mutation admission is closed"):
            _admit(
                WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids)),
                request_id=_next(ids),
                generation_id=context["generation_id"],
                run_id=run_id,
            )


def test_candidate_admission_accepts_only_explicit_open_control(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _service, _candidate_id = _prepare_candidate(session, ids, context, task_id)
        control = session.get(rel.MutationAdmissionControl, context["generation_id"])
        control.state = "open"
        control.control_revision += 1
        control.opened_at = NOW
        control.updated_at = NOW
        session.flush()
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        admission = _admit(
            WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids)),
            request_id=_next(ids),
            generation_id=context["generation_id"],
            run_id=run_id,
        )
        assert admission.replayed is False


def test_sqlite_guard_rejects_candidate_request_when_control_row_is_missing(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with pytest.raises(IntegrityError, match="mutation admission is closed"):
        with session_scope(factory) as session:
            _service, _candidate_id = _prepare_candidate(session, ids, context, task_id)
            control = session.get(rel.MutationAdmissionControl, context["generation_id"])
            session.delete(control)
            session.flush()
            run_id = _next(ids)
            _register_run(session, generation_id=context["generation_id"], run_id=run_id)
            payload = {"task": "direct-trigger-probe"}
            session.add(
                wf.ServiceRequest(
                    request_id=_next(ids),
                    generation_id=context["generation_id"],
                    run_id=run_id,
                    owner_id="owner-1",
                    principal_class="agent",
                    command_name="start",
                    canonical_payload_sha256=hashlib.sha256(
                        b'{"task":"direct-trigger-probe"}'
                    ).hexdigest(),
                    canonical_payload=payload,
                    protocol_release="protocol-1",
                    dish_release="dish-42619b9",
                    admitted_at=NOW,
                )
            )
            session.flush()


def test_default_command_port_uses_full_projection_authority(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        port = PostgresCommandPort(
            session,
            cursor_secret=SECRET,
            uuid_factory=lambda: _next(ids),
        )
        assert isinstance(port.projection_recorder, ProjectionService)
        assert callable(port.projection_recorder.record)
        assert callable(port.projection_recorder.recover)
        assert callable(port.projection_recorder.unresolved_attempt_id)
        assert callable(port.projection_recorder.task_freshness)


@pytest.mark.database_boundary
def test_default_projection_intent_and_authoritative_create_roll_back_together(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        _activate_projection(session, ids, context["generation_id"])

    with pytest.raises(RuntimeError, match="force rollback"):
        with session_scope(factory) as session:
            run_id = _next(ids)
            _register_run(session, generation_id=context["generation_id"], run_id=run_id)
            port = PostgresCommandPort(
                session,
                cursor_secret=SECRET,
                uuid_factory=lambda: _next(ids),
                lease_duration=timedelta(minutes=10),
            )
            result = port.execute(
                CommandCall(
                    command_name="create",
                    arguments={"title": "Default projection rollback"},
                    owner_id="owner-1",
                    principal_class="agent",
                    run_id=run_id,
                    request_id=_next(ids),
                    now=NOW,
                )
            )
            assert result.ok
            event = session.get(
                tx.ProjectionOutboxEvent,
                uuid.UUID(result.data["projection_event_id"]),
            )
            assert event is not None
            assert event.command_execution_id is not None
            raise RuntimeError("force rollback")

    with session_scope(factory) as session:
        created = session.scalar(
            select(func.count())
            .select_from(models.DishTask)
            .where(models.DishTask.creation_route == "create")
        )
        outbox = session.scalar(select(func.count()).select_from(tx.ProjectionOutboxEvent))
        assert created == 0
        assert outbox == 0
