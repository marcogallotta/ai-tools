from __future__ import annotations
import hashlib
import uuid
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.command_port import PostgresCommandPort
from dish_pg.database import session_scope
from dish_pg.read_model import PostgresReadModel
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db
from tests.support.postgresql.release import _prepare_candidate
from tests.support.postgresql.abandonment_terminal_migration import build_0016_abandonment_fixture
from tests.support.postgresql.command import (
    _add_verification_queue,
    _call,
    _inspect,
    _port,
    _prepare_for_verification,
    _start_initial,
    _start_verification,
    _verification_ready,
)




def _case_test_verifier_reconstruction_is_bound_to_latest_verification_cycle(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    verifier_run = _next(ids)
    new_verifier_run = _next(ids)
    operation_id: uuid.UUID

    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        author_run = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=author_run)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=verifier_run,
            owner="verifier-owner",
            agent="codex",
        )
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=author_run)
        operation_id = uuid.UUID(started.data["operation_id"])
        _prepare_for_verification(
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=author_run,
        )
        _start_verification(
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=verifier_run,
        )
        _inspect(
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=verifier_run,
        )
        rejected = port.execute(
            _call(
                "reject",
                run_id=verifier_run,
                request_id=_next(ids),
                owner="verifier-owner",
                principal="verification",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "agent": "codex",
                    "route": "large",
                    "reason": "material correction",
                    "file_text": __import__(
                        "tests.support.canonical", fromlist=["TASK"]
                    ).TASK.replace(
                        "A compact side dish for testing texture.",
                        "A materially corrected side dish for testing texture.",
                    ),
                },
            )
        )
        assert rejected.ok
        cycles = session.scalars(
            select(wf.VerificationCycle)
            .where(wf.VerificationCycle.operation_id == operation_id)
            .order_by(wf.VerificationCycle.cycle_sequence)
        ).all()
        assert len(cycles) == 2
        assert cycles[0].cycle_id != cycles[1].cycle_id
        assert session.scalar(
            select(func.count()).select_from(wf.OperationActorFact).where(
                wf.OperationActorFact.operation_id == operation_id,
                wf.OperationActorFact.actor_role == "verification",
            )
        ) == 1

    with session_scope(factory) as session:
        operation = session.get(wf.WorkflowOperation, operation_id)
        state = session.get(models.DishState, (context["generation_id"], task_id))
        version = session.get(models.ContentVersion, state.current_content_version_id)
        snapshot = PostgresReadModel(
            session, cursor_secret=b"cycle-bound-read-model-secret-32"
        )._workflow_snapshot(
            generation_id=context["generation_id"],
            task_id=task_id,
            title=version.title,
            body=version.body,
            operation=operation,
        )
        assert snapshot.verifier_established is False
        assert snapshot.dish_inspect_current is False

        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=new_verifier_run,
            owner="new-verifier-owner",
            agent="codex",
        )
        _start_verification(
            _port(session, ids),
            ids,
            task_id=task_id,
            operation_id=str(operation_id),
            run_id=new_verifier_run,
            owner="new-verifier-owner",
            agent="codex",
        )

    with session_scope(factory) as session:
        operation = session.get(wf.WorkflowOperation, operation_id)
        state = session.get(models.DishState, (context["generation_id"], task_id))
        version = session.get(models.ContentVersion, state.current_content_version_id)
        snapshot = PostgresReadModel(
            session, cursor_secret=b"cycle-bound-read-model-secret-32"
        )._workflow_snapshot(
            generation_id=context["generation_id"],
            task_id=task_id,
            title=version.title,
            body=version.body,
            operation=operation,
        )
        assert snapshot.verifier_established is True
        assert snapshot.dish_inspect_current is False
